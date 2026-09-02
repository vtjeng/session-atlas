import copy
from collections import Counter
import json
import os
import re
import sqlite3
import tempfile
import unittest

from ccx_parse import (_has_substantive_activity, _new_activity, _new_milestone,
                       _timeline_dict)
from codex_parse import (_parse_rollout, build_codex_timelines,
                         build_history_only_timelines)
from generate_site import (MINIMAP_MAX_ENTRIES, RECOVERED_PROMPT_EXPLANATION,
                           _group_codex_timelines, _merge_timelines, render,
                           render_index)


def _record(ts, record_type, payload):
    return {"timestamp": ts, "type": record_type, "payload": payload}


def _token_count(ts, total_input, cached_input, output):
    return _record(ts, "event_msg", {
        "type": "token_count",
        "info": {"total_token_usage": {
            "input_tokens": total_input,
            "cached_input_tokens": cached_input,
            "output_tokens": output,
        }},
    })


def _entry_ids(page):
    return re.findall(r'<div class="entry [^"]*" id="([^"]+)"', page)


def _session_ids(page):
    return re.findall(r'<div class="sess" id="([^"]+)"', page)


def _fragment_refs(page):
    return re.findall(r'href="#([^"]+)"', page)


def _element_ids(page):
    return re.findall(r' id="([^"]+)"', page)


