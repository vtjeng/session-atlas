# session-atlas

session-atlas turns local Claude Code and Codex CLI transcripts into static HTML
timelines.

The project index summarizes activity across projects.

<p align="center"><img src="docs/images/project-index-preview.png" alt="Project index with an activity summary and two project cards" width="720"></p>

Each project page shows prompts and the assistant activity that followed them,
including tool use, changed files, token usage, [recorded or estimated active
time](#usage-and-cost-accounting), and estimated cost.

<p align="center"><img src="docs/images/timeline-entry.png" alt="Session content with a prompt, token usage, files, and tool calls" width="720"></p>

## Quick start

The site generator requires Python 3.9 or later on a Unix-like system such as
Linux or macOS. It uses only the Python standard library. Run the commands below
from the repository root.

### All projects

Generate a project index and pages for every project for which session-atlas
finds at least one timeline entry:

```bash
python3 generate_site.py --all
```

This command writes the project index to `site/index.html`. A `--all` render
also removes an obsolete `index.html` created by session-atlas. It then removes
the project's directory if that directory is empty. Files that session-atlas
did not create remain in place.

### Single project

Generate a page for one project by passing `generate_site.py` a project
directory name, also called its basename, or a repository path:

```bash
python3 generate_site.py example-project
python3 generate_site.py "$HOME/src/example-project"
```

The command writes the page under `site/` using a stable path-derived slug and
prints its exact `file://` URL. Open that URL in a browser. The page contains its
styles and scripts, so it works without a web server or network connection.

> [!NOTE]
> **Project lookup:** The site generator combines Claude Code and Codex CLI
> sessions when both tools recorded activity for the project. It can also find
> a Codex-only project by basename or repository path. For Claude Code, it also
> accepts the project's transcript directory under `~/.claude/projects/`.

## Privacy

> [!WARNING]
> Source transcripts, archives, and generated pages can contain prompts,
> assistant-response excerpts, local paths, shell commands, file names, Git
> branches, model identifiers, and usage data. Review them before sharing.

By default, the scripts read live transcripts from your home directory, access
those files and any archives through filesystem paths, write all output
locally, and do not upload source data.

Git ignores `site/` and `archive/`. A normal clone therefore contains the
source code, tests, documentation, and optional systemd units, but it does not
contain your local transcripts, generated pages, or local archive.

The generator sets site directories to owner-only mode `0700` and generated
HTML and lock files to owner-only mode `0600`.

The committed documentation images are generated from synthetic transcript
fixtures under `tests/fixtures/transcripts/`. The screenshot workflow does not
read an existing `site/` or transcripts from a developer's home directory.

## Where to go next

> [!NOTE]
> The remaining sections are optional. Use this table to find the detail you
> need.

| Goal | Go to |
| --- | --- |
| See how the index and project pages are organized | [What the atlas shows](#what-the-atlas-shows) |
| Automate refreshes, archive transcripts, or inspect parser output | [Advanced tasks](#advanced-tasks) |
| Understand classification, timelines, and accounting | [Reference](#reference) |
| Run tests or refresh documentation images | [Development](#development) |

## What the atlas shows

These representative images show the project index, a project overview,
expanded cost details, and session content.

### Project index summary

The top of the project index summarizes activity across every project and links
to the method and token counts behind its cost estimate.

![Project index summary including estimated cost](docs/images/project-log-summary.png)

### Project index cards

The project index compares projects on a shared time axis. Source badges
distinguish a project with both Claude Code and Codex CLI sessions from a
Claude-only project.

![Two project cards with different transcript sources](docs/images/project-cards.png)

### Project overview

An individual project page summarizes its sessions, activity, models, tools,
and estimated cost. An expandable panel explains the estimate and breaks it
down by model and token category.

![Individual project overview with activity statistics](docs/images/project-overview.png)

### Expanded estimated cost

The expanded accounting control shows estimated cost by model and token
category in one table.

![Expanded estimated-cost control with one cost table](docs/images/expanded-cost.png)

### Session content

Each session groups prompts with the assistant activity that followed them.
A `claude` or `codex` badge in the session header identifies the transcript
source.

![Session content with a prompt, token usage, files, and tool calls](docs/images/timeline-entry.png)

## Advanced tasks

### Refresh automatically with systemd

The units in `systemd/user/` can refresh `site/` every ten minutes on a Linux
system that runs user-level systemd. Other platforms require a different
scheduler.

The supplied service assumes that the checkout is at `~/src/session-atlas` and
Python is at `/usr/bin/python3`. Edit `WorkingDirectory` and `ExecStart` in
`systemd/user/session-atlas-render.service` if either path differs.

Install and start the timer from the repository root:

```bash
systemctl --user link \
  "$PWD/systemd/user/session-atlas-render.service" \
  "$PWD/systemd/user/session-atlas-render.timer"
systemctl --user daemon-reload
systemctl --user enable --now session-atlas-render.timer
systemctl --user start session-atlas-render.service
```

Check the schedule and the most recent render:

```bash
systemctl --user list-timers session-atlas-render.timer
systemctl --user status session-atlas-render.service
```

Generated project and index pages show their refresh time in the header and
footer.

The timer renders the site but does not archive transcripts. Schedule
`archive_transcripts.py` separately if you need automatic retention.

### Archive transcripts

Use `archive_transcripts.py` to preserve Claude Code session files and Codex CLI
rollout files when source files might be cleaned up or a tool reinstalled:

```bash
python3 archive_transcripts.py
```

The default command creates a retention-oriented local mirror under `archive/`.
It never deletes an archived session. When a source file grows, the command
atomically replaces the archived copy with the larger file. It keeps the
archived copy when the source is smaller. It also sets archive directories to
owner-only mode `0700` and archived files to owner-only mode `0600`.

This behavior is not a deletion or redaction workflow. In particular,
shortening or removing a source transcript does not remove the fuller archived
copy. An audited replacement and deletion workflow remains a TODO. Until that
exists, remove sensitive archived data deliberately and account for every copy
and backup that may retain it.

A local mirror does not protect against disk or machine loss. Write the archive
to storage that is backed up or mounted from another device when you need that
protection:

```bash
python3 archive_transcripts.py --dest /mnt/backup/session-atlas
```

Replace `/mnt/backup/session-atlas` with a path on backed-up or separately
mounted storage. Pass the same path when rendering:

```bash
python3 generate_site.py --all --archive /mnt/backup/session-atlas
```

`generate_site.py --all` reads the union of live transcripts and the selected
archive root, then deduplicates sessions. The archive can contain private data.

The archiver does not copy `~/.codex/history.jsonl` or
`~/.codex/logs_2.sqlite`, so it does not preserve prompts recovered from those
history sources. Back up those files separately if you need them.

### Inspect Claude Code statistics

`ccx_parse.py` provides a lower-level view of one Claude Code project:

```bash
python3 ccx_parse.py example-project
```

It prints aggregate statistics as JSON, then reports how many timeline
milestones and sessions it found. An input milestone groups one human input
with the assistant activity that occurred before the next input. The parser can
also retain substantive activity that occurs before a session's first input.

If a transcript contains malformed JSON or a record that is not UTF-8, the
parser warns with the file and line number, skips that record, and continues.
Generated project pages and index cards show the number of skipped records so a
partial render is not mistaken for a complete one.

## Reference

### Input classification

The parsers distinguish text that you typed from records inserted by the CLI or
created by a child agent. A child agent, also called a subagent, is an agent
started by the main session to perform part of a task.

#### Claude Code

Claude Code stores each session as JSONL under
`~/.claude/projects/<munged-cwd>/<session-uuid>.jsonl`. The munged directory
name replaces each `/` in the project path with `-`.

In these files, a record with `role: user` can represent several kinds of data:

| Record | Classification | Reason |
| --- | --- | --- |
| Free-text prompt | The parser records it as `prompt`. | You typed it. |
| Slash command such as `/goal ...` | The parser records it as `command`. | You typed it. |
| `<local-command-stdout>` | The parser excludes it. | It is command output. |
| `isMeta`, `tool_result`, `<task-notification>`, `<system-reminder>`, or `[Request interrupted]` | The parser excludes it. | The CLI inserted it. |
| `isSidechain` | The parser excludes it. | It belongs to a child agent. |

`classify_user()` in `ccx_parse.py` implements this classification.

#### Codex CLI

Codex CLI stores sessions under
`~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`. The parser treats `event_msg`
records whose payload type is `user_message` as inputs, except in child-agent
sessions. It ignores the separate `response_item` user records because those
records can also contain injected `AGENTS.md` instructions and environment
context.

Child-agent assignments do not count as human input, but their activity and
token usage remain part of the project totals. A forked child transcript starts
with copied records from its parent. The parser skips those copied records and
then counts the child's own activity and incremental token usage after its first
`task_started` event.

When a session has no discovered rollout, session-atlas can recover its prompts
from `~/.codex/history.jsonl` and map them to a project with
`~/.codex/logs_2.sqlite`. The page labels these entries `recovered` because the
available sources do not establish why the rollout is absent. Those sources do
not contain the reply, tool activity, token usage, or cost.

### Timeline behavior

#### Sessions and navigation

Project pages group retained top-level Claude Code sessions and Codex CLI
rollouts into separate sections. Nested Claude Code child transcripts add their
token totals to the entry that started them instead of creating another
section.

Entries use anchors such as `#s02-04`, where `02` is the session number and
`04` is the entry number within that session. Session headings use anchors such
as `#s02`.

#### Charts and timestamps

The top ribbon places entries from all sections on one chronological axis and
colors them by session. The right-hand minimap uses square-root-scaled tick
widths for entries with more active time, or more output tokens when timing data
is unavailable. The project index uses square-root-scaled bar heights for
active time, with output tokens as the fallback when timing data is unavailable.

The generator renders timestamps in the local timezone of the machine that
runs it. Transcript timestamps are stored in UTC.

#### Automated sessions

Codex CLI sessions from temporary `codex exec` working directories are grouped
under the interactive checkout when their Git remotes match. Project pages hide
`codex exec` and child-agent sessions by default. The page toggle adds
`?show-automated=1` to the URL. Project totals include these sessions whether or
not they are visible.

#### Claude Code project paths

Claude Code's munged directory names are lossy because path separators and real
dashes both become `-`. The parser uses the `cwd` field from a transcript record
when available. Otherwise, it reconstructs a best-effort path from the munged
directory name.

#### Project output directories

Project pages display the original project name and path, but the generator
writes each page under a restricted ASCII slug: a readable basename plus a
stable hash suffix derived from the full path. Single-project and `--all` runs
therefore use the same directory without storing a path manifest, and projects
with matching basenames cannot overwrite one another. The generator rejects
unsafe slugs and existing project-directory symlinks instead of writing outside
`--out`.

### Usage and cost accounting

Claude Code token totals come from assistant `usage` fields, and duration totals
come from system `turn_duration` records. Codex CLI token totals come from
changes in cumulative `token_count` records. Because Codex does not record turn
durations, the parser estimates active time from each input timestamp through
the last activity record before the next input.

Cost is an estimate based on the per-model list rates in `pricing.py`. The rate
table in `pricing.py` uses input, output, cache-read, standard cache-write, and
one-hour cache-write rates per million tokens as of the `AS_OF` date in that
file. It excludes batch, priority, long-context, volume, and enterprise pricing.

A dollar figure ending in `+` is partial because at least one model has no
listed rate. The accounting panel shows each unpriced model and its token count;
add the missing model to `pricing.py` to include its cost. The page labels a
complete figure `est. cost` and a partial figure `partial est. cost`.

## Development

Run the test suite from the repository root:

```bash
python3 -m unittest discover -s tests -v
```

The screenshot workflow requires Node.js 20 or later. Install the exact
Playwright version in `package-lock.json` and its Chromium build, then refresh
the README screenshots:

```bash
npm ci
npm run screenshots:install
npm run screenshots
```

The screenshot command builds a temporary site through the production parsers
and renderer using only `tests/fixtures/transcripts/`. It captures that site
without replacing text or statistics in the browser.

The main files have these roles:

| Path | Purpose |
| --- | --- |
| `generate_site.py` | Generates project pages and the all-project index. |
| `ccx_parse.py` | `build_timeline()` resolves Claude Code projects and builds their timelines. |
| `codex_parse.py` | `build_codex_timelines()` builds timelines from Codex CLI rollout files. |
| `archive_transcripts.py` | Copies live transcript files into a nondeleting local archive. |
| `pricing.py` | Applies the model rates in `estimate_cost()`. |
| `scripts/build_screenshot_site.py` | Builds the documentation site from synthetic transcript fixtures. |
| `tests/fixtures/transcripts/` | Holds invented Claude Code and Codex CLI records for parser and screenshot tests. |
| `docs/transcript-formats.md` | Documents transcript fields and parser mappings for future parser changes. |

## License

session-atlas is available under the [MIT License](LICENSE).
