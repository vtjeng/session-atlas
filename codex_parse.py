#!/usr/bin/env python3
"""Parser for OpenAI Codex CLI session rollouts (~/.codex/sessions/YYYY/MM/DD/*.jsonl).

See ``docs/transcript-formats.md`` for the shared parser contract and source
record mappings.

`build_codex_timelines()` groups every rollout by its project (`session_meta.cwd`)
and returns one timeline dict per project with the exact same shape as
`ccx_parse.build_timeline`, so the site generator renders both sources alike.
Each session dict carries `"tool": "codex"`.

Rollout records are `{"timestamp": <ISO8601 UTC>, "type": T, "payload": {...}}`:
  session_meta   first line; payload has id, cwd, cli_version, timestamp, git?
  event_msg      payload.type "user_message" is the ONLY reliable source of
                 genuinely-typed prompts (the response_item user stream is
                 polluted with injected AGENTS.md / environment_context);
                 "token_count" carries cumulative token usage;
                 "patch_apply_end" (June 2026+) lists changed files.
  response_item  the model-facing transcript: assistant messages, reasoning,
                 function_call (shell etc.), custom_tool_call (apply_patch).
  turn_context   per-turn config; the only place the model name appears.

Forked subagent rollouts begin with a replay of their parent's history. The
replayed records use fresh envelope timestamps and include the parent's
cumulative token counters, so they must be skipped through the subagent's first
real `task_started` event. Independent subagents have no replay and are parsed
from the beginning.

Prompts whose session has no discovered rollout can be recovered from
`~/.codex/history.jsonl` and mapped to projects with the indexed diagnostics
database. The source data does not establish why a rollout is absent, and it
does not retain replies, tools, tokens, or cost. Codex has no turn-duration
records, so active time is approximated as (last activity record ts - milestone
ts).
"""
import glob
import json
import os
import re
import sqlite3
from collections import Counter
from datetime import datetime, timezone

from ccx_parse import (_add_tokens, _finalize_milestone, _first_line,
                       _new_milestone, _parse_diagnostic, _timeline_dict,
                       parse_iso)

CODEX_SESSIONS = os.path.expanduser("~/.codex/sessions")
CODEX_HISTORY = os.path.expanduser("~/.codex/history.jsonl")
CODEX_LOGS = os.path.expanduser("~/.codex/logs_2.sqlite")

# These payload types dominate the (large) files by volume and are unused here;
# skipping them before json.loads keeps a full parse of ~500MB fast.
_SKIP_MARKS = ('"payload":{"type":"function_call_output"',
               '"payload":{"type":"reasoning"')

_TOOL_NAMES = {"exec_command": "Shell", "exec": "Shell", "write_stdin": "Stdin",
               "update_plan": "Plan", "spawn_agent": "Agent",
               "wait_agent": "WaitAgent", "wait": "WaitAgent",
               "view_image": "ViewImage", "request_user_input": "AskUser"}

_PATCH_PREFIXES = ("*** Update File: ", "*** Add File: ", "*** Delete File: ")


def _is_subagent_meta(meta):
    """Whether session metadata identifies a non-user Codex thread."""
    source = meta.get("source")
    return (meta.get("thread_source") == "subagent"
            or isinstance(source, dict) and "subagent" in source)


def _subagent_label(meta):
    source = meta.get("source")
    subagent = source.get("subagent") if isinstance(source, dict) else None
    if not isinstance(subagent, dict):
        return None
    spawned = subagent.get("thread_spawn")
    if isinstance(spawned, dict) and spawned.get("agent_path"):
        return spawned["agent_path"]
    return subagent.get("other")


def _subagent_parent(meta):
    if meta.get("forked_from_id"):
        return meta["forked_from_id"], "fork of"
    source = meta.get("source")
    subagent = source.get("subagent") if isinstance(source, dict) else None
    spawned = subagent.get("thread_spawn") if isinstance(subagent, dict) else None
    if isinstance(spawned, dict) and spawned.get("parent_thread_id"):
        return spawned["parent_thread_id"], "spawned by"
    return None, None


def _ts_ms(ts):
    dt = parse_iso(ts)
    return dt.timestamp() * 1000 if dt else None


def _patch_files(patch, cwd):
    files = []
    for line in patch.splitlines():
        for pre in _PATCH_PREFIXES:
            if line.startswith(pre):
                p = line[len(pre):].strip()
                if p and not os.path.isabs(p):
                    p = os.path.join(cwd or "", p)
                if p:
                    files.append(p)
    return files


def rollout_paths(root=CODEX_SESSIONS):
    """All rollout files under a sessions root (live or an archive mirror)."""
    return sorted(glob.glob(os.path.join(root, "*", "*", "*", "rollout-*.jsonl")))


