#!/usr/bin/env python3
"""Parse Codex CLI rollouts into shared project timelines.

``build_codex_timelines()`` is the main rollout entry point, and
``build_history_only_timelines()`` recovers prompt-only timelines. See
``docs/transcript-formats.md#shared-timeline-shape-the-contract`` for the output
schema and source-record mappings.
"""
import glob
import json
import os
import re
import sqlite3
from collections import Counter
from datetime import datetime, timezone

from ccx_parse import (_add_tokens, _finalize_milestone, _first_line,
                       _new_activity, _new_milestone, _parse_diagnostic,
                       _timeline_dict, parse_iso)

CODEX_SESSIONS = os.path.expanduser("~/.codex/sessions")
CODEX_HISTORY = os.path.expanduser("~/.codex/history.jsonl")
CODEX_LOGS = os.path.expanduser("~/.codex/logs_2.sqlite")

# Function-call outputs and reasoning payloads are unused here; inspect their
# discriminator before json.loads to avoid decoding them.
_SKIP_MARKS = ('"payload":{"type":"function_call_output"',
               '"payload":{"type":"reasoning"')

_TOOL_NAMES = {"exec_command": "Shell", "exec": "Shell", "write_stdin": "Stdin",
               "update_plan": "Plan", "spawn_agent": "Agent",
               "wait_agent": "WaitAgent", "wait": "WaitAgent",
               "view_image": "ViewImage", "request_user_input": "AskUser"}

_PATCH_PREFIXES = ("*** Update File: ", "*** Add File: ", "*** Delete File: ")


def _response_user_text(payload):
    """Return current-format human text, excluding injected user-role context."""
    if payload.get("role") != "user":
        return None
    metadata = payload.get("internal_chat_message_metadata_passthrough") or {}
    kinds = metadata.get("content_item_kinds")
    if kinds != ["user.text"]:
        return None
    content = payload.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    parts = [block.get("text", "") for block in content
             if isinstance(block, dict)
             and block.get("type") in ("input_text", "text")
             and isinstance(block.get("text"), str)]
    return "\n".join(parts)


def _is_subagent_meta(meta):
    """Whether session metadata identifies a non-user Codex thread."""
    source = meta.get("source")
    return (meta.get("thread_source") == "subagent"
            or isinstance(source, dict) and "subagent" in source)


def _subagent_label(meta):
    source = meta.get("source")
    subagent = source.get("subagent") if isinstance(source, dict) else None
    if not isinstance(subagent, dict):
        return meta.get("agent_path") or meta.get("agent_nickname")
    spawned = subagent.get("thread_spawn")
    if isinstance(spawned, dict) and spawned.get("agent_path"):
        return spawned["agent_path"]
    return (subagent.get("other") or meta.get("agent_path")
            or meta.get("agent_nickname"))


def _subagent_parent(meta):
    if meta.get("forked_from_id"):
        return meta["forked_from_id"], "fork of"
    if meta.get("parent_thread_id"):
        return meta["parent_thread_id"], "spawned by"
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
    """Return prompt-only timelines for history sessions absent from
    ``known_session_ids``."""
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
    """Return parsed rollout data, or ``None`` when no session metadata is
    parsed, ``cwd`` is missing, or no milestone survives finalization."""
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
                if pt == "message" and p.get("role") == "user":
                    text = _response_user_text(p)
                    if text is None:
                        continue
                    close(cur)
                    if not text.strip():
                        cur = None
                        continue
                    if is_subagent:
                        # Assignments in current-format rollouts are response
                        # items rather than event_msg user_message records.
                        cur = _new_milestone("session", None, ts, sess_id)
                    else:
                        kind = "command" if text.startswith("/") else "prompt"
                        cur = _new_milestone(kind, text, ts, sess_id)
                elif pt == "message" and p.get("role") == "assistant":
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


