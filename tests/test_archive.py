import copy
import json
import os
import shutil
import tempfile
import unittest
from collections import Counter
from unittest import mock

import generate_site
from ccx_parse import build_timeline
from codex_parse import _parse_rollout, build_codex_timelines, rollout_paths
from generate_site import SessionArchive, _merge_timelines


FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "transcripts")
# The fixture Codex rollout and the Claude Code project that shares its path.
ROLLOUT = rollout_paths(os.path.join(FIXTURES, "codex"))[0]
CLAUDE_PROJECT = os.path.join(FIXTURES, "claude", "-home-demo-src-example-project")
CLAUDE_BASE = os.path.basename(CLAUDE_PROJECT)


class SessionArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.archive = SessionArchive(os.path.join(self.tmp.name, "archive"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_codex_entry_reproduces_the_rollout_parse(self):
        parsed = _parse_rollout(ROLLOUT)
        self.archive.save_codex(ROLLOUT, parsed)
        [entry] = self.archive.codex_entries()

        self.assertEqual(SessionArchive.load_codex(entry), parsed)
        self.assertEqual(
            build_codex_timelines([entry], parse=SessionArchive.load_codex),
            build_codex_timelines([ROLLOUT]))

    def test_claude_entries_reproduce_the_project_sessions(self):
        timeline = build_timeline(CLAUDE_PROJECT)
        self.archive.save_claude(CLAUDE_BASE, timeline)

        # No session is live, so every archived session comes back.
        restored = _merge_timelines(self.archive.claude_timelines(set()))
        for key in ("project_path", "sessions", "milestones", "stats"):
            self.assertEqual(restored[key], timeline[key], key)

    def test_a_live_claude_session_is_not_read_from_the_archive(self):
        timeline = build_timeline(CLAUDE_PROJECT)
        self.archive.save_claude(CLAUDE_BASE, timeline)
        live = {(CLAUDE_BASE, session["id"]) for session in timeline["sessions"]}

        self.assertEqual(self.archive.claude_timelines(live), [])

    def test_entries_and_their_directories_are_owner_only(self):
        # A world-readable umask shows the archive does not inherit its modes.
        previous = os.umask(0o022)
        try:
            self.archive.save_codex(ROLLOUT, _parse_rollout(ROLLOUT))
        finally:
            os.umask(previous)
        [entry] = self.archive.codex_entries()

        self.assertEqual(os.stat(entry).st_mode & 0o777, 0o600)
        for directory in (self.archive.root, os.path.dirname(entry)):
            self.assertEqual(os.stat(directory).st_mode & 0o777, 0o700)

    def test_codex_sessions_are_ordered_by_rollout_name_across_directories(self):
        parsed = _parse_rollout(ROLLOUT)
        later = copy.deepcopy(parsed)
        later[1]["id"] = "later"
        for milestone in later[2]:
            milestone["session"] = "later"
        # The later rollout sits in a directory that sorts first, as an
        # archive entry and a live rollout can.
        paths = {"/b/rollout-2026-03-15T17-00-00-a.jsonl": parsed,
                 "/a/rollout-2026-03-16T17-00-00-b.json": later}

        [timeline] = build_codex_timelines(list(paths), parse=paths.get)

        self.assertEqual([s["id"] for s in timeline["sessions"]],
                         [parsed[1]["id"], "later"])


class ArchivedRenderTests(unittest.TestCase):
    """Full ``--all`` renders before and after the transcripts are deleted."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = self.tmp.name
        self.projects = os.path.join(root, "claude-projects")
        shutil.copytree(os.path.join(FIXTURES, "claude"), self.projects)
        self.codex_home = os.path.join(root, "codex-home")
        shutil.copytree(os.path.join(FIXTURES, "codex"),
                        os.path.join(self.codex_home, "sessions"))
        # Claude Code can leave a session's transcript in a second project
        # directory, such as a worktree's, with no timeline entries. That
        # project is skipped, and its copy must not replace the real one.
        session = os.listdir(os.path.join(self.projects, CLAUDE_BASE))[0]
        worktree = os.path.join(self.projects, CLAUDE_BASE + "--claude-worktrees-empty")
        os.makedirs(worktree)
        with open(os.path.join(worktree, session), "w") as fh:
            fh.write(json.dumps({"type": "summary", "summary": "no prompts",
                                 "leafUuid": "leaf"}) + "\n")
        self.out = os.path.join(root, "site")
        self.archive = os.path.join(root, "archive")

    def tearDown(self):
        self.tmp.cleanup()

    def render(self):
        """Return {project path: (sessions, prompts)} from one --all render."""
        written = {}

        def record(tl, *args, **kwargs):
            written[tl["project_path"]] = (tl["stats"]["sessions"], tl["stats"]["prompts"])
            return os.path.join(self.out, "unused.html")

        with mock.patch.object(generate_site, "PROJECTS", self.projects), \
                mock.patch.object(generate_site, "_write_project", side_effect=record), \
                mock.patch.object(generate_site, "_atomic_write_text"):
            generate_site.generate_all(self.out, self.archive, [self.codex_home])
        return written

    def delete_transcripts(self):
        shutil.rmtree(self.projects)
        os.makedirs(self.projects)
        shutil.rmtree(os.path.join(self.codex_home, "sessions"))
        os.makedirs(os.path.join(self.codex_home, "sessions"))

    def test_sessions_outlive_their_transcripts(self):
        live = self.render()
        # Both fixture projects render: one from both tools, one from Claude
        # Code. The worktree project has no timeline entries and is skipped.
        self.assertEqual(sorted(live), ["/home/demo/src/docs-site",
                                        "/home/demo/src/example-project"])

        self.delete_transcripts()

        self.assertEqual(self.render(), live)

    def test_a_warm_parse_cache_refills_a_missing_archive(self):
        live = self.render()
        shutil.rmtree(self.archive)
        self.assertEqual(self.render(), live)

        self.delete_transcripts()

        self.assertEqual(self.render(), live)

if __name__ == "__main__":
    unittest.main()
