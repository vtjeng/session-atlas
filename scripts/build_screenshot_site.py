#!/usr/bin/env python3
"""Build the documentation screenshot site from synthetic transcripts."""

import argparse
from datetime import datetime, timedelta, timezone
import os
import sys


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from ccx_parse import build_timeline  # noqa: E402
from codex_parse import build_codex_timelines, rollout_paths  # noqa: E402
from generate_site import (  # noqa: E402
    _allocate_project_slugs,
    _atomic_write_text,
    _merge_timelines,
    _write_project,
    render_index,
)


FIXTURES = os.path.join(REPO_ROOT, "tests", "fixtures", "transcripts")
# A fixed refresh time keeps documentation images reproducible. The offset is
# Pacific daylight time on the fixture date, not a value copied from a session.
REFRESHED_AT = datetime(
    2026, 3, 15, 14, 30, tzinfo=timezone(timedelta(hours=-7)))


def build_site(out):
    """Parse the synthetic JSONL fixtures and render a complete local site."""
    by_path = {}
    claude_root = os.path.join(FIXTURES, "claude")
    with os.scandir(claude_root) as scan:
        project_dirs = sorted(
            (entry.path for entry in scan if entry.is_dir()),
            key=os.path.basename,
        )
    for project_dir in project_dirs:
        timeline = build_timeline(project_dir)
        by_path.setdefault(timeline["project_path"].rstrip("/"), []).append(timeline)

    codex_root = os.path.join(FIXTURES, "codex")
    for timeline in build_codex_timelines(rollout_paths(codex_root)):
        by_path.setdefault(timeline["project_path"].rstrip("/"), []).append(timeline)

    entries = []
    index_path = os.path.join(out, "index.html")
    slugs = _allocate_project_slugs(by_path)
    for project_path, timelines in sorted(by_path.items()):
        timeline = _merge_timelines(timelines)
        slug = slugs[project_path]
        _write_project(
            timeline,
            out,
            slug,
            index_path=index_path,
            refreshed_at=REFRESHED_AT,
        )
        entries.append((slug, timeline))

    _atomic_write_text(
        index_path,
        render_index(
            entries,
            refreshed_at=REFRESHED_AT,
            source_label=(
                "/home/demo/.claude/projects  ·  /home/demo/.codex/sessions"),
        ),
    )
    print(f"Wrote {index_path} ({len(entries)} synthetic projects)")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, help="output directory")
    args = parser.parse_args()
    build_site(os.path.abspath(args.out))


if __name__ == "__main__":
    main()
