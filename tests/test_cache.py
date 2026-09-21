import os
import stat
import tempfile
import unittest
from unittest import mock

import generate_site
from codex_parse import build_codex_timelines, rollout_paths
from ccx_parse import _subagent_usage
from generate_site import ParseCache


FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "transcripts")


class ParseCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = os.path.join(self.tmp.name, "site")
        # One source file whose size and modification time drive the key.
        self.source = os.path.join(self.tmp.name, "rollout.jsonl")
        with open(self.source, "w", encoding="utf-8") as fh:
            fh.write('{"type":"session_meta"}\n')
        self.calls = 0

    def tearDown(self):
        self.tmp.cleanup()

    def compute(self):
        self.calls += 1
        return {"parsed": self.calls}

    def test_a_second_render_reads_the_stored_parse(self):
        first = ParseCache(self.out).get("rollout", self.source, [self.source], self.compute)
        second = ParseCache(self.out)
        again = second.get("rollout", self.source, [self.source], self.compute)
        self.assertEqual(first, {"parsed": 1})
        self.assertEqual(again, {"parsed": 1})
        self.assertEqual(self.calls, 1)
        self.assertEqual((second.hits, second.misses), (1, 0))
        # The cache holds transcript data, so it is owner-only like the pages.
        self.assertEqual(stat.S_IMODE(os.stat(second.dir).st_mode), 0o700)
        entries = os.listdir(second.dir)
        self.assertEqual(len(entries), 1)
        self.assertEqual(
            stat.S_IMODE(os.stat(os.path.join(second.dir, entries[0])).st_mode), 0o600)

    def test_a_changed_source_file_is_parsed_again(self):
        cache = ParseCache(self.out)
        cache.get("rollout", self.source, [self.source], self.compute)
        # An appended record changes the size; a rollout grows while its
        # session runs, so this is the common invalidation.
        with open(self.source, "a", encoding="utf-8") as fh:
            fh.write('{"type":"event_msg"}\n')
        value = cache.get("rollout", self.source, [self.source], self.compute)
        self.assertEqual(value, {"parsed": 2})
        self.assertEqual(self.calls, 2)
        # A touched file with the same size also misses: the key holds the
        # modification time as well, since a rewrite can keep the size.
        os.utime(self.source, ns=(1, 1))
        cache.get("rollout", self.source, [self.source], self.compute)
        self.assertEqual(self.calls, 3)

    def test_a_parser_change_is_parsed_again(self):
        ParseCache(self.out).get("rollout", self.source, [self.source], self.compute)
        with mock.patch.object(generate_site, "_parser_version", return_value="changed"):
            ParseCache(self.out).get("rollout", self.source, [self.source], self.compute)
        self.assertEqual(self.calls, 2)

    def test_a_corrupt_entry_is_parsed_again(self):
        cache = ParseCache(self.out)
        cache.get("rollout", self.source, [self.source], self.compute)
        entry = os.path.join(cache.dir, os.listdir(cache.dir)[0])
        with open(entry, "wb") as fh:
            fh.write(b"not a pickle")
        value = ParseCache(self.out).get("rollout", self.source, [self.source], self.compute)
        self.assertEqual(value, {"parsed": 2})

    def test_a_missing_source_file_is_computed_without_caching(self):
        cache = ParseCache(self.out)
        missing = os.path.join(self.tmp.name, "gone.jsonl")
        value = cache.get("rollout", missing, [missing], self.compute)
        self.assertEqual(value, {"parsed": 1})
        self.assertEqual((cache.hits, cache.misses), (0, 0))

    def test_prune_removes_entries_a_run_did_not_use(self):
        first = ParseCache(self.out)
        first.get("rollout", self.source, [self.source], self.compute)
        gone = os.path.join(self.tmp.name, "deleted-later.jsonl")
        with open(gone, "w", encoding="utf-8") as fh:
            fh.write("{}\n")
        first.get("rollout", gone, [gone], self.compute)
        self.assertEqual(len(os.listdir(first.dir)), 2)
        # The next run sees only the surviving transcript, so the other
        # transcript's entry is stale and prune removes it, and nothing else.
        os.unlink(gone)
        second = ParseCache(self.out)
        second.get("rollout", self.source, [self.source], self.compute)
        self.assertEqual(second.prune(), 1)
        self.assertEqual(len(os.listdir(second.dir)), 1)
        self.assertEqual(second.hits, 1)

    def subagent_file(self):
        # A nested transcript in Claude's shape: one message streamed as two
        # assistant records with growing usage (the reader keeps the field-wise
        # maximum), a second message on another model, and one malformed
        # assistant line that becomes a diagnostic (the reader decodes only
        # lines that look like assistant records once it has a start time).
        path = os.path.join(self.tmp.name, "agent-1.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('{"type":"assistant","timestamp":"2026-03-15T09:42:00.000Z",'
                     '"message":{"id":"m1","model":"opus-4-8",'
                     '"usage":{"input_tokens":10,"output_tokens":5}}}\n')
            fh.write('{"type":"assistant","timestamp":"2026-03-15T09:42:01.000Z",'
                     '"message":{"id":"m1","model":"opus-4-8",'
                     '"usage":{"input_tokens":10,"output_tokens":40}}}\n')
            fh.write('{"type":"assistant","message":{broken\n')
            fh.write('{"type":"assistant","timestamp":"2026-03-15T09:43:00.000Z",'
                     '"message":{"id":"m2","model":"sonnet-5",'
                     '"usage":{"input_tokens":7,"output_tokens":3}}}\n')
        return path

    def test_subagent_usage_is_reused_with_its_diagnostics(self):
        path = self.subagent_file()
        direct_diags = []
        direct = _subagent_usage(path, direct_diags)
        first = ParseCache(self.out)
        first_diags = []
        self.assertEqual(first.subagent_usage()(path, first_diags), direct)
        second = ParseCache(self.out)
        second_diags = []
        self.assertEqual(second.subagent_usage()(path, second_diags), direct)
        # 40 output tokens: the field-wise maximum of the two m1 records, not
        # their sum; the malformed line is reported on a hit as on a miss.
        self.assertEqual(direct[0]["opus-4-8"]["out"], 40)
        self.assertEqual(len(direct_diags), 1)
        self.assertEqual(first_diags, direct_diags)
        self.assertEqual(second_diags, direct_diags)
        self.assertEqual((first.hits, first.misses), (0, 1))
        self.assertEqual((second.hits, second.misses), (1, 0))

    def test_touch_keeps_subagent_entries_through_prune(self):
        path = self.subagent_file()
        ParseCache(self.out).subagent_usage()(path, [])
        # A later run reuses the project without reading the subagent entry;
        # touching it marks it as still belonging to the corpus.
        later = ParseCache(self.out)
        self.assertEqual(later.prune(), 1)
        ParseCache(self.out).subagent_usage()(path, [])
        kept = ParseCache(self.out)
        kept.touch("subagent", [path])
        self.assertEqual(kept.prune(), 0)

    def test_codex_timelines_are_the_same_through_the_cache(self):
        paths = rollout_paths(os.path.join(FIXTURES, "codex"))
        direct = build_codex_timelines(paths)
        cache = ParseCache(self.out)
        cached_once = build_codex_timelines(paths, parse=cache.rollout_parser())
        cached_twice = build_codex_timelines(paths, parse=ParseCache(self.out).rollout_parser())
        self.assertEqual(cached_once, direct)
        self.assertEqual(cached_twice, direct)
        # Every fixture rollout missed on the first pass and hit on the second.
        self.assertEqual((cache.hits, cache.misses), (0, len(paths)))


if __name__ == "__main__":
    unittest.main()
