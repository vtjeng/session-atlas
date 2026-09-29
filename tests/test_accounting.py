import json
import os
import re
import tempfile
import unittest
from unittest import mock

import generate_site
import pricing
from ccx_parse import _subagent_usage, build_timeline
from generate_site import (_allocate_project_slugs, _atomic_write_text,
                           _breakdown_table, _project_output_dir, cost_display)


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
        # Each TTL bucket is billed at its own field of the rate tuple.
        _, _, _, cache_write, cache_write_1h = pricing.PRICES["claude-opus-4-8"]
        self.assertAlmostEqual(cats["cc"]["cost"], 15 * cache_write / 1_000_000)
        self.assertAlmostEqual(cats["cc1h"]["cost"], 30 * cache_write_1h / 1_000_000)
        self.assertAlmostEqual(total, sum(c["cost"] for c in cats.values()))

    def test_cost_is_tokens_times_rate_and_none_bills_nothing(self):
        # A made-up rate keeps the arithmetic independent of vendor prices:
        # $2 input, $8 output, $0.50 cache read, and no cache-write prices.
        rates = {"test-model": (2.0, 8.0, 0.5, None, None)}
        # Round token counts give exact costs: 1M input is $2, 250k output
        # $2, 2M cache reads $1; the cache writes have no rate.
        tokens = {"in": 1_000_000, "out": 250_000, "cr": 2_000_000,
                  "cc": 300, "cc1h": 400}
        with mock.patch.dict(pricing.PRICES, rates):
            cats, total, unpriced = pricing.cost_breakdown({"test-model": tokens})
        self.assertEqual(unpriced, [])
        self.assertEqual({k: c["cost"] for k, c in cats.items()},
                         {"in": 2.0, "out": 2.0, "cr": 1.0, "cc": 0.0, "cc1h": 0.0})
        # An unbilled category still reports its tokens.
        self.assertEqual({k: c["tokens"] for k, c in cats.items()}, tokens)
        self.assertEqual(total, 5.0)

    def test_every_rate_is_ordered_like_a_real_price(self):
        # On both vendors' pages, output costs at least as much as input and a
        # cache read less than input. A row that breaks either rule most likely
        # has swapped or mistyped columns; if a vendor really prices a model
        # that way, add it here as an exception.
        for model, (pin, pout, cr, _, _) in pricing.PRICES.items():
            with self.subTest(model=model):
                self.assertGreaterEqual(pout, pin)
                if cr is not None:
                    self.assertLess(cr, pin)

    def test_claude_aliases_and_dated_ids_share_rates(self):
        # Anthropic documents both IDs for these models, and a transcript can
        # record either, so each pair must carry the same rates.
        pairs = [
            ("claude-opus-4-5", "claude-opus-4-5-20251101"),
            ("claude-opus-4-1", "claude-opus-4-1-20250805"),
            ("claude-opus-4-0", "claude-opus-4-20250514"),
            ("claude-sonnet-4-5", "claude-sonnet-4-5-20250929"),
            ("claude-sonnet-4-0", "claude-sonnet-4-20250514"),
            ("claude-haiku-4-5", "claude-haiku-4-5-20251001"),
        ]
        for alias, dated in pairs:
            with self.subTest(alias=alias):
                self.assertEqual(pricing.PRICES[alias], pricing.PRICES[dated])

    def test_rates_table_lists_only_models_with_token_use(self):
        # opus-5 and gpt-5.5-pro carry tokens; sonnet-5 appears with all-zero
        # counts; codex-auto-review has tokens but no rate. Only the first two
        # get a rates row, in PRICES order, so the table stays short even
        # though PRICES lists every published model.
        used = {key: 1_000 for key, _ in pricing.CATEGORIES}
        by_model = {
            "gpt-5.5-pro": used,
            "claude-opus-5": used,
            "claude-sonnet-5": {key: 0 for key, _ in pricing.CATEGORIES},
            "codex-auto-review": used,
        }
        card = generate_site.cost_method_html(by_model, "test")
        rates = card.split("Rates used (per 1M tokens):", 1)[1]
        rows = re.findall(r'<td class="mdl fam-\w+">([^<]+)</td>', rates)
        self.assertEqual(rows, ["opus-5", "gpt-5.5-pro"])
        # gpt-5.5-pro has no cached-input price, so its cache-read cell shows
        # the same dash as an unbilled cache-write category.
        self.assertIsNone(pricing.PRICES["gpt-5.5-pro"][2])
        self.assertRegex(rates, r'<td class="mdl fam-gpt">gpt-5.5-pro</td>'
                                r'<td>\$[\d.,]+</td><td>\$[\d.,]+</td><td>&mdash;</td>')

    def test_rates_table_is_omitted_when_no_model_is_priced(self):
        # A page whose only model has no rate has nothing to list, so the
        # rates heading goes too; the excluded-model note still explains why.
        card = generate_site.cost_method_html(
            {"codex-auto-review": {"in": 5}}, "test")
        self.assertNotIn("Rates used", card)
        self.assertIn("Excluded (no rate): codex-auto-review.", card)

    def test_each_rate_field_has_one_documented_category(self):
        self.assertEqual(len(pricing.CATEGORY_SPECS), 5)
        self.assertEqual(len(pricing.CATEGORIES), 5)
        for rates in pricing.PRICES.values():
            self.assertEqual(len(rates), len(pricing.CATEGORY_SPECS))
        card = generate_site.cost_method_html({}, "test")
        for key, label, help_text in pricing.CATEGORY_SPECS:
            self.assertTrue(key)
            self.assertTrue(label)
            self.assertTrue(help_text)
            # The panel's category list is generated from these specs.
            self.assertIn(f"<li><b>{generate_site.esc(label)}:</b> "
                          f"{generate_site.esc(help_text)}</li>", card)

    def test_model_breakdowns_follow_the_rates_table_order(self):
        # The cost and token tables list models as the rates table does:
        # families together, newest first, and a model with no rate last.
        model_ids = [
            "gpt-5.6-sol", "claude-opus-5", "gpt-5.3-codex",
            "claude-haiku-4-5", "claude-sonnet-5", "codex-auto-review",
            "claude-fable-5", "claude-fable-5-1",
        ]
        ordered = sorted(model_ids, key=generate_site._model_sort_key)
        self.assertEqual(ordered, [
            "claude-fable-5-1", "claude-fable-5", "claude-opus-5",
            "claude-sonnet-5", "claude-haiku-4-5",
            "gpt-5.6-sol", "gpt-5.3-codex", "codex-auto-review",
        ])
        self.assertEqual(ordered[:5], [
            m for m in pricing.PRICES if m in ordered][:5])

    def test_panel_tables_share_one_column_grid(self):
        # Every table sets its width to its columns' sum, so fixed layout
        # applies and a category column is the same width in each: a
        # seven-column matrix is 120 + 6 * 90 = 660px, the six-column rates
        # table 570px.
        seven = generate_site._grid_open(["model", "a", "b", "c", "d", "e", "total"])
        six = generate_site._grid_open(["model", "a", "b", "c", "d", "e"])
        self.assertIn('<table class="grid" style="width:660px">', seven)
        self.assertIn('<table class="grid" style="width:570px">', six)

    def test_unknown_models_are_visible_and_make_estimate_partial(self):
        by_model = {
            # One million input tokens at a made-up $5 rate is a $5 estimate.
            "test-priced": {"in": 1_000_000, "out": 0, "cr": 0,
                            "cc": 0, "cc1h": 0},
            "codex-auto-review": {"in": 2_000_000, "out": 3, "cr": 4,
                                  "cc": 0, "cc1h": 0},
        }
        with mock.patch.dict(pricing.PRICES,
                             {"test-priced": (5.0, 0.0, 0.0, 0.0, None)}):
            _, shown, label, title = cost_display(by_model)
            table = _breakdown_table(by_model, "test")
        # "+" marks the estimate as partial, because one model has no rate.
        self.assertEqual(shown, "$5+")
        self.assertEqual(label, "est. API cost")
        self.assertIn("unpriced: codex-auto-review", title)
        self.assertIn("Estimated cost by model (test):", table)
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