def iter_rollout_metas(root=CODEX_SESSIONS):
    """Yield (path, session_meta payload) reading only each file's first line —
    cheap project discovery without parsing full rollouts."""
    for path in rollout_paths(root):
        try:
            with open(path, "rb") as fh:
                raw_line = fh.readline()
            try:
                line = raw_line.decode("utf-8")
                rec = json.loads(line)
            except UnicodeDecodeError:
                _parse_diagnostic([], path, 1, "non-UTF-8 transcript record")
                continue
            except json.JSONDecodeError:
                _parse_diagnostic([], path, 1, "malformed JSON transcript record")
                continue
        except OSError:
            continue
        if rec.get("type") == "session_meta":
            yield path, rec.get("payload") or {}


def _history_only_cwd(connection, session_id):
    """Recover an ephemeral thread's cwd from its indexed diagnostic rows."""
    rows = connection.execute(
        "SELECT feedback_log_body FROM logs "
        "WHERE thread_id = ? AND feedback_log_body IS NOT NULL "
        "ORDER BY ts, ts_nanos, id LIMIT 80", (session_id,))
    fallback = None
    for (body,) in rows:
        match = re.search(
            r'legacy_fallback_cwd: AbsolutePathBuf\("((?:\\.|[^"\\])*)"\)', body)
        if match:
            try:
                return json.loads(f'"{match.group(1)}"')
            except json.JSONDecodeError:
                return match.group(1)
        if fallback is None:
            match = re.search(r'\bcwd=([^\s}:]+)', body)
            if match:
                fallback = match.group(1)
    return fallback


def _history_ts(timestamp):
    try:
        return (datetime.fromtimestamp(timestamp, timezone.utc)
                .isoformat(timespec="milliseconds").replace("+00:00", "Z"))
    except (OverflowError, TypeError, ValueError):
        return None


def build_history_only_timelines(known_session_ids, history_path=CODEX_HISTORY,
                                 logs_path=CODEX_LOGS):
    """Build recovered-prompt timelines for sessions without a known rollout.

    Human prompts can remain in ``history.jsonl`` after or without a rollout,
    and indexed diagnostics can retain the thread's cwd. Assistant replies,
    tool activity, tokens, and cost are not durable in these sources, so this
    parser does not invent them or claim that the session was a specific kind.
    """
    if not os.path.isfile(history_path) or not os.path.isfile(logs_path):
        return []
    history = {}
    diagnostics = []
    try:
        with open(history_path, "rb") as fh:
            for line_number, raw_line in enumerate(fh, 1):
                try:
                    line = raw_line.decode("utf-8")
                    item = json.loads(line)
                except UnicodeDecodeError:
                    _parse_diagnostic(
                        diagnostics, history_path, line_number,
                        "non-UTF-8 transcript record")
                    continue
                except json.JSONDecodeError:
                    _parse_diagnostic(
                        diagnostics, history_path, line_number,
                        "malformed JSON transcript record")
                    continue
                sid = item.get("session_id")
                text = (item.get("text") or "").strip()
                # Slash commands and shell escapes are client actions, not
                # recoverable human prompts.
                if (not sid or sid in known_session_ids or not text
                        or text.startswith(("/", "!"))):
                    continue
                ts = _history_ts(item.get("ts"))
                if ts:
                    history.setdefault(sid, []).append((ts, text))
    except OSError:
        return []

    projects = {}
    try:
        db = sqlite3.connect(f"file:{logs_path}?mode=ro", uri=True)
        try:
            for sid, prompts in history.items():
                cwd = _history_only_cwd(db, sid)
                if not cwd:
                    continue
                prompts.sort()
                session = {
                    "id": sid, "last_ts": prompts[-1][0], "title": None,
                    "tool": "codex", "originator": "codex_history_only",
                    "repository_url": None, "is_subagent": False,
                    "subagent_label": None, "parent_session_id": None,
                    "parent_relation": None, "is_history_only": True,
                }
                milestones = []
                for ts, text in prompts:
                    milestones.append(_new_milestone("recovered", text, ts, sid))
                project = projects.setdefault(
                    cwd, {"sessions": [], "milestones": [], "branches": Counter()})
                project["sessions"].append(session)
                project["milestones"].extend(milestones)
        finally:
            db.close()
    except (OSError, sqlite3.Error):
        return []

    return [_timeline_dict(CODEX_HISTORY, cwd, project["sessions"],
                           project["milestones"], project["branches"],
                           diagnostics=diagnostics)
            for cwd, project in sorted(projects.items())]


