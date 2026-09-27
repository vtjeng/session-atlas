#!/usr/bin/env python3
"""Create a retention archive of Claude Code and Codex transcripts.

    python3 archive_transcripts.py                 # -> ./archive/{claude,codex}/...
    python3 archive_transcripts.py --dest /backup  # custom archive root
    python3 archive_transcripts.py --codex-home ~/.codex --codex-home DIR
                                                   # rollouts from two Codex homes

This retention-only command never deletes archived files or replaces a fuller
copy with a smaller source. See README.md#archive-transcripts for operator
procedure and privacy consequences, and
docs/transcript-formats.md#live-and-archived-inputs for archive layout and
parser selection.
"""
import argparse
import glob
import os
import shutil

from ccx_parse import PROJECTS, _iter_subagent_transcripts
from codex_parse import (CODEX_HOME, CODEX_HOME_HELP, codex_home_dir,
                         codex_home_rollouts, select_rollouts)


def _private_parent(dest_root, dest):
    """Create the destination's directory chain with owner-only access."""
    root = os.path.abspath(dest_root)
    parent = os.path.abspath(os.path.dirname(dest))
    if os.path.commonpath((root, parent)) != root:
        raise ValueError(f"Archive destination escapes its root: {dest}")

    current = root
    os.makedirs(current, mode=0o700, exist_ok=True)
    os.chmod(current, 0o700)
    relative = os.path.relpath(parent, root)
    if relative == ".":
        return
    for part in relative.split(os.sep):
        current = os.path.join(current, part)
        os.makedirs(current, mode=0o700, exist_ok=True)
        os.chmod(current, 0o700)


def _copy(src, dest, dest_root):
    """Atomic copy; returns 'new', 'updated', 'kept', or 'shrunk'."""
    _private_parent(dest_root, dest)
    ssize = os.path.getsize(src)
    if os.path.exists(dest):
        os.chmod(dest, 0o600)
        dsize = os.path.getsize(dest)
        if ssize == dsize:
            return "kept"
        if ssize < dsize:
            return "shrunk"  # never replace a fuller archived copy
        verdict = "updated"
    else:
        verdict = "new"
    tmp = dest + ".tmp"
    shutil.copy2(src, tmp)
    os.chmod(tmp, 0o600)
    os.replace(tmp, dest)
    return verdict


def archive(dest_root, codex_homes):
    jobs = []
    for d in sorted(glob.glob(os.path.join(PROJECTS, "*"))):
        if not os.path.isdir(d):
            continue
        # Archive top-level session files and nested subagent/workflow transcripts.
        # Nested files live at <project>/<session>/subagents/**/*.jsonl and fall
        # outside the top-level glob. Keep each file's path under the project dir so
        # build_timeline finds them in the archive exactly as it does in the live tree.
        for f in sorted(glob.glob(os.path.join(d, "*.jsonl"))) + _iter_subagent_transcripts(d):
            rel = os.path.join("claude", os.path.basename(d), os.path.relpath(f, d))
            jobs.append((f, os.path.join(dest_root, rel)))
    for f in select_rollouts(codex_home_rollouts(codex_homes)):
        # archived_sessions has no date subdirectories, so take YYYY/MM/DD from
        # the file name, rollout-YYYY-MM-DDT..., which matches the live one.
        name = os.path.basename(f)
        rel = os.path.join("codex", name[8:12], name[13:15], name[16:18], name)
        jobs.append((f, os.path.join(dest_root, rel)))

    counts = {"new": 0, "updated": 0, "kept": 0, "shrunk": 0}
    for src, dest in jobs:
        verdict = _copy(src, dest, dest_root)
        counts[verdict] += 1
        if verdict == "shrunk":
            print(f"  ! source smaller than archive, kept archive: {src}")
    print(f"Archived to {os.path.abspath(dest_root)}: "
          f"{counts['new']} new · {counts['updated']} updated · "
          f"{counts['kept']} unchanged"
          + (f" · {counts['shrunk']} kept-larger" if counts["shrunk"] else ""))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dest", default="./archive",
                    help="archive root (default %(default)s; gitignored)")
    # argparse's append adds to a non-empty default, so the default is applied
    # only when the option is absent.
    ap.add_argument("--codex-home", action="append", type=codex_home_dir,
                    metavar="DIR", help=CODEX_HOME_HELP)
    args = ap.parse_args()
    archive(args.dest, args.codex_home or [CODEX_HOME])


if __name__ == "__main__":
    main()
