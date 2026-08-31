# Transcript formats (Claude Code & Codex CLI)

This reference describes the fields exercised by the sanitized fixtures under
`tests/fixtures/transcripts/` and the parser behavior they test. Transcript
formats can change between CLI releases. Both parsers emit the same top-level,
milestone, and activity fields; their session dictionaries share a base schema
and Codex adds source-specific metadata. `generate_site.py` renders either and
merges them per repository path.

## Shared timeline shape (the contract)

```
timeline = {project_dir, project_name, project_path, git_branches,
            sessions:  [{id, last_ts, title, tool: "claude"|"codex", ...}],
            milestones:[{kind: "prompt"|"command"|"recovered"|"session", text, ts,
                         session: <session id>, activity}],
            diagnostics: [<skipped-record warning>],
            stats: ccx_parse._aggregate(milestones, sessions)}
activity = ccx_parse._new_activity()   # tools, tool_events(≤40), files,
                                       # tokens_*, tokens_by_model, duration_ms,
                                       # assistant_turns, models, gist, title
```

Both session types contain `id`, `last_ts`, `title`, and `tool`. Codex sessions
also contain `originator`, `repository_url`, `is_subagent`, `subagent_label`,
`parent_session_id`, `parent_relation`, and optional `is_history_only`.

A milestone is an attribution interval. `prompt`, `command`, and `recovered`
intervals start at retained inputs; `session` intervals collect substantive
machine activity before the first retained input or at a child-task boundary.
Each interval owns activity until the next boundary, and empty `session`
intervals are discarded.

## Claude Code (`~/.claude/projects/<munged-cwd>/<session-uuid>.jsonl`)

- Munged dir name = cwd with `/`→`-` (LOSSY: real dashes collide). Always take
  the true path from the `cwd` field inside records.
- Record types: `user`, `assistant` (message.model, message.usage,
  content blocks thinking/text/tool_use), `ai-title` (rolling title, no
  timestamp — position attributes it), `system` (subtype `turn_duration` →
  durationMs), plus ignorable bookkeeping types.
