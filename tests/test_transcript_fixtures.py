import json
import os
import subprocess
import sys
import tempfile
import unittest
import warnings

from ccx_parse import build_timeline
from codex_parse import build_codex_timelines, rollout_paths
from generate_site import _allocate_project_slugs, render


FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "transcripts")


class TranscriptFixtureTests(unittest.TestCase):
    def test_screenshot_site_is_built_only_from_synthetic_fixtures(self):
        with tempfile.TemporaryDirectory() as tmp:
            site = os.path.join(tmp, "site")
            builder = os.path.join(
                os.path.dirname(os.path.dirname(__file__)),
                "scripts",
                "build_screenshot_site.py",
            )

            completed = subprocess.run(
                [sys.executable, builder, "--out", site],
                check=True,
                capture_output=True,
                text=True,
            )
            # Screenshot navigation must follow the stable slug emitted for
            # the fixture path rather than assuming the bare project name.
            fixture_path = "/home/demo/src/example-project"
            fixture_slug = _allocate_project_slugs([fixture_path])[fixture_path]
            self.assertTrue(os.path.isfile(
                os.path.join(site, fixture_slug, "index.html")))
            # Generated pages contain transcript data. Owner-only directory
            # mode prevents another local account from enumerating project
            # slugs even though each HTML file is already mode 0600.
            self.assertEqual(os.stat(site).st_mode & 0o777, 0o700)
            self.assertEqual(
                os.stat(os.path.join(site, fixture_slug)).st_mode & 0o777,
                0o700,
            )
            # The Claude-only fixture has one prompt and one milestone in one
            # session, so each noun must use its singular form in CLI output.
            self.assertIn(
                "1 prompt + 0 commands · 1 milestone · 1 session",
                completed.stdout,
            )

            with open(os.path.join(site, "index.html"), encoding="utf-8") as fh:
                index = fh.read()
            for fixture_name in ("example-project", "docs-site"):
                fixture_project_path = f"/home/demo/src/{fixture_name}"
                fixture_project_slug = _allocate_project_slugs(
                    [fixture_project_path])[fixture_project_path]
                self.assertIn(
                    f'<a class="proj" href="{fixture_project_slug}/index.html">',
                    index,
                )
            # The Claude-only docs-site card has one input and one changed file.
            self.assertIn('<b>1</b> input</span>', index)
            self.assertIn('<b>1</b> file</span>', index)

            rendered_pages = []
            for root, _, files in os.walk(site):
                for filename in files:
                    if not filename.endswith(".html"):
                        continue
                    with open(
                            os.path.join(root, filename), encoding="utf-8") as fh:
                        rendered_pages.append(fh.read())
            rendered = "\n".join(rendered_pages)
            # The synthetic home must replace the machine's actual home in the
            # source label and every transcript-derived project page.
            self.assertIn("/home/demo/", rendered)
            self.assertNotIn(os.path.expanduser("~"), rendered)
            # The invented example models a short agentic change: 38k Claude
            # output tokens plus 20k Codex output tokens. At the committed list
            # prices its total rounds to $3, which exercises nontrivial metrics
            # without copying a real transcript.
            self.assertIn("58.0k", rendered)
            self.assertIn("~<b>$3</b>", rendered)

    def test_claude_fixture_statistics(self):
        # The example-project fixture contains one prompt and one slash command
        # so both human-input classifications run through the real JSONL parser.
        project_dir = os.path.join(
            FIXTURES, "claude", "-home-demo-src-example-project")

        timeline = build_timeline(project_dir)

        self.assertEqual(timeline["project_path"], "/home/demo/src/example-project")
        self.assertEqual(timeline["stats"]["prompts"], 1)
        self.assertEqual(timeline["stats"]["commands"], 1)
        self.assertEqual(timeline["stats"]["sessions"], 1)
        # Eight turns and 38k output tokens represent two ordinary agentic
        # inputs with several inspect/edit/test cycles.
        self.assertEqual(timeline["stats"]["assistant_turns"], 8)
        self.assertEqual(timeline["stats"]["tokens_out"], 38_000)
        self.assertEqual(timeline["diagnostics"], [])
        # The implementation and its regression test are the two synthetic
        # files changed during the first prompt.
        self.assertEqual(
            timeline["stats"]["files_changed"],
            [
                "/home/demo/src/example-project/cache.py",
                "/home/demo/src/example-project/tests/test_cache.py",
            ],
        )

    def test_codex_fixture_statistics(self):
        # The single Codex rollout contributes one prompt, two tool calls, and
        # one changed file without relying on a developer's local sessions.
        paths = rollout_paths(os.path.join(FIXTURES, "codex"))

        timelines = build_codex_timelines(paths)

        self.assertEqual(len(timelines), 1)
        timeline = timelines[0]
        self.assertEqual(timeline["project_path"], "/home/demo/src/example-project")
        self.assertEqual(timeline["stats"]["prompts"], 1)
        self.assertEqual(timeline["stats"]["tool_calls"], 2)
        # Three turns and 20k output tokens model a compact inspect/patch/check
        # Codex session while keeping the two existing tool classifications.
        self.assertEqual(timeline["stats"]["assistant_turns"], 3)
        self.assertEqual(timeline["stats"]["tokens_out"], 20_000)
        self.assertEqual(timeline["diagnostics"], [])
        self.assertEqual(
            timeline["stats"]["files_changed"],
            ["/home/demo/src/example-project/tests/test_cache.py"],
        )

    def test_parser_outputs_share_the_documented_timeline_schema(self):
        claude_dir = os.path.join(
            FIXTURES, "claude", "-home-demo-src-example-project")
        claude = build_timeline(claude_dir)
        codex = build_codex_timelines(
            rollout_paths(os.path.join(FIXTURES, "codex")))[0]

        self.assertEqual(set(claude), set(codex))
        self.assertEqual(
            set(claude["milestones"][0]), set(codex["milestones"][0]))
        self.assertEqual(
            set(claude["milestones"][0]["activity"]),
            set(codex["milestones"][0]["activity"]),
        )
        self.assertLessEqual(
            set(claude["sessions"][0]), set(codex["sessions"][0]))

    def test_malformed_record_warns_and_marks_the_render_partial(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "44444444-4444-4444-8444-444444444444.jsonl")
            # One valid prompt keeps the timeline renderable; the following
            # malformed line verifies that a partial parse is not silent.
            prompt = {
                "type": "user",
                "timestamp": "2026-03-15T18:00:00.000Z",
                "cwd": "/home/demo/src/malformed-example",
                "message": {"role": "user", "content": "Keep the valid record."},
            }
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(prompt) + "\n")
                fh.write('{"type":"assistant"\n')

            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                timeline = build_timeline(tmp)

        self.assertEqual(len(caught), 1)
        self.assertIn("skipped malformed JSON", str(caught[0].message))
        self.assertEqual(len(timeline["diagnostics"]), 1)
        self.assertIn("<b>1</b> skipped transcript record", render(timeline))
