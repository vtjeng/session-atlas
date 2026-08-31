#!/usr/bin/env python3
"""Parse Claude Code transcripts into shared project timelines.

``build_timeline()`` is the main entry point. See
``docs/transcript-formats.md#shared-timeline-shape-the-contract`` for the output
schema and source-record mappings.
"""
import glob
import json
import os
import re
import warnings
from collections import Counter
from datetime import datetime

PROJECTS = os.path.expanduser("~/.claude/projects")


def parse_iso(ts):
    """ISO-8601 timestamp string -> datetime (or None). Normalizes a
    trailing 'Z' for fromisoformat and swallows unparseable values. Shared so
    the same shim isn't reimplemented per consumer."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None

INJECTED_TAGS = ("task-notification", "system-reminder", "command-message",
                 "command-args", "local-command-stdout", "local-command-stderr")

# tool_use blocks that mutate the working tree -> "files changed"
WRITE_TOOLS = {"Edit", "Write", "NotebookEdit", "MultiEdit"}


# --------------------------------------------------------------------------- #
# project resolution
# --------------------------------------------------------------------------- #
def _is_transcript_dir(d):
    """True if d holds at least one file that parses as a Claude Code transcript
    (guards against code repos that merely contain unrelated .jsonl data files)."""
    for f in glob.glob(os.path.join(d, "*.jsonl")):
        try:
            rec = next(iter_json_records(f), None)
            if rec is None:
                continue
            if rec.get("sessionId") or rec.get("type") in (
                    "user", "assistant", "summary", "system"):
                return True
        except OSError:
            continue
    return False


def find_project_dir(target):
    """Resolve a project name, repo path, or explicit transcript directory.

    Name and repository lookup search ``~/.claude/projects``; an explicit
    transcript directory may be elsewhere.
    """
    # 1. an explicit transcript directory passed directly
    if os.path.isdir(target) and _is_transcript_dir(target):
        return target
    # 2. a real code-repo path -> munge it (every "/" becomes "-")
    if os.path.isdir(target):
        munged = os.path.join(PROJECTS, os.path.abspath(target).replace("/", "-"))
        if os.path.isdir(munged):
            return munged
    # 3. a bare munged dir name living under ~/.claude/projects
    if not os.path.isabs(target):
        direct = os.path.join(PROJECTS, target)
        if os.path.isdir(direct):
            return direct
    # 4. match by repo basename against munged dir names (…-<basename>)
    base = os.path.basename(target.rstrip("/"))
    matches = [d for d in glob.glob(os.path.join(PROJECTS, "*"))
               if os.path.isdir(d) and d.rstrip("/").endswith("-" + base)]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise SystemExit(f"No project directory found for {target!r} under {PROJECTS}")
    raise SystemExit("Ambiguous; matched:\n  " + "\n  ".join(matches))


def unmunge(dirname):
    """Best-effort reconstruction of the original absolute path from the munged
    directory name (…-home-user-…). Munging is lossy (real dashes collide with
    separators), so this is only for display."""
    base = os.path.basename(dirname.rstrip("/"))
    return "/" + base.lstrip("-").replace("-", "/")


# --------------------------------------------------------------------------- #
# user-record classification
# --------------------------------------------------------------------------- #
def _content_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [b["text"] for b in content
                 if isinstance(b, dict) and b.get("type") == "text" and "text" in b]
        return "\n".join(parts) if parts else None
    return None


def classify_user(record):
    """Return (kind, text) for a genuine human input, else None. kind is
    "prompt" (free text) or "command" (slash command, unwrapped to '/name args')."""
    # isSidechain marks a subagent's own side-conversation; its user-role turns
    # are not things the human typed, so they must never become milestones.
    if record.get("type") != "user" or record.get("isMeta") or record.get("isSidechain"):
        return None
    text = _content_text(record.get("message", {}).get("content"))
    if text is None:
        return None
    stripped = text.lstrip()

    if stripped.startswith("<command-name>"):
        name = re.search(r"<command-name>(.*?)</command-name>", text, re.S)
        args = re.search(r"<command-args>(.*?)</command-args>", text, re.S)
        cmd = (name.group(1).strip() if name else "")
        if not cmd:
            return None
        arg = (args.group(1).strip() if args else "")
        return ("command", f"{cmd} {arg}".strip())

    m = re.match(r"<([a-zA-Z0-9_-]+)>", stripped)
    if m and m.group(1) in INJECTED_TAGS:
        return None
    if stripped.startswith("[Request interrupted") or not stripped:
        return None
    return ("prompt", text.strip())


# --------------------------------------------------------------------------- #
# tool_use labeling
# --------------------------------------------------------------------------- #
def _first_line(s, n=80):
    """First non-empty-stripped line of a (possibly None) string, capped at n."""
    lines = (s or "").strip().splitlines()
    return lines[0][:n] if lines else ""


def _tool_label(name, inp):
    """Short human label + optional changed-file path for one tool_use block."""
    inp = inp or {}
    fp = inp.get("file_path") or inp.get("notebook_path")
    changed = fp if name in WRITE_TOOLS and fp else None
    if name == "Bash":
        label = _first_line(inp.get("command"))
    elif fp:
        label = os.path.basename(fp)
    elif name in ("Grep", "Glob"):
        label = inp.get("pattern") or inp.get("glob") or ""
    elif name in ("Task", "Agent"):
        label = inp.get("description") or ""
    else:
        label = inp.get("description") or inp.get("prompt") or ""
        label = label[:80]
    return label, changed


# --------------------------------------------------------------------------- #
# main parse
# --------------------------------------------------------------------------- #
def _parse_diagnostic(diagnostics, path, line_number, reason):
    """Record and emit one skipped-record diagnostic."""
    message = f"{path}:{line_number}: skipped {reason}"
    diagnostics.append(message)
    warnings.warn(message, RuntimeWarning, stacklevel=3)


def iter_json_records(path, diagnostics=None):
    """Yield each non-blank JSON record from a .jsonl file, skipping blank and
    malformed lines and reporting every skipped record."""
    diagnostics = diagnostics if diagnostics is not None else []
    with open(path, "rb") as fh:
        for line_number, raw_line in enumerate(fh, 1):
            try:
                line = raw_line.decode("utf-8").strip()
            except UnicodeDecodeError:
                _parse_diagnostic(
                    diagnostics, path, line_number, "non-UTF-8 transcript record")
                continue
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                _parse_diagnostic(
                    diagnostics, path, line_number, "malformed JSON transcript record")
                continue


def _load_records(project_dir, paths=None, diagnostics=None):
    """Return ``(first timestamp, file-derived session ID, records)`` tuples
    ordered by first timestamp; records retain their file order."""
    per_file = []
    paths = paths if paths is not None else glob.glob(os.path.join(project_dir, "*.jsonl"))
    for path in sorted(paths):
        recs = list(iter_json_records(path, diagnostics))
        first_ts = next((r.get("timestamp") for r in recs if r.get("timestamp")), "")
        per_file.append((first_ts, os.path.basename(path)[:-6], recs))
    per_file.sort(key=lambda t: t[0])  # sessions in chronological order
    return per_file


def _new_activity():
    return {"tools": Counter(), "tool_events": [], "files": [],
            "tokens_in": 0, "tokens_out": 0, "cache_read": 0, "cache_create": 0,
            "tokens_by_model": {},  # model -> {in,out,cr,cc,cc1h}; drives cost estimates
            "subagents": [],        # per-transcript {by_model,start} runs folded in here (display only)
            "duration_ms": 0, "assistant_turns": 0, "models": Counter(), "gist": None,
            "title": None}


def _add_tokens(activity, model, ti, to, cr, cc, cc1h=0):
    """Add token counts to the activity's flat totals and its per-model breakdown.

    A project can span models with very different rates, so the flat totals
    aren't enough to cost accurately — we also bucket by model here, keeping the
    two in lockstep by accumulating both from one place.
    """
    activity["tokens_in"] += ti
    activity["tokens_out"] += to
    activity["cache_read"] += cr
    activity["cache_create"] += cc + cc1h
    if not (ti or to or cr or cc or cc1h):
        return
    d = activity["tokens_by_model"].setdefault(
        model or "<unknown>", {"in": 0, "out": 0, "cr": 0, "cc": 0, "cc1h": 0})
    d["in"] += ti
    d["out"] += to
    d["cr"] += cr
    d["cc"] += cc
    d["cc1h"] += cc1h


def merge_token_models(dst, src):
    """Fold a per-model {model -> {in,out,cr,cc,cc1h}} map into ``dst`` in place."""
    for mid, tk in (src or {}).items():
        d = dst.setdefault(mid, {"in": 0, "out": 0, "cr": 0, "cc": 0, "cc1h": 0})
        for k in ("in", "out", "cr", "cc", "cc1h"):
            d[k] += tk.get(k, 0)
    return dst


def _new_milestone(kind, text, ts, session):
    """A milestone with a fresh activity accumulator. Both parsers emit this shape."""
    return {"kind": kind, "text": text, "ts": ts, "session": session,
            "activity": _new_activity()}


def _has_substantive_activity(activity):
    """Whether an activity has work beyond timestamps and display metadata."""
    return bool(
        activity["assistant_turns"] or activity["tools"] or activity["files"]
        or activity["tokens_in"] or activity["tokens_out"]
        or activity["cache_read"] or activity["cache_create"]
    )


def _finalize_milestone(m, milestones):
    """Deduplicate files and append the milestone unless it is an empty session boundary."""
    a = m["activity"]
    # Duration alone is bookkeeping and does not prove substantive activity.
    if m["kind"] == "session" and not _has_substantive_activity(a):
        return
    a["files"] = sorted(set(a["files"]))
    milestones.append(m)


# --------------------------------------------------------------------------- #
# subagent / workflow token rollup
# --------------------------------------------------------------------------- #
# Nested transcript paths fall outside _load_records's top-level glob, so token
# attribution scans them separately.
def _iter_subagent_transcripts(project_dir):
    """Nested subagent/workflow transcript paths under a project dir (any depth)."""
    return sorted(glob.glob(
        os.path.join(project_dir, "*", "subagents", "**", "*.jsonl"), recursive=True))


def _subagent_usage(path, diagnostics=None):
    """Return ``(per-model token usage, first timestamp)`` for a nested
    transcript, taking a field-wise maximum for each message ID before summing."""
    per_msg = {}   # message.id -> [in, out, cr, cc, cc1h] field-wise max
    model_of = {}
    start = None
    diagnostics = diagnostics if diagnostics is not None else []
    with open(path, "rb") as fh:
        for line_number, raw_line in enumerate(fh, 1):
            try:
                line = raw_line.decode("utf-8")
            except UnicodeDecodeError:
                _parse_diagnostic(
                    diagnostics, path, line_number, "non-UTF-8 transcript record")
                continue
            # Nested logs are mostly user/tool-result/attachment records. Inspect
            # their discriminator before json.loads; tolerate whitespace/key order
            # with a regex rather than relying on compact serialization.
            is_assistant = bool(re.search(r'"type"\s*:\s*"assistant"', line))
            if not is_assistant and start is not None:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                _parse_diagnostic(
                    diagnostics, path, line_number, "malformed JSON transcript record")
                continue
            if start is None and r.get("timestamp"):
                start = r["timestamp"]
            if r.get("type") != "assistant":
                continue
            msg = r.get("message", {}) or {}
            u = msg.get("usage") or {}
            creation = u.get("cache_creation") or {}
            total_cc = u.get("cache_creation_input_tokens", 0) or 0
            cc1h = creation.get("ephemeral_1h_input_tokens", 0) or 0
            cur_u = [u.get("input_tokens", 0) or 0, u.get("output_tokens", 0) or 0,
                     u.get("cache_read_input_tokens", 0) or 0, total_cc, cc1h]
            if not any(cur_u):
                continue
            mid = msg.get("id") or r.get("uuid")
            prev = per_msg.get(mid, [0, 0, 0, 0, 0])
            per_msg[mid] = [max(c, p) for c, p in zip(cur_u, prev)]
            model_of[mid] = msg.get("model")
    by_model = {}
    for mid, vals in per_msg.items():
        d = by_model.setdefault(model_of.get(mid) or "<unknown>",
                                {"in": 0, "out": 0, "cr": 0, "cc": 0, "cc1h": 0})
        d["in"] += vals[0]; d["out"] += vals[1]; d["cr"] += vals[2]
        d["cc"] += max(0, vals[3] - vals[4]); d["cc1h"] += vals[4]
    return by_model, start


def _attribute_subagents(project_dir, milestones, sessions, paths=None,
                         diagnostics=None):
    """Fold nested subagent/workflow token usage into the spawning milestone.

    Attribution is by time: use the latest same-session milestone at or before
    the child start. If that session has no milestone, consider every project
    milestone; if none precedes the child, use the first candidate. The child's
    whole usage lands on the selected milestone and flows into every cost rollup.
    """
    if not milestones:
        return
    by_session = {}
    for m in milestones:
        by_session.setdefault(m["session"], []).append(m)  # chronological per session

    def spawning_milestone(sid, start):
        cands = by_session.get(sid) or milestones
        st = parse_iso(start)
        prior = None
        for m in cands:
            mt = parse_iso(m["ts"])
            if mt is not None and (st is None or mt <= st):
                prior = m
        return prior or cands[0]

    session_cutoff = {s["id"]: parse_iso(s.get("last_ts")) for s in sessions}
    paths = paths if paths is not None else _iter_subagent_transcripts(project_dir)
    for path in paths:
        by_model, start = _subagent_usage(path, diagnostics)
        if not by_model:
            continue
        parts = os.path.normpath(path).split(os.sep)
        try:
            subagents_i = len(parts) - 1 - parts[::-1].index("subagents")
            sid = parts[subagents_i - 1]
        except (ValueError, IndexError):
            continue
        # Parent and child files are live and can grow while a render runs. Do
        # not mix a child that began beyond the parent snapshot's cutoff into an
        # earlier prompt; the next scheduled render will include both coherently.
        child_start = parse_iso(start)
        if child_start and session_cutoff.get(sid) and child_start > session_cutoff[sid]:
            continue
        a = spawning_milestone(sid, start)["activity"]
        a["subagents"].append({"by_model": by_model, "start": start})  # additive; totals stay whole
        for mid, tk in by_model.items():
            _add_tokens(a, mid, tk["in"], tk["out"], tk["cr"], tk["cc"], tk["cc1h"])


def build_timeline(project_dir, session_paths=None, subagent_paths=None):
    diagnostics = []
    per_file = _load_records(project_dir, session_paths, diagnostics)
    milestones = []
    sessions = []
    cur = None  # current milestone dict being accumulated
    real_cwd = None      # exact project path (munging is lossy; records aren't)
    git_branches = Counter()
    msg_usage = {}       # message.id -> field-wise-max usage already billed (see below)

    def close(m):
        if m:
            _finalize_milestone(m, milestones)

    for first_ts, sid, recs in per_file:
        sess = {"id": sid, "last_ts": first_ts, "title": None, "tool": "claude"}
        # session-start pseudo-milestone captures pre-first-prompt activity
        cur = _new_milestone("session", None, first_ts, sid)
        for r in recs:
            t = r.get("type")
            ts = r.get("timestamp")
            if ts:
                sess["last_ts"] = ts
            if real_cwd is None and r.get("cwd"):
                real_cwd = r["cwd"]
            if r.get("gitBranch"):
                git_branches[r["gitBranch"]] += 1

            if t == "user":
                got = classify_user(r)
                if got:
                    close(cur)
                    cur = _new_milestone(got[0], got[1], ts, sid)
                continue

            if cur is None:
                continue
            a = cur["activity"]

            if t == "assistant":
                msg = r.get("message", {})
                model = msg.get("model")
                # Usage repeats per content block and can grow while streaming.
                # Add only growth in each message's field-wise maxima; still
                # inspect every content block for tool events.
                mid = msg.get("id") or r.get("uuid")
                seen = mid in msg_usage
                prev = msg_usage.get(mid, (0, 0, 0, 0, 0))
                u = msg.get("usage") or {}
                creation = u.get("cache_creation") or {}
                total_cc = u.get("cache_creation_input_tokens", 0) or 0
                cc1h = creation.get("ephemeral_1h_input_tokens", 0) or 0
                cur_u = (u.get("input_tokens", 0) or 0, u.get("output_tokens", 0) or 0,
                         u.get("cache_read_input_tokens", 0) or 0, total_cc, cc1h)
                state = tuple(max(c, p) for c, p in zip(cur_u, prev))
                msg_usage[mid] = state
                deltas = [max(0, state[i] - prev[i]) for i in range(3)]
                # A stream can begin with only aggregate cache creation and add
                # TTL detail later. Reclassify that already-counted write rather
                # than counting the detailed one a second time. Persist the
                # monotonic aggregate and TTL maxima, so a later duplicate that
                # omits detail cannot undo the classification.
                prev_standard = max(0, prev[3] - prev[4])
                standard = max(0, state[3] - state[4])
                deltas.extend((standard - prev_standard, state[4] - prev[4]))
                _add_tokens(a, model, *deltas)
                if not seen:
                    a["assistant_turns"] += 1
                    # "<synthetic>" is a placeholder for turns with no real model call.
                    if model and model != "<synthetic>":
                        a["models"][model] += 1
                for b in msg.get("content", []) or []:
                    if not isinstance(b, dict):
                        continue
                    bt = b.get("type")
                    if bt == "text" and not a["gist"] and b.get("text", "").strip():
                        a["gist"] = b["text"].strip()[:280]
                    elif bt == "tool_use":
                        name = b.get("name", "?")
                        a["tools"][name] += 1
                        label, changed = _tool_label(name, b.get("input"))
                        if changed:
                            a["files"].append(changed)
                        if len(a["tool_events"]) < 40:
                            a["tool_events"].append({"name": name, "label": label})
            elif t == "ai-title":
                a["title"] = r.get("aiTitle") or a["title"]
                sess["title"] = r.get("aiTitle") or sess["title"]
            elif t == "system" and r.get("subtype") == "turn_duration":
                a["duration_ms"] += r.get("durationMs", 0) or 0
        close(cur)
        cur = None
        sessions.append(sess)

    # Roll nested subagent/workflow token usage into the milestones that spawned it,
    # before aggregation so it reaches every cost figure.
    _attribute_subagents(
        project_dir, milestones, sessions, subagent_paths, diagnostics)

    project_path = real_cwd or unmunge(project_dir)
    return _timeline_dict(
        project_dir, project_path, sessions, milestones, git_branches,
        diagnostics=diagnostics)


def _timeline_dict(project_dir, project_path, sessions, milestones, branches,
                   diagnostics=None):
    """Assemble the timeline dict both parsers return (identical shape)."""
    return {
        "project_dir": project_dir,
        "project_name": os.path.basename(project_path.rstrip("/")) or project_path,
        "project_path": project_path,
        "git_branches": dict(branches.most_common()),
        "sessions": sessions,
        "milestones": milestones,
        "diagnostics": list(diagnostics or []),
        "stats": _aggregate(milestones, sessions),
    }


def _aggregate(milestones, sessions):
    tools, models, files = Counter(), Counter(), set()
    tin = tout = cread = ccreate = dur = 0
    prompts = cmds = recovered_prompts = turns = 0
    by_model = {}
    for m in milestones:
        a = m["activity"]
        tools.update(a["tools"])
        models.update(a["models"])
        files.update(a["files"])
        tin += a["tokens_in"]; tout += a["tokens_out"]
        cread += a["cache_read"]; ccreate += a["cache_create"]
        merge_token_models(by_model, a.get("tokens_by_model"))
        dur += a["duration_ms"]; turns += a["assistant_turns"]
        if m["kind"] == "prompt":
            prompts += 1
        elif m["kind"] == "command":
            cmds += 1
        elif m["kind"] == "recovered":
            recovered_prompts += 1
    ts = [m["ts"] for m in milestones if m["ts"]]
    all_ts = ts + [s["last_ts"] for s in sessions if s["last_ts"]]
    return {
        "sessions": len(sessions),
        "prompts": prompts, "commands": cmds,
        "recovered_prompts": recovered_prompts,
        "assistant_turns": turns,
        "tools": dict(tools.most_common()),
        "tool_calls": sum(tools.values()),
        "models": dict(models.most_common()),
        "files_changed": sorted(files),
        "tokens_in": tin, "tokens_out": tout,
        "cache_read": cread, "cache_create": ccreate,
        "tokens_by_model": by_model,
        "active_ms": dur,
        "first_ts": min(all_ts) if all_ts else None,
        "last_ts": max(all_ts) if all_ts else None,
    }


if __name__ == "__main__":
    import sys
    d = find_project_dir(sys.argv[1] if len(sys.argv) > 1 else ".")
    tl = build_timeline(d)
    print(json.dumps(tl["stats"], indent=2))
    print(f"\n{len(tl['milestones'])} milestones across {len(tl['sessions'])} session(s)")
