import copy
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
import warnings

from ccx_parse import _new_milestone, build_timeline
from codex_parse import build_codex_timelines, rollout_paths
from generate_site import _allocate_project_slugs, render


FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "transcripts")


def _entry_ids(page):
    return re.findall(r'<div class="entry [^"]*" id="([^"]+)"', page)


def _session_ids(page):
    return re.findall(r'<div class="sess" id="([^"]+)"', page)


def _fragment_refs(page):
    return re.findall(r'href="#([^"]+)"', page)


def _element_ids(page):
    return re.findall(r' id="([^"]+)"', page)


class TranscriptFixtureTests(unittest.TestCase):
    def test_anchors_ignore_new_earlier_session(self):
        project_dir = os.path.join(
            FIXTURES, "claude", "-home-demo-src-example-project")
        timeline = build_timeline(project_dir)
        original = render(timeline)

        augmented = copy.deepcopy(timeline)
        extra_sid = "earlier-session"
        augmented["sessions"].insert(0, {
            "id": extra_sid,
            "last_ts": "2026-03-14T16:00:00.000Z",
            "title": "Earlier session",
            "tool": "claude",
        })
        augmented["milestones"].insert(0, _new_milestone(
            "prompt", "An earlier conversation", "2026-03-14T16:00:01.000Z",
            extra_sid, "earlier-record"))
        augmented["milestones"][1]["text"] = "The same source record, revised"
        updated = render(augmented)

        self.assertEqual(_entry_ids(original), _entry_ids(updated)[1:])
        self.assertEqual(_session_ids(original), _session_ids(updated)[1:])
        for page in (original, updated):
            self.assertTrue(set(_fragment_refs(page)) <= set(_element_ids(page)))

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
        self.assertEqual(
            [response["text"] for response in timeline["milestones"][0]["activity"]["responses"]],
            [
                "The project page can reuse a bounded cache and invalidate it after writes.",
                "The bounded cache now invalidates after writes, with a regression test for stale entries.",
            ],
        )
        page = render(timeline)
        self.assertIn("response excerpts", page)
        self.assertIn("The bounded cache now invalidates after writes", page)
        # Generated pages embed the SVG so each output file remains standalone.
        self.assertIn(
            '<link rel="icon" type="image/svg+xml" '
            'href="data:image/svg+xml,%3Csvg', page)

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
        self.assertEqual(
            [response["text"] for response in timeline["milestones"][0]["activity"]["responses"]],
            [
                "I will run the focused cache test before changing the invalidation path.",
                "The failing case isolates stale entries after a write.",
                "The regression test now covers stale entries after a write.",
            ],
        )
        page = render(timeline)
        self.assertIn("3 response excerpts", page)
        self.assertIn(
            '<summary>3 response excerpts · 1 file · 2 tool calls</summary>', page)
        self.assertIn(
            'class="response-heading">response excerpts', page)
        self.assertIn('class="response-meta">response 1', page)
        self.assertIn('class="tools-label">tools used:</span>', page)
        self.assertIn("The failing case isolates stale entries after a write", page)
        self.assertIn(
            'title="Assistant turns attributed to this model; not sessions"',
            page)
        # The fixture has one Shell call, so both aggregate and per-turn tool
        # readouts exercise the name-before-count × syntax and shared color.
        self.assertIn('<b class="model-turns">&times;3</b> turns</span>', page)
        self.assertIn('<span class="chip">Shell <b class="tool-count">&times;1</b></span>', page)
        self.assertIn(
            '<span><span class="tn">Shell</span> '
            '<span class="tool-count">&times;1</span></span>', page)
        self.assertNotIn("&#x2387;", page)

    def test_bash_wrappers_render_as_one_terminal_line(self):
        records = [
            {
                "type": "user", "timestamp": "2026-03-15T18:00:00.000Z",
                "cwd": "/home/demo/src/example-project",
                "message": {"role": "user", "content": "<bash-input>git push</bash-input>"},
            },
            {
                "type": "user", "timestamp": "2026-03-15T18:00:01.000Z",
                "cwd": "/home/demo/src/example-project",
                "message": {"role": "user", "content":
                            "<bash-stdout>To https://example.com/example-project.git\n"
                            "   d7f1f1c..19cf64e  master -&gt; master</bash-stdout>"
                            "<bash-stderr></bash-stderr>"},
            },
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "bash-wrapper.jsonl")
            with open(path, "w", encoding="utf-8") as fh:
                for record in records:
                    fh.write(json.dumps(record) + "\n")
            timeline = build_timeline(tmp, session_paths=[path])

        self.assertEqual(timeline["stats"]["commands"], 1)
        self.assertEqual(timeline["stats"]["prompts"], 0)
        self.assertEqual(len(timeline["milestones"]), 1)
        self.assertEqual(
            timeline["milestones"][0]["text"],
            "git push | To https://example.com/example-project.git d7f1f1c..19cf64e master -> master",
        )
        page = render(timeline)
        self.assertIn('class="ask terminal"', page)
        self.assertNotIn("<bash-input>", page)
        self.assertIn("git push", page)
        self.assertIn("master -&gt; master", page)

    def test_parser_outputs_share_the_documented_timeline_schema(self):
        claude_dir = os.path.join(
            FIXTURES, "claude", "-home-demo-src-example-project")
        claude = build_timeline(claude_dir)
        codex = build_codex_timelines(
            rollout_paths(os.path.join(FIXTURES, "codex")))[0]

        self.assertEqual(set(claude), set(codex))
        self.assertEqual(
            set(claude["milestones"][0]), set(codex["milestones"][0]))
        self.assertTrue(all(m["source_id"] for m in claude["milestones"]))
        self.assertTrue(all(m["source_id"] for m in codex["milestones"]))
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
