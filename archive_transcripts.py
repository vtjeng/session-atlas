#!/usr/bin/env python3
"""Append-only local archive of Claude Code and Codex transcripts.

    python3 archive_transcripts.py                 # -> ./archive/{claude,codex}/...
    python3 archive_transcripts.py --dest /backup  # custom archive root

Copies every session file into the archive, mirroring each source's layout:

    archive/claude/<munged-project-dir>/<session>.jsonl
    archive/claude/<munged-project-dir>/<session>/subagents/**/*.jsonl
    archive/codex/YYYY/MM/DD/rollout-*.jsonl

Rules that make it safe to run any time (cron, post-session hook, manually):
- never deletes anything from the archive;
- re-copies a file only when the source is LARGER (live session files are
  append-only, so larger = newer superset); a smaller source is left alone
  and reported, never allowed to clobber a fuller archived copy;
- writes via a temp file + atomic rename, so a crash can't truncate an entry.
- sets directories to owner-only mode 0700 and files to owner-only mode 0600.

This archive is retention-oriented. It is not a deletion or redaction
workflow: a smaller or removed source does not replace or remove an archived
copy.

`generate_site.py --all` reads the union of live + archived files (sessions
deduplicated), so archived history keeps rendering even after the live copy
is gone (e.g. Claude Code's cleanupPeriodDays purge, reinstall, new machine).
"""
import argparse
import glob
import os
import shutil

from ccx_parse import PROJECTS, _iter_subagent_transcripts
from codex_parse import rollout_paths


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


def archive(dest_root):
    # TODO: Add an audited replacement/deletion workflow for intentional
    # redaction. This archive remains retention-oriented until then.
    jobs = []
    for d in sorted(glob.glob(os.path.join(PROJECTS, "*"))):
        if not os.path.isdir(d):
            continue
        # top-level session files + nested subagent/workflow transcripts (which live
        # at <project>/<session>/subagents/**/*.jsonl and carry the bulk of a fan-out's
        # token spend). Mirror both, keeping each file's path under the project dir so
        # build_timeline finds them in the archive exactly as it does in the live tree.
        for f in sorted(glob.glob(os.path.join(d, "*.jsonl"))) + _iter_subagent_transcripts(d):
            rel = os.path.join("claude", os.path.basename(d), os.path.relpath(f, d))
            jobs.append((f, os.path.join(dest_root, rel)))
    for f in rollout_paths():
        rel = os.path.join("codex", *f.split(os.sep)[-4:])
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
                    help="archive root (default ./archive, gitignored)")
    args = ap.parse_args()
    archive(args.dest)


if __name__ == "__main__":
    main()