def _parse_rollout(path):
    """Parse one rollout into project data and skipped-record diagnostics.

    Returns ``(cwd, session, milestones, git_branches, diagnostics)``, or
    ``None`` for a file with no session metadata or genuine user input.
    """
    sess_id = os.path.basename(path)[-42:-6]  # uuid from filename, fallback only
    cwd = None
    sess = None
    milestones = []
    branches = Counter()
    cur = None            # milestone being accumulated
    cur_last_ms = None    # ts (ms) of last activity record in cur
    model = None
    prev_tok = {"in": 0, "out": 0, "cr": 0}
    is_subagent = False
    replaying_fork = False
    session_started_s = None
    diagnostics = []

    def close(m):
        nonlocal cur_last_ms
        if not m:
            return
        if cur_last_ms is not None:
            start = _ts_ms(m["ts"])
            if start is not None and cur_last_ms > start:
                m["activity"]["duration_ms"] = int(cur_last_ms - start)
        _finalize_milestone(m, milestones)
        cur_last_ms = None

    with open(path, "rb") as fh:
        for line_number, raw_line in enumerate(fh, 1):
            try:
                line = raw_line.decode("utf-8")
            except UnicodeDecodeError:
                _parse_diagnostic(
                    diagnostics, path, line_number, "non-UTF-8 transcript record")
                continue
            head = line[:140]
            if any(mark in head for mark in _SKIP_MARKS):
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                _parse_diagnostic(
                    diagnostics, path, line_number, "malformed JSON transcript record")
                continue
            t = rec.get("type")
            ts = rec.get("timestamp")
            p = rec.get("payload") or {}

            if t == "session_meta":
                # Forked rollouts can replay the parent's session_meta after
                # their own first line. Keep the child identity and state.
                if sess is not None:
                    continue
                sess_id = p.get("id") or sess_id
                cwd = p.get("cwd")
                start = p.get("timestamp") or ts
                g = p.get("git") or {}
                is_subagent = _is_subagent_meta(p)
                parent_session_id, parent_relation = _subagent_parent(p)
                sess = {"id": sess_id, "last_ts": start, "title": None, "tool": "codex",
                        "originator": p.get("originator"),
                        "repository_url": g.get("repository_url"),
                        "is_subagent": is_subagent,
                        "subagent_label": _subagent_label(p),
                        "parent_session_id": parent_session_id,
                        "parent_relation": parent_relation}
                cur = _new_milestone("session", None, start, sess_id)
                replaying_fork = bool(p.get("forked_from_id"))
                start_dt = parse_iso(start)
                session_started_s = int(start_dt.timestamp()) if start_dt else None
                if g.get("branch"):
                    branches[g["branch"]] += 1
                continue
            if sess is None:
                return None  # not a rollout file

            if replaying_fork:
                # The replay contains cumulative counters we need as the
                # baseline for the child's first real delta. Do not emit any
                # copied prompts, tools, files, turns, or tokens.
                if t == "event_msg" and p.get("type") == "token_count":
                    tot = (p.get("info") or {}).get("total_token_usage") or {}
                    if tot:
                        prev_tok = {
                            "in": tot.get("input_tokens", 0) or 0,
                            "out": tot.get("output_tokens", 0) or 0,
                            "cr": tot.get("cached_input_tokens", 0) or 0,
                        }
                if (t == "event_msg" and p.get("type") == "task_started"
                        and session_started_s is not None
                        and isinstance(p.get("started_at"), (int, float))
                        and p["started_at"] >= session_started_s):
                    replaying_fork = False
                    close(cur)
                    cur = _new_milestone("session", None, ts, sess_id)
                continue

            if ts:
                sess["last_ts"] = ts

            if t == "turn_context":
                model = p.get("model") or model
                continue

            if t == "event_msg":
                pt = p.get("type")
                if pt == "task_started" and is_subagent:
                    # Reused agents receive follow-up tasks without a
                    # user_message. Split them here so idle time and activity
                    # stay scoped to the assignment that produced them.
                    close(cur)
                    cur = _new_milestone("session", None, ts, sess_id)
                    continue
                if pt == "user_message":
                    close(cur)
                    text = (p.get("message") or "").strip()
                    if not text:
                        cur = None
                        continue
                    if is_subagent:
                        # This is an agent assignment or follow-up, not text
                        # typed by the human. Keep its work and usage without
                        # inflating the site's prompt count.
                        cur = _new_milestone("session", None, ts, sess_id)
                    else:
                        kind = "command" if text.startswith("/") else "prompt"
                        cur = _new_milestone(kind, text, ts, sess_id)
                    continue
                if cur is None:
                    continue
                a = cur["activity"]
                if pt == "token_count":
                    info = p.get("info") or {}
                    tot = info.get("total_token_usage") or {}
                    if tot:
                        new = {"in": tot.get("input_tokens", 0) or 0,
                               "out": tot.get("output_tokens", 0) or 0,
                               "cr": tot.get("cached_input_tokens", 0) or 0}
                        di = max(0, new["in"] - prev_tok["in"])
                        do = max(0, new["out"] - prev_tok["out"])
                        dcr = max(0, new["cr"] - prev_tok["cr"])
                        # OpenAI's input_tokens INCLUDES cached_input_tokens, so
                        # the fresh (full-price) input is the difference — bill the
                        # cached slice once, at the cache-read rate, not twice.
                        # (Anthropic keeps the two disjoint; only Codex needs this.)
                        fresh = max(0, di - dcr)
                        # Codex reports no cache-creation; attribute to the model
                        # named by the most recent turn_context.
                        _add_tokens(a, model, fresh, do, dcr, 0)
                        prev_tok = new
                    cur_last_ms = _ts_ms(ts) or cur_last_ms
                elif pt == "patch_apply_end":
                    a["files"].extend((p.get("changes") or {}).keys())
                    cur_last_ms = _ts_ms(ts) or cur_last_ms
                continue

            if t == "response_item" and cur is not None:
                a = cur["activity"]
                pt = p.get("type")
                if pt == "message" and p.get("role") == "assistant":
                    a["assistant_turns"] += 1
                    if model:
                        a["models"][model] += 1
                    if not a["gist"]:
                        content = p.get("content") or []
                        txt = next((b.get("text", "") for b in content
                                    if isinstance(b, dict) and b.get("text")), "")
                        if txt.strip():
                            a["gist"] = txt.strip()[:280]
                elif pt in ("function_call", "custom_tool_call", "web_search_call",
                            "tool_search_call"):
                    if pt == "web_search_call":
                        name = "WebSearch"
                        action = p.get("action") or {}
                        label = action.get("query") or " ".join(action.get("queries") or [])
                    elif pt == "tool_search_call":
                        name = "ToolSearch"
                        args = p.get("arguments")
                        label = args.get("query", "") if isinstance(args, dict) else ""
                    elif pt == "custom_tool_call":
                        raw = p.get("name", "?")
                        name = "Patch" if raw == "apply_patch" else _TOOL_NAMES.get(raw, raw)
                        label = ""
                        if raw == "apply_patch":
                            changed = _patch_files(p.get("input") or "", cwd)
                            a["files"].extend(changed)
                            label = " ".join(os.path.basename(f) for f in changed[:3])
                    else:
                        raw = p.get("name", "?")
                        name = _TOOL_NAMES.get(raw, raw)
                        label = ""
                        try:
                            args = json.loads(p.get("arguments") or "{}")
                            label = _first_line(args.get("cmd"))
                        except (json.JSONDecodeError, AttributeError):
                            pass
                    a["tools"][name] += 1
                    if len(a["tool_events"]) < 40:
                        a["tool_events"].append({"name": name, "label": label[:80]})
                cur_last_ms = _ts_ms(ts) or cur_last_ms

    close(cur)
    if not milestones or cwd is None:
        return None
    return cwd, sess, milestones, branches, diagnostics