def _merge_subagent_activity(dst, src):
    """Fold one Codex rollout's activity into an attached parent entry."""
    dst["tools"].update(src.get("tools") or {})
    dst["tool_events"].extend(src.get("tool_events") or [])
    dst["tool_events"] = dst["tool_events"][:40]
    dst["files"].extend(src.get("files") or [])
    dst["duration_ms"] += src.get("duration_ms", 0)
    dst["assistant_turns"] += src.get("assistant_turns", 0)
    dst["models"].update(src.get("models") or {})
    dst["subagents"].extend(src.get("subagents") or [])
    if not dst.get("gist") and src.get("gist"):
        dst["gist"] = src["gist"]
    if not dst.get("title") and src.get("title"):
        dst["title"] = src["title"]

    # _add_tokens keeps the flat totals and the per-model cost map in lockstep.
    by_model = src.get("tokens_by_model") or {}
    if by_model:
        for model, tokens in by_model.items():
            _add_tokens(dst, model, tokens.get("in", 0), tokens.get("out", 0),
                        tokens.get("cr", 0), tokens.get("cc", 0),
                        tokens.get("cc1h", 0))
    else:
        # Be tolerant of hand-built/shared fixtures that only carry flat totals.
        dst["tokens_in"] += src.get("tokens_in", 0)
        dst["tokens_out"] += src.get("tokens_out", 0)
        dst["cache_read"] += src.get("cache_read", 0)
        dst["cache_create"] += src.get("cache_create", 0)
    dst["files"] = sorted(set(dst["files"]))


def _associate_codex_subagents(sessions, milestones):
    """Attach known child rollouts to the Codex sessions that spawned them.

    Claude presents nested work inside its parent session. Codex records that
    work as separate rollout files, so make the same presentation choice when
    the child metadata gives us a parent ID. Rollouts whose parent is absent
    remain standalone automated sessions and can still be filtered by the UI.
    """
    by_id = {session["id"]: session for session in sessions}
    by_session = {sid: [] for sid in by_id}
    for milestone in milestones:
        by_session.setdefault(milestone["session"], []).append(milestone)

    candidates = [session for session in sessions
                  if session.get("is_subagent")
                  and session.get("parent_session_id") in by_id]
    if not candidates:
        return

    def depth(session_id, seen=None):
        seen = set() if seen is None else seen
        if session_id in seen:
            return 0
        seen.add(session_id)
        parent = by_id[session_id].get("parent_session_id")
        if parent not in by_id:
            return 0
        return 1 + depth(parent, seen)

    folded = set()
    # Fold nested children first, so a parent rollout carries its own child
    # entry when that parent is subsequently folded into the root session.
    for child in sorted(candidates, key=lambda session: depth(session["id"]),
                        reverse=True):
        child_id = child["id"]
        if child_id in folded:
            continue
        parent_id = child.get("parent_session_id")
        if parent_id not in by_id or parent_id in folded:
            continue
        child_milestones = by_session.get(child_id, [])
        if not child_milestones:
            continue
        start = min((m["ts"] for m in child_milestones if m.get("ts")),
                    key=lambda ts: _ts_ms(ts) if _ts_ms(ts) is not None else float("inf"),
                    default=child.get("last_ts"))
        activity = _new_activity()
        for child_milestone in child_milestones:
            _merge_subagent_activity(activity, child_milestone["activity"])
        if not any((activity["tools"], activity["files"], activity["assistant_turns"],
                    activity["tokens_in"], activity["tokens_out"],
                    activity["cache_read"], activity["cache_create"],
                    activity["subagents"])):
            continue
        label = child.get("subagent_label")
        text = f"triggered {label} subagent" if label else "triggered subagent"
        attached = _new_milestone(
            "subagent", text, start, parent_id)
        attached["activity"] = activity
        by_session.setdefault(parent_id, []).append(attached)
        folded.add(child_id)

    if not folded:
        return

    for sid, session_milestones in by_session.items():
        session_milestones.sort(
            key=lambda m: _ts_ms(m.get("ts"))
            if _ts_ms(m.get("ts")) is not None else float("inf"))
    sessions[:] = [session for session in sessions if session["id"] not in folded]
    milestones[:] = [m for session in sessions
                     for m in by_session.get(session["id"], [])]


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

    for proj in projects.values():
        _associate_codex_subagents(proj["sessions"], proj["milestones"])

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
