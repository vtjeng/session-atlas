#!/usr/bin/env python3
"""Generate a static timeline site from Claude Code and Codex CLI transcripts.

    python3 generate_site.py example-project                 # -> ./site/<stable-slug>/index.html
    python3 generate_site.py /path/to/project --out ./out    # by path, custom out dir
    python3 generate_site.py --all                           # every project + index page

The page shows each prompt and the activity that followed it, including tools,
files, tokens, and agent active time. A top ribbon shows when sessions occurred, and a
right-hand minimap tracks document position. The generator renders timestamps
in the local timezone and writes each page as one dependency-free HTML file.
"""
import argparse
import contextlib
import fcntl
import functools
import glob
import hashlib
import html
import itertools
import json
import math
import os
import re
import tempfile
import unicodedata
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from ccx_parse import (PROJECTS, _aggregate, _has_substantive_activity,
                       _is_transcript_dir, _iter_subagent_transcripts,
                       build_timeline, find_project_dir,
                       merge_token_models, parse_iso)
from codex_parse import (CODEX_SESSIONS, build_codex_timelines,
                         build_history_only_timelines,
                         _associate_codex_subagents, iter_rollout_metas,
                         rollout_paths)
import pricing

GENERATOR_META = '<meta name="generator" content="session-atlas">'
PAGE_PROVENANCE = "generated from local transcripts"
INPUT_COUNT_EXPLANATION = (
    "Prompts, commands, and recovered prompts counted as inputs."
)
RECOVERED_PROMPT_EXPLANATION = (
    "Recovered from Codex history because no rollout was found; typically "
    "associated with `/btw`, but not always. The assistant reply and structured "
    "usage details are unavailable."
)
_SAFE_PROJECT_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_MAX_PROJECT_SLUG = 80
# The minimap adds one positioned DOM node and one geometry read per entry.
# Large pages omit it so the navigation aid does not compete with the log.
MINIMAP_MAX_ENTRIES = 1000


# The favicon's background and stroke colours; a shared page swaps them.
_ICON_BACKGROUND = "#15171b"
_ICON_STROKE = "#e9e6df"


@functools.lru_cache(maxsize=2)
def favicon_data_url(shared=False):
    """Return the favicon as an SVG data URL.

    With ``shared``, the icon's background and stroke are swapped, so the tab
    of a page downloaded with a share button differs from a project page's.
    """
    svg = Path(__file__).with_name("favicon.svg").read_text(encoding="utf-8")
    if shared:
        svg = (svg.replace(_ICON_BACKGROUND, "\0")
               .replace(_ICON_STROKE, _ICON_BACKGROUND)
               .replace("\0", _ICON_STROKE))
    return f"data:image/svg+xml,{quote(svg, safe='')}"


def help_button():
    """The ? button that opens the shortcuts dialog."""
    return ('<button type="button" class="shelp" id="shelp" title="shortcuts (?)"'
            ' aria-label="shortcuts">?</button>')


def help_html(*, project, stepper=False, explorer=False, ribbon=False, rail=False):
    """The shortcuts dialog for one page, listing only the groups the page has.

    ``project`` is a project page (the index has no sessions); ``stepper``
    means more than one session, so j and k and the fold-all caret exist;
    ``explorer`` means the usage explorer rendered; ``ribbon`` and ``rail``
    mean the time ribbon and the right-hand minimap rendered.
    """
    groups = []
    if project:
        rows = []
        if stepper:
            rows.append(("<kbd>j</kbd> <kbd>k</kbd>", "next and previous session"))
        rows.append(("<kbd>s</kbd>",
                     "share the session at the reading line, the one just under the sticky bar"))
        rows.append(("click a session header", "collapse or expand that session"))
        if stepper:
            rows.append(("<span class=\"kglyph\">&#9662;</span> in the sticky bar",
                         "collapse or expand every session"))
        groups.append(("Sessions", rows))
    if explorer:
        # the keys act while the chart has focus; a click on a bar pins it and
        # focuses the chart, so it is listed first and the note says so
        groups.append(("Chart", [
            ("click a bar", "pin the readout to it (this also focuses the chart)"),
            ("<kbd>&larr;</kbd> <kbd>&rarr;</kbd>",
             "step the pinned bar; with nothing pinned, start from the last active bar"),
            ("<kbd>Home</kbd> <kbd>End</kbd>", "first and last bar"),
            ("<kbd>Esc</kbd>", "unpin"),
            ("drag across the chart", "zoom the window to those bars"),
            ("drag the strip below the chart", "move or resize the window"),
        ]))
    if project:
        rows = []
        if ribbon:
            rows.append(("click the ribbon under the sticky bar",
                         "jump to the nearest entry in time"))
        if rail:
            rows.append(("drag the rail at the right edge", "scroll the page, on wide screens"))
        rows.append(("click the project name, or the session title",
                     "top of the page, or top of that session"))
        rows.append(("the share glyph", "download that session or entry as a page"))
        groups.append(("Page", rows))
    notes = {"Chart": "The keys act while the chart has focus: click a bar, or tab to the chart."}
    sections = "".join(
        f'<section><h3>{title}</h3>'
        + (f'<p class="help-note">{notes[title]}</p>' if title in notes else "")
        + '<dl>' + "".join(f'<dt>{k}</dt><dd>{v}</dd>' for k, v in rows)
        + '</dl></section>' for title, rows in groups)
    return ('<dialog class="help" id="help" aria-labelledby="helpTitle"><div class="help-box">'
            '<div class="help-head"><h2 id="helpTitle">Shortcuts</h2>'
            '<button type="button" class="help-close" id="helpClose" aria-label="close">'
            '&times;</button></div>'
            f'{sections}'
            '<p class="help-foot"><kbd>?</kbd> opens this list &middot; <kbd>Esc</kbd> closes it</p>'
            '</div></dialog>')


def favicon_link():
    """Return the standalone-page favicon as an embedded SVG data URL."""
    return f'<link rel="icon" type="image/svg+xml" href="{favicon_data_url()}">'

# ------------------------------------------------------------------ helpers -- #
def esc(s):
    return html.escape(s if s is not None else "", quote=True)


_MD_CODE = re.compile(r"`([^`\n]+)`")
_MD_STRONG = re.compile(r"\*\*([^*\n]+?)\*\*")
_MD_EM = re.compile(r"(?<!\*)\*([^*\n]+?)\*(?!\*)")


def inline_markdown(s):
    """Render a safe, small inline-Markdown subset for response excerpts."""
    tokens = []

    def stash(tag):
        tokens.append(tag)
        return f"\x00{len(tokens) - 1}\x00"

    text = _MD_CODE.sub(
        lambda match: stash(f'<code>{match.group(1)}</code>'), esc(s))
    text = _MD_STRONG.sub(r"<strong>\1</strong>", text)
    text = _MD_EM.sub(r"<em>\1</em>", text)
    return re.sub(
        r"\x00(\d+)\x00", lambda match: tokens[int(match.group(1))], text)


def _stable_anchor(prefix, *parts):
    """Hash source identity into a short, URL-safe in-page anchor."""
    raw = "\x1f".join("" if part is None else str(part) for part in parts)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]
    return f"{prefix}-{digest}"


def _s(n):
    """Plural suffix: '' for exactly one, 's' otherwise."""
    return "" if n == 1 else "s"


def _private_directory(path):
    """Create a generated-output directory and enforce owner-only access."""
    os.makedirs(path, mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)


def _project_slug_base(project_path):
    """Return a readable, URL-safe basename for a project path."""
    raw = os.path.basename((project_path or "").rstrip("/"))
    if not raw:
        return "root"
    normalized = unicodedata.normalize("NFKD", raw)
    ascii_name = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", ascii_name).strip("._-")
    slug = slug[:_MAX_PROJECT_SLUG].rstrip("._-")
    return slug or "project"


def _hashed_project_slug(base, project_path, digest_length):
    digest = hashlib.sha256(project_path.encode("utf-8")).hexdigest()
    suffix = "--" + digest[:digest_length]
    stem = base[:_MAX_PROJECT_SLUG - len(suffix)].rstrip("._-") or "project"
    return stem + suffix


def _allocate_project_slugs(project_paths):
    """Allocate safe slugs that do not depend on which projects were rendered."""
    paths = sorted(set(project_paths))
    bases = {path: _project_slug_base(path) for path in paths}
    allocated = {}
    used = set()
    for path in paths:
        base = bases[path]
        candidate = _hashed_project_slug(base, path, 64)
        if candidate.casefold() in used:
            raise ValueError(f"Could not allocate a unique project slug for {path!r}")
        used.add(candidate.casefold())
        allocated[path] = candidate
    return allocated


def _project_output_dir(out, slug):
    """Resolve one project directory beneath ``out`` and reject redirection."""
    root = os.path.abspath(out)
    if not _SAFE_PROJECT_SLUG.fullmatch(slug):
        raise ValueError(f"Unsafe project slug: {slug!r}")
    destination = os.path.abspath(os.path.join(root, slug))
    if os.path.commonpath((root, destination)) != root:
        raise ValueError(f"Project output escapes --out: {destination}")
    if os.path.lexists(destination) and os.path.islink(destination):
        raise ValueError(f"Project output directory is a symlink: {destination}")
    return destination


def tool_pill(t):
    """Color-coded claude/codex badge (falls back to plain style for others)."""
    t = t or "claude"
    return f'<span class="tooltag t-{esc(t)}">{esc(t)}</span>'


@functools.lru_cache(maxsize=None)
def parse_ts(ts):
    """ISO timestamp -> aware datetime in the *local* timezone (or None).
    Memoized: each milestone's timestamp is formatted many times per page, and
    --all re-parses the same strings across projects."""
    dt = parse_iso(ts)
    return dt.astimezone() if dt else None


def _fmt(ts, pat):
    dt = parse_ts(ts)
    return dt.strftime(pat) if dt else ""


def fmt_ts(ts):
    return _fmt(ts, "%b %-d, %Y · %H:%M")


def now_local():
    """Current local time as an aware datetime."""
    return datetime.now().astimezone()


def fmt_dt(dt):
    return dt.strftime("%b %-d, %Y · %H:%M")


def refresh_stamp(dt, bold=False):
    """Absolute refresh time plus a live relative-age hook."""
    t = esc(fmt_dt(dt))
    iso = esc(dt.isoformat(timespec="seconds"))
    if bold:
        t = f"<b>{t}</b>"
    return f'{t} <span class="age" data-refreshed-at="{iso}">(just now)</span>'


def fmt_date(ts):
    return _fmt(ts, "%b %-d, %Y")


def fmt_date_short(ts):
    """Year-less date for dense card stats (the hero already states the year)."""
    return _fmt(ts, "%b %-d")


def fmt_clock(ts):
    return _fmt(ts, "%H:%M")


def fmt_dayrule(ts):
    return _fmt(ts, "%a · %b %-d").upper()


