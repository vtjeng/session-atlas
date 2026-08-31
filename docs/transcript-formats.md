# Transcript formats (Claude Code & Codex CLI)

This reference describes the fields exercised by the sanitized fixtures under
`tests/fixtures/transcripts/` and the parser behavior they test. Transcript
formats can change between CLI releases. Both parsers emit the same timeline
dict shape; `generate_site.py` renders either and merges them per repository
path. If you change one parser's output shape, change the other parser and
`_merge_timelines` in `generate_site.py` together.

## Shared timeline shape (the contract)

```
timeline = {project_dir, project_name, project_path, git_branches,
            sessions:  [{id, last_ts, title, tool: "claude"|"codex"}],
            milestones:[{kind: "prompt"|"command"|"recovered"|"session", text, ts,
                         session: <session id>, activity}],
            diagnostics: [<skipped-record warning>],
            stats: ccx_parse._aggregate(milestones, sessions)}
activity = ccx_parse._new_activity()   # tools, tool_events(≤40), files,
                                       # tokens_*, tokens_by_model, duration_ms,
                                       # assistant_turns, models, gist, title
```
Milestone = one thing the human typed + all machine work until the next one.
A `kind:"session"` pseudo-milestone captures pre-first-prompt activity and is
dropped unless it contains substantive activity such as tool use or tokens.

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
  Claude Code writes ONE record per content block (thinking / text / each
  tool_use), all sharing one `message.id` and repeating (main thread) — or, in
  streamed subagent logs, accumulating toward — the SAME `message.usage`. Summing
  per record N-counts a multi-block message (top-of-tree ~2–3×); `build_timeline`
  bills each `message.id` once via a running field-wise MAX (input/cache are
  constant across the records, output climbs in subagent streams). Preserve
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
  Their usage is often the bulk of a fan-out's spend; `_attribute_subagents`
  rolls each file's (deduped) tokens into the milestone that spawned it, by
  the transcript's first timestamp, so cost isn't silently understated. If a
  child begins after the parent snapshot read by the current render, defer it
  until the next render rather than attributing it to the preceding prompt.
  Reconcile against `/cost`, but
  note `/cost` is process-scoped and spans multiple session-ids after a `/clear`.
- Timestamps are UTC ISO-8601; the site renders local time via
  `datetime.astimezone()`.
- Retention: Claude Code can delete transcripts according to
  `cleanupPeriodDays`. Use the archive when you need retention beyond the
  source tool's cleanup window.

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
  `role=="assistant"` → `content[0].text`.
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
  to the first prompt.
- Reasoning: plaintext `summary` in 0.106; ONLY `encrypted_content` from
  0.136 on (unusable — skip).
- Perf: large rollouts are dominated by token-count, tool-output, and reasoning
  records. `codex_parse.py` skips unused payload types by substring test before
  `json.loads` (`_SKIP_MARKS`). Keep that guard if you add record types.
- Retention: As of August 30, 2026, Codex has no configurable transcript TTL.
  [openai/codex#6015](https://github.com/openai/codex/issues/6015) is the open
  request for configurable retention.

## Archive & regeneration

- `python3 archive_transcripts.py` → append-only mirror under `./archive/`
  (gitignored, private). It uses mode `0700` for directories and `0600` for
  files. It copies a source only when it is larger and never lets a smaller
  source replace a fuller copy. This is a retention workflow, not a redaction
  workflow.
- `generate_site.py --all` builds a per-project live/archive manifest before
  parsing and selects the larger copy of every relative parent/subagent path.
  This preserves a fuller archived child alongside a fuller live parent and
  prevents duplicate JSON decoding.
- `generate_site.py --all` reads live ∪ archive: Codex deduped per file
  (basename, keep larger), Claude deduped per session id in
  `_merge_timelines` (keep more milestones, then later last_ts).

## Verify after parser changes

```bash
python3 -m unittest tests.test_transcript_fixtures -v
python3 scripts/build_screenshot_site.py --out /tmp/session-atlas-fixture-site
npm run screenshots
```

The fixture builder and screenshot command must read only the sanitized,
committed transcript fixtures. Use local transcripts only for deliberate,
private investigations.
