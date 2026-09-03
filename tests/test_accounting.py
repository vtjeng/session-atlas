import json
import os
import tempfile
import unittest
from unittest import mock

import generate_site
import pricing
from archive_transcripts import _copy
from ccx_parse import _subagent_usage, build_timeline
from generate_site import (_allocate_project_slugs, _atomic_write_text,
                           _breakdown_table, _claude_manifest,
                           _project_output_dir, cost_display)


def _assistant(ts, mid, model, usage):
    return {"type": "assistant", "timestamp": ts,
            "message": {"id": mid, "model": model, "usage": usage, "content": []}}


class AccountingTests(unittest.TestCase):
    def test_project_slugs_are_safe_stable_and_order_independent(self):
        # The matching basenames exercise path-derived suffixes. The root and
        # punctuation-heavy paths exercise empty and unsafe basename handling.
        paths = [
            "/",
            "/work/a/example project",
            "/work/b/example project",
            "/work/javascript:alert(1)#fragment",
        ]

        forward = _allocate_project_slugs(paths)
        reverse = _allocate_project_slugs(list(reversed(paths)))

        self.assertEqual(forward, reverse)
        # A standalone render must choose the same directory as an --all
        # render; otherwise two separately rendered projects can overwrite one
        # another before either invocation discovers the basename collision.
        for path in paths:
            self.assertEqual(
                _allocate_project_slugs([path])[path],
                forward[path],
            )
        # "root" is the readable fallback for the filesystem root, followed by
        # the same stable path-derived suffix used for every other project.
        self.assertRegex(forward["/"], r"^root--[0-9a-f]{64}$")
        self.assertNotEqual(
            forward["/work/a/example project"],
            forward["/work/b/example project"],
        )
        for slug in forward.values():
            self.assertRegex(slug, r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
            self.assertRegex(slug, r"--[0-9a-f]{64}$")
            self.assertLessEqual(len(slug), 80)
        dangerous = forward["/work/javascript:alert(1)#fragment"]
        self.assertNotIn(":", dangerous)
        self.assertNotIn("#", dangerous)

    def test_project_output_directory_rejects_escape_and_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_root = os.path.join(tmp, "site")
            outside = os.path.join(tmp, "outside")
            os.makedirs(output_root)
            os.makedirs(outside)
            # A child symlink models a stale or hostile output directory that
            # would otherwise redirect the generated page outside --out.
            os.symlink(outside, os.path.join(output_root, "linked"))

            with self.assertRaises(ValueError):
                _project_output_dir(output_root, "..")
            with self.assertRaises(ValueError):
                _project_output_dir(output_root, "linked")

            safe = _project_output_dir(output_root, "example-project")
            self.assertEqual(
                os.path.commonpath((os.path.abspath(output_root), safe)),
                os.path.abspath(output_root),
            )

    def test_full_render_prunes_stale_generated_pages(self):
        with tempfile.TemporaryDirectory() as tmp:
            # These directory names cover an active page, a current generated
            # page, a pre-marker generated page, a user page, and a generated
            # directory containing an extra file that the renderer does not own.
            cases = ("active", "stale", "legacy", "personal", "stale-with-notes")
            for name in cases:
                os.makedirs(os.path.join(tmp, name))

            # The explicit generator marker is the ownership signal for new pages.
            generated = '<meta name="generator" content="session-atlas">'
            # The two legacy fragments jointly identify pages written before the
            # explicit marker existed, including the stale pages in ./site now.
            legacy = ('<title>Legacy &middot; project log</title>'
                      '<footer>generated from local transcripts</footer>')
            # This unrelated HTML must survive even though its directory is stale.
            personal = '<title>Personal notes</title>'
            pages = {
                "active": generated,
                "stale": generated,
                "legacy": legacy,
                "personal": personal,
                "stale-with-notes": generated,
            }
            for name, body in pages.items():
                with open(os.path.join(tmp, name, "index.html"), "w") as fh:
                    fh.write(body)
            # The renderer owns its marked index page but not the sibling note.
            with open(os.path.join(tmp, "stale-with-notes", "notes.txt"), "w") as fh:
                fh.write("keep")

            removed = generate_site._prune_stale_project_pages(
                tmp, {"active"})

            # All three generated pages are stale, including the one beside a note.
            self.assertEqual(removed, ["legacy", "stale", "stale-with-notes"])
            self.assertTrue(os.path.exists(os.path.join(tmp, "active", "index.html")))
            self.assertFalse(os.path.exists(os.path.join(tmp, "legacy")))
            self.assertFalse(os.path.exists(os.path.join(tmp, "stale")))
            self.assertTrue(os.path.exists(os.path.join(tmp, "personal", "index.html")))
            self.assertFalse(os.path.exists(
                os.path.join(tmp, "stale-with-notes", "index.html")))
            self.assertTrue(os.path.exists(
                os.path.join(tmp, "stale-with-notes", "notes.txt")))

    def test_archive_copy_enforces_private_file_and_directory_modes(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "source.jsonl")
            archive_root = os.path.join(tmp, "archive")
            destination = os.path.join(archive_root, "codex", "2026", "entry.jsonl")
            with open(source, "w") as fh:
                # One complete JSON record is enough to exercise the copy path.
                fh.write('{}\n')
            # A world-readable source verifies that the archive does not preserve
            # a permissive source mode.
            os.chmod(source, 0o644)

            self.assertEqual(_copy(source, destination, archive_root), "new")
            self.assertEqual(os.stat(destination).st_mode & 0o777, 0o600)
            self.assertEqual(os.stat(archive_root).st_mode & 0o777, 0o700)
            self.assertEqual(
                os.stat(os.path.join(archive_root, "codex")).st_mode & 0o777,
                0o700,
            )
            self.assertEqual(
                os.stat(os.path.join(archive_root, "codex", "2026")).st_mode & 0o777,
                0o700,
            )

            # A later no-op archive run must repair a mode changed after creation.
            os.chmod(destination, 0o644)
            self.assertEqual(_copy(source, destination, archive_root), "kept")
            self.assertEqual(os.stat(destination).st_mode & 0o777, 0o600)

    def test_claude_cache_write_ttls_survive_stream_dedup_and_price_separately(self):
        usage1 = {"input_tokens": 3, "output_tokens": 4,
                  "cache_read_input_tokens": 5, "cache_creation_input_tokens": 30,
                  "cache_creation": {"ephemeral_5m_input_tokens": 10,
                                     "ephemeral_1h_input_tokens": 20}}
        usage2 = {**usage1, "output_tokens": 7, "cache_creation_input_tokens": 45,
                  "cache_creation": {"ephemeral_5m_input_tokens": 15,
                                     "ephemeral_1h_input_tokens": 30}}
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "agent.jsonl")
            with open(path, "w") as fh:
                fh.write(json.dumps({"type": "user", "timestamp": "2026-07-01T00:00:00Z"}) + "\n")
                fh.write(json.dumps(_assistant("2026-07-01T00:00:01Z", "m1",
                                               "claude-opus-4-8", usage1)) + "\n")
                fh.write(json.dumps(_assistant("2026-07-01T00:00:02Z", "m1",
                                               "claude-opus-4-8", usage2)) + "\n")
            by_model, start = _subagent_usage(path)
        self.assertEqual(start, "2026-07-01T00:00:00Z")
        self.assertEqual(by_model["claude-opus-4-8"],
                         {"in": 3, "out": 7, "cr": 5, "cc": 15, "cc1h": 30})
        cats, total, unpriced = pricing.cost_breakdown(by_model)
        self.assertFalse(unpriced)
        self.assertAlmostEqual(cats["cc"]["cost"], 15 * 6.25 / 1_000_000)
        self.assertAlmostEqual(cats["cc1h"]["cost"], 30 * 10 / 1_000_000)
        self.assertAlmostEqual(total, sum(c["cost"] for c in cats.values()))

    def test_fable_5_1_price_breakdown(self):
        # One million tokens in each category exercises Fable 5.1's full rate
        # card, including its lower cache-read price.
        tokens = {key: 1_000_000 for key, _ in pricing.CATEGORIES}
        cats, total, unpriced = pricing.cost_breakdown({
            "claude-fable-5-1": tokens})

        self.assertFalse(unpriced)
        self.assertEqual(
            {key: cats[key]["cost"] for key, _ in pricing.CATEGORIES},
            {"in": 10.0, "out": 50.0, "cr": 0.25, "cc": 12.5, "cc1h": 20.0})
        self.assertEqual(total, 92.75)
        # The generated accounting panel must expose the newly priced model.
        card = generate_site.cost_method_html(
            {"claude-fable-5-1": tokens}, "test")
        self.assertIn('<td class="mdl fam-claude">fable-5-1</td>', card)
        self.assertIn('Estimated cost by model (test):', card)
        self.assertIn('<ul class="category-help"><li><b>input:</b>', card)

    def test_each_rate_field_has_one_documented_category(self):
        self.assertEqual(len(pricing.CATEGORY_SPECS), 5)
        self.assertEqual(len(pricing.CATEGORIES), 5)
        for rates in pricing.PRICES.values():
            self.assertEqual(len(rates), len(pricing.CATEGORY_SPECS))
        for key, label, help_text in pricing.CATEGORY_SPECS:
            self.assertTrue(key)
            self.assertTrue(label)
            self.assertTrue(help_text)

    def test_model_breakdowns_use_shared_family_capability_order(self):
        model_ids = [
            "gpt-5.6-sol", "claude-opus-5", "gpt-5.3-codex",
            "claude-haiku-4-5", "claude-sonnet-5", "codex-auto-review",
        ]
        ordered = sorted(model_ids, key=generate_site._model_sort_key)
        self.assertEqual(ordered, [
            "claude-haiku-4-5", "claude-sonnet-5", "claude-opus-5",
            "gpt-5.3-codex", "gpt-5.6-sol", "codex-auto-review",
        ])

    def test_unknown_models_are_visible_and_make_estimate_partial(self):
        by_model = {
            "claude-opus-4-8": {"in": 1_000_000, "out": 0, "cr": 0,
                                  "cc": 0, "cc1h": 0},
            "codex-auto-review": {"in": 2_000_000, "out": 3, "cr": 4,
                                  "cc": 0, "cc1h": 0},
        }
        _, shown, label, title = cost_display(by_model)
        self.assertEqual(shown, "$5+")
        self.assertEqual(label, "est. API cost")
        self.assertIn("unpriced: codex-auto-review", title)
        table = _breakdown_table(by_model, "test")
        self.assertIn("codex-auto-review", table)
        self.assertIn("2.0M", table)

    def test_atomic_write_preserves_old_page_if_generation_fails_before_publish(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "index.html")
            _atomic_write_text(path, "old")
            with open(path) as fh:
                self.assertEqual(fh.read(), "old")
            with mock.patch("generate_site.os.replace", side_effect=OSError("stop")):
                with self.assertRaises(OSError):
                    _atomic_write_text(path, "broken")
            with open(path) as fh:
                self.assertEqual(fh.read(), "old")
            self.assertFalse([n for n in os.listdir(tmp) if n.startswith(".render-")])
            _atomic_write_text(path, "new")
            with open(path) as fh:
                self.assertEqual(fh.read(), "new")

    def test_claude_manifest_takes_fuller_parent_and_child_independently(self):
        with tempfile.TemporaryDirectory() as tmp:
            live = os.path.join(tmp, "live", "project")
            archive = os.path.join(tmp, "archive", "project")
            for root in (live, archive):
                os.makedirs(os.path.join(root, "sid", "subagents"))
            paths = {
                os.path.join(live, "sid.jsonl"): "live-parent-long",
                os.path.join(archive, "sid.jsonl"): "old",
                os.path.join(live, "sid", "subagents", "agent-a.jsonl"): "old",
                os.path.join(archive, "sid", "subagents", "agent-a.jsonl"):
                    "archive-child-long",
            }
            for path, content in paths.items():
                with open(path, "w") as fh:
                    fh.write(content)
            top, nested = _claude_manifest([live, archive])
        self.assertEqual(top, [os.path.join(live, "sid.jsonl")])
        self.assertEqual(nested,
                         [os.path.join(archive, "sid", "subagents", "agent-a.jsonl")])

    def test_main_claude_parser_keeps_both_cache_write_ttls(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sid.jsonl")
            records = [
                {"type": "user", "timestamp": "2026-07-01T00:00:00Z",
                 "sessionId": "sid", "cwd": "/repo",
                 "message": {"content": "prompt"}},
                # Some streams expose the aggregate before the TTL detail.
                _assistant("2026-07-01T00:00:01Z", "m1", "claude-sonnet-5", {
                    "input_tokens": 1, "output_tokens": 2,
                    "cache_creation_input_tokens": 12}),
                _assistant("2026-07-01T00:00:02Z", "m1", "claude-sonnet-5", {
                    "input_tokens": 1, "output_tokens": 2,
                    "cache_creation_input_tokens": 12,
                    "cache_creation": {"ephemeral_5m_input_tokens": 5,
                                       "ephemeral_1h_input_tokens": 7}}),
                # A later duplicate can omit detail; it must not reclassify the
                # one-hour tokens back into the standard-write bucket.
                _assistant("2026-07-01T00:00:03Z", "m1", "claude-sonnet-5", {
                    "input_tokens": 1, "output_tokens": 2,
                    "cache_creation_input_tokens": 12}),
            ]
            with open(path, "w") as fh:
                for record in records:
                    fh.write(json.dumps(record) + "\n")
            tl = build_timeline(tmp)
        self.assertEqual(tl["stats"]["tokens_by_model"]["claude-sonnet-5"],
                         {"in": 1, "out": 2, "cr": 0, "cc": 5, "cc1h": 7})

    def test_child_beyond_parent_snapshot_is_deferred(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = os.path.join(tmp, "sid.jsonl")
            child_dir = os.path.join(tmp, "sid", "subagents")
            os.makedirs(child_dir)
            child = os.path.join(child_dir, "agent-a.jsonl")
            with open(parent, "w") as fh:
                fh.write(json.dumps({"type": "user", "timestamp": "2026-07-01T00:00:00Z",
                                     "sessionId": "sid", "cwd": "/repo",
                                     "message": {"content": "first"}}) + "\n")
            with open(child, "w") as fh:
                fh.write(json.dumps({"type": "user",
                                     "timestamp": "2026-07-01T00:01:00Z"}) + "\n")
                fh.write(json.dumps(_assistant("2026-07-01T00:01:01Z", "m1",
                                               "claude-opus-4-8",
                                               {"output_tokens": 9})) + "\n")
            tl = build_timeline(tmp)
        self.assertEqual(tl["stats"]["tokens_out"], 0)
        self.assertFalse(tl["milestones"][0]["activity"]["subagents"])


if __name__ == "__main__":
    unittest.main()