def fmt_dur(ms):
    if not ms:
        return "—"
    s = ms / 1000
    if s < 60:
        return f"{s:.0f}s"
    m = s / 60
    if m < 60:
        return f"{m:.0f}m"
    h = int(m // 60)
    return f"{h}h {int(m % 60)}m"


def gap_secs(a, b):
    da, db = parse_ts(a), parse_ts(b)
    if not da or not db:
        return 0
    return (db - da).total_seconds()


def fmt_gap(secs):
    if secs < 90:
        return f"{secs:.0f}s later"
    mins = secs / 60
    if mins < 90:
        return f"{mins:.0f}m later"
    hrs = mins / 60
    if hrs < 36:
        return f"{hrs:.1f}h later"
    return f"{hrs/24:.1f}d later"


def fmt_num(n):
    if n is None:
        return "0"
    if n >= 1_000_000_000:
        return f"{n/1e9:.1f}B"
    if n >= 1_000_000:
        return f"{n/1e6:.1f}M"
    if n >= 1_000:
        return f"{n/1e3:.1f}k"
    return str(n)


def fmt_cost(d):
    """Rounded whole-dollar amount — cents are visual noise on these estimates.
    Sub-dollar costs show ``<$1`` rather than rounding to a misleading ``$0``.
    (The per-1M-token rate table keeps its precision via ``_rate``.)"""
    if not d:
        return "$0"
    if d < 1:
        return "<$1"
    return f"${round(d):,}"


def cost_display(by_model):
    """Primary estimate text/label/title, visibly partial if any model is unpriced."""
    _, total, unpriced = pricing.cost_breakdown(by_model or {})
    shown = fmt_cost(total)
    label = "est. API cost"
    if unpriced:
        shown += "+"
    title = cost_breakdown_title(by_model or {})
    return total, shown, label, title


def _rate(x):
    """Format a $/1M-token rate, keeping sub-cent precision when it matters."""
    return f"${x:,.2f}" if round(x * 100) == x * 100 else f"${x:,.3f}"


_GRID_CLOSE = "</tbody></table></div>"


def _grid_open(headers):
    """Open a ``.grid`` table: a ``.tw`` scroll wrapper, a colgroup whose first
    column is wide, and the header row. ``headers[0]`` labels the wide column.
    Pair with ``_GRID_CLOSE``."""
    cols = '<col class="cm">' + "<col>" * (len(headers) - 1)
    ths = "".join(f"<th>{h}</th>" for h in headers)
    return (f'<div class="tw"><table class="grid"><colgroup>{cols}</colgroup>'
            f"<thead><tr>{ths}</tr></thead><tbody>")


def _model_td(mid):
    """A model-name table cell, colored by vendor family."""
    fam = model_family(mid)
    cls = f' class="mdl fam-{fam}"' if fam else ""
    return f'<td{cls}>{esc(clean_model(mid))}</td>'


_MODEL_DISPLAY_ORDER = (
    # Keep the familiar families together, with capability tiers increasing.
    "claude-haiku-4-5",
    "claude-haiku-4-5-20251001",
    "claude-sonnet-5",
    "claude-opus-4-6",
    "claude-opus-4-8",
    "claude-opus-5",
    "claude-fable-5",
    "claude-fable-5-1",
    "gpt-5.3-codex",
    "gpt-5.4",
    "gpt-5.5",
    "gpt-5.6-luna",
    "gpt-5.6-terra",
    "gpt-5.6-sol",
    "gpt-5.6",
)
_MODEL_DISPLAY_RANK = {mid: rank for rank, mid in enumerate(_MODEL_DISPLAY_ORDER)}


def _model_sort_key(mid):
    """Sort known models by family and curated low-to-high capability tier."""
    fam = model_family(mid)
    family_rank = {"claude": 0, "gpt": 1}.get(fam, 2)
    rank = _MODEL_DISPLAY_RANK.get(mid)
    if rank is not None:
        return family_rank, 0, rank, clean_model(mid).lower()
    # Keep newly observed or unpriced models visible, but after known models in
    # their family; their relative order remains predictable by model name.
    return family_rank, 1, 0, clean_model(mid).lower()


def _sort_model_rows(rows):
    return sorted(rows, key=lambda row: _model_sort_key(row[0]))


def _model_table(models, overall, cell, fmt, heading, multi):
    """A model x token-type matrix. ``cell(cats, k)`` pulls a cell's raw number;
    the last column is that row summed, and (when ``multi``) a total row sums the
    models. ``overall`` is the aggregate cats used for that total row."""
    def cells(cats):
        vals = [cell(cats, k) for k, _ in pricing.CATEGORIES]
        tds = "".join(f'<td>{esc(fmt(v))}</td>' for v in vals)
        return f'{tds}<td>{esc(fmt(sum(vals)))}</td>'

    body = [f'<tr>{_model_td(mid)}{cells(cats)}</tr>' for mid, cats in models]
    if multi:
        body.append(f'<tr class="tot"><td>total</td>{cells(overall)}</tr>')
    headers = ["model", *(label for _, label in pricing.CATEGORIES), "total"]
    return (f'<p class="sh">{heading}</p>'
            + _grid_open(headers) + "".join(body) + _GRID_CLOSE)


def _breakdown_table(by_model, scope):
    """Where this page's estimate goes: a cost matrix (model x token type) and a
    matching token-count matrix, so any API-cost figure is traceable to model and token."""
    by_model = by_model or {}
    overall, total, unpriced = pricing.cost_breakdown(by_model)
    if not total and not unpriced:
        return ""
    priced = []
    for mid, tk in by_model.items():
        cats, mt, _ = pricing.cost_breakdown({mid: tk})
        if mt:
            priced.append((mt, mid, cats))
    models = _sort_model_rows([(mid, cats) for _, mid, cats in priced])
    multi = len(models) > 1
    cost_table = (_model_table(models, overall, lambda c, k: c[k]["cost"], fmt_cost,
                               f'Estimated cost by model ({esc(scope)}):', multi)
                  if models else "")

    token_models = []
    token_overall = {k: {"tokens": 0, "cost": 0.0} for k, _ in pricing.CATEGORIES}
    for mid, tk in by_model.items():
        cats = {k: {"tokens": tk.get(k, 0), "cost": 0.0}
                for k, _ in pricing.CATEGORIES}
        if any(c["tokens"] for c in cats.values()):
            token_models.append((mid, cats))
            for k, _ in pricing.CATEGORIES:
                token_overall[k]["tokens"] += cats[k]["tokens"]
    token_models = _sort_model_rows(token_models)
    token_table = _model_table(
        token_models, token_overall, lambda c, k: c[k]["tokens"], fmt_num,
        '&hellip; and the token counts behind it:', len(token_models) > 1)
    return cost_table + token_table


def cost_method_html(by_model, scope):
    """Expandable pricing panel in the page hero: computation, breakdown, and rates."""
    _, _, unpriced = pricing.cost_breakdown(by_model or {})
    cat_labels = [label for _, label in pricing.CATEGORIES]
    rate_rows = []
    for mid, (pin, pout, pcr, pcc, pcc1h) in pricing.PRICES.items():
        cw = "&mdash;" if pcc is None else _rate(pcc)  # None = category not billed
        cw1h = "&mdash;" if pcc1h is None else _rate(pcc1h)
        rate_rows.append(
            f'<tr>{_model_td(mid)}<td>{_rate(pin)}</td>'
            f'<td>{_rate(pout)}</td><td>{_rate(pcr)}</td><td>{cw}</td>'
            f'<td>{cw1h}</td></tr>')
    excl = ""
    if unpriced:
        excl = (f'<p class="excl">Excluded (no rate): {esc(", ".join(unpriced))}. '
                f'Add them to <code>pricing.py</code> to include their API cost.</p>')
    category_help = "".join(
        f'<li><b>{esc(label)}:</b> {esc(help_text)}</li>'
        for _, label, help_text in pricing.CATEGORY_SPECS
    )
    category_help = f'<ul class="category-help">{category_help}</ul>'
    return (
        '<details class="pricing"><summary>How is est. API cost estimated?</summary>'
        '<div class="pricing-body">'
        "<p>This is an API list-price estimate. Each model's published rate is applied "
        "to the tokens attributed to that model, including cache reads and writes.</p>"
        f'{category_help}'
        f"<p>Rates are standard published list prices per 1M tokens, as of "
        f"<b>{esc(pricing.AS_OF)}</b>.</p>"
        f'{_breakdown_table(by_model, scope)}'
        '<p class="sh">Rates used (per 1M tokens):</p>'
        f'{_grid_open(["model", *cat_labels])}{"".join(rate_rows)}{_GRID_CLOSE}'
        f'{excl}</div></details>')


def cost_breakdown_title(by_model):
    """Tooltip text: per-model API-cost split behind an est. API cost figure."""
    parts = []
    for mid, tk in by_model.items():
        c = pricing.estimate_cost({mid: tk})
        if c:
            parts.append((c, f"{clean_model(mid)} {fmt_cost(c)}"))
    _, _, unpriced = pricing.cost_breakdown(by_model)
    text = " · ".join(s for _, s in sorted(parts, reverse=True))
    if unpriced:
        suffix = "unpriced: " + ", ".join(unpriced)
        text = f"{text} · {suffix}" if text else suffix
    return text


def clean_model(m):
    return m.replace("claude-", "")


def model_family(m):
    """Vendor family for coloring: 'claude' (Anthropic), 'gpt' (OpenAI), or ''."""
    m = (m or "").lower()
    if m.startswith(("claude", "opus", "sonnet", "haiku", "fable")):
        return "claude"
    if m.startswith(("gpt", "chatgpt", "codex", "o1", "o3", "o4")):
        return "gpt"
    return ""


def mag(m):
    """Per-entry 'work magnitude': agent active time if timed, else tokens out."""
    return m["activity"]["duration_ms"] or m["activity"]["tokens_out"]


def _sc_var(num):
    """CSS custom-property setting a session's color from the 8-color cycle."""
    return f"--sc:var(--s{(num - 1) % 8 + 1})"


def _stat_cards_html(cards):
    """Hero stat tiles shared by the project page and the index-page hero.

    Each tile is ``(key, label, number, detail, tooltip)``. The key is a
    ``data-k`` hook the usage explorer uses to recompute the tile for the
    selected date window.
    """
    out = []
    for k, label, n, detail, tip in cards:
        title = f' title="{esc(tip)}"' if tip else ""
        out.append(f'<div class="stat" data-k="{esc(k)}"{title}>'
                   f'<div class="l lbl">{esc(label)}</div><div class="n">{esc(n)}</div>'
                   f'<div class="d">{detail}</div></div>')
    return "".join(out)


def _summary_stat_cards(*, sessions, inputs, active_ms, tokens_out,
                        days_active, by_model, series=None, window=None):
    """Build the shared summary tiles used by project and index heroes.

    ``series`` and ``window`` (see ``_daily_series`` and ``_series_window``)
    supply the streak and busiest-day tiles and every tile's detail line.
    """
    _, cost_text, cost_label, cost_title = cost_display(by_model or {})
    d = _tile_details(series, window) if series and window else {}
    dash = "&mdash;"
    streak = window["streak"] if window else 0
    busy_n = fmt_dur(window["busy_v"]) if window and window["busy"] is not None else "—"
    return [
        ("sessions", f'session{_s(sessions)}', fmt_num(sessions),
         d.get("sessions", dash), None),
        ("inputs", f'input{_s(inputs)}', fmt_num(inputs),
         d.get("inputs", dash), INPUT_COUNT_EXPLANATION),
        ("active", "agent active time", fmt_dur(active_ms), d.get("active", dash), None),
        ("tok", "tokens out", fmt_num(tokens_out), d.get("tok", dash), None),
        ("days", f'day{_s(days_active)} active', fmt_num(days_active),
         d.get("days", dash), None),
        ("cost", cost_label, cost_text, d.get("cost", dash), cost_title),
        ("streak", "longest streak", f"{streak} day{_s(streak)}", d.get("streak", dash),
         "Longest run of consecutive days with activity in this range"),
        ("busiest", "busiest day", busy_n, d.get("busiest", dash),
         "Day with the most agent active time in this range"),
    ]


# ------------------------------------------------------------ usage series -- #
# The hero chart and the live stat cards share one dense per-day series. The
# generator embeds it as JSON and renders the all-time state; the client
# re-sums it for the selected window so the chart, cards, and rates agree.
# Day keys: s sessions started, i inputs, a active ms, o tokens out, ti uncached
# input, cr cache read, cw cache write, c est. cost, u unpriced flag, m cost by
# model, p cost by project (index only). Idle days are null.
USAGE_METRICS = (("cost", "est. API cost"), ("tok", "tokens out"),
                 ("act", "agent active time"))
USAGE_PRESETS = (7, 30, 90)
USAGE_INTERVALS = ("auto", "hour", "day", "week")
USAGE_MAX_HOURLY_DAYS = 31  # hourly bars need a window of a month or less
USAGE_AUTO_HOUR_MAX_DAYS = USAGE_MAX_HOURLY_DAYS   # "auto": hourly up to a month ...
USAGE_AUTO_DAY_MAX_DAYS = 182   # ... daily up to 26 weeks, weekly beyond
USAGE_MINI_MAX_BARS = 360   # the static minimap folds a longer history into bins
USAGE_MIN_ACTIVE_DAYS = 2   # a one-day history has nothing to explore


def _day_ordinal(ts):
    d = parse_ts(ts)
    return d.date().toordinal() if d else None


def _fmt_day(o):
    return date.fromordinal(o).strftime("%b %-d, %Y")


def _fmt_day_short(o):
    return date.fromordinal(o).strftime("%b %-d")


def _nice(v):
    """Smallest 1, 2, 4, 5, or 10 × 10^k at or above v: the chart's top
    gridline. Halving any of these gives a round number for the middle line."""
    if v <= 0:
        return 1
    e = 10.0 ** math.floor(math.log10(v))
    for m in (1, 2, 4, 5, 10):
        r = m * e
        if r >= v - 1e-9:
            return int(r) if r >= 1 else r
    return int(10 * e)


def _half(v):
    h = v / 2
    return int(h) if float(h).is_integer() else h


def _axis_cost(v):
    return f"${v:.2f}" if v < 1 else f"${round(v):,}"


def _fmt_money(d):
    """Dollar amount with cents below $100; per-day and per-unit figures need them."""
    return f"${d:,.0f}" if d >= 100 else f"${d:.2f}"


def _day_hour(ts):
    """``(day ordinal, hour)`` of a timestamp in local time, or None."""
    d = parse_ts(ts)
    return (d.date().toordinal(), d.hour) if d else None


def _hour_slices(ts, duration_ms):
    """``[(day ordinal, hour, fraction)]`` for the local hours an entry's
    activity spans, from its timestamp for its active duration."""
    start = parse_ts(ts)
    if not duration_ms or duration_ms <= 0:
        return [(start.date().toordinal(), start.hour, 1.0)]
    end = start + timedelta(milliseconds=duration_ms)
    out, t = [], start
    while t < end:
        boundary = t.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        seg_end = min(boundary, end)
        out.append((t.date().toordinal(), t.hour,
                    (seg_end - t).total_seconds() * 1000 / duration_ms))
        t = seg_end
    return out


def _spread(total, fractions):
    """Split an integer total across slices by fraction, keeping the exact sum."""
    out, assigned, cum = [], 0, 0.0
    for f in fractions:
        cum += f
        target = round(total * cum)
        out.append(target - assigned)
        assigned = target
    if out:
        out[-1] += total - assigned
    return out


def _finish_bucket(b, keys):
    """Price a bucket's tokens and drop its zero fields for the JSON series.

    ``m`` and ``p`` map a model or project to ``[cost, tokens out, active
    ms]`` so the readout can split any plotted metric.
    """
    _, total, unpriced = pricing.cost_breakdown(b["by_model"])
    d = {k: b[k] for k in keys if b[k]}
    if total:
        d["c"] = round(total, 4)
    if unpriced:
        d["u"] = 1
    by_model = {}
    for mid, tk in b["by_model"].items():
        row = [round(pricing.estimate_cost({mid: tk}), 4), tk.get("out", 0), b["ma"].get(mid, 0)]
        if any(row):
            by_model[mid] = row
    if by_model:
        d["m"] = by_model
    if b["p"]:
        d["p"] = {k: [round(v[0], 4), v[1], v[2]] for k, v in b["p"].items()}
    return d


def _daily_series(entries, refreshed):
    """Per-day and per-hour usage for one or many timelines.

    ``entries`` is ``[(label, timeline)]``; ``label`` names the project in the
    per-bucket ``p`` split and is only used when there is more than one entry.
    Returns ``{"first": ISO date, "days": [...], "hours": [...]}`` or None
    without any dated milestone. ``days`` is dense from the first activity
    through the refresh day. ``hours`` is sparse: ``[hour index, bucket]``
    pairs for active hours only, where the index counts hours from midnight
    of the first day, without the cache fields only the daily tiles need.

    An entry's tokens, cost, and active time are spread over the hours from
    its timestamp for its active duration, in proportion to the time in each
    hour, so a long task fills the hours it ran rather than the hour it
    started. Inputs count in the starting hour, and a session counts in the
    day and hour of its first milestone.
    """
    buckets = {}   # (day ordinal, hour or None) -> accumulator

    def bucket(o, h):
        return buckets.setdefault((o, h), {
            "s": 0, "i": 0, "a": 0, "o": 0, "ti": 0, "cr": 0, "cw": 0,
            "by_model": {}, "ma": {}, "p": {}})

    multi = len(entries) > 1
    for label, tl in entries:
        first_seen = {}
        for m in tl["milestones"]:
            when = _day_hour(m["ts"])
            if when is None:
                continue
            sid = m["session"]
            if sid not in first_seen or when < first_seen[sid]:
                first_seen[sid] = when
            a = m["activity"]
            # Active time is recorded per entry, not per model: attribute it to
            # the entry's most-used model, as the timeline's model chip does.
            dominant = max(a["models"], key=a["models"].get) if a.get("models") else None
            slices = _hour_slices(m["ts"], a["duration_ms"])
            fractions = [f for _, _, f in slices]
            flat = {k: _spread(a[src], fractions) for k, src in (
                ("a", "duration_ms"), ("o", "tokens_out"), ("ti", "tokens_in"),
                ("cr", "cache_read"), ("cw", "cache_create"))}
            per_model = {
                mid: {k: _spread(v, fractions) for k, v in tk.items()}
                for mid, tk in (a.get("tokens_by_model") or {}).items()}
            for n, (o, h, _) in enumerate(slices):
                slice_models = {mid: {k: v[n] for k, v in tk.items()}
                                for mid, tk in per_model.items()}
                cost = pricing.estimate_cost(slice_models)
                for b in (bucket(o, None), bucket(o, h)):
                    if n == 0 and m["kind"] in ("prompt", "command", "recovered"):
                        b["i"] += 1
                    for k, values in flat.items():
                        b[k] += values[n]
                    merge_token_models(b["by_model"], slice_models)
                    if dominant and flat["a"][n]:
                        b["ma"][dominant] = b["ma"].get(dominant, 0) + flat["a"][n]
                    if multi and (cost or flat["o"][n] or flat["a"][n]):
                        row = b["p"].setdefault(label, [0.0, 0, 0])
                        row[0] += cost
                        row[1] += flat["o"][n]
                        row[2] += flat["a"][n]
        for session in tl["sessions"]:
            if _is_automated_codex(session):
                continue
            when = first_seen.get(session["id"])
            if when is None:
                when = _day_hour(session.get("last_ts"))
            if when is not None:
                bucket(when[0], None)["s"] += 1
                bucket(*when)["s"] += 1
    if not buckets:
        return None
    first = min(o for o, _ in buckets)
    last = max(max(o for o, _ in buckets), refreshed.date().toordinal())
    days = [
        _finish_bucket(buckets[(o, None)], ("s", "i", "a", "o", "ti", "cr", "cw"))
        if (o, None) in buckets else None
        for o in range(first, last + 1)]
    hours = [
        [(o - first) * 24 + h, _finish_bucket(b, ("s", "i", "a", "o"))]
        for (o, h), b in sorted(kv for kv in buckets.items() if kv[0][1] is not None)]
    return {"first": date.fromordinal(first).isoformat(), "days": days, "hours": hours}


def _series_window(series, a=0, b=None):
    """Sum ``days[a..b]`` the way the client does for the selected window.

    Returns the flat sums plus ``days`` (active days), ``streak`` and
    ``streak_start`` (the longest run of consecutive active days and where it
    begins), ``busy`` and ``busy_v`` (the day with the most active time and
    that time), ``m`` (cost by model), and ``u`` (partial cost).
    """
    days = series["days"]
    if b is None:
        b = len(days) - 1
    t = {"s": 0, "i": 0, "a": 0, "o": 0, "ti": 0, "cr": 0, "cw": 0, "c": 0.0,
         "u": False, "days": 0, "streak": 0, "streak_start": None,
         "busy": None, "busy_v": 0, "m": {}}
    run = 0
    for i in range(a, b + 1):
        d = days[i]
        if d is None:
            run = 0
            continue
        run += 1
        if run > t["streak"]:
            t["streak"], t["streak_start"] = run, i - run + 1
        t["days"] += 1
        for k in ("s", "i", "a", "o", "ti", "cr", "cw"):
            t[k] += d.get(k, 0)
        t["c"] += d.get("c", 0)
        t["u"] = t["u"] or bool(d.get("u"))
        if d.get("a", 0) > t["busy_v"]:
            t["busy_v"], t["busy"] = d["a"], i
        for mid, row in (d.get("m") or {}).items():
            t["m"][mid] = t["m"].get(mid, 0) + row[0]
    return t


def _tile_details(series, w, a=0, b=None):
    """Detail line for each stat tile, as HTML, for the window ``a..b``."""
    days = series["days"]
    if b is None:
        b = len(days) - 1
    first_o = date.fromisoformat(series["first"]).toordinal()
    n = b - a + 1
    plus = "+" if w["u"] else ""
    prompt_tokens = w["ti"] + w["cr"] + w["cw"]
    if w["streak"]:
        s0 = first_o + w["streak_start"]
        streak = (_fmt_day_short(s0) if w["streak"] == 1
                  else f"{_fmt_day_short(s0)} – {_fmt_day_short(s0 + w['streak'] - 1)}")
    else:
        streak = "—"
    busiest = (f"{_fmt_dow(first_o + w['busy'])} · {_fmt_day_short(first_o + w['busy'])}"
               if w["busy"] is not None else "—")

    def rate(value, unit):
        return f"<b>{esc(value)}</b> {unit}"

    return {
        "sessions": rate(f"{w['i'] / w['s']:.1f}", "inputs per session") if w["s"] else "—",
        "inputs": rate(_fmt_money(w["c"] / w["i"]) + plus, "per input") if w["i"] else "—",
        "active": (rate(_fmt_money(w["c"] / (w["a"] / 3_600_000)) + plus, "per active hour")
                   if w["a"] else "—"),
        "tok": rate(fmt_num(round(w["o"] / w["i"])), "per input") if w["i"] else "—",
        "days": f"of {n} day{_s(n)}",
        "cost": (rate(f"{round(w['cr'] / prompt_tokens * 100)}%", "cache hit rate")
                 if prompt_tokens else "—"),
        "streak": esc(streak),
        "busiest": esc(busiest),
    }


def _auto_interval(n_days):
    """The interval the ``auto`` setting picks for a window of ``n_days``."""
    if n_days <= USAGE_AUTO_HOUR_MAX_DAYS:
        return "hour"
    if n_days <= USAGE_AUTO_DAY_MAX_DAYS:
        return "day"
    return "week"


def _merge_bucket(t, d):
    """Add bucket ``d`` into accumulator ``t`` (the client's ``merge``)."""
    for k in ("s", "i", "a", "o", "c"):
        t[k] = t.get(k, 0) + d.get(k, 0)
    if d.get("u"):
        t["u"] = 1
    for key in ("m", "p"):
        for name, row in (d.get(key) or {}).items():
            acc = t.setdefault(key, {}).setdefault(name, [0, 0, 0])
            for n, v in enumerate(row):
                acc[n] += v
    return t


def _buckets(series, interval, a=0, b=None):
    """Bars for days ``a..b`` at ``interval``: ``(key, bucket, from, to)``
    tuples where ``bucket`` is None when idle and ``from``/``to`` are day
    indices. Mirrors the client's ``bucketsFor``."""
    days = series["days"]
    if b is None:
        b = len(days) - 1
    if interval == "day":
        return [(i, days[i], i, i) for i in range(a, b + 1)]
    if interval == "hour":
        by_hour = dict(series["hours"])
        return [(h, by_hour.get(h), h // 24, h // 24) for h in range(a * 24, (b + 1) * 24)]
    first_o = date.fromisoformat(series["first"]).toordinal()
    out = []
    ws = a - date.fromordinal(first_o + a).weekday()   # Monday
    while ws <= b:
        lo, hi = max(a, ws), min(b, ws + 6)
        t = None
        for i in range(lo, hi + 1):
            if days[i]:
                t = _merge_bucket(t or {}, days[i])
        out.append((ws, t, lo, hi))
        ws += 7
    return out


def _binned(values, max_bins):
    """At most ``max_bins`` bars: a longer series folds into bins of its peak."""
    if len(values) <= max_bins:
        return values
    g = math.ceil(len(values) / max_bins)
    return [max(values[i:i + g]) for i in range(0, len(values), g)]


def _step_from(steps, n):
    """The first step that leaves at most eight ticks over ``n`` buckets."""
    for st in steps:
        if n / st <= 8:
            return st
    return steps[-1] * math.ceil(n / 8 / steps[-1])


def _ticks(series, interval, buckets):
    """``[(bucket index, label)]`` axis ticks, anchored to midnight for hours
    and to the window start otherwise. Mirrors the client's ``ticks``."""
    first_o = date.fromisoformat(series["first"]).toordinal()
    n = len(buckets)
    if interval == "hour":
        st = _step_from((1, 2, 3, 6, 12, 24, 48, 72, 168, 336), n)
        return [(j, _fmt_day_short(first_o + frm) if key % 24 == 0 else f"{key % 24:02d}:00")
                for j, (key, _, frm, _) in enumerate(buckets) if key % st == 0]
    if interval == "week":
        st = _step_from((1, 2, 4, 8, 13, 26, 52), n)
    else:
        st = _step_from((1, 2, 7, 14, 30, 60, 90, 180, 365), n)
    fmt = "%b %Y" if st >= (26 if interval == "week" else 60) else "%b %-d"
    return [(j, date.fromordinal(first_o + buckets[j][0]).strftime(fmt))
            for j in range(0, n, st)]


def _fmt_dow(o):
    return date.fromordinal(o).strftime("%a")


def _model_span(mid):
    fam = model_family(mid)
    cls = f"mdl fam-{fam}" if fam else "mdl"
    return f'<span class="{cls}">{esc(clean_model(mid))}</span>'


def _readout_head(series, interval, bucket):
    first_o = date.fromisoformat(series["first"]).toordinal()
    key, _, frm, to = bucket
    if interval == "hour":
        h = key % 24
        return (f"{_fmt_dow(first_o + frm)} · {_fmt_day(first_o + frm)} · "
                f"{h:02d}:00–{(h + 1) % 24:02d}:00")
    if interval == "week":
        return f"{_fmt_day_short(first_o + frm)} – {_fmt_day(first_o + to)}"
    return f"{_fmt_dow(first_o + key)} · {_fmt_day(first_o + key)}"


def _readout_html(series, interval, bucket):
    """The inspection row for one bar: its totals, then the top models and,
    on a multi-project series, the top projects by cost, each name followed
    by its value."""
    d = bucket[1]
    if not d:
        line1 = "no activity"
    else:
        plus = "+" if d.get("u") else ""
        n_in, n_s = d.get("i", 0), d.get("s", 0)
        line1 = (f'<b>{esc(_fmt_money(d.get("c", 0)))}{plus}</b> est. API cost · '
                 f'<b>{esc(fmt_num(d.get("o", 0)))}</b> tokens out · '
                 f'<b>{esc(fmt_dur(d.get("a", 0)))}</b> agent active · '
                 f'<b>{n_in}</b> input{_s(n_in)} · <b>{n_s}</b> session{_s(n_s)} started')
    lines = [f'<div class="uro-line" id="uRo1">{line1}</div>']
    multi = any("p" in x for x in series["days"] if x)
    for key, label, model in (("m", "models", True), ("p", "projects", False)):
        if key == "p" and not multi:
            continue
        items = sorted(((d or {}).get(key) or {}).items(), key=lambda kv: -kv[1][0])
        items = [(k, v) for k, v in items if v[0]]
        cells = [f'<span class="ui">{_model_span(k) if model else f"<span>{esc(k)}</span>"}'
                 f'<span>{esc(_fmt_money(v[0]))}</span></span>' for k, v in items[:3]]
        if len(items) > 3:
            cells.append(f"<span>+{len(items) - 3} more</span>")
        lines.append(f'<div class="uro-line" id="uRo{len(lines) + 1}">'
                     f'<span class="uro-k">{label}</span>{"".join(cells)}</div>')
    return (f'<div class="uro-head"><time id="uRoDate">{esc(_readout_head(series, interval, bucket))}</time>'
            '<button type="button" class="ubtn" id="uPin" disabled>unpin</button></div>'
            + "".join(lines))


def _bars_html(values, vmax, titles=None):
    """A ``.ubars`` grid: one bar per value, heights relative to ``vmax``."""
    n = len(values)
    cls = "ubars" + (" packed" if n > 240 else " dense" if n > 90 else "")
    bars = []
    for i, v in enumerate(values):
        h = v / vmax * 100 if vmax else 0
        tip = f' title="{esc(titles[i])}"' if titles else ""
        bars.append(f'<i style="height:{h:.2f}%"{tip}></i>')
    return (f'<div class="{cls}" style="grid-template-columns:repeat({n},1fr)">'
            f'{"".join(bars)}</div>')


def _usage_html(series):
    """The hero's usage explorer, rendered for the whole series with the cost
    metric at the interval ``auto`` picks for it; ``USAGE_JS`` takes over for
    windowing, other metrics and intervals, and hover, and mirrors the view
    in the URL's query string.
    """
    days = series["days"]
    if sum(1 for d in days if d is not None) < USAGE_MIN_ACTIVE_DAYS:
        return ""
    n = len(days)
    first_o = date.fromisoformat(series["first"]).toordinal()
    interval = _auto_interval(n)
    buckets = _buckets(series, interval)
    vals = [d.get("c", 0) if d else 0 for _, d, _, _ in buckets]
    top = _nice(max(vals))
    titles = [f'{_readout_head(series, interval, bk)} · '
              f'{_fmt_money(v) + " est. API cost" if bk[1] else "no activity"}'
              for bk, v in zip(buckets, vals)]
    metrics = "".join(
        f'<button type="button" class="metric{" on" if k == "cost" else ""}" '
        f'data-m="{k}">{esc(label)}</button>' for k, label in USAGE_METRICS)
    options = "".join(f'<option value="{p}">{p}d</option>' for p in USAGE_PRESETS)
    intervals = []
    for k in USAGE_INTERVALS:
        cls = "interval" + (" on" if k == "auto" else "") + (" eff" if k == interval else "")
        disabled = " disabled" if k == "hour" and n > USAGE_MAX_HOURLY_DAYS else ""
        title = ("hourly up to a month, daily up to 26 weeks, then weekly" if k == "auto"
                 else f"windows of {USAGE_MAX_HOURLY_DAYS} days or fewer" if k == "hour"
                 else f"one bar per {k}")
        intervals.append(f'<button type="button" class="{cls}" data-i="{k}"{disabled} '
                         f'title="{esc(title)}">{k}</button>')
    grid = "".join(f'<i style="left:{(j + 0.5) / len(buckets) * 100:.3f}%"></i>'
                   for j, _ in _ticks(series, interval, buckets))
    labels = "".join(
        f'<span style="left:{(j + 0.5) / len(buckets) * 100:.3f}%">{esc(label)}</span>'
        for j, label in _ticks(series, interval, buckets))
    last_active = max(j for j, bk in enumerate(buckets) if bk[1])
    mini_vals = _binned(vals, USAGE_MINI_MAX_BARS)   # the minimap shows the whole history
    iso_first, iso_last = series["first"], date.fromordinal(first_o + n - 1).isoformat()
    payload = json.dumps(series, separators=(",", ":")).replace("</", "<\\/")
    return (
        '<section class="usage" id="usage">'
        '<div class="uhead">'
        f'<div class="seg umet" role="group" aria-label="chart metric">{metrics}</div>'
        '<div class="uctl">'
        f'<div class="seg uint" role="group" aria-label="plotting interval">{"".join(intervals)}</div>'
        '<div class="urange">'
        f'<input type="date" id="uFrom" aria-label="window start" value="{iso_first}" '
        f'min="{iso_first}" max="{iso_last}"><span class="usep">&ndash;</span>'
        f'<input type="date" id="uTo" aria-label="window end" value="{iso_last}" '
        f'min="{iso_first}" max="{iso_last}">'
        f'<span class="udays" id="uDays">&middot; {n} day{_s(n)}</span></div>'
        f'<select class="uwin" id="uWin" aria-label="time range">{options}'
        '<option value="all" selected>all</option>'
        '<option value="custom" disabled hidden>custom</option></select></div></div>'
        f'<div class="uro" id="uRo">{_readout_html(series, interval, buckets[last_active])}</div>'
        '<div class="uchart">'
        f'<span class="uy" id="uYTop" style="bottom:100%">{esc(_axis_cost(top))}</span>'
        f'<span class="uy" id="uYMid" style="bottom:50%">{esc(_axis_cost(_half(top)))}</span>'
        '<div class="uplot" id="uPlot" tabindex="0" '
        'aria-label="usage over time; arrow keys step through bars">'
        f'<div class="uvg" id="uVg">{grid}</div><i class="ugl" style="bottom:50%"></i>'
        f'{_bars_html(vals, top, titles)}'
        '<i class="uhov" id="uHov" hidden></i><i class="usel" id="uSel" hidden></i>'
        '</div></div>'
        f'<div class="uaxis" id="uAxis">{labels}</div>'
        f'<div class="umini" id="uMini">{_bars_html(mini_vals, max(mini_vals))}'
        '<div class="brush" id="uBrush" style="left:0%;width:100%">'
        '<b class="grip l"></b><b class="grip r"></b></div></div>'
        f'<div class="uminiaxis"><span>{esc(_fmt_day(first_o))}</span>'
        f'<span>{esc(_fmt_day(first_o + n - 1))}</span></div>'
        '<p class="uhelp">Hover a bar to inspect it, click to pin it. Drag on the chart '
        'to zoom in, or drag the minimap window to move or resize it.</p>'
        f'<script type="application/json" id="usageData">{payload}</script>'
        '</section>')


def _session_tools(sessions):
    """Sorted set of the CLI tools that produced these sessions."""
    return sorted({s["tool"] for s in sessions})


def _is_codex_exec(session):
    return session.get("originator") == "codex_exec"


def _is_automated_codex(session):
    return _is_codex_exec(session) or session.get("is_subagent", False)


def _input_count(stats):
    """Return prompt, command, and recovered-prompt inputs."""
    return stats["prompts"] + stats["commands"] + stats.get("recovered_prompts", 0)


def _timeline_repository(tl):
    """Dominant Git remote recorded by a Codex timeline, if any."""
    urls = [s.get("repository_url") for s in tl["sessions"] if s.get("repository_url")]
    return Counter(urls).most_common(1)[0][0] if urls else None


def _group_codex_timelines(timelines):
    """Group automated working directories under their interactive checkout.

    When an automated timeline uses a different working directory from the
    interactive checkout, grouping by cwd alone can split one repository across
    project cards. A repository's canonical path comes from an interactive
    timeline; automated timelines with the same recorded remote are assigned to it.
    """
    canonical = {}
    for tl in timelines:
        if not any(not _is_automated_codex(s) for s in tl["sessions"]):
            continue
        repository = _timeline_repository(tl)
        if not repository:
            continue
        repo_name = os.path.basename(repository.rstrip("/")).removesuffix(".git")
        path = tl["project_path"].rstrip("/")
        rank = (os.path.basename(path) != repo_name, path.startswith("/tmp/"),
                len(path), path)
        if repository not in canonical or rank < canonical[repository][0]:
            canonical[repository] = (rank, path)

    grouped = {}
    for tl in timelines:
        path = tl["project_path"].rstrip("/")
        repository = _timeline_repository(tl)
        if (tl["sessions"] and all(_is_automated_codex(s) for s in tl["sessions"])
                and repository in canonical):
            path = canonical[repository][1]
        grouped.setdefault(path, []).append(tl)
    return grouped


# --------------------------------------------------------------- rendering -- #
# Design language: two voices on a time spine. Everything the human typed is
# serif with a session-colored square marker; machine activity is mono inside
# recessed readout panels, with steel-colored accents. Colors run on three axes
# kept distinct: vendor (--claude orange / --codex green, semantic & reserved),
# session identity (--s1..s8, an 8-hue cycle avoiding the vendor hues), and
# voice (--human amber / --machine steel). Bars validated for CVD + contrast.
CSS = """
:root{
  --bg:#15171b; --panel:#1b1e25; --panel2:#232730; --line:#2a2f39; --spine:#333a46;
  --ink:#e9e6df; --dim:#9aa1ac; --faint:#6b727e;
  --human:#e3b25c; --machine:#8fc1e0; --bar:#468cc6;
  /* session-identity hue cycle */
  --s1:#468cc6; --s2:#9d7cc9; --s3:#c26787; --s4:#4fb0a4;
  --s5:#cf83c0; --s6:#6f86d8; --s7:#b95c74; --s8:#57c0d0;
  --claude:#d98a5c; --codex:#57b08a;
  --mono:ui-monospace,"Cascadia Code","SF Mono",Menlo,Consolas,"DejaVu Sans Mono",monospace;
  --serif:"Iowan Old Style","Palatino Linotype",Palatino,"Book Antiqua",Georgia,"Times New Roman",serif;
  color-scheme:dark;
}
@media (prefers-color-scheme:light){
  :root{--bg:#f5f4f0;--panel:#fbfaf7;--panel2:#edece7;--line:#dcdad2;--spine:#c9c7bf;
    --ink:#26282d;--dim:#5b616c;--faint:#8b9098;
    --human:#8a5c0a;--machine:#0d608f;--bar:#0f6fa8;
    --s1:#0f6fa8;--s2:#7a5aa8;--s3:#a8446b;--s4:#1f8478;
    --s5:#9c4c8f;--s6:#3f56a8;--s7:#9a3b55;--s8:#12889b;
    --claude:#b25f2c;--codex:#2f8a63;color-scheme:light}
}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{margin:0;background:var(--bg);color:var(--ink);font-family:var(--mono);
  font-size:13px;line-height:1.5;-webkit-font-smoothing:antialiased}
a{color:inherit}
button{font:inherit;color:inherit}
.wrap{max-width:880px;margin:0 auto;padding:0 24px}
.lbl{font-size:10px;letter-spacing:.14em;text-transform:uppercase;color:var(--faint)}

/* ---- sticky session stepper ---- */
.topbar{position:sticky;top:0;z-index:30;border-bottom:1px solid var(--line);
  background:color-mix(in srgb,var(--bg) 88%,transparent);backdrop-filter:blur(10px)}
.topbar .wrap{display:flex;flex-direction:column;gap:8px;
  padding-top:8px;padding-bottom:9px}
/* crumb (back-link + project name) on the left, session stepper on the right;
   they wrap as one group each when the bar gets narrow */
.tbtop{display:flex;align-items:center;justify-content:space-between;
  gap:7px 16px;flex-wrap:wrap;width:100%}
/* flex-basis 0 + min-width:0 so a long title doesn't force the stepper to wrap:
   the crumb shrinks and the description ellipsizes, keeping the stepper inline.
   Below ~360px even name + stepper stop fitting, so .tbtop stacks (see media). */
.crumb{display:flex;align-items:baseline;gap:9px;flex:1 1 0;min-width:0}
.crumb-sep{color:var(--faint);flex:0 0 auto}
/* name + title are buttons (in-page nav): strip the chrome, keep them as text */
.crumb-name,.crumb-desc{appearance:none;-webkit-appearance:none;border:0;background:none;
  padding:0;cursor:pointer;text-align:left}
.crumb-name:hover,.crumb-desc:hover{text-decoration:underline}
.crumb-name:focus-visible,.crumb-desc:focus-visible{outline:2px solid var(--machine);
  outline-offset:2px;border-radius:2px}
.crumb-name{font-family:var(--serif);font-size:15px;color:var(--ink);white-space:nowrap;
  flex:0 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis}
/* current session's title — secondary, so it grows into any spare room (flex-basis
   0, grow 1) and is the first thing to ellipsize as the bar narrows; the name only
   starts truncating once the description is gone */
.crumb-desc{flex:1 1 0;min-width:0;white-space:nowrap;overflow:hidden;
  text-overflow:ellipsis;color:var(--dim);font-size:12.5px}
.crumb-desc::before{content:"\\2022";margin:0 8px 0 1px;color:var(--faint)}
.crumb-desc:empty{display:none}
.backlink{display:inline-flex;align-items:center;gap:6px;flex:0 0 auto;
  font-size:10px;letter-spacing:.12em;text-transform:uppercase;color:var(--faint);
  text-decoration:none;white-space:nowrap;transition:color .12s}
.backlink .ar{font-size:12px;line-height:1;transition:transform .12s}
.backlink:hover,.backlink:focus-visible{color:var(--machine);outline:none}
.backlink:hover .ar,.backlink:focus-visible .ar{transform:translateX(-3px)}
/* session time-ribbon: dots on a real-date axis, full-width under the crumb row */
.ribbon{display:flex;align-items:center;gap:10px;width:100%}
.rdate{flex:0 0 auto;font-size:9px;color:var(--faint);white-space:nowrap}
.rtrack{position:relative;flex:1 1 auto;height:16px;cursor:pointer}
.rtrack::before{content:"";position:absolute;left:0;right:0;top:50%;height:1px;background:var(--line)}
/* faint per-entry dots: where a session's activity actually fell (spread + density) */
.etick{position:absolute;top:50%;width:3px;height:3px;border-radius:50%;
  transform:translate(-50%,-50%);background:var(--sc,var(--bar));opacity:.4;pointer-events:none}
.sdot{position:absolute;top:50%;width:8px;height:8px;padding:0;border-radius:50%;
  transform:translate(-50%,-50%);border:1px solid var(--bg);
  background:var(--sc,var(--bar));cursor:pointer;transition:transform .12s}
.sdot:hover{transform:translate(-50%,-50%) scale(1.4)}
.sdot:focus-visible{outline:2px solid var(--machine);outline-offset:2px}
/* neutral playhead marking the current reading position on the time axis */
.rhead{position:absolute;top:0;bottom:0;width:2px;transform:translateX(-50%);z-index:2;
  background:color-mix(in srgb,var(--ink) 55%,transparent);pointer-events:none;transition:left .12s ease-out}
.rhead::after{content:"";position:absolute;top:50%;left:50%;width:7px;height:7px;border-radius:50%;
  transform:translate(-50%,-50%);background:var(--ink);box-shadow:0 0 0 2px var(--bg)}

/* ---- vertical minimap (right rail): y = document position, so it maps 1:1
   to scrolling; session color blocks, per-entry activity ticks, and a moving
   viewport window. Drag/click anywhere on it to scrub. ---- */
.minimap{position:fixed;top:0;right:0;bottom:0;width:48px;z-index:20;
  display:flex;flex-direction:column;box-sizing:border-box;padding:6px 0;cursor:pointer;
  background:color-mix(in srgb,var(--bg) 80%,transparent);
  border-left:1px solid var(--line);backdrop-filter:blur(6px);
  -webkit-user-select:none;user-select:none;touch-action:none}
.mm-track{position:relative;flex:1 1 auto;margin:4px 0}
.mm-sess{position:absolute;left:0;right:0;opacity:.14}
.mm-tick{position:absolute;right:0;height:2px;border-radius:1px;opacity:.7}
.mm-view{position:absolute;left:0;right:0;min-height:6px;
  background:color-mix(in srgb,var(--ink) 12%,transparent);
  border-top:1.5px solid var(--ink);border-bottom:1.5px solid var(--ink)}
body{padding-right:0}
body.has-right-rail{padding-right:56px}
@media (max-width:759px){.minimap{display:none}body.has-right-rail{padding-right:0}}
.sessnav{display:flex;align-items:center;gap:8px;flex:0 0 auto;
  letter-spacing:.1em;text-transform:uppercase}
.sesscount{color:var(--dim);white-space:nowrap}
.sesscount b{color:var(--ink);font-weight:600}
/* glyph buttons: prev/next, the fold-all caret ahead of them, and the ? that opens
   the shortcuts dialog (the sticky bar's right end on a project page, the hero's
   top right on the index) */
.snav,.sfold,.shelp{width:19px;height:19px;display:inline-flex;align-items:center;justify-content:center;
  padding:0;border:1px solid var(--line);border-radius:4px;background:var(--panel);
  color:var(--dim);cursor:pointer;font-size:13px;line-height:1;
  transition:border-color .12s,color .12s}
.snav:hover,.sfold:hover,.shelp:hover{border-color:var(--machine);color:var(--ink)}
.snav:disabled{opacity:.35;cursor:default}
.snav:focus-visible,.sfold:focus-visible,.shelp:focus-visible{outline:2px solid var(--machine);outline-offset:2px}
.sessnav[hidden]{display:none}
.tbtop .shelp{flex:0 0 auto}
header.hero{position:relative}
.hero>.shelp{position:absolute;right:0;top:22px}
/* shortcuts dialog: ? or the ? button opens it; Esc, the close button, or a click
   on the backdrop closes it. Keys are keycaps, gestures are plain text. */
.help{padding:0;border:1px solid var(--line);border-radius:8px;background:var(--panel);
  color:var(--ink);width:min(560px,calc(100vw - 32px));max-height:calc(100vh - 32px);overflow:auto}
.help::backdrop{background:color-mix(in srgb,var(--bg) 72%,transparent)}
.help-box{padding:18px 22px 20px}
.help-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:4px}
.help h2{font-family:var(--serif);font-size:20px;font-weight:500;margin:0}
.help-close{appearance:none;-webkit-appearance:none;border:0;background:none;padding:2px 6px;
  border-radius:4px;color:var(--dim);font:inherit;font-size:18px;line-height:1;cursor:pointer}
.help-close:hover{color:var(--ink)}
.help-close:focus-visible{outline:2px solid var(--machine);outline-offset:2px}
.help h3{margin:14px 0 6px;font-size:9.5px;letter-spacing:.12em;text-transform:uppercase;
  color:var(--faint)}
.help dl{display:grid;grid-template-columns:minmax(0,200px) 1fr;gap:6px 16px;margin:0;
  font-size:12px;line-height:18px;color:var(--dim)}
.help dt{color:var(--ink)}
.help dd{margin:0}
.help kbd{display:inline-block;min-width:18px;padding:0 5px;border:1px solid var(--line);
  border-bottom-width:2px;border-radius:4px;background:var(--bg);font:inherit;font-size:11px;
  line-height:16px;text-align:center;color:var(--ink)}
.help .kglyph{color:var(--dim)}
.help-note{margin:0 0 8px;font-size:11px;line-height:16px;color:var(--dim)}
.help-foot{margin:16px 0 0;font-size:11px;color:var(--faint)}
@media (max-width:640px){.help dl{grid-template-columns:1fr;gap:2px 0}.help dd{margin-bottom:6px}}

/* ---- hero ---- */
header.hero{padding:16px 0 30px;border-bottom:1px solid var(--line)}
h1{font-family:var(--serif);font-size:38px;font-weight:500;letter-spacing:-.01em;
  margin:12px 0 6px}
.path{font-size:11.5px;color:var(--faint);word-break:break-all}
.range{font-size:12px;color:var(--dim);margin-top:12px}
.range b{color:var(--ink);font-weight:600}
.age{color:var(--faint);white-space:nowrap}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-top:26px}
.usage+.stats{margin-top:14px}
/* stat tiles; whole-pixel line heights keep the page below on the pixel grid */
.stat{min-width:0;padding:12px 14px 11px;background:var(--panel);border:1px solid var(--line);
  border-radius:8px}
.stat .l{line-height:14px}
.stat .n{margin-top:4px;font-size:21px;line-height:28px;font-weight:600;letter-spacing:-.02em}
.stat .d{margin-top:3px;font-size:11px;line-height:16px;color:var(--dim);white-space:nowrap;
  overflow:hidden;text-overflow:ellipsis}
.stat .d b{color:var(--ink);font-weight:600}
/* model turns, tool counts, and parser notes under the tiles, in the usage readout's
   form (a label column, then the items) rather than badges, which suggested something
   to click. Each item is its name followed by its count; items flow and wrap. */
.meta{margin-top:26px;font-size:11px;line-height:16px;color:var(--dim)}
.meta-line{display:flex;align-items:flex-start}
.meta-line+.meta-line{margin-top:4px}
.meta .uro-k{flex:0 0 64px}
.meta-cells{display:flex;flex-wrap:wrap;gap:0 18px;min-width:0}
.mc{display:inline-flex;gap:6px;white-space:nowrap}
.meta b{color:var(--ink);font-weight:600}
.meta .mdl,.meta .tn{color:var(--machine)}
.meta .mdl.fam-claude{color:var(--claude)}
.meta .mdl.fam-gpt{color:var(--codex)}
.tooltag{display:inline-block;font-size:9px;letter-spacing:.12em;text-transform:uppercase;
  padding:1px 6px;border:1px solid var(--line);border-radius:4px;color:var(--dim)}
.tooltag.t-claude{color:var(--claude);border-color:var(--claude)}
.tooltag.t-codex{color:var(--codex);border-color:var(--codex)}
.origintag{display:inline-block;margin-left:4px;font-size:9px;letter-spacing:.1em;
  text-transform:uppercase;color:var(--machine)}
.origintag.t-recovered{color:var(--human);border:1px solid color-mix(in srgb,var(--human) 35%,transparent);
  border-radius:3px;padding:1px 4px;letter-spacing:.08em}
.forktag{margin-left:7px;font-size:9px;color:var(--faint);text-decoration:none}
a.forktag:hover,a.forktag:focus-visible{color:var(--machine);text-decoration:underline;
  outline:none}
/* ---- log ---- */
.log{position:relative;padding:8px 0 60px}
.log::before{content:"";position:absolute;left:68px;top:24px;bottom:24px;width:1px;
  background:var(--line)}
.day{position:relative;display:flex;align-items:center;gap:14px;margin:34px 0 22px;z-index:1}
.day .lbl{background:var(--bg);padding-right:6px}
.day::after{content:"";flex:1;height:1px;background:var(--line)}
.sess{position:relative;margin:42px 0 26px;padding:20px 0 0 92px;z-index:1;
  border-top:1px solid var(--spine);scroll-margin-top:72px}
.sess .sn{color:var(--sc,var(--human))}
.sess .sw{display:inline-block;width:7px;height:7px;margin-right:8px;
  background:var(--sc,var(--bar))}
.sess a{text-decoration:none}
.sess a:hover .sn,.sess a:focus-visible .sn{text-decoration:underline}
.sess .stitle{display:block;font-size:15px;font-weight:600;margin-top:6px}
.sess .sstats{display:block;font-size:11px;color:var(--dim);margin-top:5px}
/* the header is the summary of its details.session-block: a click on it folds the
   session's entries. The caret is a clipped box the size of an entry mark, centered
   on the spine in the entry-mark column, and turns to point right when folded; the
   ::after box behind it breaks the spine for a few pixels above and below it. */
summary.sess{cursor:pointer;list-style:none;user-select:none}
summary.sess::-webkit-details-marker{display:none}
summary.sess::before{content:"";position:absolute;left:63px;top:27px;width:10px;height:8px;
  background:var(--faint);clip-path:polygon(0 0,100% 0,50% 100%);transition:transform .12s}
summary.sess::after{content:"";position:absolute;left:56px;top:22px;width:24px;height:18px;
  background:var(--bg);z-index:-1}
.session-block:not([open])>summary.sess::before{transform:rotate(-90deg)}
summary.sess:hover::before{background:var(--ink)}
summary.sess:focus-visible{outline:2px solid var(--machine);outline-offset:4px;border-radius:2px}
/* share: a bare glyph that downloads a standalone page of its session or entry. The
   symbol is a tray with an arrow rising from it, drawn as a mask so it takes the
   button's colour: the clock's faint grey at rest, the accent on hover. The 24px box is
   the hit target; only the 13px glyph shows. The session's flows after its badges on
   the label row; an entry's sits under its clock and shows only on the entry at the
   reading line (.current), on an entry under the pointer or with keyboard focus, and
   on every entry where there is no hover. */
.share{appearance:none;-webkit-appearance:none;width:24px;height:24px;display:inline-flex;
  align-items:center;justify-content:center;padding:0;border:0;border-radius:4px;
  background:none;color:var(--faint);cursor:pointer;transition:color .12s,opacity .12s}
.share::before{content:"";width:13px;height:13px;background:currentColor;
  -webkit-mask:var(--share-icon) center/contain no-repeat;mask:var(--share-icon) center/contain no-repeat}
.share:hover{color:var(--machine)}
.share:focus-visible{outline:2px solid var(--machine);outline-offset:2px}
.share{--share-icon:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16' fill='none' stroke='%23000' stroke-width='1.7' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M3.5 8.5v4a1.5 1.5 0 0 0 1.5 1.5h6a1.5 1.5 0 0 0 1.5-1.5v-4'/%3E%3Cpath d='M8 10.5V2.5M5 5.5 8 2.5l3 3'/%3E%3C/svg%3E")}
/* negative margins keep the 24px box from growing the label row */
summary.sess .share{vertical-align:middle;margin:-4px 0 -4px 2px}
.entry .share{position:absolute;left:34px;top:19px;opacity:0}
.entry:hover .share,.entry.current .share,.entry .share:focus-visible{opacity:1}
@media (hover:none){.entry .share{opacity:1}}
.gapnote{padding-left:92px;margin:-8px 0 16px;font-size:10.5px;color:var(--faint);
  letter-spacing:.08em}
.entry{position:relative;padding:0 0 34px 92px;scroll-margin-top:72px}
.entry.quiet{padding-bottom:20px}
/* clickable timeline marker; the one at the reading position is ringed (JS .current) */
.emark{position:absolute;left:56px;top:0;width:24px;height:24px;z-index:1;
  cursor:pointer;display:block;text-decoration:none}
.emark::after{content:"";position:absolute;left:9px;top:7px;width:7px;height:7px;
  background:var(--sc,var(--human));transition:transform .12s,box-shadow .12s}
.entry.session .emark::after{background:var(--bg);border:1px solid var(--faint)}
.entry.subagent .emark::after{background:var(--machine);border-radius:2px}
.emark:hover::after{transform:scale(1.5)}
.emark:focus-visible{outline:2px solid var(--machine);outline-offset:2px}
.entry.current .emark::after{box-shadow:0 0 0 3px color-mix(in srgb,var(--sc,var(--human)) 40%,transparent)}
.entry.current .clock{color:var(--human)}
.clock{position:absolute;left:0;top:3px;width:52px;text-align:right;
  font-size:11px;color:var(--faint);text-decoration:none}
a.clock:hover,a.clock:focus-visible{color:var(--human);outline:none}
.ask{font-family:var(--serif);font-size:16.5px;line-height:1.55;max-width:62ch;
  white-space:pre-wrap;overflow-wrap:anywhere}
.terminal{font-family:var(--mono);font-size:12.5px;line-height:1.45;max-width:100%;
  white-space:nowrap;overflow-x:auto;color:var(--dim)}
.terminal .cmdname{color:var(--human)}
.terminal .term-sep{padding:0 8px;color:var(--faint)}
.terminal .term-out{color:var(--dim)}
.terminal .term-err{color:var(--human)}
.ask.clip{max-height:148px;overflow:hidden;cursor:pointer;
  -webkit-mask-image:linear-gradient(#000 64%,transparent);
  mask-image:linear-gradient(#000 64%,transparent)}
.ask .cmdname{font-family:var(--mono);font-size:13px;color:var(--human);
  padding-right:4px}
.ask-open{font-size:12px;color:var(--faint)}
.entry.subagent .ask{font-family:var(--mono);font-size:12px;color:var(--machine)}
.recovered-note{margin-top:7px;font-size:10.5px;color:var(--faint)}
.entry.recovered .emark::after{background:var(--bg);border:2px solid var(--human);
  border-radius:50%}

/* machine readout */
.ro{margin-top:12px;max-width:660px;background:var(--panel);border:1px solid var(--line);
  border-radius:8px;padding:10px 14px 11px;font-size:11.5px;color:var(--dim)}
.rostat{display:flex;flex-wrap:wrap;gap:4px 16px}
.rostat b{color:var(--ink);font-weight:600}
.rostat .mdl{color:var(--machine)}
.rostat .sub{color:var(--machine)}
.rostat .mdl.fam-claude{color:var(--claude)}
.rostat .mdl.fam-gpt{color:var(--codex)}
.rotools{margin-top:6px;display:flex;flex-wrap:wrap;gap:4px 14px}
.rotools .tools-label{color:var(--faint);letter-spacing:.04em}
.rotools .tn{color:var(--machine)}
.rotools .tool-count{color:var(--ink)}
details.more{margin-top:9px;border-top:1px dashed var(--line);padding-top:8px}
details.more>summary{cursor:pointer;font-size:10.5px;letter-spacing:.1em;
  text-transform:uppercase;color:var(--faint);list-style:none;user-select:none}
details.more>summary::-webkit-details-marker{display:none}
details.more>summary::before{content:"\\25B8  "}
details.more[open]>summary::before{content:"\\25BE  "}
details.more>summary:focus-visible{outline:2px solid var(--machine);outline-offset:2px}
.responses{margin-top:15px}
.detail-section+.detail-section{margin-top:14px;padding-top:12px;border-top:1px solid var(--line)}
.response-heading,.detail-heading{margin-bottom:10px;font-size:9.5px;letter-spacing:.14em;
  text-transform:uppercase;color:var(--faint)}
.response-item+.response-item{margin-top:9px}
.response-meta{margin-bottom:3px;font-size:10.5px;font-style:italic;
  letter-spacing:.02em;color:var(--faint)}
.response-text{font-family:var(--serif);font-size:12.5px;line-height:1.45;color:var(--dim)}
.gist{margin:10px 0;padding-left:12px;border-left:2px solid var(--bar);
  font-size:12px;color:var(--dim);white-space:pre-wrap}
.files{margin-top:10px}
.files .detail-heading{margin-bottom:5px}
.files code{display:block;font-size:11.5px;color:var(--ink);padding:1px 0}
.telog{margin-top:10px;display:grid;grid-template-columns:auto 1fr;gap:2px 14px;
  font-size:11px}
.telog .tn{color:var(--machine);white-space:nowrap}
.telog .tl{color:var(--dim);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}

footer{border-top:1px solid var(--line);margin-top:20px;padding:22px 0 70px;
  font-size:11px;color:var(--faint);text-align:center}
.pricing{margin-top:22px}
.pricing>summary{cursor:pointer;display:inline-flex;align-items:center;gap:7px;
  font-size:12px;font-weight:600;letter-spacing:.01em;color:var(--dim);
  padding:6px 12px;border:1px solid var(--line);border-radius:6px;
  background:var(--panel);list-style:none;
  transition:border-color .12s,color .12s,background .12s}
.pricing>summary::-webkit-details-marker{display:none}
.pricing>summary::before{content:"\\25B8";color:var(--faint);font-size:10px}
.pricing[open]>summary::before{content:"\\25BE"}
.pricing[open]>summary{color:var(--ink);border-color:var(--spine)}
.pricing>summary:hover{border-color:var(--machine);color:var(--ink);background:var(--panel2)}
.pricing>summary:focus-visible{outline:2px solid var(--machine);outline-offset:2px}
.pricing-body{max-width:660px;margin:14px 0 0;text-align:left;
  color:var(--dim);font-size:11.5px;line-height:1.55}
.pricing-body p{margin:0 0 9px}
.pricing-body ul.category-help{margin:0 0 9px;padding-left:18px}
.pricing-body li{padding-left:2px}
.pricing-body p.sh{margin:18px 0 6px}   /* section heading: air above, tight to its table */
.pricing-body code{font-size:11px;color:var(--ink)}
.pricing table{border-collapse:collapse;margin:0;font-variant-numeric:tabular-nums}
.pricing th,.pricing td{padding:3px 9px;text-align:right;
  border-top:1px solid var(--line);white-space:nowrap}
.pricing th:first-child,.pricing td:first-child{text-align:left}
.pricing thead th{color:var(--faint);font-weight:600;border-top:none;
  font-size:10px;letter-spacing:.03em}
/* fixed-column grid shared by the cost and token-count matrices; the rate
   table reuses their model and token-category widths but omits the total column */
.pricing table.grid{table-layout:fixed;width:auto}
.pricing table.grid col{width:90px}
.pricing table.grid col.cm{width:124px}
.pricing td.mdl{color:var(--machine)}
.pricing td.mdl.fam-claude{color:var(--claude)}
.pricing td.mdl.fam-gpt{color:var(--codex)}
.pricing tr.tot td{border-top:1px solid var(--spine);color:var(--ink);font-weight:600}
.pricing .tw{overflow-x:auto}
.pricing .excl{margin-top:12px;color:var(--faint)}

/* ---- usage explorer: a card with a toolbar, a day readout, a recessed daily
   plot, and a minimap brush. --ugut holds the y labels and aligns the plot
   and minimap columns. Line heights are whole pixels so the content below
   stays on the pixel grid the screenshot baselines were captured on. ---- */
.usage{--ugut:50px;margin-top:26px;padding:14px 18px 12px;background:var(--panel);
  border:1px solid var(--line);border-radius:8px}
.uhead{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;
  gap:8px 18px;min-height:22px}
/* the window itself is the range readout: two borderless date inputs */
.urange{display:flex;align-items:center;gap:4px;font-size:12px;line-height:22px;
  color:var(--faint);white-space:nowrap}
.urange input{font:inherit;font-size:12px;font-weight:600;height:22px;padding:0 3px;
  color:var(--ink);background:none;border:1px solid transparent;border-radius:4px;
  cursor:pointer;-webkit-appearance:none;appearance:none;
  transition:border-color .12s,background .12s}
.urange input::-webkit-calendar-picker-indicator{display:none}
.urange input:hover,.urange input:focus-visible{border-color:var(--line);
  background:var(--panel2);outline:none}
.udays{margin-left:4px}
.uctl{display:flex;flex-wrap:wrap;align-items:center;gap:8px 10px}
/* in auto mode the interval in effect is underlined beside the "auto" choice */
.seg button.eff{text-decoration:underline;text-underline-offset:3px;text-decoration-color:var(--machine)}
.seg{display:inline-flex;height:22px;border:1px solid var(--line);border-radius:5px;
  background:var(--panel);overflow:hidden}
.seg button{appearance:none;-webkit-appearance:none;border:0;border-left:1px solid var(--line);
  background:none;padding:0 9px;font-size:11px;color:var(--dim);cursor:pointer;
  white-space:nowrap;transition:color .12s,background .12s}
.seg button:first-child{border-left:0}
.seg button:hover{color:var(--ink)}
.seg button.on{background:var(--panel2);color:var(--ink)}
.seg button:disabled{opacity:.4;cursor:default;color:var(--dim)}
.seg button:focus-visible{outline:2px solid var(--machine);outline-offset:-2px}
select.uwin{font:inherit;font-size:11px;height:22px;padding:0 4px 0 8px;color:var(--dim);
  background:var(--panel);border:1px solid var(--line);border-radius:5px;cursor:pointer}
select.uwin:hover{color:var(--ink)}
select.uwin:focus-visible{outline:2px solid var(--machine);outline-offset:-1px}
/* day readout: the hovered day, or the pinned one, or the last active day */
.uro{margin-top:12px;padding:6px 0 8px;border-top:1px solid var(--line);
  border-bottom:1px solid var(--line);font-size:11px;line-height:16px;color:var(--dim)}
.uro-head{display:flex;justify-content:space-between;align-items:center;gap:8px;height:22px}
.uro-head time{font-size:12px;font-weight:600;color:var(--ink)}
.ubtn{font:inherit;font-size:10.5px;height:20px;padding:0 8px;color:var(--dim);
  background:var(--panel2);border:1px solid var(--line);border-radius:4px;cursor:pointer;
  transition:color .12s,border-color .12s}
.ubtn:disabled{opacity:.4;cursor:default}
.ubtn:not(:disabled):hover{color:var(--ink);border-color:var(--machine)}
.ubtn:focus-visible{outline:2px solid var(--machine);outline-offset:1px}
.uro-line{height:16px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.uro-k{display:inline-block;width:64px;font-size:9.5px;letter-spacing:.12em;
  text-transform:uppercase;color:var(--faint)}
/* an item is its name followed by its value; items flow along the line */
.ui{display:inline-flex;gap:6px;margin-right:18px;vertical-align:top;
  font-variant-numeric:tabular-nums;white-space:nowrap}
.uro b{color:var(--ink);font-weight:600}
.uro .mdl{color:var(--machine)}
.uro .mdl.fam-claude{color:var(--claude)}
.uro .mdl.fam-gpt{color:var(--codex)}
/* daily plot, recessed into the card */
.uchart{position:relative;height:150px;margin-top:14px;padding-left:var(--ugut)}
.uy{position:absolute;left:0;width:42px;text-align:right;font-size:10px;line-height:1;
  white-space:nowrap;color:var(--faint);transform:translateY(50%);font-variant-numeric:tabular-nums}
.uplot{position:relative;height:100%;background:var(--bg);border:1px solid var(--line);
  border-radius:4px;overflow:hidden;touch-action:none;-webkit-user-select:none;user-select:none}
.uplot:focus-visible{outline:2px solid var(--machine);outline-offset:2px}
.uvg i{position:absolute;top:0;bottom:0;width:1px;background:var(--line)}
.ugl{position:absolute;left:0;right:0;height:1px;background:var(--line)}
.ubars{position:absolute;inset:0;display:grid;align-items:end;gap:0 2px;padding:0 1px}
.ubars.dense{gap:0 1px}
.ubars.packed{gap:0}
.ubars i{display:block;width:100%;max-width:24px;justify-self:center;
  background:var(--bar);border-radius:2px 2px 0 0;transition:background .12s}
.ubars i.hot,.ubars i.pin{background:color-mix(in srgb,var(--bar) 55%,var(--ink))}
.uhov{position:absolute;top:0;bottom:0;pointer-events:none;
  background:color-mix(in srgb,var(--ink) 7%,transparent)}
.usel{position:absolute;top:0;bottom:0;pointer-events:none;
  background:color-mix(in srgb,var(--ink) 10%,transparent);
  border-left:1px solid var(--ink);border-right:1px solid var(--ink)}
.uhov[hidden],.usel[hidden]{display:none}
.uaxis{position:relative;height:16px;margin:5px 0 0 var(--ugut);font-size:10px;line-height:16px;
  color:var(--faint)}
.uaxis span{position:absolute;top:0;transform:translateX(-50%);white-space:nowrap}
/* minimap: the whole history, with the window shown above as a brush */
.umini{position:relative;height:36px;margin:12px 0 0 var(--ugut);background:var(--bg);
  border:1px solid var(--line);border-radius:4px;cursor:crosshair;touch-action:none;
  -webkit-user-select:none;user-select:none}
.umini .ubars{gap:0 1px}
.umini .ubars i{border-radius:0;opacity:.4}
.brush{position:absolute;top:-1px;bottom:-1px;box-sizing:border-box;cursor:grab;
  background:color-mix(in srgb,var(--bar) 16%,transparent);
  border-left:1.5px solid var(--ink);border-right:1.5px solid var(--ink)}
.brush .grip{position:absolute;top:50%;width:5px;height:14px;margin-top:-7px;border-radius:3px;
  background:var(--panel);border:1px solid var(--ink);cursor:ew-resize}
.brush .grip.l{left:-4px}
.brush .grip.r{right:-4px}
.uminiaxis{display:flex;justify-content:space-between;margin:4px 0 0 var(--ugut);
  font-size:10px;line-height:16px;color:var(--faint)}
.uhelp{margin:8px 0 0;font-size:10.5px;line-height:16px;color:var(--faint)}

@media (max-width:640px){
  .stats{grid-template-columns:repeat(2,1fr)}
  .stat .d{white-space:normal;min-height:32px}   /* two reserved lines, so a wrap moves nothing */
  h1{font-size:30px}
  .log::before,.emark{display:none}
  .entry,.sess,.gapnote{padding-left:0}
  summary.sess::before{position:static;display:inline-block;margin-right:8px}
  summary.sess::after{display:none}
  .entry .share{left:auto;right:-4px;top:-4px}   /* top right, on the clock's line */
  .clock{position:static;display:block;width:auto;text-align:left;margin-bottom:4px}
  .clock::before{content:"";display:inline-block;width:7px;height:7px;
    background:var(--sc,var(--human));margin-right:8px}
  .entry.session .clock::before{background:var(--bg);border:1px solid var(--faint)}
  .entry.subagent .clock::before{background:var(--machine);border-radius:2px}
  .entry.current .clock::before{box-shadow:0 0 0 3px color-mix(in srgb,var(--sc,var(--human)) 40%,transparent)}
}
/* on the narrowest phones (<=360px) the crumb + stepper stop fitting on one line
   even with the title and name ellipsized, so the sticky bar stacks them. The
   explicit width:100% is needed because align-items:stretch alone won't shrink the
   crumb (a flex container) below its content — width:100% gives it a definite size
   so its title ellipsizes to fit. */
@media (max-width:360px){
  .tbtop{flex-direction:column;align-items:stretch;gap:6px}
  .crumb{width:100%}
}
@media (prefers-reduced-motion:reduce){
  html{scroll-behavior:auto}
  .share{transition:none}
}
"""

JS = """
const smooth=matchMedia('(prefers-reduced-motion: reduce)').matches?'auto':'smooth';
const entries=[...document.querySelectorAll('.entry')];
const sessions=[...document.querySelectorAll('.sess')];
const docTop=el=>el.getBoundingClientRect().top+window.scrollY;
const docH=()=>document.documentElement.scrollHeight||1;

// ---- folded sessions. Each session is a details.session-block whose header is
// its summary. Closed content keeps a box in Chromium (it is
// content-visibility:hidden, not display:none), so scroll tracking and the minimap
// skip an entry by its block's open state, not by its geometry. Folds persist per
// page in localStorage, keyed by the source-backed session anchor, so they survive
// a regeneration. ----
const blocks=[...document.querySelectorAll('details.session-block')];
const blockOf=new WeakMap(entries.map(e=>[e,e.closest('details.session-block')]));
const shown=e=>{const b=blockOf.get(e);return !b||b.open;};
const anchorOf=b=>b.querySelector('.sess')?.id||'';
const FOLD_KEY='session-atlas:folded:'+location.pathname;
function writeFolds(){ try{
  const ids=blocks.filter(b=>!b.open).map(anchorOf);
  if(ids.length) localStorage.setItem(FOLD_KEY,JSON.stringify(ids)); else localStorage.removeItem(FOLD_KEY);
}catch(e){} }
function hashTarget(){ try{ return location.hash.length>1?document.getElementById(decodeURIComponent(location.hash.slice(1))):null; }
  catch(e){ return null; } }
{ let folded=[]; try{ folded=JSON.parse(localStorage.getItem(FOLD_KEY)||'[]'); }catch(e){}
  if(!Array.isArray(folded)) folded=[];
  // the session an entry fragment points into stays open, so the fragment can scroll to it
  const t=hashTarget(), keep=t&&!t.matches('.sess')?t.closest('details.session-block'):null;
  blocks.forEach(b=>{ if(b!==keep&&folded.includes(anchorOf(b))) b.open=false; }); }

// The current entry is the latest one whose top has crossed the reading line,
// defaulting to the first before any one crosses. It changes when the next
// entry crosses the line and drives the URL anchor and ribbon playhead.
const topbar=document.querySelector('.topbar');
const entrySM=entries.length?parseFloat(getComputedStyle(entries[0]).scrollMarginTop)||0:0;
const headOff=()=>topbar?topbar.getBoundingClientRect().bottom:entrySM;  // reading-line offset
function currentId(){
  const line=headOff()+1;
  let id=null;                                     // the first shown entry until one crosses the line
  for(const e of entries){ if(!shown(e)) continue;
    if(id===null||e.getBoundingClientRect().top<=line) id=e.id; else break; }
  return id;
}
let hashTimer=null;
function syncHash(id){
  if(!id) return;
  clearTimeout(hashTimer);
  hashTimer=setTimeout(()=>{ if(location.hash!=='#'+id) history.replaceState(null,'','#'+id); },200);
}

// ---- vertical minimap: y = document position, so it maps 1:1 to scrolling ----
const mm=document.getElementById('minimap');
const track=document.getElementById('mmtrack');
const rhead=document.getElementById('rhead');       // ribbon playhead (present iff a ribbon rendered)
const crumbDesc=document.getElementById('crumbDesc'); // sticky subtitle, tracks the current session's title
const crumbName=document.getElementById('crumbName'); // project name in the crumb (-> back to top)
let mmView=null, curSel=null;
function buildMap(){
  if(!track || !matchMedia('(min-width:760px)').matches) return;
  const H=docH();
  const root=getComputedStyle(document.documentElement);
  const col=['--s1','--s2','--s3','--s4','--s5','--s6','--s7','--s8'].map(v=>root.getPropertyValue(v).trim());
  const sessColor=n=>col[(n-1)%col.length];
  track.textContent='';
  sessions.forEach((s,i)=>{                       // one color block per session
    const n=Number(s.dataset.sessionIndex)||1;
    const top=docTop(s), bot=(i+1<sessions.length)?docTop(sessions[i+1]):H;
    const b=document.createElement('div'); b.className='mm-sess';
    b.style.top=(top/H*100)+'%'; b.style.height=Math.max(0,(bot-top)/H*100)+'%';
    b.style.background=sessColor(n); track.appendChild(b);
  });
  entries.forEach(e=>{                             // one tick per shown entry, length=work
    if(!shown(e)) return;
    const n=Number(e.dataset.sessionIndex)||1;
    const w=parseFloat(e.dataset.w)||0;
    const t=document.createElement('div'); t.className='mm-tick';
    t.style.top=(docTop(e)/H*100)+'%';
    t.style.width=(22+w*64).toFixed(0)+'%';
    t.style.background=sessColor(n); track.appendChild(t);
  });
  mmView=document.createElement('div'); mmView.className='mm-view'; track.appendChild(mmView);
  updateMap();
}
function updateMap(){
  if(mmView){
    const H=docH();
    mmView.style.top=(window.scrollY/H*100)+'%';
    mmView.style.height=(window.innerHeight/H*100)+'%';
  }
  const id=currentId();
  syncHash(id||sessions[posIdx()]?.id);    // every session folded: the hash names the header at the line
  if(id&&id!==curSel){                     // move the timeline marker highlight with scroll
    const prev=curSel&&document.getElementById(curSel); if(prev) prev.classList.remove('current');
    const el=document.getElementById(id);
    if(el){ el.classList.add('current');
      // glide the ribbon playhead to that input's spot on the time axis
      if(rhead&&el.dataset.rf) rhead.style.left=el.dataset.rf+'%'; }
    curSel=id;
  }
}
if(mm){
  let drag=false;
  const scrub=y=>{
    const r=track.getBoundingClientRect();
    if(!r.height) return;
    const f=Math.min(1,Math.max(0,(y-r.top)/r.height));
    const de=document.documentElement, prev=de.style.scrollBehavior;
    de.style.scrollBehavior='auto';                // instant while scrubbing
    window.scrollTo(0, f*docH()-window.innerHeight/2);
    de.style.scrollBehavior=prev;
  };
  mm.addEventListener('pointerdown',e=>{drag=true;mm.setPointerCapture(e.pointerId);scrub(e.clientY);});
  mm.addEventListener('pointermove',e=>{if(drag)scrub(e.clientY);});
  mm.addEventListener('pointerup',()=>{drag=false;});
  mm.addEventListener('pointercancel',()=>{drag=false;});
}

// ---- session tracker + smooth-scroll, shared by the ribbon, timeline anchors,
// the crumb, and the session stepper. `target` is the scrollY we're gliding to
// (or null); tracking it lets us pin the session counter until the glide lands.
// `curSessIdx` is the session at the reading line — the counter and crumb both
// read it, so they can't disagree. It lives at module scope (not in the stepper
// block) so the crumb still tracks correctly on single-session pages, where the
// stepper UI never renders. ----
const atBottom=()=>window.innerHeight+window.scrollY>=docH()-2;
const clampY=y=>Math.max(0,Math.min(docH()-window.innerHeight,y));
const sessCur=document.getElementById('sessCur');    // counter, present iff >1 session
const sessTotal=document.getElementById('sessTotal');
const sessNav=document.querySelector('.sessnav');
const snav=[...document.querySelectorAll('.snav')];  // prev/next, present iff >1 session
const dots=[...document.querySelectorAll('.sdot')]; // session starts
if(sessTotal) sessTotal.textContent=sessions.length;
if(sessNav) sessNav.hidden=sessions.length<2;
let target=null, settleT=0, curSessIdx=0, painted=-1;
function posIdx(){                          // session at the reading line (0 if only one)
  if(sessions.length<2) return 0;
  const landY=headOff()+10;
  let idx=0;
  for(let i=0;i<sessions.length;i++){ if(sessions[i].getBoundingClientRect().top<=landY) idx=i; else break; }
  if(atBottom()){ for(let i=sessions.length-1;i>idx;i--){ if(sessions[i].getBoundingClientRect().top<window.innerHeight){idx=i;break;} } }
  return idx;
}
function paint(){ if(curSessIdx===painted) return; painted=curSessIdx;   // skip redundant DOM writes on unchanged scroll frames
  if(sessCur) sessCur.textContent=curSessIdx+1;
  if(crumbDesc) crumbDesc.textContent=sessions[curSessIdx]?.dataset.t||'';
  snav.forEach(b=>{const d=+b.dataset.d;b.disabled=(d<0&&curSessIdx===0)||(d>0&&curSessIdx===sessions.length-1);}); }
function settle(){clearTimeout(settleT);   // fallback if the scroll settles short or never fires
  settleT=setTimeout(()=>{target=null;resync();},250);}
function resync(){
  // While gliding to a clicked target, keep the counter pinned until we arrive.
  // A fixed timer released mid-scroll on long jumps, snapping the counter back.
  if(target!==null){
    if(Math.abs(window.scrollY-target)<=2||atBottom()){target=null;clearTimeout(settleT);}
    else{settle();return;}
  }
  curSessIdx=posIdx(); paint();
}
function glideTo(y){target=clampY(y);settle();window.scrollTo({top:target,behavior:smooth});}
function scrollToY(y,n){                    // glide to y and pin the counter to session n until we land
  curSessIdx=Math.min(sessions.length-1,Math.max(0,n)); paint(); glideTo(y); }
function jumpToEntry(el){                   // bring an entry to the reading line, pinning its session
  const block=el.closest('details.session-block'), header=block?.querySelector('.sess');
  if(block&&!block.open&&el!==header) block.open=true;   // unfold first, so the entry has a position to glide to
  scrollToY(docTop(el)-headOff(),Math.max(0,sessions.indexOf(header))); }
// ribbon: click the strip -> nearest entry in time (active for a single session too)
const rtrack=document.getElementById('rtrack');
if(rtrack) rtrack.addEventListener('click',e=>{
  if(e.target.closest('.sdot')) return;              // session-dot clicks do not scrub the strip
  const r=rtrack.getBoundingClientRect(); if(!r.width) return;
  const p=Math.max(0,Math.min(100,(e.clientX-r.left)/r.width*100));
  let best=null,bd=Infinity;
  for(const el of entries){ const d=Math.abs(parseFloat(el.dataset.rf)-p); if(d<bd){bd=d;best=el;} }
  if(best) jumpToEntry(best);
});
// timeline markers/clocks -> smooth navigation, or immediate with reduced motion; no flicker
const logEl=document.querySelector('.log');
if(logEl) logEl.addEventListener('click',e=>{
  const a=e.target.closest('a[href^="#"]'); if(!a) return;
  const el=document.getElementById(a.getAttribute('href').slice(1));
  if(!el) return;
  e.preventDefault(); history.replaceState(null,'','#'+el.id);
  jumpToEntry(el);
});
// crumb nav: project name -> top of page, session title -> top of the current session
if(crumbName) crumbName.addEventListener('click',()=>glideTo(0));
if(crumbDesc) crumbDesc.addEventListener('click',()=>{const el=sessions[curSessIdx];if(el)jumpToEntry(el);});

// ---- session stepper UI: prev/next buttons + j/k keys, only when there's >1 session ----
if(sessions.length>1&&sessCur){
  const goTo=n=>{ n=Math.min(sessions.length-1,Math.max(0,n));
    if(n!==curSessIdx) scrollToY(docTop(sessions[n])-headOff(),n); };
  const jump=d=>goTo(curSessIdx+d);
  snav.forEach(b=>b.addEventListener('click',()=>jump(+b.dataset.d)));
  dots.forEach(dt=>dt.addEventListener('click',()=>goTo(
    sessions.findIndex(session=>session.id===dt.dataset.s))));
  addEventListener('keydown',e=>{
    if(e.metaKey||e.ctrlKey||e.altKey||document.querySelector('dialog[open]')) return;
    const t=e.target; if(t&&(/^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName)||t.isContentEditable)) return;
    const k=e.key.toLowerCase();
    if(k==='j'){e.preventDefault();jump(1);} else if(k==='k'){e.preventDefault();jump(-1);}
  });
}
curSessIdx=posIdx(); paint();               // initial counter + crumb subtitle

// ---- fold-all button (in the stepper) and the toggle listener. A toggle moves
// everything below its header: the body ResizeObserver rebuilds the minimap, and
// the reading line is re-read here so the marker and the hash leave a hidden entry.
const sfold=document.getElementById('sfold');
function paintFold(){ if(!sfold) return;
  const any=blocks.some(b=>b.open), label=(any?'collapse':'expand')+' all sessions';
  sfold.textContent=any?'\\u25BE':'\\u25B8'; sfold.title=label; sfold.setAttribute('aria-label',label); }
if(sfold) sfold.addEventListener('click',()=>{
  // keep the current session's header where it is on screen, so the fold reads as
  // the other sessions closing around it rather than as the page jumping
  const anchor=sessions[curSessIdx], before=anchor?anchor.getBoundingClientRect().top:0;
  const open=!blocks.some(b=>b.open); blocks.forEach(b=>{b.open=open;});
  if(anchor){ const de=document.documentElement, prev=de.style.scrollBehavior;
    de.style.scrollBehavior='auto'; window.scrollBy(0,anchor.getBoundingClientRect().top-before);
    de.style.scrollBehavior=prev; }
});
// fold-all fires one toggle per session; the work runs once, on the next frame
let foldRaf=0;
addEventListener('toggle',e=>{ if(!(e.target instanceof Element)||!e.target.matches('details.session-block')) return;
  if(foldRaf) return;
  foldRaf=requestAnimationFrame(()=>{ foldRaf=0; writeFolds(); paintFold(); updateMap(); resync(); }); },true);
paintFold();

// ---- share: download a standalone page holding one session or one entry, for a
// trusted colleague. The clone keeps every field of the unit, including paths, cost,
// and times, and drops only what works nowhere but inside this page: the session id
// in the label and in a fork tag's tooltip, anchor ids and fragment links, the
// timeline mark, the scroll data attributes, these controls, and the prompt
// clipping, which the page script expands and the extract cannot. ----
const PROJECT=document.querySelector('.hero h1')?.textContent||document.title;
const PROJECT_PATH=document.querySelector('.hero .path')?.textContent||'';
const SHARED_ICON=document.body.dataset.sharedIcon||'';   // the favicon with its colours swapped
const PAGE_CSS=[...document.querySelectorAll('head style')].map(s=>s.textContent).join('\\n');
const p2=n=>String(n).padStart(2,'0');
const fmtDay=iso=>{const d=new Date(iso);return isNaN(d)?'':d.toLocaleDateString('en-US',{month:'short',day:'numeric',year:'numeric'});};
const stampOf=iso=>{const d=new Date(iso);return isNaN(d)?'entry':d.getFullYear()+'-'+p2(d.getMonth()+1)+'-'+p2(d.getDate())+'-'+p2(d.getHours())+p2(d.getMinutes());};
const slug=s=>s.toLowerCase().replace(/[^a-z0-9]+/g,'-').replace(/^-+|-+$/g,'').slice(0,40)||'project';
const escH=s=>s.replace(/[&<>"]/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[ch]));
function extractHtml(block,entry){
  const kids=[...block.children], c=block.cloneNode(true);
  const all=[...block.querySelectorAll('.entry')], at=entry?all.indexOf(entry):-1;
  if(entry){                                   // keep the entry and the day rule before it
    let day=null; for(let n=entry.previousElementSibling;n;n=n.previousElementSibling){ if(n.classList.contains('day')){day=n;break;} }
    const keep=new Set([entry,day].filter(Boolean).map(el=>kids.indexOf(el)));
    [...c.children].forEach((ch,i)=>{ if(ch.tagName!=='SUMMARY'&&!keep.has(i)) ch.remove(); }); }
  c.setAttribute('open','');
  const hdr=c.querySelector('summary.sess'), lbl=hdr?.querySelector('a.lbl');
  if(entry){                                   // say what the extract leaves out of the session
    const note=(n,w)=>{ const d=document.createElement('div'); d.className='gapnote';
      d.textContent='\u00b7 \u00b7 \u00b7 '+n+' '+w+' entr'+(n===1?'y':'ies')+' not shown'; return d; };
    const ce=c.querySelector('.entry'), before=at, after=all.length-at-1;
    if(before>0) ce.before(note(before,'earlier'));
    if(after>0) ce.after(note(after,'later'));
    const st=hdr?.querySelector('.sstats'); if(st) st.textContent='whole session: '+st.textContent; }
  if(lbl){ const s=document.createElement('span'); s.className='lbl';
    s.append(lbl.querySelector('.sw').cloneNode(true),lbl.querySelector('.sn').cloneNode(true)); lbl.replaceWith(s); }
  c.querySelectorAll('.forktag').forEach(t=>{ const s=document.createElement('span'); s.className='forktag';
    s.textContent=t.textContent.replace(/\\s[0-9a-f]{8}$/,' session'); t.replaceWith(s); });
  c.querySelectorAll('.share,.emark').forEach(el=>el.remove());
  c.querySelectorAll('a[href^="#"]').forEach(a=>{ const s=document.createElement('span'); s.className=a.className; s.textContent=a.textContent; a.replaceWith(s); });
  c.querySelectorAll('[id]').forEach(el=>el.removeAttribute('id'));
  c.querySelectorAll('*').forEach(el=>{ for(const a of [...el.attributes]) if(a.name.startsWith('data-')) el.removeAttribute(a.name); });
  c.querySelectorAll('.ask.clip').forEach(el=>{ el.classList.remove('clip'); el.removeAttribute('title'); });
  const es=entry?[entry]:[...block.querySelectorAll('.entry[data-ts]')];
  const first=es[0]?.dataset.ts, last=es[es.length-1]?.dataset.ts;
  const when=first?fmtDay(first)+(last&&fmtDay(last)!==fmtDay(first)?' \u2192 '+fmtDay(last):''):'';
  const sn=hdr?.querySelector('.sn')?.textContent||'session', unit=entry?'entry':'session';
  return '<!doctype html><html lang="en"><head><meta charset="utf-8">'
    +'<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="generator" content="session-atlas">'
    +(SHARED_ICON?'<link rel="icon" type="image/svg+xml" href="'+SHARED_ICON+'">':'')
    +'<title>'+escH(PROJECT)+' \u00b7 '+escH(sn)+'</title><style>'+PAGE_CSS+'</style></head><body class="shared"><div class="wrap">'
    +'<header class="hero"><h1>'+escH(PROJECT)+'</h1><div class="path">'+escH(PROJECT_PATH)+'</div>'
    +'<div class="range"><b>'+escH(sn)+'</b>'+(entry?' \u00b7 entry <b>'+(at+1)+' of '+all.length+'</b>':'')+(when?' \u00b7 <b>'+escH(when)+'</b>':'')+'</div></header>'
    +'<div class="log">'+c.outerHTML+'</div>'
    +'<footer>shared via <a href="https://github.com/vtjeng/session-atlas">session-atlas</a> \u00b7 '+unit+' exported '+escH(fmtDay(new Date().toISOString()))+'</footer></div></body></html>';
}
function shareBlock(block,entry){
  const n=(block.querySelector('summary.sess .sn')?.textContent||'').replace(/\\D/g,'')||'1';
  const name=slug(PROJECT)+'--session-'+n+(entry?'--'+stampOf(entry.dataset.ts||''):'')+'.html';
  const url=URL.createObjectURL(new Blob([extractHtml(block,entry)],{type:'text/html'}));
  const a=document.createElement('a'); a.href=url; a.download=name; document.body.appendChild(a); a.click(); a.remove();
  setTimeout(()=>URL.revokeObjectURL(url),1000);
}
if(logEl) logEl.addEventListener('click',e=>{
  const b=e.target.closest('.share'); if(!b) return;
  e.preventDefault();                          // the button, not the summary, is the click's target
  const block=b.closest('details.session-block'); if(!block) return;
  shareBlock(block,b.dataset.share==='entry'?b.closest('.entry'):null);
});
addEventListener('keydown',e=>{                 // s: share the session at the reading line
  if(e.metaKey||e.ctrlKey||e.altKey||document.querySelector('dialog[open]')) return;
  const t=e.target; if(t&&(/^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName)||t.isContentEditable)) return;
  if(e.key.toLowerCase()!=='s') return;
  const block=sessions[curSessIdx]?.closest('details.session-block'); if(block){ e.preventDefault(); shareBlock(block,null); }
});

// ---- shared scroll + layout listeners ----
let raf=0;
addEventListener('scroll',()=>{ if(raf) return; raf=requestAnimationFrame(()=>{raf=0;
  updateMap();
  resync();
});},{passive:true});
let rb=0;
const rebuild=()=>{clearTimeout(rb);rb=setTimeout(buildMap,120);};
if(window.ResizeObserver) new ResizeObserver(rebuild).observe(document.body);
addEventListener('resize',rebuild);
document.querySelectorAll('.ask.clip').forEach(el=>{
  el.title='show full prompt';
  el.addEventListener('click',()=>{el.classList.remove('clip');el.removeAttribute('title');});
});
buildMap();
"""


HELP_JS = """
// ---- shortcuts dialog: ? or the ? button toggles it; the browser closes it on Esc,
// and the close button or a click on the backdrop closes it too
const help=document.getElementById('help');
if(help&&help.showModal){
  const toggleHelp=()=>{ if(help.open) help.close(); else help.showModal(); };
  document.getElementById('shelp')?.addEventListener('click',toggleHelp);
  document.getElementById('helpClose')?.addEventListener('click',()=>help.close());
  help.addEventListener('click',e=>{ if(e.target===help) help.close(); });   // the backdrop
  addEventListener('keydown',e=>{
    if(e.metaKey||e.ctrlKey||e.altKey||e.key!=='?') return;
    const t=e.target; if(t&&(/^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName)||t.isContentEditable)) return;
    e.preventDefault(); toggleHelp(); });
}
"""


REFRESH_JS = """
const refreshEls=document.querySelectorAll('[data-refreshed-at]');
function refreshAge(ms){
  const sec=Math.max(0,Math.round(ms/1000));
  if(sec<45) return 'just now';
  if(sec<90) return '1 min ago';
  const min=Math.round(sec/60);
  if(min<60) return `${min} min ago`;
  const hr=Math.round(min/60);
  if(hr<24) return `${hr} hr${hr===1?'':'s'} ago`;
  const day=Math.round(hr/24);
  if(day<30) return `${day} day${day===1?'':'s'} ago`;
  const mo=Math.round(day/30);
  if(mo<12) return `${mo} mo ago`;
  const yr=Math.round(day/365);
  return `${yr} yr${yr===1?'':'s'} ago`;
}
function updateRefreshAge(){
  const now=Date.now();
  refreshEls.forEach(el=>{
    const t=Date.parse(el.dataset.refreshedAt);
    if(Number.isFinite(t)) el.textContent=`(${refreshAge(now-t)})`;
  });
}
if(refreshEls.length){
  updateRefreshAge();
  setInterval(updateRefreshAge,30000);
}
"""


# Usage explorer: re-buckets the embedded series for the selected window and
# interval and repaints the chart, brush, readout, and stat tiles. Number
# formats mirror fmt_num, fmt_dur, fmt_cost, and _fmt_money so a recomputed
# tile matches the server-rendered one for the same window.
USAGE_JS = """
(function(){
const box=document.getElementById('usage'); if(!box) return;
const D=JSON.parse(document.getElementById('usageData').textContent);
const days=D.days, N=days.length, H=new Map(D.hours||[]), MS=86400000;
const dayNum=iso=>{const [y,m,d]=iso.split('-').map(Number);return Math.round(Date.UTC(y,m-1,d)/MS);};
const D0=dayNum(D.first);
const MON=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
const DOW=['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
const dateOf=i=>new Date((D0+i)*MS);
const isoOf=i=>dateOf(i).toISOString().slice(0,10);
const fmtDate=i=>{const d=dateOf(i);return MON[d.getUTCMonth()]+' '+d.getUTCDate()+', '+d.getUTCFullYear();};
const fmtShort=i=>{const d=dateOf(i);return MON[d.getUTCMonth()]+' '+d.getUTCDate();};
const fmtMonYear=i=>{const d=dateOf(i);return MON[d.getUTCMonth()]+' '+d.getUTCFullYear();};
const fmtDow=i=>DOW[dateOf(i).getUTCDay()];
const pad=h=>String(h).padStart(2,'0');
const todayIdx=()=>{const t=new Date();return Math.round(Date.UTC(t.getFullYear(),t.getMonth(),t.getDate())/MS)-D0;};
const clamp=i=>Math.max(0,Math.min(N-1,i));
const s=n=>n===1?'':'s';
const DOT=' \\u00b7 ', DASH='\\u2014', RANGE=' \\u2013 ';
const loc=n=>Math.round(n).toLocaleString('en-US');
const fmtNum=n=>n>=1e9?(n/1e9).toFixed(1)+'B':n>=1e6?(n/1e6).toFixed(1)+'M':n>=1e3?(n/1e3).toFixed(1)+'k':String(n);
const fmtDur=ms=>{if(!ms)return DASH;const sec=ms/1000;if(sec<60)return Math.round(sec)+'s';
  const m=sec/60;if(m<60)return Math.round(m)+'m';const h=Math.floor(m/60);return h+'h '+Math.floor(m%60)+'m';};
const fmtCost=d=>!d?'$0':d<1?'<$1':'$'+loc(d);
const fmtMoney=d=>d>=100?'$'+loc(d):'$'+d.toFixed(2);
const axisCost=v=>v<1?'$'+v.toFixed(2):'$'+loc(v);
const axisDur=ms=>{const h=ms/36e5;return h>=1&&Number.isInteger(h)?h+'h':fmtDur(ms);};
const nice=v=>{if(v<=0)return 1;const e=Math.pow(10,Math.floor(Math.log10(v)));
  for(const m of [1,2,4,5,10]){if(m*e>=v-1e-9)return m*e;}return 10*e;};
const niceMs=ms=>{const min=ms/6e4;if(min<=60){for(const m of [1,2,4,10,20,40,60])if(m>=min-1e-9)return m*6e4;}
  return nice(min/60)*36e5;};
const cleanModel=m=>m.replace(/claude-/g,'');
const family=m=>{m=m.toLowerCase();if(/^(claude|opus|sonnet|haiku|fable)/.test(m))return 'claude';
  if(/^(gpt|chatgpt|codex|o1|o3|o4)/.test(m))return 'gpt';return '';};
// col: the column of a model's or project's [cost, tokens out, active ms] split
const METRIC={
  cost:{get:d=>d.c||0,nice:nice,axis:axisCost,col:0,fmt:fmtMoney},
  tok:{get:d=>d.o||0,nice:nice,axis:fmtNum,col:1,fmt:fmtNum},
  act:{get:d=>d.a||0,nice:niceMs,axis:axisDur,col:2,fmt:fmtDur}};
const MAX_HOURLY_DAYS=31, AUTO_HOUR_MAX_DAYS=MAX_HOURLY_DAYS, AUTO_DAY_MAX_DAYS=182;
const autoInterval=n=>n<=AUTO_HOUR_MAX_DAYS?'hour':n<=AUTO_DAY_MAX_DAYS?'day':'week';
const $=id=>document.getElementById(id);
const plot=$('uPlot'), mini=$('uMini'), brush=$('uBrush'), hov=$('uHov'), sel=$('uSel'), vg=$('uVg'), axis=$('uAxis');
const bars=plot.querySelector('.ubars'), mbars=mini.querySelector('.ubars');
const yTop=$('uYTop'), yMid=$('uYMid'), daysEl=$('uDays');
const fromIn=$('uFrom'), toIn=$('uTo'), winSel=$('uWin');
const roDate=$('uRoDate'), ro1=$('uRo1'), ro2=$('uRo2'), ro3=$('uRo3'), pinBtn=$('uPin');
const metrics=[...box.querySelectorAll('.metric')], intervals=[...box.querySelectorAll('.interval')];
const tiles={}; document.querySelectorAll('.stat[data-k]').forEach(el=>{tiles[el.dataset.k]=el;});
let metric='cost', interval='auto', A=0, B=N-1, hot=-1, pinned=-1, cur=[], eff='day';
const effective=()=>interval==='auto'?autoInterval(B-A+1):interval;
bars.querySelectorAll('i[title]').forEach(i=>i.removeAttribute('title'));   // the readout replaces native tooltips

// ---- buckets: {k: key in the interval's index space, d: summed fields or null, from, to: day indices}
const weekStart=i=>i-((dateOf(i).getUTCDay()+6)%7);               // Monday
const addRow=(into,k,row)=>{ const r=into[k]||(into[k]=[0,0,0]); row.forEach((v,i)=>{r[i]+=v;}); };
function merge(t,d){ t.s+=d.s||0; t.i+=d.i||0; t.a+=d.a||0; t.o+=d.o||0; t.c+=d.c||0; if(d.u)t.u=1;
  for(const k in d.m||{}) addRow(t.m||(t.m={}),k,d.m[k]); for(const k in d.p||{}) addRow(t.p||(t.p={}),k,d.p[k]); return t; }
function bucketsFor(iv,a,b){
  const out=[];
  if(iv==='day'){ for(let i=a;i<=b;i++) out.push({k:i,d:days[i],from:i,to:i}); }
  else if(iv==='hour'){ for(let h=a*24;h<(b+1)*24;h++) out.push({k:h,d:H.get(h)||null,from:Math.floor(h/24),to:Math.floor(h/24)}); }
  else { for(let ws=weekStart(a);ws<=b;ws+=7){ let t=null;
    for(let i=Math.max(a,ws);i<=Math.min(b,ws+6);i++) if(days[i]) t=merge(t||{s:0,i:0,a:0,o:0,c:0},days[i]);
    out.push({k:ws,d:t,from:Math.max(a,ws),to:Math.min(b,ws+6)}); } }
  return out;
}
// the y scale is locked to the whole history for the metric and interval, so
// a moving window never rescales the bars
const scaleCache={};
function scaleMax(){ const key=metric+'/'+eff; if(!(key in scaleCache)){ const M=METRIC[metric]; let v=0;
  bucketsFor(eff,0,N-1).forEach(x=>{ if(x.d){const y=M.get(x.d); if(y>v)v=y;} }); scaleCache[key]=M.nice(v); } return scaleCache[key]; }
const dens=n=>n>240?' packed':n>90?' dense':'';
function paintBars(el,values,vmax){
  const n=values.length; let h='';
  for(const v of values) h+='<i style="height:'+(vmax?v/vmax*100:0).toFixed(2)+'%"></i>';
  el.className='ubars'+dens(n); el.style.gridTemplateColumns='repeat('+n+',1fr)'; el.innerHTML=h;
}
// at most one bar per two pixels: wider histories fold into bins that show their peak
function binned(values,maxBins){ if(values.length<=maxBins) return values; const g=Math.ceil(values.length/maxBins), out=[];
  for(let i=0;i<values.length;i+=g) out.push(Math.max(...values.slice(i,i+g))); return out; }
function sumWin(a,b){
  const t={s:0,i:0,a:0,o:0,ti:0,cr:0,cw:0,c:0,u:false,days:0,streak:0,streakStart:-1,busy:-1,busyV:0,m:{}}; let run=0;
  for(let i=a;i<=b;i++){const d=days[i]; if(!d){run=0;continue;}
    run++; if(run>t.streak){t.streak=run;t.streakStart=i-run+1;} t.days++;
    t.s+=d.s||0; t.i+=d.i||0; t.a+=d.a||0; t.o+=d.o||0; t.ti+=d.ti||0; t.cr+=d.cr||0; t.cw+=d.cw||0; t.c+=d.c||0;
    if(d.u)t.u=true; if((d.a||0)>t.busyV){t.busyV=d.a;t.busy=i;}
    for(const k in (d.m||{}))t.m[k]=(t.m[k]||0)+d.m[k][0];}
  return t;
}
// ---- axis ticks: at most eight, anchored to midnight for hours and to the window start otherwise
const stepFrom=(steps,n)=>steps.find(st=>n/st<=8)||steps[steps.length-1]*Math.ceil(n/8/steps[steps.length-1]);
function ticks(){
  const n=cur.length, out=[];
  if(eff==='hour'){ const st=stepFrom([1,2,3,6,12,24,48,72,168,336],n);
    cur.forEach((x,j)=>{ if(x.k%st===0) out.push([j,x.k%24===0?fmtShort(x.from):pad(x.k%24)+':00']); }); }
  else if(eff==='week'){ const st=stepFrom([1,2,4,8,13,26,52],n);
    for(let j=0;j<n;j+=st) out.push([j,st>=26?fmtMonYear(cur[j].k):fmtShort(cur[j].k)]); }
  else { const st=stepFrom([1,2,7,14,30,60,90,180,365],n);
    for(let j=0;j<n;j+=st) out.push([j,st>=60?fmtMonYear(cur[j].k):fmtShort(cur[j].k)]); }
  return out;
}
function paintAxis(){
  const n=cur.length, t=ticks(); let g='', l='';
  t.forEach(([j])=>{const x=((j+0.5)/n*100).toFixed(3)+'%'; g+='<i style="left:'+x+'"></i>'; l+='<span style="left:'+x+'"></span>';});
  vg.innerHTML=g; axis.innerHTML=l; t.forEach(([,label],k)=>{axis.children[k].textContent=label;});
}
function el(tag,cls,text){const e=document.createElement(tag); if(cls)e.className=cls; if(text!=null)e.textContent=text; return e;}
function pairs(target,list){ list.forEach(([v,l],k)=>{ if(k) target.appendChild(document.createTextNode(DOT));
  target.appendChild(el('b',null,v)); target.appendChild(document.createTextNode(' '+l)); }); }
function setTile(k,n,label,detail){ const t=tiles[k]; if(!t) return;
  t.querySelector('.n').textContent=n; if(label!=null) t.querySelector('.l').textContent=label;
  const d=t.querySelector('.d'); d.textContent=''; if(typeof detail==='string') d.textContent=detail; else pairs(d,[detail]); }
function paintTiles(){
  const w=sumWin(A,B), n=B-A+1, plus=w.u?'+':'', pt=w.ti+w.cr+w.cw;
  setTile('sessions',fmtNum(w.s),'session'+s(w.s),w.s?[(w.i/w.s).toFixed(1),'inputs per session']:DASH);
  setTile('inputs',fmtNum(w.i),'input'+s(w.i),w.i?[fmtMoney(w.c/w.i)+plus,'per input']:DASH);
  setTile('active',fmtDur(w.a),null,w.a?[fmtMoney(w.c/(w.a/36e5))+plus,'per active hour']:DASH);
  setTile('tok',fmtNum(w.o),null,w.i?[fmtNum(Math.round(w.o/w.i)),'per input']:DASH);
  setTile('days',fmtNum(w.days),'day'+s(w.days)+' active','of '+n+' day'+s(n));
  setTile('cost',fmtCost(w.c)+plus,null,pt?[Math.round(w.cr/pt*100)+'%','cache hit rate']:DASH);
  if(tiles.cost) tiles.cost.title=Object.entries(w.m).sort((x,y)=>y[1]-x[1]).map(([k,v])=>cleanModel(k)+' '+fmtCost(v)).join(DOT);
  const st=w.streakStart, en=st+w.streak-1;
  setTile('streak',w.streak+' day'+s(w.streak),null,!w.streak?DASH:w.streak===1?fmtShort(st):fmtShort(st)+RANGE+fmtShort(en));
  setTile('busiest',w.busy>=0?fmtDur(w.busyV):DASH,null,w.busy>=0?fmtDow(w.busy)+DOT+fmtShort(w.busy):DASH);
}
// ---- readout: the hovered bucket, else the pinned one, else the window's last active bucket
function head(x){
  if(eff==='hour'){ const h=x.k%24; return fmtDow(x.from)+DOT+fmtDate(x.from)+DOT+pad(h)+':00'+'\\u2013'+pad((h+1)%24)+':00'; }
  if(eff==='week') return fmtShort(x.from)+RANGE+fmtDate(x.to);
  return fmtDow(x.k)+DOT+fmtDate(x.k);
}
// an item is its name followed by its value; items flow along the line
function split(target,obj,model){ const key=target.firstChild, M=METRIC[metric]; target.textContent=''; target.appendChild(key);
  const items=Object.entries(obj||{}).map(([k,row])=>[k,row[M.col]]).filter(([,v])=>v>0).sort((x,y)=>y[1]-x[1]);
  items.slice(0,3).forEach(([k,v])=>{ const cell=el('span','ui');
    cell.appendChild(el('span',model?'mdl fam-'+family(k):null,model?cleanModel(k):k));
    cell.appendChild(el('span',null,M.fmt(v))); target.appendChild(cell); });
  if(items.length>3) target.appendChild(el('span',null,'+'+(items.length-3)+' more')); }
function lastActive(){ for(let j=cur.length-1;j>=0;j--) if(cur[j].d) return j; return cur.length-1; }
function paintReadout(j){
  const x=cur[j], d=x.d; roDate.textContent=head(x); ro1.textContent='';
  if(!d) ro1.textContent='no activity';
  else pairs(ro1,[[fmtMoney(d.c||0)+(d.u?'+':''),'est. API cost'],[fmtNum(d.o||0),'tokens out'],[fmtDur(d.a||0),'agent active'],
    [String(d.i||0),'input'+s(d.i||0)],[String(d.s||0),'session'+s(d.s||0)+' started']]);
  if(ro2) split(ro2,d&&d.m,true); if(ro3) split(ro3,d&&d.p,false);
  pinBtn.disabled=pinned<0;
}
const pinIdx=()=>cur.findIndex(x=>x.k===pinned);
function mark(){ const j=pinIdx(); [...bars.children].forEach((b,i)=>b.classList.toggle('pin',i===j)); }
function setPin(key){ pinned=key; const j=pinIdx(); if(j<0) pinned=-1; mark(); paintReadout(j>=0?j:lastActive()); }
function showBucket(j){
  if(hot!==j){ if(hot>=0&&bars.children[hot]) bars.children[hot].classList.remove('hot'); hot=j; bars.children[j].classList.add('hot'); }
  const n=cur.length; hov.hidden=false; hov.style.left=(j/n*100)+'%'; hov.style.width=(100/n)+'%';
  paintReadout(j);
}
function leave(){ if(hot>=0&&bars.children[hot]) bars.children[hot].classList.remove('hot'); hot=-1; hov.hidden=true;
  const j=pinIdx(); paintReadout(j>=0?j:lastActive()); }

function presetOf(){                        // which preset the window equals, if any
  if(A===0&&B===N-1) return 'all';
  const t=clamp(todayIdx());
  for(const o of winSel.options){const p=+o.value; if(p&&B===t&&A===Math.max(0,t-p+1)) return o.value;}
  return null;
}
function paintIntervals(){ intervals.forEach(b=>{ b.classList.toggle('on',b.dataset.i===interval);
  b.classList.toggle('eff',interval==='auto'&&b.dataset.i===eff); }); }
function render(){
  const M=METRIC[metric], n=B-A+1;
  const hourly=intervals.find(b=>b.dataset.i==='hour'); if(hourly){ hourly.disabled=n>MAX_HOURLY_DAYS;
    if(hourly.disabled&&interval==='hour'){ interval='day'; pinned=-1; } }
  const was=eff; eff=effective(); if(eff!==was){ pinned=-1; renderMini(); } paintIntervals();
  cur=bucketsFor(eff,A,B); const top=scaleMax();
  paintBars(bars,cur.map(x=>x.d?M.get(x.d):0),top); hot=-1; hov.hidden=true;
  yTop.textContent=M.axis(top); yMid.textContent=M.axis(top/2);
  daysEl.textContent='\\u00b7 '+n+' day'+s(n);
  brush.style.left=(A/N*100)+'%'; brush.style.width=(n/N*100)+'%';
  winSel.value=presetOf()||'custom';
  fromIn.value=isoOf(A); toIn.value=isoOf(B);
  paintAxis(); paintTiles(); setPin(pinned);
}
// the minimap shows the whole history at the plotted interval, binned to the pixel grid
function renderMini(){ const M=METRIC[metric];
  const values=binned(bucketsFor(eff,0,N-1).map(x=>x.d?M.get(x.d):0),Math.max(1,Math.floor(mbars.getBoundingClientRect().width/2)));
  paintBars(mbars,values,Math.max(...values)); }
let miniTimer=0; addEventListener('resize',()=>{clearTimeout(miniTimer); miniTimer=setTimeout(renderMini,120);});
function setWin(a,b){ a=clamp(a); b=clamp(b); if(a>b)[a,b]=[b,a]; if(a===A&&b===B) return; A=a; B=b; render(); }
// ---- the view lives in the query string as separate fields, defaults omitted:
// ?range=30d or ?from=2026-03-01&to=2026-03-15, &metric=tok|act, &interval=hour|day|week.
// A browser that refuses to rewrite a file URL's query gets the same fields in the fragment.
function stateParams(){ const q=new URLSearchParams(), p=presetOf();
  if(p&&p!=='all') q.set('range',p+'d'); else if(!p){ q.set('from',isoOf(A)); q.set('to',isoOf(B)); }
  if(metric!=='cost') q.set('metric',metric); if(interval!=='auto') q.set('interval',interval); return q.toString(); }
function syncUrl(){ const q=stateParams(), want=location.pathname+(q?'?'+q:'')+location.hash;
  if(want===location.href.slice(location.origin.length)&&location.origin!=='null') return;
  try{ history.replaceState(null,'',want); }
  catch(e){ history.replaceState(null,'',location.pathname+location.search+(q?'#'+q:'')); } }
function applyPreset(p){ const t=clamp(todayIdx()); if(p==='all') setWin(0,N-1); else setWin(t-(+p)+1,t); }
function setMetric(k){ if(k===metric) return; metric=k;
  metrics.forEach(b=>b.classList.toggle('on',b.dataset.m===k)); renderMini(); render(); }
function setIntervalMode(k){ if(k===interval) return; interval=k; pinned=-1; render(); }
function readUrl(){
  let q=new URLSearchParams(location.search); if(![...q.keys()].length) q=new URLSearchParams(location.hash.slice(1));
  setMetric(METRIC[q.get('metric')]?q.get('metric'):'cost');
  setIntervalMode(/^(auto|hour|day|week)$/.test(q.get('interval')||'')?q.get('interval'):'auto');
  const pd=/^(\\d+)d$/.exec(q.get('range')||''), from=q.get('from'), to=q.get('to'), iso=/^\\d{4}-\\d{2}-\\d{2}$/;
  if(from&&to&&iso.test(from)&&iso.test(to)) setWin(dayNum(from)-D0,dayNum(to)-D0);
  else if(pd) applyPreset(pd[1]); else applyPreset('all');
}

// ---- controls
winSel.addEventListener('change',()=>{ if(winSel.value==='custom') return;
  applyPreset(winSel.value); winSel.value=presetOf()||'custom'; syncUrl(); });   // "7d" may already be "all"
metrics.forEach(b=>b.addEventListener('click',()=>{setMetric(b.dataset.m); syncUrl();}));
intervals.forEach(b=>b.addEventListener('click',()=>{ if(!b.disabled){setIntervalMode(b.dataset.i); syncUrl();} }));
// a from-date past the to-date pulls the to-date along (and the reverse), never swaps them
const onDate=e=>{ if(!fromIn.value||!toIn.value) return;
  let a=dayNum(fromIn.value)-D0, b=dayNum(toIn.value)-D0;
  if(a>b){ if(e.target===fromIn) b=a; else a=b; }
  setWin(a,b); syncUrl(); };
[fromIn,toIn].forEach(inp=>{ inp.addEventListener('change',onDate);
  inp.addEventListener('click',()=>{ try{inp.showPicker();}catch(e){} }); });
pinBtn.addEventListener('click',()=>setPin(-1));

// ---- minimap brush: drag inside to move, an edge to resize, outside to draw anew
let mode=null, x0=0, A0=0, B0=0;
const idxAt=(x,el)=>{const r=el.getBoundingClientRect();return clamp(Math.floor((x-r.left)/r.width*N));};
mini.addEventListener('pointerdown',e=>{
  const r=mbars.getBoundingClientRect(); if(!r.width) return;
  const lx=r.left+A/N*r.width, rx=r.left+(B+1)/N*r.width, x=e.clientX;
  // moving a brush that already spans everything is a no-op, so draw anew instead
  const whole=A===0&&B===N-1;
  mode=Math.abs(x-lx)<=7?'l':Math.abs(x-rx)<=7?'r':(x>lx&&x<rx&&!whole)?'m':'n';
  x0=x; A0=A; B0=B; mini.setPointerCapture(e.pointerId); e.preventDefault();
  if(mode==='n'){const i=idxAt(x,mbars); setWin(i,i);}
});
mini.addEventListener('pointermove',e=>{ if(!mode) return;
  const r=mbars.getBoundingClientRect(); if(!r.width) return;
  const i=idxAt(e.clientX,mbars);
  if(mode==='m'){ const span=B0-A0, a=Math.max(0,Math.min(N-1-span,A0+Math.round((e.clientX-x0)/r.width*N))); setWin(a,a+span); }
  else if(mode==='l') setWin(Math.min(i,B),B);
  else if(mode==='r') setWin(A,Math.max(i,A));
  else { const j=idxAt(x0,mbars); setWin(Math.min(i,j),Math.max(i,j)); }
});
const endBrush=()=>{ if(mode){mode=null; syncUrl();} };
mini.addEventListener('pointerup',endBrush); mini.addEventListener('pointercancel',endBrush);

// ---- main chart: hover to inspect a bucket, click to pin it, drag across buckets to zoom in
let drag=null;
const colAt=x=>{const r=bars.getBoundingClientRect(), n=cur.length; return Math.max(0,Math.min(n-1,Math.floor((x-r.left)/r.width*n)));};
plot.addEventListener('pointerdown',e=>{ drag={x0:e.clientX,moved:false}; plot.setPointerCapture(e.pointerId); });
plot.addEventListener('pointermove',e=>{
  const j=colAt(e.clientX);
  if(drag){ if(Math.abs(e.clientX-drag.x0)>4) drag.moved=true;
    if(drag.moved){ const n=cur.length, i=colAt(drag.x0), a=Math.min(i,j), b=Math.max(i,j);
      sel.hidden=false; sel.style.left=(a/n*100)+'%'; sel.style.width=((b-a+1)/n*100)+'%';
      if(hot>=0&&bars.children[hot]) bars.children[hot].classList.remove('hot'); hot=-1; hov.hidden=true; return; } }
  showBucket(j);
});
plot.addEventListener('pointerup',e=>{
  const j=colAt(e.clientX);
  if(drag&&drag.moved){ const i=colAt(drag.x0), a=cur[Math.min(i,j)].from, b=cur[Math.max(i,j)].to; drag=null; sel.hidden=true;
    setWin(a,b); syncUrl(); showBucket(colAt(e.clientX)); return; }   // the pointer is still over the (new) plot
  if(drag){ const key=cur[j].k; setPin(pinned===key?-1:key); }
  drag=null; sel.hidden=true;
});
plot.addEventListener('pointercancel',()=>{drag=null; sel.hidden=true;});
plot.addEventListener('pointerleave',()=>{ if(!drag) leave(); });
plot.addEventListener('keydown',e=>{
  const at=pinIdx(), c=at>=0?at:lastActive(); let j;
  if(e.key==='ArrowLeft') j=Math.max(0,c-1); else if(e.key==='ArrowRight') j=Math.min(cur.length-1,c+1);
  else if(e.key==='Home') j=0; else if(e.key==='End') j=cur.length-1;
  else if(e.key==='Escape') j=-1; else return;
  e.preventDefault(); setPin(j>=0?cur[j].k:-1);
});
eff=effective(); cur=bucketsFor(eff,A,B); paintIntervals(); renderMini();
readUrl();
})();
"""


def _terminal_ask(m):
    """Render Claude's bash input/output wrappers as one compact terminal line."""
    parts = m.get("terminal") or {}
    bits = []
    if parts.get("input"):
        bits.append(f'<span class="cmdname">{esc(parts["input"])}</span>')
    if parts.get("stdout"):
        if bits:
            bits.append('<span class="term-sep">&rarr;</span>')
        bits.append(f'<span class="term-out">{esc(parts["stdout"])}</span>')
    if parts.get("stderr"):
        if bits:
            bits.append('<span class="term-sep">&middot;</span>')
        bits.append(f'<span class="term-err">stderr: {esc(parts["stderr"])}</span>')
    if not bits:
        bits.append(esc(m.get("text") or "terminal output"))
    return f'<div class="ask terminal">{"".join(bits)}</div>'


def render(tl, home=None, refreshed_at=None):
    """``home``: href of the index page (e.g. ``../index.html``), or None when
    this project was rendered on its own and no index exists to link back to."""
    s = tl["stats"]
    ms = tl["milestones"]
    first, last = s["first_ts"], s["last_ts"]
    first_dt, last_dt = parse_ts(first), parse_ts(last)
    span_s = (last_dt - first_dt).total_seconds() if first_dt and last_dt else 1
    span_s = max(span_s, 1)

    def frac(ts):
        d = parse_ts(ts)
        if not d or not first_dt:
            return 0.0
        return max(0.0, min(1.0, (d - first_dt).total_seconds() / span_s))

    def rf(ts):
        # ribbon-relative percent (0.5..99.5) shared by the ticks, session dots
        # and per-entry data-rf so the scrubber and rail stay in lockstep
        return f"{0.5 + frac(ts) * 99:.2f}"

    # ---- aggregates the parser doesn't precompute
    days_active = {d.date() for m in ms if (d := parse_ts(m["ts"]))}
    sess_agg = {}
    for m in ms:
        a = m["activity"]
        g = sess_agg.setdefault(m["session"], {
            "prompts": 0, "commands": 0, "recovered": 0,
            "turns": 0, "active": 0, "tok": 0,
            "files": set(), "by_model": {}})
        if m["kind"] in ("prompt", "command"):
            g[m["kind"] + "s"] += 1
            if m["text"] and "first_text" not in g:
                g["first_text"] = m["text"]
        elif m["kind"] == "recovered":
            g["recovered"] += 1
            if m["text"] and "first_text" not in g:
                g["first_text"] = m["text"]
        g["turns"] += a["assistant_turns"]
        g["active"] += a["duration_ms"]
        g["tok"] += a["tokens_out"]
        g["files"].update(a["files"])
        merge_token_models(g["by_model"], a.get("tokens_by_model"))

    # session display title: Claude's session summary, else the first non-empty
    # prompt, command, or recovered prompt. Shared by the log header and sticky
    # crumb; each session header stores it in data-t for scroll tracking.
    sess_by_id = {x["id"]: x for x in tl["sessions"]}

    def session_title(sid):
        session = sess_by_id.get(sid) or {}
        t = session.get("title")
        if not t:
            ft = (sess_agg.get(sid, {}).get("first_text") or "").strip().replace("\n", " ")
            t = ft[:80] + "…" if len(ft) > 80 else ft
        if not t and session.get("is_subagent"):
            label = session.get("subagent_label")
            t = label or "subagent session"
        if not t:
            t = "codex exec session" if _is_codex_exec(session) else "untitled session"
        return t

    def origin_tag(session):
        badge = ""
        if session.get("is_history_only"):
            badge = ('<span class="origintag t-recovered" '
                     f'title="{esc(RECOVERED_PROMPT_EXPLANATION)}">'
                     'recovered</span>')
        elif session.get("is_subagent"):
            badge = ('<span class="origintag" '
                     'title="Delegated Codex subagent work; not a separate human conversation">'
                     'subagent</span>')
        elif _is_codex_exec(session):
            badge = ('<span class="origintag" '
                     'title="Non-interactive Codex task; not a separate human conversation">'
                     'codex exec</span>')
        parent = session.get("parent_session_id")
        relation = session.get("parent_relation")
        if not parent or not relation:
            return badge
        if parent in sess_idx:
            parent_num = sess_idx[parent]
            link = (f'<a class="forktag" href="#{session_anchors[parent]}"'
                    f' title="{esc(relation)} conversation {esc(parent[:8])}">'
                    f'{esc(relation)} session {parent_num:02d}</a>')
        else:
            link = (f'<span class="forktag" title="parent conversation is outside this page">'
                    f'{esc(relation)} {esc(parent[:8])}</span>')
        return badge + link

    real_models = list(s["models"])
    multi_model = len(real_models) > 1
    refreshed = refreshed_at or now_local()
    # ---- hero: the usage explorer and the cards read the same daily series
    series = _daily_series([(None, tl)], refreshed)
    window = _series_window(series) if series else None
    stat_cards = _summary_stat_cards(
        sessions=s["sessions"],
        inputs=_input_count(s),
        active_ms=s["active_ms"],
        tokens_out=s["tokens_out"],
        days_active=window["days"] if window else len(days_active),
        by_model=s.get("tokens_by_model"),
        series=series, window=window,
    )
    stats_html = _stat_cards_html(stat_cards)
    usage_html = _usage_html(series) if series else ""

    # models, tools, and parser notes as readout lines: a label, then each
    # name followed by its count
    def meta_line(label, cells, title=None):
        attr = f' title="{esc(title)}"' if title else ""
        return (f'<div class="meta-line"{attr}><span class="uro-k">{label}</span>'
                f'<span class="meta-cells">{"".join(cells)}</span></div>')

    meta_lines = []
    if real_models:
        cells = []
        for m in real_models:
            model_turns = s["models"][m]
            cells.append(f'<span class="mc">{_model_span(m)}<span>'
                         f'<b>&times;{model_turns}</b> turn{_s(model_turns)}</span></span>')
        meta_lines.append(meta_line(
            "models", cells,
            "Assistant turns attributed to this model; not sessions"))
    if s["tools"]:
        cells = [f'<span class="mc"><span class="tn">{esc(k)}</span>'
                 f'<span><b>&times;{v}</b></span></span>'
                 for k, v in list(s["tools"].items())[:6]]
        meta_lines.append(meta_line("tools", cells))
    diagnostic_count = len(tl.get("diagnostics") or [])
    if diagnostic_count:
        meta_lines.append(meta_line(
            "parser",
            [f'<span><b>{diagnostic_count}</b> skipped transcript '
             f'record{_s(diagnostic_count)}</span>'],
            "The parser skipped malformed or non-UTF-8 transcript records"))

    rendered_sessions = [session for session in tl["sessions"]
                         if session["id"] in sess_agg]
    total = len(rendered_sessions)

    range_html = ""
    if first:
        n_days = (last_dt.date() - first_dt.date()).days + 1 if first_dt and last_dt else 1
        range_html = (f'<b>{esc(fmt_date(first))}</b> &rarr; <b>{esc(fmt_date(last))}</b>'
                      f' &middot; {n_days} day{_s(n_days)}'
                      f' &middot; refreshed {refresh_stamp(refreshed, bold=True)}')

    # ---- navigation: source-backed opaque anchors, a sticky session stepper,
    # and the vertical minimap rail. Display numbers remain positional, but
    # links use session/source identity so adding a conversation cannot retarget
    # an existing fragment.
    sess_idx = {x["id"]: i + 1 for i, x in enumerate(rendered_sessions)}
    sess_tool = {x["id"]: x["tool"] for x in rendered_sessions}
    session_anchors = {
        sid: _stable_anchor("session", sess_tool.get(sid), sid)
        for sid in sess_idx
    }
    sess_automated = {x["id"]: _is_automated_codex(x) for x in rendered_sessions}
    entry_ids, anchor_counts, _seen, sess_first = [], Counter(), Counter(), {}
    for m in ms:
        sid = m["session"]
        _seen[sid] += 1
        source_id = m.get("source_id")
        if not source_id:
            source_id = "fallback:{}:{}:{}".format(
                m.get("kind", ""), m.get("ts", ""), m.get("text", ""))
        base = _stable_anchor("entry", sess_tool.get(sid), sid, source_id)
        occurrence = anchor_counts[base]
        anchor_counts[base] += 1
        entry_ids.append(base if occurrence == 0 else f"{base}-{occurrence + 1}")
        sess_first.setdefault(sid, m["ts"])   # session start, for the time ribbon
    # per-entry work magnitude (0..1, sqrt) sets minimap tick length
    vmax_w = max((mag(m) for m in ms), default=0)

    # ---- sticky top bar: a persistent crumb (back-link + project name + the
    # current session's title) that keeps context and a way home as the hero
    # scrolls away, over a time ribbon. The title tracks the session at the reading
    # line (updated by JS), so it isn't repeated inline under every input down the
    # log. The session stepper is added only when there's more than one session.
    back = (f'<a class="backlink" href="{esc(home)}">'
            f'<span class="ar" aria-hidden="true">&larr;</span> All projects</a>'
            f'<span class="crumb-sep">/</span>' if home else "")
    first_title = esc(session_title(ms[0]["session"])) if ms else ""
    # name and title double as in-page nav: name -> top of page, title -> the top
    # of the current session's section (both wired up in JS)
    crumb = (f'<div class="crumb">{back}'
             f'<button type="button" class="crumb-name" id="crumbName"'
             f' title="Back to top">{esc(tl["project_name"])}</button>'
             f'<button type="button" class="crumb-desc" id="crumbDesc"'
             f' title="Jump to this session">{first_title}</button></div>')

    stepper = ""
    if total > 1:
        stepper = ('<div class="sessnav">'
                   f'<button class="sfold" id="sfold" title="collapse all sessions"'
                   f' aria-label="collapse all sessions">&#9662;</button>'
                   f'<button class="snav" data-d="-1" title="previous session (k)"'
                   f' aria-label="previous session">&lsaquo;</button>'
                   f'<span class="sesscount">session <b id="sessCur">1</b> / '
                   f'<b id="sessTotal">{total}</b></span>'
                   f'<button class="snav" data-d="1" title="next session (j)"'
                   f' aria-label="next session">&rsaquo;</button></div>')

    # time ribbon: a faint dot per entry shows when activity actually fell — its
    # spread and density. It renders for a single session too, where it reads as a
    # scrubber of that session's timeline; clickable session-start dots are added
    # only when there's more than one session to tell apart.
    ribbon = ""
    if len(ms) >= 2:
        ticks = [f'<span class="etick" style="left:{rf(m["ts"])}%;'
                 f'{_sc_var(sess_idx.get(m["session"], 1))}"'
                 f'{" data-automated" if sess_automated.get(m["session"]) else ""}'
                 f'></span>' for m in ms]
        dots = []
        if total > 1:
            # one clickable dot per session start — shows how sessions cluster and
            # how far apart they are (the temporal relationship the rail can't show)
            for x in rendered_sessions:
                num = sess_idx[x["id"]]
                dots.append(
                    f'<button class="sdot" data-s="{session_anchors[x["id"]]}"'
                    f'{" data-automated" if sess_automated.get(x["id"]) else ""}'
                    f' style="left:{rf(sess_first.get(x["id"]) or first)}%;{_sc_var(num)}"'
                    f' title="session {num:02d} &middot; {esc(fmt_date(sess_first.get(x["id"])))}"'
                    f' aria-label="jump to session {num:02d}"></button>')
        ribbon = (f'<div class="ribbon">'
                  f'<span class="rdate">{esc(fmt_date_short(first))}</span>'
                  f'<div class="rtrack" id="rtrack">{"".join(ticks)}{"".join(dots)}'
                  f'<div class="rhead" id="rhead" style="left:0.5%"></div></div>'
                  f'<span class="rdate">{esc(fmt_date_short(last))}</span></div>')

    topbar = (f'<div class="topbar"><div class="wrap">'
              f'<div class="tbtop">{crumb}{stepper}{help_button()}</div>{ribbon}</div></div>')
    minimap = ""
    body_class = "has-right-rail"
    if len(ms) <= MINIMAP_MAX_ENTRIES:
        minimap = ('<aside class="minimap" id="minimap" aria-label="timeline minimap">'
                   '<div class="mm-track" id="mmtrack"></div></aside>')

    # ---- log entries
    nodes = []
    cur_session = None
    cur_day = None
    prev_ts = None
    session_open = False
    for i, m in enumerate(ms):
        a = m["activity"]
        kind = m["kind"]

        if m["session"] != cur_session:
            if session_open:
                nodes.append('</details>')
            cur_session = m["session"]
            session_open = True
            num = sess_idx.get(cur_session, 1)
            g = sess_agg.get(cur_session, {})
            stitle = session_title(cur_session)
            session = sess_by_id.get(cur_session) or {}
            bits = []
            if g.get("prompts"):
                bits.append(f'{g["prompts"]} prompt{_s(g["prompts"])}')
            if g.get("commands"):
                bits.append(f'{g["commands"]} command{_s(g["commands"])}')
            if g.get("recovered"):
                bits.append(
                    f'{g["recovered"]} recovered prompt{_s(g["recovered"])}')
            if g.get("active"):
                bits.append(f'{fmt_dur(g["active"])} agent active')
            if g.get("files"):
                bits.append(f'{len(g["files"])} file{_s(len(g["files"]))}')
            if g.get("tok"):
                bits.append(f'{fmt_num(g["tok"])} tok out')
            scost, stext, _, _ = cost_display(g.get("by_model") or {})
            if scost or pricing.cost_breakdown(g.get("by_model") or {})[2]:
                bits.append(f'~{stext}')
            session_id = session_anchors[cur_session]
            auto_attr = " data-automated" if sess_automated.get(cur_session) else ""
            # each session is a <details> whose header is the <summary>, so a click
            # on the header folds the entries. Sessions start open, so the page
            # reads the same without JavaScript.
            nodes.append(f'<details class="session-block" open{auto_attr}>')
            # data-t: session title, surfaced live in the sticky crumb as this
            # header scrolls past the reading line (so it tracks the session stepper).
            nodes.append(
                f'<summary class="sess" id="{session_id}" data-t="{esc(stitle)}" '
                f'data-session-index="{num}" style="{_sc_var(num)}">'
                f'<a class="lbl" href="#{session_id}"><span class="sw"></span>'
                f'<span class="sn">session {num:02d}</span> '
                f'&middot; {esc(cur_session[:8])}</a> '
                f'{tool_pill(sess_tool.get(cur_session))}'
                f'{origin_tag(session)}'
                f'<button type="button" class="share" data-share="session"'
                f' title="download this session as a standalone page"'
                f' aria-label="share this session"></button>'
                f'<span class="stitle">{esc(stitle)}</span>'
                f'<span class="sstats">{esc(" · ".join(bits))}</span></summary>')
            prev_ts = None
            cur_day = None

        d = parse_ts(m["ts"])
        if d and d.date() != cur_day:
            cur_day = d.date()
            nodes.append(f'<div class="day"><span class="lbl">{esc(fmt_dayrule(m["ts"]))}</span></div>')

        secs = gap_secs(prev_ts, m["ts"]) if prev_ts else 0
        if secs >= 1800:
            nodes.append(f'<div class="gapnote">&middot; &middot; &middot; {esc(fmt_gap(secs))}</div>')
        prev_ts = m["ts"]

        if kind == "session":
            ask = '<div class="ask-open">activity without a human prompt</div>'
        elif kind == "terminal":
            ask = _terminal_ask(m)
        else:
            txt = m["text"] or ""
            clip = " clip" if len(txt) > 700 else ""
            if kind == "command":
                if m.get("terminal"):
                    ask = _terminal_ask(m)
                else:
                    name, _, rest = txt.partition(" ")
                    ask = (f'<div class="ask{clip}"><span class="cmdname">{esc(name)}</span>'
                           f'{esc(rest)}</div>')
            elif kind == "recovered":
                ask = (f'<div class="ask{clip}">{esc(txt)}</div>'
                       f'<div class="recovered-note">'
                       f'{esc(RECOVERED_PROMPT_EXPLANATION)}</div>')
            else:
                ask = f'<div class="ask{clip}">{esc(txt)}</div>'

        # machine readout
        ro = ""
        if _has_substantive_activity(a) or a.get("subagents"):
            stat_bits = [
                f'<span><b>{a["assistant_turns"]}</b> '
                f'turn{_s(a["assistant_turns"])}</span>'
            ]
            if a["duration_ms"]:
                stat_bits.append(f'<span><b>{esc(fmt_dur(a["duration_ms"]))}</b> agent active</span>')
            if a["tokens_out"]:
                stat_bits.append(f'<span><b>{esc(fmt_num(a["tokens_out"]))}</b> tok out</span>')
            icost, itext, _, _ = cost_display(a.get("tokens_by_model") or {})
            if icost or pricing.cost_breakdown(a.get("tokens_by_model") or {})[2]:
                stat_bits.append(f'<span><b>~{esc(itext)}</b></span>')
            if a["files"]:
                stat_bits.append(
                    f'<span><b>{len(a["files"])}</b> '
                    f'file{_s(len(a["files"]))}</span>')
            if multi_model and a["models"]:
                dom = max(a["models"], key=a["models"].get)
                mfam = model_family(dom)
                mcls = f"mdl fam-{mfam}" if mfam else "mdl"
                stat_bits.append(f'<span class="{mcls}">{esc(clean_model(dom))}</span>')
            responses = a.get("responses") or []
            subs = a.get("subagents") or []
            if subs:
                sub_by_model = {}
                for run in subs:
                    merge_token_models(sub_by_model, run["by_model"])
                scost = pricing.estimate_cost(sub_by_model)
                bit = f'spawned <b>{len(subs)}</b> subagent{"s" if len(subs) != 1 else ""}'
                if scost and icost:  # share of THIS milestone's cost, not a second total
                    pct = scost / icost * 100
                    bit += f' · <b>{"&lt;1%" if pct < 1 else f"{round(pct)}%"}</b> of API cost'
                stat_bits.append(f'<span class="sub">{bit}</span>')

            tools = sorted(a["tools"].items(), key=lambda kv: -kv[1])
            tool_bits = []
            if tools:
                tool_bits.append('<span class="tools-label">tools used:</span>')
                tool_bits.extend(
                    f'<span><span class="tn">{esc(k)}</span> '
                    f'<span class="tool-count">&times;{v}</span></span>'
                    for k, v in tools[:5])
                if len(tools) > 5:
                    tool_bits.append(f'<span>+{len(tools) - 5} more</span>')

            detail_bits = []
            if responses:
                response_rows = []
                for num, response in enumerate(responses, 1):
                    if isinstance(response, dict):
                        response_text = response.get("text") or ""
                        response_ts = response.get("ts")
                    else:
                        response_text = str(response)
                        response_ts = None
                    response_label = f"response {num}"
                    if response_ts:
                        response_label += f" · {fmt_clock(response_ts)}"
                    response_rows.append(
                        f'<div class="response-item"><div class="response-meta">'
                        f'{esc(response_label)}</div><div class="response-text">'
                        f'{inline_markdown(response_text)}</div></div>')
                response_heading = f'response excerpt{_s(len(responses))}'
                detail_bits.append(
                    f'<div class="detail-section responses"><div class="response-heading">'
                    f'{response_heading}</div>{"".join(response_rows)}</div>')
            elif a.get("gist"):
                detail_bits.append(f'<div class="gist">{esc(a["gist"])}</div>')
            if a["files"]:
                rows = "".join(f'<code>{esc(short_path(f, tl))}</code>' for f in a["files"])
                detail_bits.append(
                    f'<div class="detail-section files"><div class="detail-heading">'
                    f'files changed</div>{rows}</div>')
            if a["tool_events"]:
                # collapse consecutive identical calls into one ×n row
                runs = [(n, l, sum(1 for _ in g)) for (n, l), g in
                        itertools.groupby(a["tool_events"],
                                          key=lambda e: (e["name"], e["label"]))]
                evs = "".join(
                    f'<span class="tn">{esc(n)}{" &times;" + str(c) if c > 1 else ""}</span>'
                    f'<span class="tl">{esc(l)}</span>'
                    for n, l, c in runs)
                detail_bits.append(
                    f'<div class="detail-section tools"><div class="detail-heading">'
                    f'tools</div><div class="telog">{evs}</div></div>')
            detail = ""
            if detail_bits:
                sumbits = []
                if responses:
                    sumbits.append(
                        f'{len(responses)} response excerpt{_s(len(responses))}')
                if a["files"]:
                    sumbits.append(
                        f'{len(a["files"])} file{_s(len(a["files"]))}')
                calls = sum(a["tools"].values())
                if calls:
                    sumbits.append(f'{calls} tool calls')
                if not sumbits:
                    sumbits.append("details")
                detail = (f'<details class="more"><summary>{esc(" · ".join(sumbits))}</summary>'
                          f'{"".join(detail_bits)}</details>')

            ro = (f'<div class="ro"><div class="rostat">{"".join(stat_bits)}</div>'
                  f'<div class="rotools">{"".join(tool_bits)}</div>{detail}</div>')

        quiet = "" if ro else " quiet"
        w = math.sqrt(mag(m) / vmax_w) if vmax_w else 0.0
        nodes.append(
            f'<div class="entry {kind}{quiet}" id="{entry_ids[i]}" '
            f'data-session-index="{sess_idx.get(m["session"], 1)}" data-w="{w:.3f}"'
            f' data-rf="{rf(m["ts"])}" data-ts="{esc(m["ts"])}"'
            f' style="{_sc_var(sess_idx.get(m["session"], 1))}">'
            f'<a class="emark" href="#{entry_ids[i]}" aria-label="scroll to this entry"></a>'
            f'<a class="clock" href="#{entry_ids[i]}" title="link to this entry">'
            f'{esc(fmt_clock(m["ts"]))}</a>'
            f'<button type="button" class="share" data-share="entry"'
            f' title="download this entry as a standalone page"'
            f' aria-label="share this entry"></button>'
            f'{ask}{ro}</div>')

    if session_open:
        nodes.append('</details>')

    return PAGE.format(
        generator_meta=GENERATOR_META,
        favicon=favicon_link(),
        shared_icon=favicon_data_url(shared=True),
        provenance=PAGE_PROVENANCE,
        title=esc(tl["project_name"]),
        css=CSS, js=JS + REFRESH_JS + USAGE_JS + HELP_JS,
        help=help_html(project=True, stepper=total > 1, explorer=bool(usage_html),
                       ribbon=bool(ribbon), rail=bool(minimap)),
        body_class=body_class,
        project=esc(tl["project_name"]),
        path=esc(tl["project_path"]),
        range=range_html,
        usage=usage_html,
        stats=stats_html,
        meta="".join(meta_lines),
        minimap=minimap, topbar=topbar,
        timeline="".join(nodes),
        last_activity=esc(fmt_ts(last)),
        refreshed=refresh_stamp(refreshed),
        n_inputs=_input_count(s),
        input_suffix=_s(_input_count(s)),
        input_count_title=esc(INPUT_COUNT_EXPLANATION),
        costnote=cost_method_html(s.get("tokens_by_model") or {}, "this project"),
    )


def short_path(p, tl):
    base = tl["project_path"].rstrip("/")
    if p.startswith(base + "/"):
        return p[len(base) + 1:]
    home = os.path.expanduser("~")
    if p.startswith(home + "/"):
        return "~" + p[len(home):]
    return p


# ------------------------------------------------------------- index page -- #
# The index shares the project-page CSS wholesale (same tokens, hero, footer);
# these rules only add the project shelf. Unused log selectors cost nothing.
INDEX_CSS = """
.axislbl{display:flex;justify-content:space-between;font-size:10px;color:var(--faint);
  margin:30px 0 8px}
.shelf{display:grid;gap:14px;padding-bottom:10px}
a.proj{display:block;padding:16px 20px 14px;border:1px solid var(--line);border-radius:8px;
  background:var(--panel);text-decoration:none;transition:border-color .12s}
a.proj:hover,a.proj:focus-visible{border-color:var(--machine);outline:none}
.phead{display:flex;align-items:center;gap:12px}
.pname{font-family:var(--serif);font-size:21px;flex:1;min-width:0}
.ptools{flex-shrink:0;display:flex;gap:5px}
.ppath{font-size:11px;color:var(--faint);margin-top:1px;word-break:break-all}
.strip{position:relative;height:30px;margin-top:12px;border-bottom:1px solid var(--spine)}
.strip i{position:absolute;bottom:0;width:2px;transform:translateX(-50%);background:var(--bar)}
/* stat cells share one template across every card so columns line up to scan */
.pstats{display:grid;grid-template-columns:repeat(auto-fit,minmax(104px,1fr));
  gap:2px 16px;font-size:11px;color:var(--dim);margin-top:10px}
.pstats span{white-space:nowrap}
.pstats b{font-weight:600;color:var(--ink);font-variant-numeric:tabular-nums}
@media (max-width:640px){.axislbl .lbl{display:none}}
"""

INDEX_PAGE = """<!doctype html><html lang="en"><head>
{generator_meta}
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
{favicon}
<title>Project logs</title>
<style>{css}</style></head><body>
<div class="wrap">
<header class="hero">
  {help_button}
  <h1>Project logs</h1>
  <div class="path">{root}</div>
  <div class="range">{range}</div>
  {usage}
  <div class="stats">{stats}</div>
  {costnote}
</header>
<div class="axislbl"><span>{gfirst}</span><span class="lbl">taller = more activity</span><span>{glast}</span></div>
<div class="shelf">{rows}</div>
<footer>{n} projects &middot; {provenance} &middot; refreshed {refreshed}</footer>
</div>
{help}
<script>{js}</script>
</body></html>"""


def render_index(entries, refreshed_at=None, source_label=None):
    """entries: list of (subdir_name, timeline) for every non-empty project."""
    refreshed = refreshed_at or now_local()
    firsts = [tl["stats"]["first_ts"] for _, tl in entries if tl["stats"]["first_ts"]]
    lasts = [tl["stats"]["last_ts"] for _, tl in entries if tl["stats"]["last_ts"]]
    gfirst, glast = min(firsts), max(lasts)
    gf_dt, gl_dt = parse_ts(gfirst), parse_ts(glast)
    span_s = max((gl_dt - gf_dt).total_seconds(), 1)

    def gfrac(ts):
        d = parse_ts(ts)
        return max(0.0, min(1.0, (d - gf_dt).total_seconds() / span_s)) if d else 0.0

    gvmax = max((mag(m) for _, tl in entries for m in tl["milestones"]), default=0)

    tot = {"sessions": 0, "inputs": 0, "active": 0, "tok": 0}
    all_by_model = {}
    for _, tl in entries:
        s = tl["stats"]
        tot["sessions"] += s["sessions"]
        tot["inputs"] += _input_count(s)
        tot["active"] += s["active_ms"]
        tot["tok"] += s["tokens_out"]
        merge_token_models(all_by_model, s.get("tokens_by_model"))
    all_days_active = {
        d.date()
        for _, tl in entries
        for m in tl["milestones"]
        if (d := parse_ts(m["ts"]))
    }
    series = _daily_series([(tl["project_name"], tl) for _, tl in entries], refreshed)
    window = _series_window(series) if series else None
    stat_cards = _summary_stat_cards(
        sessions=tot["sessions"],
        inputs=tot["inputs"],
        active_ms=tot["active"],
        tokens_out=tot["tok"],
        days_active=window["days"] if window else len(all_days_active),
        by_model=all_by_model,
        series=series, window=window,
    )
    stats_html = _stat_cards_html(stat_cards)
    usage_html = _usage_html(series) if series else ""

    rows = []
    for sub, tl in sorted(entries, key=lambda e: e[1]["stats"]["last_ts"] or "", reverse=True):
        s = tl["stats"]
        bars = []
        for m in tl["milestones"]:
            v = mag(m)
            h = 8 + 90 * math.sqrt(v / gvmax) if gvmax else 30
            tip = m.get("text") or "session opened"
            tip = f'{fmt_ts(m["ts"])} — {tip[:60]}'
            bars.append(f'<i style="left:{0.3 + gfrac(m["ts"])*99.4:.3f}%;'
                        f'height:{h:.1f}%" title="{esc(tip)}"></i>')
        cells = [
            f'<b>{s["sessions"]}</b> session{_s(s["sessions"])}',
            f'<b>{_input_count(s)}</b> input{_s(_input_count(s))}',
            f'<b>{esc(fmt_dur(s["active_ms"]))}</b> agent active',
            f'<b>{len(s["files_changed"])}</b> file{_s(len(s["files_changed"]))}',
            f'<b>{esc(fmt_num(s["tokens_out"]))}</b> tok out',
            f'~<b>{esc(cost_display(s.get("tokens_by_model") or {})[1])}</b>',
            f'seen <b>{esc(fmt_date_short(s["last_ts"]))}</b>',
        ]
        diagnostic_count = len(tl.get("diagnostics") or [])
        if diagnostic_count:
            cells.append(
                f'<b>{diagnostic_count}</b> skipped record{_s(diagnostic_count)}')
        stats = "".join(f'<span>{c}</span>' for c in cells)
        badges = "".join(tool_pill(t) for t in _session_tools(tl["sessions"]))
        rows.append(
            f'<a class="proj" href="{esc(sub)}/index.html">'
            f'<div class="phead"><div class="pname">{esc(tl["project_name"])}</div>'
            f'<div class="ptools">{badges}</div></div>'
            f'<div class="ppath">{esc(tl["project_path"])}</div>'
            f'<div class="strip">{"".join(bars)}</div>'
            f'<div class="pstats">{stats}</div></a>')

    return INDEX_PAGE.format(
        generator_meta=GENERATOR_META,
        favicon=favicon_link(),
        provenance=PAGE_PROVENANCE,
        css=CSS + INDEX_CSS,
        root=esc(source_label or "Claude Code and Codex data"),
        range=(f'<b>{esc(fmt_date(gfirst))}</b> &rarr; <b>{esc(fmt_date(glast))}</b>'
               f' &middot; {len(entries)} projects'
               f' &middot; refreshed {refresh_stamp(refreshed, bold=True)}'),
        usage=usage_html,
        stats=stats_html,
        gfirst=esc(fmt_date(gfirst)), glast=esc(fmt_date(glast)),
        rows="".join(rows),
        n=len(entries),
        refreshed=refresh_stamp(refreshed),
        js=REFRESH_JS + USAGE_JS + HELP_JS,
        help_button=help_button(),
        help=help_html(project=False, explorer=bool(usage_html)),
        costnote=cost_method_html(all_by_model, "all projects"),
    )


PAGE = """<!doctype html><html lang="en"><head>
{generator_meta}
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
{favicon}
<title>{title} · project log</title>
<style>{css}</style></head><body class="{body_class}" data-shared-icon="{shared_icon}">
{minimap}
{topbar}
<div class="wrap">
<header class="hero">
  <h1>{project}</h1>
  <div class="path">{path}</div>
  <div class="range">{range}</div>
  {usage}
  <div class="stats">{stats}</div>
  <div class="meta">{meta}</div>
  {costnote}
</header>
<div class="log">{timeline}</div>
<footer><span title="{input_count_title}">{n_inputs} input{input_suffix}</span> &middot; {provenance} &middot; last activity {last_activity} &middot; refreshed {refreshed}</footer>
</div>
{help}
<script>{js}</script>
</body></html>"""


def _merge_timelines(tls):
    """Merge timelines for the same project into one: sessions in chronological
    order, each session's milestone chunk kept together, stats recomputed."""
    if len(tls) == 1:
        tl = tls[0]
        _associate_codex_subagents(tl["sessions"], tl["milestones"])
        tl["stats"] = _aggregate(tl["milestones"], tl["sessions"])
        return tl
    chunks = []
    branches = Counter()
    diagnostics = []
    for tl in tls:
        by_sess = {}
        for m in tl["milestones"]:
            by_sess.setdefault(m["session"], []).append(m)
        for s in tl["sessions"]:
            ms = by_sess.get(s["id"], [])
            first = (ms[0]["ts"] if ms else None) or s["last_ts"] or ""
            chunks.append((first, s, ms))
        branches.update(tl.get("git_branches", {}))
        diagnostics.extend(tl.get("diagnostics") or [])
    # the same session can arrive twice (live + archived copy): keep the
    # fuller one — more milestones, then later last activity
    def fullness(c):
        return (len(c[2]), c[1].get("last_ts") or "")
    best = {}
    for c in chunks:
        sid = c[1]["id"]
        if sid not in best or fullness(c) > fullness(best[sid]):
            best[sid] = c
    chunks = sorted(best.values(), key=lambda c: c[0])
    sessions = [c[1] for c in chunks]
    milestones = [m for c in chunks for m in c[2]]
    _associate_codex_subagents(sessions, milestones)
    return {**tls[0],
            "git_branches": dict(branches.most_common()),
            "sessions": sessions, "milestones": milestones,
            "diagnostics": diagnostics,
            "stats": _aggregate(milestones, sessions)}


def _atomic_write_text(path, content):
    """Publish one generated page without exposing a truncated partial file."""
    _private_directory(os.path.dirname(path))
    fd, tmp = tempfile.mkstemp(prefix=".render-", suffix=".tmp",
                               dir=os.path.dirname(path), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def _is_generated_project_page(path):
    """Return whether an HTML file is a session-atlas project page."""
    marker = GENERATOR_META.encode()
    legacy_header = b"project log</title>"
    # Frozen legacy ownership marker; do not couple it to current page copy.
    legacy_footer = b"generated from local transcripts"
    try:
        with open(path, "rb") as fh:
            head = fh.read(64 * 1024)
            if marker in head:
                return True
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 64 * 1024))
            tail = fh.read()
    except OSError:
        return False
    return legacy_header in head and legacy_footer in tail


def _prune_stale_project_pages(out, active_names):
    """Remove obsolete project pages owned by the generator.

    A stale directory may contain files the generator does not own. Remove its
    generated ``index.html`` and remove the directory only when that leaves it
    empty.
    """
    removed = []
    with os.scandir(out) as scan:
        entries = sorted(scan, key=lambda item: item.name)
    for entry in entries:
        if entry.name in active_names or not entry.is_dir(follow_symlinks=False):
            continue
        index_path = os.path.join(entry.path, "index.html")
        if not _is_generated_project_page(index_path):
            continue
        try:
            os.unlink(index_path)
        except OSError:
            continue
        try:
            os.rmdir(entry.path)
        except OSError:
            pass
        removed.append(entry.name)
    return removed


@contextlib.contextmanager
def _render_lock(out):
    """Serialize timer and ad-hoc renders so one site is one generation."""
    _private_directory(out)
    with open(os.path.join(out, ".render.lock"), "w") as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _claude_manifest(dirs):
    """Select the fuller live/archive copy of every relative Claude log path."""
    manifest = {}
    for d in dirs:
        paths = glob.glob(os.path.join(d, "*.jsonl")) + _iter_subagent_transcripts(d)
        for path in paths:
            rel = os.path.relpath(path, d)
            candidate = (os.path.getsize(path), path)
            if rel not in manifest or candidate[0] > manifest[rel][0]:
                manifest[rel] = candidate
    top = [manifest[rel][1] for rel in manifest
           if "subagents" not in os.path.normpath(rel).split(os.sep)]
    nested = [manifest[rel][1] for rel in manifest
              if "subagents" in os.path.normpath(rel).split(os.sep)]
    return sorted(top), sorted(nested)


def _write_project(tl, out, slug, index_path=None, refreshed_at=None):
    outdir = _project_output_dir(out, slug)
    _private_directory(outdir)
    outfile = os.path.join(outdir, "index.html")
    # back-link target, derived from where the index actually lives relative to
    # this page (rather than hardcoding "../"), or None when rendered standalone
    home = os.path.relpath(index_path, outdir) if index_path else None
    _atomic_write_text(outfile, render(tl, home=home, refreshed_at=refreshed_at))
    s = tl["stats"]
    print(f"Wrote {outfile}")
    recovered = (f" + {s['recovered_prompts']} recovered "
                 f"prompt{_s(s['recovered_prompts'])}"
                 if s.get("recovered_prompts") else "")
    milestones = len(tl["milestones"])
    print(f"  {s['prompts']} prompt{_s(s['prompts'])} + "
          f"{s['commands']} command{_s(s['commands'])}{recovered} · "
          f"{milestones} milestone{_s(milestones)} · "
          f"{s['sessions']} session{_s(s['sessions'])}")
    return outfile


def generate_all(out, archive):
    with _render_lock(out):
        return _generate_all_locked(out, archive)


def _generate_all_locked(out, archive):
    # Build one per-project manifest first. Live and archive can each hold the
    # fuller copy of a different append-only file; choosing the largest file by
    # relative path forms the correct union and avoids decoding duplicates.
    claude_dirs = sorted(glob.glob(os.path.join(PROJECTS, "*")))
    if os.path.isdir(os.path.join(archive, "claude")):
        claude_dirs += sorted(glob.glob(os.path.join(archive, "claude", "*")))
    dir_groups = {}
    for d in claude_dirs:
        if os.path.isdir(d):
            dir_groups.setdefault(os.path.basename(d.rstrip("/")), []).append(d)
    by_path = {}
    for base, dirs in sorted(dir_groups.items()):
        top, nested = _claude_manifest(dirs)
        if not top:
            if any(d.startswith(PROJECTS + os.sep) for d in dirs):
                print(f"  skipped (no transcripts): {base}")
            continue
        tl = build_timeline(dirs[0], session_paths=top, subagent_paths=nested)
        if not tl["milestones"]:
            print(f"  skipped (no inputs): {base}")
            continue
        by_path.setdefault(tl["project_path"].rstrip("/"), []).append(tl)

    # codex: dedup live vs archived copies of the same rollout before parsing
    codex_files = {}
    roots = [CODEX_SESSIONS]
    if os.path.isdir(os.path.join(archive, "codex")):
        roots.append(os.path.join(archive, "codex"))
    for root in roots:
        for p in rollout_paths(root):
            n = os.path.basename(p)
            size = os.path.getsize(p)
            if n not in codex_files or size > codex_files[n][0]:
                codex_files[n] = (size, p)
    codex_paths = [p for _, p in codex_files.values()]
    codex_timelines = build_codex_timelines(codex_paths)
    # Every selected rollout's first metadata record is its authoritative ID.
    known_codex_ids = set()
    for path in codex_paths:
        try:
            with open(path, "rb") as fh:
                record = json.loads(fh.readline().decode("utf-8"))
            meta = record.get("payload") or {}
            if record.get("type") == "session_meta" and meta.get("id"):
                known_codex_ids.add(meta["id"])
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
    codex_timelines.extend(build_history_only_timelines(known_codex_ids))
    for path, timelines in _group_codex_timelines(codex_timelines).items():
        by_path.setdefault(path, []).extend(timelines)

    entries = []
    index_outfile = os.path.join(out, "index.html")
    refreshed = now_local()
    merged = [(path, _merge_timelines(tls)) for path, tls in sorted(by_path.items())]
    slugs = _allocate_project_slugs(path for path, _ in merged)
    # Activity controls display order only. Slugs depend only on project paths,
    # so changing usage cannot exchange two projects' URLs.
    merged.sort(key=lambda e: -_input_count(e[1]["stats"]))
    for path, tl in merged:
        slug = slugs[path]
        _write_project(tl, out, slug, index_path=index_outfile, refreshed_at=refreshed)
        entries.append((slug, tl))
    if not entries:
        raise SystemExit("No projects with any input found")
    _atomic_write_text(index_outfile, render_index(entries, refreshed_at=refreshed))
    removed = _prune_stale_project_pages(out, {name for name, _ in entries})
    for name in removed:
        print(f"Removed stale {os.path.join(out, name, 'index.html')}")
    print(f"Wrote {index_outfile} ({len(entries)} projects)")
    print(f"  open: {Path(index_outfile).resolve().as_uri()}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("project", nargs="?",
                    help="project basename (e.g. example-project) or path")
    ap.add_argument("--all", action="store_true",
                    help="render every discovered Claude Code and Codex project, "
                         "including archived sessions, plus an index page")
    ap.add_argument("--out", default="./site",
                    help="output directory (default %(default)s)")
    ap.add_argument("--archive", default="./archive",
                    help="archive root read by --all (default %(default)s; "
                         "see archive_transcripts.py)")
    args = ap.parse_args()

    if args.all:
        generate_all(args.out, args.archive)
    elif args.project:
        with _render_lock(args.out):
            # Parse under the same lock as publication. Otherwise an older
            # standalone snapshot can wait behind --all and overwrite its newer
            # project page after the full generation completes.
            tl = _single(args.project)
            project_path = tl["project_path"].rstrip("/")
            slug = _allocate_project_slugs([project_path])[project_path]
            outfile = _write_project(tl, args.out, slug)
        print(f"  open: {Path(outfile).resolve().as_uri()}")
    else:
        ap.error("give a project name/path, or --all")


def _single(target):
    """Build one project timeline from its selected Claude and Codex inputs.

    Primary Codex rollouts are selected by working directory. Related
    ``codex_exec`` rollouts can also match by repository URL, and only selected
    rollout files are parsed fully.
    """
    tls = []
    path = None
    try:
        tl = build_timeline(find_project_dir(target))
        path = tl["project_path"].rstrip("/")
        tls.append(tl)
    except SystemExit as e:
        if "Ambiguous" in str(e):
            raise
        if os.path.isdir(target):
            path = os.path.abspath(target).rstrip("/")

    metas = list(iter_rollout_metas())
    matches = []
    matched_repositories = set()
    for p, meta in metas:
        cwd = (meta.get("cwd") or "").rstrip("/")
        if not cwd:
            continue
        if (path and cwd == path) or (not path and os.path.basename(cwd) == target):
            matches.append((p, cwd))
            if meta.get("originator") != "codex_exec":
                repository = (meta.get("git") or {}).get("repository_url")
                if repository:
                    matched_repositories.add(repository)
    cwds = {c for _, c in matches}
    if not path and len(cwds) > 1:
        raise SystemExit("Ambiguous; Codex sessions match:\n  " + "\n  ".join(sorted(cwds)))
    if matched_repositories:
        seen = {p for p, _ in matches}
        for p, meta in metas:
            repository = (meta.get("git") or {}).get("repository_url")
            if (p not in seen and meta.get("originator") == "codex_exec"
                    and repository in matched_repositories):
                matches.append((p, (meta.get("cwd") or "").rstrip("/")))
    matched_paths = [p for p, _ in matches]
    tls.extend(build_codex_timelines(matched_paths))
    known_codex_ids = {meta.get("id") for _, meta in metas if meta.get("id")}
    history_timelines = build_history_only_timelines(known_codex_ids)
    for timeline in history_timelines:
        history_path = timeline["project_path"].rstrip("/")
        if ((path and history_path == path)
                or (not path and os.path.basename(history_path) == target)):
            tls.append(timeline)
    if not tls:
        raise SystemExit(f"No Claude or Codex transcripts found for {target!r}")
    return _merge_timelines(tls)


if __name__ == "__main__":
    main()
