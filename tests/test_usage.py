import os
import shutil
import tempfile
import unittest
from datetime import date, timedelta

from ccx_parse import _add_tokens, _new_milestone, build_timeline
from codex_parse import build_codex_timelines, rollout_paths
from generate_site import (_auto_interval, _axis_cost, _binned, _buckets,
                           _daily_series, _merge_timelines, _nice, _series_window,
                           _step_from, _tile_details, _usage_html, parse_ts, render,
                           render_index)


FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "transcripts")


def _fixture_entries():
    """The two synthetic projects, merged the way ``--all`` merges them."""
    claude = os.path.join(FIXTURES, "claude")
    example = build_timeline(os.path.join(claude, "-home-demo-src-example-project"))
    docs = build_timeline(os.path.join(claude, "-home-demo-src-docs-site"))
    codex = build_codex_timelines(rollout_paths(os.path.join(FIXTURES, "codex")))
    merged = _merge_timelines([example, *codex])
    return [("docs-site", docs), ("example-project", merged)]


class UsageSeriesTests(unittest.TestCase):
    def test_daily_series_buckets_fixture_activity_by_local_day(self):
        entries = _fixture_entries()
        docs, example = entries[0][1], entries[1][1]
        # The refresh time is the last fixture activity, so the series ends on
        # the last active day and the idle tail stays out of this test.
        last_ts = max(m["ts"] for _, tl in entries for m in tl["milestones"])
        refreshed = parse_ts(last_ts)

        series = _daily_series(entries, refreshed)

        docs_day = parse_ts(docs["milestones"][0]["ts"]).date()
        example_day = parse_ts(example["milestones"][0]["ts"]).date()
        # docs-site is three days before example-project in every timezone,
        # so the dense series has two active days around two idle ones.
        self.assertEqual(series["first"], docs_day.isoformat())
        self.assertEqual((example_day - docs_day).days, 3)
        self.assertEqual(len(series["days"]), 4)
        self.assertEqual(series["days"][1:3], [None, None])
        first, last = series["days"][0], series["days"][3]
        # One prompt in one session: the docs-site fixture's whole content.
        self.assertEqual((first["s"], first["i"]), (1, 1))
        # Splits carry [cost, tokens out, active ms] so the readout can follow
        # the plotted metric; docs-site is the only project on its day.
        self.assertEqual(first["p"], {"docs-site": [first["c"], first["o"], first["a"]]})
        # Two sessions (Claude plus Codex) with three inputs on the last day,
        # and the two vendors' models each priced separately.
        self.assertEqual((last["s"], last["i"]), (2, 3))
        self.assertEqual(
            set(last["m"]), {"claude-opus-4-8", "claude-sonnet-5", "gpt-5.6-sol"})
        self.assertAlmostEqual(sum(v[0] for v in last["m"].values()), last["c"], places=3)
        self.assertEqual(sum(v[1] for v in last["m"].values()), last["o"])
        # Every fixture entry names one model, so attributing each entry's
        # active time to its most-used model accounts for all of the day.
        self.assertEqual(sum(v[2] for v in last["m"].values()), last["a"])
        self.assertNotIn("u", last)   # every fixture model has a list rate
        # Hour buckets are sparse: one per local hour with activity, indexed
        # from midnight of the first day, carrying the readout fields but not
        # the cache fields only the daily tiles use.
        first_o = docs_day.toordinal()
        expected_hours = set()
        for _, tl in entries:
            for m in tl["milestones"]:
                at = parse_ts(m["ts"])
                expected_hours.add((at.date().toordinal() - first_o) * 24 + at.hour)
        self.assertEqual([idx for idx, _ in series["hours"]], sorted(expected_hours))
        hour_fields = {k for _, h in series["hours"] for k in h}
        self.assertTrue(hour_fields <= {"s", "i", "a", "o", "c", "m", "p"}, hour_fields)
        self.assertEqual(sum(h.get("i", 0) for _, h in series["hours"]), first["i"] + last["i"])
        self.assertEqual(sum(h.get("s", 0) for _, h in series["hours"]), 3)

    def test_window_summary_matches_aggregate_statistics(self):
        entries = _fixture_entries()
        refreshed = parse_ts(max(tl["stats"]["last_ts"] for _, tl in entries))
        series = _daily_series(entries, refreshed)

        window = _series_window(series)

        # Summing the series over the whole range must reproduce the totals
        # the hero cards show, or the "all" preset would change the numbers.
        stats = [tl["stats"] for _, tl in entries]
        self.assertEqual(window["s"], sum(s["sessions"] for s in stats))
        self.assertEqual(
            window["i"], sum(s["prompts"] + s["commands"] for s in stats))
        self.assertEqual(window["a"], sum(s["active_ms"] for s in stats))
        self.assertEqual(window["o"], sum(s["tokens_out"] for s in stats))
        self.assertEqual(window["days"], 2)
        # Two active days separated by idle days never form a longer streak;
        # the busiest day is the 29-minute example-project day at index 3.
        self.assertEqual(window["streak"], 1)
        self.assertEqual(window["busy"], 3)
        # Tile details for the fixture site: $3.02 over 0.55 active hours is
        # $5.50 an hour; over four inputs it is $0.76 each; 2.3M cache reads
        # out of 2.488M prompt tokens is a 92% hit rate; 62.5k output tokens
        # over four inputs is 15.6k each; the one-day streak names its day.
        details = _tile_details(series, window)
        self.assertEqual(details["active"], "<b>$5.50</b> per active hour")
        self.assertEqual(details["inputs"], "<b>$0.76</b> per input")
        self.assertEqual(details["cost"], "<b>92%</b> cache hit rate")
        self.assertEqual(details["tok"], "<b>15.6k</b> per input")
        self.assertEqual(details["sessions"], "<b>1.3</b> inputs per session")
        self.assertEqual(details["days"], "of 4 days")
        self.assertEqual(details["streak"], "Mar 12")
        self.assertEqual(details["busiest"], "Sun · Mar 15")

    def test_entry_usage_spreads_across_the_hours_it_ran(self):
        # A 90-minute entry starting at half past the hour puts one third of
        # its tokens, cost, and active time in the starting hour and two
        # thirds in the next, while its input and session stay where it began.
        # 300 uncached input and 3,000 output tokens on sonnet-5 give a cost
        # that splits the same way.
        ts = "2026-01-01T23:30:00.000Z"
        entry = _new_milestone("prompt", "spread me", ts, "s1", "rec-1")
        activity = entry["activity"]
        activity["duration_ms"] = 5_400_000
        activity["assistant_turns"] = 1
        activity["models"]["claude-sonnet-5"] = 1
        _add_tokens(activity, "claude-sonnet-5", 300, 3000, 0, 0)
        timeline = {"sessions": [{"id": "s1", "last_ts": ts, "tool": "claude"}],
                    "milestones": [entry]}
        start = parse_ts(ts)

        series = _daily_series([(None, timeline)], start + timedelta(hours=2))

        first_o = date.fromisoformat(series["first"]).toordinal()
        hour_of = lambda at: (at.date().toordinal() - first_o) * 24 + at.hour
        hours = dict(series["hours"])
        h0, h1 = hour_of(start), hour_of(start + timedelta(hours=1))
        # The first slice runs to the next hour boundary in the local zone.
        share = (60 - start.minute) / 90
        self.assertEqual(sorted(hours), [h0, h1])
        self.assertEqual(hours[h0]["o"], round(3000 * share))
        self.assertEqual(hours[h1]["o"], 3000 - round(3000 * share))
        self.assertEqual(hours[h0]["a"], round(5_400_000 * share))
        self.assertAlmostEqual(hours[h0]["c"], (hours[h0]["c"] + hours[h1]["c"]) * share, places=3)
        self.assertEqual(hours[h0]["m"]["claude-sonnet-5"][1:], [hours[h0]["o"], hours[h0]["a"]])
        self.assertEqual((hours[h0]["i"], hours[h0]["s"]), (1, 1))
        self.assertNotIn("i", hours[h1])
        self.assertNotIn("s", hours[h1])
        # The day buckets are the hour buckets summed, so the totals survive.
        window = _series_window(series)
        self.assertEqual((window["o"], window["a"], window["i"], window["s"]),
                         (3000, 5_400_000, 1, 1))

    def test_buckets_by_hour_and_by_monday_week(self):
        entries = _fixture_entries()
        refreshed = parse_ts(max(tl["stats"]["last_ts"] for _, tl in entries))
        series = _daily_series(entries, refreshed)

        hours = _buckets(series, "hour")
        weeks = _buckets(series, "week")

        # Four days give 96 hourly bars, active only where the hours list is,
        # and one Monday-based week clipped to the four fixture days.
        self.assertEqual(len(hours), 96)
        self.assertEqual([h for h, d, _, _ in hours if d], [idx for idx, _ in series["hours"]])
        self.assertEqual(len(weeks), 1)
        _, week, frm, to = weeks[0]
        self.assertEqual((frm, to), (0, 3))
        window = _series_window(series)
        self.assertEqual((week["s"], week["i"], week["o"]), (window["s"], window["i"], window["o"]))
        self.assertAlmostEqual(week["c"], window["c"], places=3)

    def test_window_summary_streak_and_busiest_day(self):
        # Three consecutive active days form the streak; the fourth active day
        # is isolated. The busiest day is the one with the most active time.
        series = {"first": "2026-01-01", "days": [
            {"a": 100, "i": 1}, None, {"a": 50, "i": 1}, {"a": 900, "i": 1},
            {"a": 10, "i": 1}, None, {"a": 5, "i": 1}]}

        whole = _series_window(series)
        tail = _series_window(series, 4, 6)

        self.assertEqual((whole["streak"], whole["streak_start"]), (3, 2))
        self.assertEqual((whole["busy"], whole["busy_v"], whole["days"]), (3, 900, 5))
        self.assertEqual((tail["streak"], tail["busy"], tail["days"]), (1, 4, 2))
        # The three-day streak runs Jan 3 to Jan 5 in the tile detail.
        self.assertEqual(_tile_details(series, whole)["streak"], "Jan 3 – Jan 5")

    def test_axis_rounding_gives_round_gridlines(self):
        # Each top value must halve to another round number for the middle
        # gridline, which the 1-2-4-5-10 ladder guarantees; 404 rounds to 500
        # rather than jumping to 1,000.
        self.assertEqual([_nice(v) for v in (0.7, 3, 12.37, 404, 58_000)],
                         [1, 4, 20, 500, 100_000])
        # Only a half-dollar middle line needs cents; the ladder keeps every
        # other gridline value whole.
        self.assertEqual([_axis_cost(0.5), _axis_cost(2000)], ["$0.50", "$2,000"])
        # At most eight ticks: daily up to 8 days, weekly up to 8 weeks, then
        # monthly steps; 3000 days would show more than eight yearly ticks, so
        # the step doubles to two years.
        day_steps = (1, 2, 7, 14, 30, 60, 90, 180, 365)
        self.assertEqual([_step_from(day_steps, n) for n in (4, 8, 9, 30, 56, 57, 204, 3000)],
                         [1, 1, 2, 7, 7, 14, 30, 730])
        # "auto" plots hours up to a week, days up to 26 weeks, then weeks.
        self.assertEqual([_auto_interval(n) for n in (1, 7, 8, 182, 183)],
                         ["hour", "hour", "day", "day", "week"])
        # A minimap folds 1,000 values into at most 360 bins of their peak.
        self.assertEqual(_binned([1, 5, 2, 9], 2), [5, 9])
        self.assertEqual(len(_binned(list(range(1000)), 360)), 334)

    def test_explorer_needs_two_active_days(self):
        entries = _fixture_entries()
        refreshed = parse_ts(max(tl["stats"]["last_ts"] for _, tl in entries))

        project_page = render(entries[1][1], refreshed_at=refreshed)
        index_page = render_index(
            [("docs", entries[0][1]), ("example", entries[1][1])],
            refreshed_at=refreshed)

        # example-project has one active day: nothing to explore, but the new
        # cards and rates still render from its series.
        self.assertNotIn('class="usage"', project_page)
        self.assertIn('data-k="streak"', project_page)
        self.assertIn("busiest day", project_page)
        self.assertIn("<b>$5.88</b> per active hour", project_page)
        # The index spans two active days, so it gets the explorer.
        self.assertIn('<section class="usage" id="usage">', index_page)
        self.assertIn('id="usageData">{"first":', index_page)
        # A four-day history plots hourly under "auto": 96 bars, ticks every
        # twelve hours, and the readout resting on the last active hour. The
        # expected labels come from the series because the hour and, in some
        # zones, the day depend on the machine's local timezone.
        series = _daily_series(
            [("docs", entries[0][1]), ("example", entries[1][1])], refreshed)
        first_day = date.fromisoformat(series["first"])
        last_hour = series["hours"][-1][0]
        last_day = first_day + timedelta(days=last_hour // 24)
        h = last_hour % 24
        self.assertIn('style="grid-template-columns:repeat(96,1fr)"', index_page)
        self.assertIn(f'<time id="uRoDate">{last_day.strftime("%a · %b %-d, %Y")} · '
                      f'{h:02d}:00–{(h + 1) % 24:02d}:00</time>', index_page)
        self.assertIn(f'>{first_day.strftime("%b %-d")}</span>', index_page)
        self.assertIn('>12:00</span>', index_page)
        self.assertIn('<option value="all" selected>all</option>'
                      '<option value="custom" disabled hidden>custom</option>', index_page)
        self.assertIn('class="interval on" data-i="auto"', index_page)
        self.assertIn('class="interval eff" data-i="hour"', index_page)
        # Models and projects each get their own keyed readout line of
        # fixed-width cells.
        self.assertIn('<div class="uro-line" id="uRo2"><span class="uro-k">models</span>'
                      '<span class="ui"><span class="mdl fam-gpt">gpt-5.6-sol</span>'
                      '<span>$0.72</span></span>', index_page)
        # The project cell adds the four minutes of the /review entry that ran
        # past ten o'clock to the Codex session's $0.72, so it is matched loosely.
        self.assertRegex(index_page, r'<div class="uro-line" id="uRo3"><span class="uro-k">projects</span>'
                                     r'<span class="ui"><span>example-project</span><span>\$\d\.\d\d</span></span>')
        # A one-day project window is the same series machinery with no explorer.
        one_day = _daily_series([(None, entries[1][1])], refreshed)
        self.assertEqual(_usage_html(one_day), "")

    def test_empty_transcript_file_is_not_a_session(self):
        src = os.path.join(FIXTURES, "claude", "-home-demo-src-example-project")
        with tempfile.TemporaryDirectory() as tmp:
            project_dir = os.path.join(tmp, "-home-demo-src-example-project")
            shutil.copytree(src, project_dir)
            # Claude Code can leave a zero-byte session file behind; it has no
            # records, so it must not count as a conversation.
            open(os.path.join(project_dir, "44444444-4444-4444-8444-444444444444.jsonl"),
                 "w").close()

            timeline = build_timeline(project_dir)

        self.assertEqual(timeline["stats"]["sessions"], 1)
        self.assertEqual([s["id"][:8] for s in timeline["sessions"]], ["11111111"])


if __name__ == "__main__":
    unittest.main()