- `role:user` records are 4 things; only 2 are typed by the human (see
  `classify_user`): free text, and slash commands wrapped in
  `<command-name>/<command-args>` tags. Excluded: `<local-command-stdout>`,
  and harness injections — `isMeta`, `isSidechain` (subagent's own turns!),
  tool_results, `<task-notification>`, `<system-reminder>`,
  `[Request interrupted…]`.
- Tokens: `message.usage` (input/output/cache_read/cache_creation). CAUTION:
  Claude Code writes one record per content block (thinking / text / each
  tool_use). Records for one `message.id` can repeat or accumulate usage in
  either top-level or nested transcripts. Summing per record would count a
  multi-block message more than once; `build_timeline` bills the running
  field-wise maximum. Preserve
  `cache_creation.ephemeral_5m_input_tokens` and
  `ephemeral_1h_input_tokens` separately: Anthropic bills them at 1.25x and 2x
  base input respectively. Bucketed by
  `message.model` into `activity.tokens_by_model` (via `_add_tokens`) so
  `pricing.py` can cost a mixed-model project. Duration: sum of `turn_duration`
  records. Model may be `"<synthetic>"` — filter it from display; it carries no
  billable tokens so it never reaches the cost table.
- Subagents/workflows: Task and workflow transcripts are NESTED at
  `<project>/<session-id>/subagents/**/*.jsonl` (below the top-level `*.jsonl`
  `_load_records` globs), `isSidechain:true`, carrying the parent's `sessionId`.
  `_attribute_subagents` rolls each file's deduplicated tokens into a milestone
  selected from the transcript's first timestamp, so cost isn't silently
  understated. If a
  child begins after the parent snapshot read by the current render, defer it
  until the next render rather than attributing it to the preceding prompt.
  Reconcile against `/cost`, but
  note `/cost` is process-scoped and spans multiple session-ids after a `/clear`.
- Timestamps are UTC ISO-8601; the site renders local time via
  `datetime.astimezone()`.

## Codex CLI (`~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl`)

Envelope on every line: `{"timestamp": <ISO8601 ms UTC>, "type": T, "payload": {…}}`.
First line is `session_meta` → `.payload.{id, cwd, cli_version, timestamp,
git{branch}? (0.142+)}`. Sessions are date-organized, NOT project-organized —
group by `cwd`.

- **Typed prompts: ONLY trust `event_msg` / `payload.type=="user_message"`
  (`.payload.message`).** The `response_item` role:user stream is polluted
  with injected AGENTS.md (`# AGENTS.md instructions…`),
  `<environment_context>`, `<user_shell_command>`, `<turn_aborted>`,
  `<subagent_notification>`; role:developer is always injected permissions.
- Recovered prompts: `build_history_only_timelines()` selects history entries
  whose session ID has no discovered rollout, then maps their working directory
  through `logs_2.sqlite`. It emits `kind:"recovered"` milestones and does not
  claim that the missing rollout came from `/btw` or any other specific path.
  The history sources do not contain assistant replies, tools, tokens, or cost.
- Assistant text: `response_item` / `payload.type=="message"` /
  `role=="assistant"` → the first nonempty `text` field in the assistant
  content list.
- Tool calls: `response_item`/`function_call` — name in `.payload.name`,
  args in `.payload.arguments` (a JSON-encoded STRING; parse for `.cmd`).
  File edits: `custom_tool_call` name `apply_patch`, patch in `.payload.input`
  (paths after `*** Update/Add/Delete File:`, may be relative → join cwd);
  0.136+ also emits `event_msg`/`patch_apply_end` with `.payload.changes`
  keyed by ABSOLUTE path. Tool-name drift: older files say `exec`/`wait`,
  newer `exec_command`/`wait_agent` (see `_TOOL_NAMES`).
- Tokens: `event_msg`/`token_count` → `.payload.info.total_token_usage.*`
  is CUMULATIVE per session → take deltas, clamp ≥ 0. NOTE `input_tokens`
  INCLUDES `cached_input_tokens` (unlike Anthropic, where they're disjoint), so
  fresh/full-price input = `input − cached` — bill the cached slice once at the
  cache rate, not twice. Each delta is attributed to the last-seen model (via
  `_add_tokens`) for per-model cost in `pricing.py`. Codex reports no
  cache-creation, so that bucket stays 0.
- Subagents: metadata identifies them through `thread_source:"subagent"` or a
  `source.subagent` object. Their `user_message` records are agent assignments,
  not human prompts. Independent subagents start their own cumulative counters
  at zero and must be included. A rollout with `forked_from_id` first replays
  the parent's history and cumulative counters with new envelope timestamps;
  skip that prefix through the first `task_started` whose integer `started_at`
  is at least the child session's start second. Retain the last replayed counter
  as the baseline, then bill the child's subsequent deltas. This boundary is
  present in both the v1 and v2 formats sampled here. Each later subagent
  `task_started` begins another non-prompt milestone; reused agents may receive
  follow-up tasks without a `user_message`, so that boundary prevents idle gaps
  from being counted as active work.
- Model: only in `turn_context.payload.model` (track last-seen). Never in
  session_meta.
- No turn durations exist → active time is approximated as (last activity
  record ts − milestone ts). No session titles exist → the site falls back
  to the first nonempty prompt, command, or recovered prompt.
- Reasoning: plaintext `summary` in 0.106; ONLY `encrypted_content` from
  0.136 on (unusable — skip).
- `codex_parse.py` skips unused tool-output and reasoning payloads by substring
  test before `json.loads` (`_SKIP_MARKS`). Keep that guard if you add record
  types.

## Live and archived inputs

The archive mirrors these source layouts:

- `archive/claude/<munged-project-dir>/<session>.jsonl`
- `archive/claude/<munged-project-dir>/<session>/subagents/**/*.jsonl`
- `archive/codex/YYYY/MM/DD/rollout-*.jsonl`

Before parsing, an `--all` render builds live/archive manifests. For each
Claude parent or nested-subagent relative path, and for each Codex rollout
basename, it parses only the larger available copy. After parsing,
`_merge_timelines` deduplicates Claude sessions by session ID, preferring more
milestones and then later `last_ts`. See
[Archive transcripts](../README.md#archive-transcripts) for commands,
retention, privacy, and recovery.

## Verify after parser changes

```bash
python3 -m unittest tests.test_codex_parse tests.test_accounting tests.test_transcript_fixtures -v
python3 scripts/build_screenshot_site.py --out /tmp/session-atlas-fixture-site
```

Run these focused checks only with the sanitized committed fixtures. See
[Development](../README.md#development) for the public screenshot-refresh
procedure.