class CodexTokenParsingTests(unittest.TestCase):
    def parse(self, records):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(
                tmp, "rollout-2026-07-20T00-00-00-00000000-0000-0000-0000-000000000001.jsonl")
            with open(path, "w") as fh:
                for record in records:
                    fh.write(json.dumps(record) + "\n")
            parsed = _parse_rollout(path)
        self.assertIsNotNone(parsed)
        return parsed

    def timeline(self, records):
        cwd, session, milestones, branches, diagnostics = self.parse(records)
        return _timeline_dict(
            "/tmp/codex", cwd, [session], milestones, branches,
            diagnostics=diagnostics)

    def test_anchors_ignore_new_earlier_codex_session(self):
        records = [
            _record("2026-07-20T00:00:00.000Z", "session_meta", {
                "id": "root", "cwd": "/repo",
                "timestamp": "2026-07-20T00:00:00.000Z"}),
            _record("2026-07-20T00:00:01.000Z", "event_msg", {
                "type": "user_message", "message": "review it"}),
            _record("2026-07-20T00:00:02.000Z", "response_item", {
                "type": "message", "role": "assistant",
                "content": [{"text": "done"}]}),
        ]
        timeline = self.timeline(records)
        original = render(timeline)

        augmented = copy.deepcopy(timeline)
        extra_sid = "earlier"
        augmented["sessions"].insert(0, {
            "id": extra_sid,
            "last_ts": "2026-07-19T00:00:00.000Z",
            "title": "Earlier session",
            "tool": "codex",
        })
        augmented["milestones"].insert(0, _new_milestone(
            "prompt", "An earlier conversation", "2026-07-19T00:00:01.000Z",
            extra_sid, "earlier-record"))
        updated = render(augmented)

        self.assertEqual(_entry_ids(original), _entry_ids(updated)[1:])
        self.assertEqual(_session_ids(original), _session_ids(updated)[1:])
        for page in (original, updated):
            self.assertTrue(set(_fragment_refs(page)) <= set(_element_ids(page)))

    def test_minimap_uses_explicit_session_indices(self):
        records = [
            _record("2026-07-20T00:00:00.000Z", "session_meta", {
                "id": "root", "cwd": "/repo",
                "timestamp": "2026-07-20T00:00:00.000Z"}),
            _record("2026-07-20T00:00:01.000Z", "event_msg", {
                "type": "user_message", "message": "review it"}),
        ]
        timeline = self.timeline(records)
        timeline["sessions"].append({
            "id": "second",
            "last_ts": "2026-07-21T00:00:00.000Z",
            "title": "Second session",
            "tool": "codex",
        })
        timeline["milestones"].append(_new_milestone(
            "prompt", "A later conversation", "2026-07-21T00:00:01.000Z",
            "second", "later-record"))

        page = render(timeline)

        self.assertIn('data-session-index="1"', page)
        self.assertIn('data-session-index="2"', page)
        self.assertIn("Number(s.dataset.sessionIndex)", page)
        self.assertIn("Number(e.dataset.sessionIndex)", page)
        self.assertNotIn("parseInt(s.id.slice(1),10)", page)
        self.assertNotIn("parseInt(e.id.slice(1),10)", page)

    def test_cumulative_usage_deduplicates_snapshots_and_splits_cached_input(self):
        records = [
            _record("2026-07-20T00:00:00.000Z", "session_meta", {
                "id": "root", "cwd": "/repo", "timestamp": "2026-07-20T00:00:00.000Z"}),
            _record("2026-07-20T00:00:01.000Z", "turn_context", {"model": "gpt-5.5"}),
            _record("2026-07-20T00:00:02.000Z", "event_msg", {
                "type": "user_message", "message": "run it"}),
            # input_tokens includes the 70 cached tokens, leaving 30 fresh.
            _token_count("2026-07-20T00:00:03.000Z", 100, 70, 10),
            # A rate-limit-only event must not reset this nonzero baseline.
            _record("2026-07-20T00:00:04.000Z", "event_msg", {
                "type": "token_count", "info": None}),
            # The next snapshot adds 20 cached + 10 fresh input and 2 output.
            _token_count("2026-07-20T00:00:05.000Z", 130, 90, 12),
            # Rate-limit refreshes can repeat an unchanged cumulative snapshot.
            _token_count("2026-07-20T00:00:06.000Z", 130, 90, 12),
            _record("2026-07-20T00:00:07.000Z", "response_item", {
                "type": "message", "role": "assistant", "content": [{"text": "done"}]}),
        ]

        _, _, milestones, _, _ = self.parse(records)
        activity = milestones[0]["activity"]
        self.assertEqual(
            activity["tokens_by_model"]["gpt-5.5"],
            {"in": 40, "out": 12, "cr": 90, "cc": 0, "cc1h": 0},
        )

    def test_session_preserves_codex_exec_originator(self):
        # This synthetic remote verifies that session metadata is preserved.
        repository = "https://example.com/example-repository.git"
        records = [
            _record("2026-07-20T00:00:00.000Z", "session_meta", {
                "id": "exec", "cwd": "/repo", "timestamp": "2026-07-20T00:00:00.000Z",
                "originator": "codex_exec",
                "git": {"repository_url": repository}}),
            _record("2026-07-20T00:00:01.000Z", "event_msg", {
                "type": "user_message", "message": "review the repository"}),
        ]

        _, session, _, _, _ = self.parse(records)
        self.assertEqual(session["originator"], "codex_exec")
        self.assertEqual(session["repository_url"], repository)

    def test_current_user_messages_count_without_counting_context_records(self):
        # These ordered timestamps and the root ID exercise one Codex turn
        # boundary without depending on a wall-clock date.
        records = [
            _record("2026-07-20T00:00:00.000Z", "session_meta", {
                "id": "root", "cwd": "/repo", "timestamp": "2026-07-20T00:00:00.000Z"}),
            # Codex stores injected instructions as a user-role message, but its
            # content-item kind identifies them as context rather than input.
            _record("2026-07-20T00:00:01.000Z", "response_item", {
                "type": "message", "role": "user",
                "internal_chat_message_metadata_passthrough": {
                    "content_item_kinds": ["agents_md.instructions"]},
                "content": [{"type": "input_text", "text": "injected instructions"}]}),
            _record("2026-07-20T00:00:02.000Z", "response_item", {
                "type": "message", "role": "user",
                "internal_chat_message_metadata_passthrough": {
                    "content_item_kinds": ["user.text"]},
                # This text is the only input that should become a prompt.
                "content": [{"type": "input_text", "text": "human prompt"}]}),
            _record("2026-07-20T00:00:03.000Z", "response_item", {
                "type": "message", "role": "assistant",
                "content": [{"text": "done"}]}),
        ]

        _, _, milestones, _, _ = self.parse(records)
        # The injected context is excluded, leaving one human prompt.
        self.assertEqual([m["kind"] for m in milestones], ["prompt"])
        self.assertEqual(milestones[0]["text"], "human prompt")

    def test_stats_count_conversations_without_automated_codex_sessions(self):
        # The fixed timestamps establish two distinct rollouts in one project.
        records = [
            _record("2026-07-20T00:00:00.000Z", "session_meta", {
                "id": "root", "cwd": "/repo", "timestamp": "2026-07-20T00:00:00.000Z"}),
            _record("2026-07-20T00:00:01.000Z", "event_msg", {
                "type": "user_message", "message": "human prompt"}),
            _record("2026-07-20T00:01:00.000Z", "session_meta", {
                "id": "child", "cwd": "/repo", "timestamp": "2026-07-20T00:01:00.000Z",
                "thread_source": "subagent",
                "source": {"subagent": {"thread_spawn": {
                    "agent_path": None,
                    "parent_thread_id": "root"}}},
                "agent_path": "/my/subagent/name",
            }),
            _record("2026-07-20T00:01:01.000Z", "response_item", {
                "type": "function_call", "name": "exec_command",
                # One tool call keeps the child rollout substantive.
                "arguments": json.dumps({"cmd": "true"})}),
        ]

        # This fixture contains one human conversation and one child rollout.
        with tempfile.TemporaryDirectory() as tmp:
            paths = []
            for name, subset in (("root", records[:2]), ("child", records[2:])):
                path = os.path.join(tmp, f"rollout-2026-07-20T00-00-00-{name}.jsonl")
                with open(path, "w") as fh:
                    for record in subset:
                        fh.write(json.dumps(record) + "\n")
                paths.append(path)
            timeline = build_codex_timelines(paths)[0]

        # The child rollout is attached to its parent instead of becoming a session.
        self.assertEqual(timeline["stats"]["sessions"], 1)
        self.assertEqual(timeline["stats"]["automated_sessions"], 0)
        self.assertEqual(len(timeline["sessions"]), 1)
        self.assertEqual([m["kind"] for m in timeline["milestones"]],
                         ["prompt", "subagent"])
        self.assertEqual(
            timeline["milestones"][1]["text"],
            "triggered /my/subagent/name subagent")
        self.assertEqual(
            timeline["milestones"][1]["activity"]["tools"], {"Shell": 1})
        page = render(timeline)
        self.assertIn("triggered /my/subagent/name subagent", page)
        self.assertNotIn('<section class="session-block" data-automated>', page)

    def test_exec_workdirs_group_under_project_and_automated_sessions_are_visible(self):
        # One synthetic remote links the interactive checkout and temporary clone.
        repository = "https://example.com/example-project.git"

        def timeline(session_id, path, originator, is_subagent=False, label=None):
            activity = _new_activity()
            # One million input tokens makes each session's $5 contribution easy to verify.
            token_count = 1_000_000
            activity["tokens_in"] = token_count
            activity["tokens_by_model"]["gpt-5.5"] = {
                "in": token_count, "out": 0, "cr": 0, "cc": 0, "cc1h": 0}
            session = {"id": session_id, "last_ts": "2026-07-20T00:00:01.000Z",
                       "title": None, "tool": "codex", "originator": originator,
                       "repository_url": repository, "is_subagent": is_subagent,
                       "subagent_label": label,
                       "parent_session_id": "tui" if is_subagent else None,
                       "parent_relation": "spawned by" if is_subagent else None}
            milestone = {"kind": "session" if is_subagent else "prompt",
                         "text": None if is_subagent else "work",
                         "ts": "2026-07-20T00:00:00.000Z", "session": session_id,
                         "activity": activity}
            return _timeline_dict(
                "/tmp/codex", path, [session], [milestone], Counter())

        # Matching the remote basename makes this checkout the canonical path.
        project_path = "/home/user/example-project"
        tui_timeline = timeline("tui", project_path, "codex-tui")
        subagent_timeline = timeline(
            "subagent", "/tmp/example-project-child", "codex-tui", is_subagent=True,
            label="/root/reviewer")
        # A different cwd verifies regrouping by remote rather than by path.
        exec_timeline = timeline("exec", "/tmp/example-project-audit", "codex_exec")

        grouped = _group_codex_timelines(
            [tui_timeline, subagent_timeline, exec_timeline])
        self.assertEqual(list(grouped), [project_path])
        merged = _merge_timelines(grouped[project_path])
        page = render(merged)

        self.assertEqual(page.count('<section class="session-block" data-automated>'), 1)
        self.assertEqual(page.count('<section class="session-block"'), 2)
        self.assertIn('>codex exec</span>', page)
        self.assertIn('triggered /root/reviewer subagent', page)
        self.assertEqual(merged["stats"]["sessions"], 1)
        self.assertEqual(merged["stats"]["automated_sessions"], 1)
        subagent_entry = next(m for m in merged["milestones"]
                              if m["kind"] == "subagent")
        self.assertEqual(subagent_entry["activity"]["tokens_in"], 1_000_000)
        # The badges should explain why these sections are not conversations.
        self.assertIn(
            'title="Non-interactive Codex task; not a separate human conversation"',
            page)
        self.assertNotIn('Show automated Codex work', page)
        self.assertNotIn('automatedToggle', page)
        self.assertNotIn('show-automated', page)
        # The session total appears in the summary cards, not again beside the
        # refresh timestamp. The sticky navigator still tracks visible sessions.
        self.assertNotIn('id="heroSessionCount"', page)
        self.assertNotIn('id="heroSessionLabel"', page)
        self.assertIn(
            '<span class="sesscount">session <b id="sessCur">1</b> /', page)
        # All three $5 sessions remain in the project summary while two are hidden.
        self.assertIn('<div class="n">$15</div>', page)

    def test_subagent_only_timeline_is_not_a_canonical_checkout(self):
        repository = "https://example.com/example-project.git"

        def timeline(session_id, path, originator, is_subagent):
            session = {
                "id": session_id,
                "last_ts": "2026-07-20T00:00:01.000Z",
                "title": None,
                "tool": "codex",
                "originator": originator,
                "repository_url": repository,
                "is_subagent": is_subagent,
                "subagent_label": None,
                "parent_session_id": None,
                "parent_relation": None,
            }
            milestone = {
                "kind": "session",
                "text": None,
                "ts": "2026-07-20T00:00:00.000Z",
                "session": session_id,
                "activity": _new_activity(),
            }
            milestone["activity"]["assistant_turns"] = 1
            return _timeline_dict(
                "/tmp/codex", path, [session], [milestone], Counter())

        subagent = timeline("subagent", "/tmp/child", "codex-tui", True)
        exec_run = timeline("exec", "/tmp/exec", "codex_exec", False)

        grouped = _group_codex_timelines([subagent, exec_run])

        self.assertEqual(set(grouped), {"/tmp/child", "/tmp/exec"})

    def test_history_only_prompts_render_as_recovered_prompt_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            history_path = os.path.join(tmp, "history.jsonl")
            logs_path = os.path.join(tmp, "logs.sqlite")
            # The known root prompt must stay excluded. The missing session has
            # two human prompts around one slash command, which must be skipped.
            history = [
                {"session_id": "root", "ts": 1784505600,
                 "text": "ordinary persisted prompt"},
                {"session_id": "missing", "ts": 1784505601,
                 "text": "compare the two approaches"},
                {"session_id": "missing", "ts": 1784505602,
                 "text": "/status"},
                {"session_id": "missing", "ts": 1784505603,
                 "text": "which one would you choose?"},
            ]
            with open(history_path, "w") as fh:
                for item in history:
                    fh.write(json.dumps(item) + "\n")

            db = sqlite3.connect(logs_path)
            db.execute(
                "CREATE TABLE logs (id INTEGER PRIMARY KEY, ts INTEGER, "
                "ts_nanos INTEGER, feedback_log_body TEXT, thread_id TEXT)")
            db.execute(
                "INSERT INTO logs VALUES (?, ?, ?, ?, ?)",
                (1, 1784505601, 0,
                 'legacy_fallback_cwd: AbsolutePathBuf("/repo")', "missing"))
            db.commit()
            db.close()

            timelines = build_history_only_timelines(
                {"root"}, history_path=history_path, logs_path=logs_path)

        self.assertEqual(len(timelines), 1)
        timeline = timelines[0]
        self.assertEqual(timeline["project_path"], "/repo")
        self.assertTrue(timeline["sessions"][0]["is_history_only"])
        self.assertEqual(
            timeline["sessions"][0]["originator"], "codex_history_only")
        self.assertEqual(timeline["stats"]["recovered_prompts"], 2)
        self.assertEqual(timeline["stats"]["prompts"], 0)
        self.assertEqual([m["kind"] for m in timeline["milestones"]],
                         ["recovered", "recovered"])

        page = render(timeline)
        index = render_index([("repo", timeline)], source_label="synthetic")
        self.assertIn('>recovered</span>', page)
        self.assertIn('compare the two approaches', page)
        self.assertIn('which one would you choose?', page)
        self.assertNotIn('/status', page)
        # Explain the common /btw source without presenting it as a certainty.
        self.assertIn("typically associated with `/btw`, but not always", page)
        self.assertEqual(page.count(RECOVERED_PROMPT_EXPLANATION), 3)
        self.assertIn(
            '<footer><span title="Prompts, commands, and recovered prompts '
            'counted as inputs.">2 inputs</span> &middot;', page)
        self.assertIn('<div class="n">1</div><div class="l lbl">session</div>', page)
        self.assertIn('<div class="n">2</div><div class="l lbl">inputs</div>', page)
        self.assertIn('<div class="l lbl">day active</div>', page)
        self.assertNotIn('<div class="l lbl">prompts</div>', page)
        self.assertNotIn('<div class="l lbl">assistant turns</div>', page)
        self.assertNotIn('<div class="l lbl">tool calls</div>', page)
        self.assertNotIn('<div class="l lbl">files changed</div>', page)
        self.assertIn('<div class="n">2</div><div class="l lbl">inputs</div>', index)
        self.assertIn('title="Prompts, commands, and recovered prompts counted as inputs."', index)
        self.assertIn('<div class="l lbl">day active</div>', index)
        self.assertNotIn('<div class="l lbl">days spanned</div>', index)
        self.assertIn('<b>2</b> inputs', index)
        shared_labels = [
            '<div class="l lbl">session</div>',
            '<div class="l lbl">inputs</div>',
            '<div class="l lbl">day active</div>',
            '<div class="l lbl">est. cost</div>',
        ]
        for summary in (page, index):
            positions = [summary.find(label) for label in shared_labels]
            self.assertEqual(positions, sorted(positions))
        overall_labels = shared_labels + ['<div class="l lbl">project</div>']
        positions = [index.find(label) for label in overall_labels]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(page.count(
            '<section class="session-block" data-automated>'), 0)

    def test_model_switch_attributes_each_cumulative_delta_to_the_active_model(self):
        records = [
            _record("2026-07-20T00:00:00.000Z", "session_meta", {
                "id": "root", "cwd": "/repo", "timestamp": "2026-07-20T00:00:00.000Z"}),
            _record("2026-07-20T00:00:01.000Z", "turn_context", {"model": "gpt-5.5"}),
            _record("2026-07-20T00:00:02.000Z", "event_msg", {
                "type": "user_message", "message": "first model"}),
            # GPT-5.5 contributes 70 cached + 30 fresh input tokens.
            _token_count("2026-07-20T00:00:03.000Z", 100, 70, 10),
            _record("2026-07-20T00:00:04.000Z", "turn_context", {"model": "gpt-5.4"}),
            # The global counter rises by 60 input, including 30 cached, plus 10 output.
            _token_count("2026-07-20T00:00:05.000Z", 160, 100, 20),
            _record("2026-07-20T00:00:06.000Z", "response_item", {
                "type": "message", "role": "assistant", "content": [{"text": "done"}]}),
        ]

        _, _, milestones, _, _ = self.parse(records)
        by_model = milestones[0]["activity"]["tokens_by_model"]
        self.assertEqual(by_model["gpt-5.5"],
                         {"in": 30, "out": 10, "cr": 70, "cc": 0, "cc1h": 0})
        self.assertEqual(by_model["gpt-5.4"],
                         {"in": 30, "out": 10, "cr": 30, "cc": 0, "cc1h": 0})

    def test_independent_subagent_usage_is_kept_without_counting_its_assignment(self):
        records = [
            _record("2026-07-20T00:00:00.000Z", "session_meta", {
                "id": "child", "cwd": "/repo", "timestamp": "2026-07-20T00:00:00.000Z",
                # Exercise the legacy source.subagent encoding by itself.
                "source": {"subagent": {"thread_spawn": {}}}}),
            _record("2026-07-20T00:00:01.000Z", "turn_context", {"model": "gpt-5.5"}),
            _record("2026-07-20T00:00:02.000Z", "event_msg", {
                "type": "user_message", "message": "agent assignment"}),
            # Independent child counters start at zero: 40 cached + 10 fresh.
            _token_count("2026-07-20T00:00:03.000Z", 50, 40, 5),
            _record("2026-07-20T00:00:04.000Z", "response_item", {
                "type": "message", "role": "assistant", "content": [{"text": "done"}]}),
        ]

        _, _, milestones, _, _ = self.parse(records)
        self.assertEqual([m["kind"] for m in milestones], ["session"])
        self.assertEqual(
            milestones[0]["activity"]["tokens_by_model"]["gpt-5.5"],
            {"in": 10, "out": 5, "cr": 40, "cc": 0, "cc1h": 0},
        )

    def test_forked_subagent_skips_replayed_history_and_keeps_child_delta(self):
        records = [
            _record("2026-07-20T00:00:00.900Z", "session_meta", {
                "id": "child", "cwd": "/repo", "timestamp": "2026-07-20T00:00:00.900Z",
                "forked_from_id": "parent", "thread_source": "subagent",
                "source": {"subagent": {"thread_spawn": {}}}}),
            # Older fork formats can replay the parent's session_meta too.
            _record("2026-07-20T00:00:00.901Z", "session_meta", {
                "id": "parent", "cwd": "/wrong", "timestamp": "2026-07-19T23:00:00.000Z"}),
            _record("2026-07-20T00:00:00.902Z", "turn_context", {"model": "gpt-5.5"}),
            _record("2026-07-20T00:00:00.903Z", "event_msg", {
                "type": "user_message", "message": "copied human prompt"}),
            _token_count("2026-07-20T00:00:00.904Z", 1000, 800, 100),
            # This started_at is from the parent's past and remains replay data.
            _record("2026-07-20T00:00:00.905Z", "event_msg", {
                "type": "task_started", "started_at": 1784505599}),
            # Session start is 1784505600.9; integer started_at 1784505600 marks child work.
            _record("2026-07-20T00:00:01.000Z", "event_msg", {
                "type": "task_started", "started_at": 1784505600}),
            _record("2026-07-20T00:00:01.100Z", "turn_context", {"model": "gpt-5.5"}),
            _record("2026-07-20T00:00:01.200Z", "event_msg", {
                "type": "user_message", "message": "child assignment"}),
            # Only this 60/10/20 increment belongs to the child: 40 fresh input.
            _token_count("2026-07-20T00:00:02.000Z", 1060, 820, 110),
            _record("2026-07-20T00:00:03.000Z", "response_item", {
                "type": "message", "role": "assistant", "content": [{"text": "done"}]}),
        ]

        cwd, session, milestones, _, _ = self.parse(records)
        self.assertEqual(cwd, "/repo")
        self.assertEqual(session["id"], "child")
        self.assertEqual(session["parent_session_id"], "parent")
        self.assertEqual(session["parent_relation"], "fork of")
        self.assertEqual([m["kind"] for m in milestones], ["session"])
        self.assertEqual(
            milestones[0]["activity"]["tokens_by_model"]["gpt-5.5"],
            {"in": 40, "out": 10, "cr": 20, "cc": 0, "cc1h": 0},
        )

    def test_interrupted_subagent_keeps_usage_without_an_assistant_message(self):
        records = [
            _record("2026-07-20T00:00:00.000Z", "session_meta", {
                "id": "child", "cwd": "/repo", "timestamp": "2026-07-20T00:00:00.000Z",
                # Exercise the thread_source encoding by itself.
                "thread_source": "subagent", "forked_from_id": "parent"}),
            # Parent replay establishes a nonzero baseline that must be excluded.
            _token_count("2026-07-20T00:00:00.100Z", 1000, 800, 100),
            _record("2026-07-20T00:00:00.200Z", "event_msg", {
                "type": "task_started", "started_at": 1784505600}),
            _record("2026-07-20T00:00:01.000Z", "turn_context", {"model": "gpt-5.5"}),
            # The task was interrupted after a model call, before any assistant message.
            # Its 80 cached + 10 fresh input tokens must still reach the total.
            _token_count("2026-07-20T00:00:02.000Z", 1090, 880, 104),
        ]

        _, _, milestones, _, _ = self.parse(records)
        self.assertEqual(len(milestones), 1)
        self.assertEqual(
            milestones[0]["activity"]["tokens_by_model"]["gpt-5.5"],
            {"in": 10, "out": 4, "cr": 80, "cc": 0, "cc1h": 0},
        )

    def test_reused_subagent_tasks_split_idle_time_without_adding_prompts(self):
        records = [
            _record("2026-07-20T00:00:00.000Z", "session_meta", {
                "id": "child", "cwd": "/repo", "timestamp": "2026-07-20T00:00:00.000Z",
                "thread_source": "subagent"}),
            _record("2026-07-20T00:00:01.000Z", "event_msg", {
                "type": "task_started", "started_at": 1784505601}),
            _record("2026-07-20T00:00:01.100Z", "turn_context", {"model": "gpt-5.5"}),
            # The first task contributes 40 cached + 10 fresh input tokens.
            _token_count("2026-07-20T00:00:04.000Z", 50, 40, 5),
            _record("2026-07-20T00:00:05.000Z", "response_item", {
                "type": "message", "role": "assistant", "content": [{"text": "first done"}]}),
            # Reuse after a 95-second idle gap; v2 emits no user_message here.
            _record("2026-07-20T00:01:40.000Z", "event_msg", {
                "type": "task_started", "started_at": 1784505700}),
            _record("2026-07-20T00:01:40.100Z", "turn_context", {"model": "gpt-5.5"}),
            # The second task contributes 20 cached + 10 fresh input tokens.
            _token_count("2026-07-20T00:01:42.000Z", 80, 60, 8),
            _record("2026-07-20T00:01:43.000Z", "response_item", {
                "type": "message", "role": "assistant", "content": [{"text": "second done"}]}),
        ]

        _, _, milestones, _, _ = self.parse(records)
        self.assertEqual([m["kind"] for m in milestones], ["session", "session"])
        self.assertEqual([m["activity"]["duration_ms"] for m in milestones], [4000, 3000])
        self.assertEqual(
            [m["activity"]["tokens_by_model"]["gpt-5.5"] for m in milestones],
            [
                {"in": 10, "out": 5, "cr": 40, "cc": 0, "cc1h": 0},
                {"in": 10, "out": 3, "cr": 20, "cc": 0, "cc1h": 0},
            ],
        )

    def test_injected_bootstrap_duration_does_not_create_an_empty_milestone(self):
        records = [
            _record("2026-07-20T00:00:00.000Z", "session_meta", {
                "id": "root", "cwd": "/repo", "timestamp": "2026-07-20T00:00:00.000Z"}),
            # Ignored model-facing bootstrap records can arrive well after session_meta.
            _record("2026-07-20T00:10:00.000Z", "response_item", {
                "type": "message", "role": "developer", "content": [{"text": "permissions"}]}),
            _record("2026-07-20T00:10:01.000Z", "event_msg", {
                "type": "user_message", "message": "real prompt"}),
            _record("2026-07-20T00:10:02.000Z", "turn_context", {"model": "gpt-5.5"}),
            _record("2026-07-20T00:10:03.000Z", "response_item", {
                "type": "message", "role": "assistant", "content": [{"text": "done"}]}),
        ]

        _, _, milestones, _, _ = self.parse(records)
        self.assertEqual([m["kind"] for m in milestones], ["prompt"])
        self.assertEqual(milestones[0]["activity"]["duration_ms"], 2000)

    def test_interrupted_tool_only_session_is_retained(self):
        records = [
            _record("2026-07-20T00:00:00.000Z", "session_meta", {
                "id": "child", "cwd": "/repo", "timestamp": "2026-07-20T00:00:00.000Z",
                "thread_source": "subagent"}),
            # A tool call is substantive even if interruption prevents text or usage output.
            _record("2026-07-20T00:00:02.000Z", "response_item", {
                "type": "function_call", "name": "exec_command",
                "arguments": json.dumps({"cmd": "true"})}),
        ]

        _, _, milestones, _, _ = self.parse(records)
        self.assertEqual(len(milestones), 1)
        self.assertEqual(milestones[0]["activity"]["tools"], {"Shell": 1})

    def test_substantive_activity_predicate_checks_each_work_bucket(self):
        for key, value in (
                ("assistant_turns", 1), ("tools", {"Shell": 1}),
                ("files", ["/repo/file.txt"]), ("tokens_in", 1),
                ("tokens_out", 1), ("cache_read", 1), ("cache_create", 1)):
            with self.subTest(key=key):
                activity = _new_activity()
                activity[key] = value
                self.assertTrue(_has_substantive_activity(activity))

        duration_only = _new_activity()
        duration_only["duration_ms"] = 60_000
        self.assertFalse(_has_substantive_activity(duration_only))

    def test_render_shows_zero_turn_tool_token_and_file_activity(self):
        records = [
            _record("2026-07-20T00:00:00.000Z", "session_meta", {
                "id": "child", "cwd": "/repo", "timestamp": "2026-07-20T00:00:00.000Z",
                "thread_source": "subagent"}),
            _record("2026-07-20T00:00:01.000Z", "turn_context", {"model": "gpt-5.5"}),
            _record("2026-07-20T00:00:02.000Z", "response_item", {
                "type": "function_call", "name": "exec_command",
                "arguments": json.dumps({"cmd": "true"})}),
            _record("2026-07-20T00:00:03.000Z", "event_msg", {
                "type": "patch_apply_end", "changes": {"/repo/result.txt": {}}}),
            # Zero-turn interrupted work still produced 80 cached + 10 fresh tokens.
            _token_count("2026-07-20T00:00:04.000Z", 90, 80, 4),
        ]

        page = render(self.timeline(records))
        self.assertIn('<div class="ro">', page)
        self.assertNotIn('entry session quiet', page)
        self.assertIn('4 tok out', page)
        self.assertIn('result.txt', page)
        self.assertIn('<span class="tn">Shell</span>', page)

    def test_render_keeps_duration_only_prompt_quiet(self):
        records = [
            _record("2026-07-20T00:00:00.000Z", "session_meta", {
                "id": "root", "cwd": "/repo", "timestamp": "2026-07-20T00:00:00.000Z"}),
            _record("2026-07-20T00:00:01.000Z", "event_msg", {
                "type": "user_message", "message": "prompt"}),
            # Ignored metadata advances time but is not machine work.
            _record("2026-07-20T00:01:01.000Z", "response_item", {
                "type": "message", "role": "developer", "content": [{"text": "metadata"}]}),
        ]

        page = render(self.timeline(records))
        self.assertIn('entry prompt quiet', page)
        self.assertNotIn('<div class="ro">', page)

    def test_build_timelines_aggregates_root_and_independent_child_once(self):
        root = [
            _record("2026-07-20T00:00:00.000Z", "session_meta", {
                "id": "root", "cwd": "/repo", "timestamp": "2026-07-20T00:00:00.000Z"}),
            _record("2026-07-20T00:00:01.000Z", "turn_context", {"model": "gpt-5.5"}),
            _record("2026-07-20T00:00:02.000Z", "event_msg", {
                "type": "user_message", "message": "human prompt"}),
            # Root contributes 70 cached + 30 fresh input tokens.
            _token_count("2026-07-20T00:00:03.000Z", 100, 70, 10),
            _record("2026-07-20T00:00:04.000Z", "response_item", {
                "type": "message", "role": "assistant", "content": [{"text": "done"}]}),
        ]
        child = [
            _record("2026-07-20T00:01:00.000Z", "session_meta", {
                "id": "child", "cwd": "/repo", "timestamp": "2026-07-20T00:01:00.000Z",
                "thread_source": "subagent"}),
            _record("2026-07-20T00:01:01.000Z", "turn_context", {"model": "gpt-5.5"}),
            _record("2026-07-20T00:01:02.000Z", "event_msg", {
                "type": "user_message", "message": "agent assignment"}),
            # Child contributes 40 cached + 10 fresh input tokens.
            _token_count("2026-07-20T00:01:03.000Z", 50, 40, 5),
            _record("2026-07-20T00:01:04.000Z", "response_item", {
                "type": "message", "role": "assistant", "content": [{"text": "done"}]}),
        ]

        with tempfile.TemporaryDirectory() as tmp:
            paths = []
            for name, records in (("root", root), ("child", child)):
                path = os.path.join(tmp, f"rollout-2026-07-20T00-00-00-{name}.jsonl")
                with open(path, "w") as fh:
                    for record in records:
                        fh.write(json.dumps(record) + "\n")
                paths.append(path)
            timelines = build_codex_timelines(paths)

        self.assertEqual(len(timelines), 1)
        stats = timelines[0]["stats"]
        self.assertEqual(stats["sessions"], 1)
        self.assertEqual(stats["automated_sessions"], 1)
        self.assertEqual(stats["prompts"], 1)
        self.assertEqual(
            stats["tokens_by_model"]["gpt-5.5"],
            {"in": 40, "out": 15, "cr": 110, "cc": 0, "cc1h": 0},
        )

    def test_large_timeline_omits_minimap(self):
        session = {
            "id": "root",
            "last_ts": "2026-07-20T00:00:01.000Z",
            "title": "large timeline",
            "tool": "codex",
        }
        milestones = [
            {
                "kind": "prompt",
                "text": f"prompt {i}",
                "ts": "2026-07-20T00:00:00.000Z",
                "session": "root",
                "activity": _new_activity(),
            }
            for i in range(MINIMAP_MAX_ENTRIES + 1)
        ]
        timeline = _timeline_dict(
            "/tmp/codex", "/repo", [session], milestones, Counter())

        page = render(timeline)

        self.assertNotIn('<aside class="minimap"', page)
        self.assertIn('<body class="has-right-rail">', page)


if __name__ == "__main__":
    unittest.main()
