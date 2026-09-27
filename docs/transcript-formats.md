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
            milestones:[{kind: "prompt"|"command"|"terminal"|"recovered"|"session"|"subagent", text, ts,
                         session: <session id>, source_id, activity}],
            diagnostics: [<skipped-record warning>],
            stats: ccx_parse._aggregate(milestones, sessions)}
activity = ccx_parse._new_activity()   # tools, tool_events(≤40), files,
                                       # tokens_*, tokens_by_model, duration_ms,
                                       # assistant_turns, models, gist,
                                       # responses(≤40 excerpts), title
```

Both session types contain `id`, `last_ts`, `title`, and `tool`. Codex sessions
also contain `originator`, `repository_url`, `is_subagent`, `subagent_label`,
`parent_session_id`, `parent_relation`, and optional `is_history_only`.
The aggregate `stats.sessions` value counts non-automated conversations;
`stats.automated_sessions` counts standalone Codex child-agent and `codex exec`
rollouts. When a child-agent rollout identifies a parent present in the parsed
project, its work is moved into a timestamped `subagent` milestone in that
parent session, so it does not add another session. Orphaned child rollouts
remain standalone. Automated activity remains included in the other aggregate
totals. Automated rollouts are separate transcript files for delegated work,
not additional human conversations.

A milestone is an attribution interval. `source_id` identifies the source record
or append-only source position used for stable in-page anchors. `prompt`,
`command`, and `recovered`
intervals start at retained inputs; `session` intervals collect substantive
machine activity before the first retained input or at a child-task boundary;
`subagent` intervals mark delegated Codex work at the child rollout's start
time. Each interval owns activity until the next boundary, and empty `session`
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
- `<bash-input>`, `<bash-stdout>`, and `<bash-stderr>` wrappers are normalized;
  adjacent input and output records become one single-line `command` entry.
- Assistant text: each nonempty `text` block is retained as a bounded response
  excerpt; repeated Claude stream records for one message are deduplicated.
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
  Reconcile against `/usage` (`/cost` is an alias). Claude Code v2.1.211 and
  later resets the total when `/clear` starts a new session; earlier versions
  accumulated cost for the process lifetime. See
  [Claude Code cost accounting](https://code.claude.com/docs/en/costs).
- Timestamps are UTC ISO-8601; the site renders local time via
  `datetime.astimezone()`.

## Codex CLI (`~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl`)

Envelope on every line: `{"timestamp": <ISO8601 ms UTC>, "type": T, "payload": {…}}`.
First line is `session_meta` → `.payload.{id, cwd, cli_version, timestamp,
git{branch}? (0.142+)}`. Sessions are date-organized, NOT project-organized —
group by `cwd`. An archived conversation's rollout moves, with the same file
name, into `~/.codex/archived_sessions/`, which has no date subdirectories.

- **Typed prompts:** older rollouts use `event_msg` /
  `payload.type=="user_message"` (`.payload.message`). Current rollouts use
  `response_item` / `payload.type=="message"` / `role=="user"`; trust those
  records only when `internal_chat_message_metadata_passthrough.content_item_kinds`
  is exactly `["user.text"]`. Other user-role records contain injected
  AGENTS.md, environment, command, interruption, or subagent context; role:
  developer is always injected permissions.
- Recovered prompts: `build_history_only_timelines()` selects history entries
  whose session ID has no discovered rollout, then maps their working directory
  through `logs_2.sqlite`. It emits `kind:"recovered"` milestones. A recovered
  prompt is typically associated with a Codex `/btw` fork, but not always; that
  is an inference from `thread/fork` log records, not a stored `/btw` field.
  The history sources do not contain assistant replies or enough structured
  tool, token, or cost data for attribution.
- Assistant text: `response_item` / `payload.type=="message"` /
  `role=="assistant"` → every nonempty text block in the assistant content list
  is retained as a bounded response excerpt.
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

An `--all` render reads Claude Code transcripts from `~/.claude/projects/` and
Codex rollouts from the `sessions` and `archived_sessions` directories of each
Codex home (`~/.codex` unless `--codex-home` names others). When a rollout
appears in more than one home, it parses only the largest copy.

`SessionArchive` in `generate_site.py` saves each parsed session as JSON with
`"format": 1`:

- `codex/<rollout name>.json`: `cwd`, `session`, `milestones`, `branches`, and
  `diagnostics`, the five values `_parse_rollout` returns. Archived rollouts
  join live ones in `build_codex_timelines`, which orders sessions by rollout
  name.
- `claude/<project directory>/<session id>.json`: `project_path`, `session`,
  and that session's `milestones` from `build_timeline`, for projects that
  render. The project directory is part of the name because one session's
  transcript can sit in two project directories, such as a checkout's and a
  worktree's. Each entry becomes a one-session timeline, and `_merge_timelines`
  orders it among the project's other sessions.

A render reads an entry only when its transcript is gone: a Codex entry when no
live rollout has its name, and a Claude entry when its project directory has
no live transcript with its session ID. When an entry's shape changes, change
`FORMAT` and convert the existing entries, because entries whose transcripts
are gone cannot be parsed again. See
[Keep sessions after their transcripts are deleted](../README.md#keep-sessions-after-their-transcripts-are-deleted)
for retention and privacy.

## Verify after parser changes

```bash
python3 -m unittest tests.test_codex_parse tests.test_accounting tests.test_transcript_fixtures -v
python3 scripts/build_screenshot_site.py --out /tmp/session-atlas-fixture-site
```

Run these focused checks only with the sanitized committed fixtures. See
[Development](../README.md#development) for the public screenshot-refresh
procedure.
