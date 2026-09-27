import os
import tempfile
import unittest
from unittest import mock

import archive_transcripts


class ArchiveCodexHomesTests(unittest.TestCase):
    def test_archive_copies_live_and_archived_rollouts_from_every_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Two Codex homes, as when the Codex CLI in WSL and the Codex
            # Windows app each keep their own. The first holds one live
            # rollout; the second holds one live rollout and one archived
            # rollout, whose archived_sessions directory has no date
            # subdirectories. The archive files each copy under the date in
            # its file name.
            first = os.path.join(tmp, "first")
            second = os.path.join(tmp, "second")
            names = [
                "rollout-2026-07-20T00-00-00-00000000-0000-0000-0000-000000000001.jsonl",
                "rollout-2026-07-20T00-00-00-00000000-0000-0000-0000-000000000002.jsonl",
                "rollout-2026-07-20T00-00-00-00000000-0000-0000-0000-000000000003.jsonl",
            ]
            folders = [os.path.join(first, "sessions", "2026", "07", "20"),
                       os.path.join(second, "sessions", "2026", "07", "20"),
                       os.path.join(second, "archived_sessions")]
            for folder, name in zip(folders, names):
                os.makedirs(folder)
                with open(os.path.join(folder, name), "w") as fh:
                    fh.write("{}\n")
            dest = os.path.join(tmp, "archive")

            # A missing Claude projects directory leaves only the Codex copies.
            with mock.patch.object(archive_transcripts, "PROJECTS",
                                   os.path.join(tmp, "no-claude-projects")):
                archive_transcripts.archive(dest, [first, second])

            self.assertEqual(
                sorted(os.listdir(os.path.join(dest, "codex", "2026", "07", "20"))),
                names)


if __name__ == "__main__":
    unittest.main()