def build_codex_timelines(paths=None):
    """Parse rollouts (all, or just `paths`) -> list of ccx-shaped timeline dicts,
    one per distinct cwd, sessions in chronological (filename) order."""
    if paths is None:
        paths = [p for p, _ in iter_rollout_metas()]
    projects = {}  # cwd -> sessions, milestones, branches, and diagnostics
    for path in sorted(paths):
        got = _parse_rollout(path)
        if not got:
            continue
        cwd, sess, ms, branches, diagnostics = got
        proj = projects.setdefault(
            cwd, {"sessions": [], "milestones": [], "branches": Counter(),
                  "diagnostics": []})
        proj["sessions"].append(sess)
        proj["milestones"].extend(ms)
        proj["branches"].update(branches)
        proj["diagnostics"].extend(diagnostics)

    return [_timeline_dict(CODEX_SESSIONS, cwd, proj["sessions"],
                           proj["milestones"], proj["branches"],
                           diagnostics=proj["diagnostics"])
            for cwd, proj in sorted(projects.items())]


if __name__ == "__main__":
    import time
    t0 = time.time()
    tls = build_codex_timelines()
    for tl in sorted(tls, key=lambda t: t["stats"]["last_ts"] or "", reverse=True):
        s = tl["stats"]
        print(f"{tl['project_name']:32s} {s['sessions']:3d} sessions "
              f"{s['prompts']:4d} prompts {s['commands']:3d} cmds "
              f"{s['tokens_out']/1e3:8.1f}k tok out   {tl['project_path']}")
    print(f"\n{len(tls)} projects · parsed in {time.time()-t0:.1f}s")
