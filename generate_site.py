#!/usr/bin/env python3
"""Generate a static timeline site from Claude Code and Codex CLI transcripts.

    python3 generate_site.py example-project                 # -> ./site/<stable-slug>/index.html
    python3 generate_site.py /path/to/project --out ./out    # by path, custom out dir
    python3 generate_site.py --all                           # every project + index page

The page shows each prompt and the activity that followed it, including tools,
files, tokens, and active time. A top ribbon shows when sessions occurred, and a
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
from datetime import datetime
from pathlib import Path

from ccx_parse import (PROJECTS, _aggregate, _has_substantive_activity,
                       _is_transcript_dir, _iter_subagent_transcripts,
                       build_timeline, find_project_dir,
                       merge_token_models, parse_iso)
from codex_parse import (CODEX_SESSIONS, build_codex_timelines,
                         build_history_only_timelines, iter_rollout_metas,
                         rollout_paths)
import pricing

GENERATOR_META = '<meta name="generator" content="session-atlas">'
PAGE_PROVENANCE = "generated from local transcripts"
RECOVERED_PROMPT_EXPLANATION = (
    "This prompt was recovered from Codex history because no rollout was found. "
    "The available sources do not contain the assistant reply, tool activity, "
    "token usage, or cost."
)
_SAFE_PROJECT_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_MAX_PROJECT_SLUG = 80

# ------------------------------------------------------------------ helpers -- #
def esc(s):
    return html.escape(s if s is not None else "", quote=True)


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
    label = "est. cost"
    if unpriced:
        shown += "+"
        label = "partial est. cost"
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
    matching token-count matrix, so any figure is traceable to model and token."""
    by_model = by_model or {}
    overall, total, unpriced = pricing.cost_breakdown(by_model)
    if not total and not unpriced:
        return ""
    priced = []
    for mid, tk in by_model.items():
        cats, mt, _ = pricing.cost_breakdown({mid: tk})
        if mt:
            priced.append((mt, mid, cats))
    priced.sort(key=lambda x: x[0], reverse=True)  # biggest cost first
    models = [(mid, cats) for _, mid, cats in priced]
    multi = len(models) > 1
    cost_table = (_model_table(models, overall, lambda c, k: c[k]["cost"], fmt_cost,
                               f'Cost by model &mdash; {esc(scope)}:', multi)
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
    token_models.sort(key=lambda item: sum(c["tokens"] for c in item[1].values()),
                      reverse=True)
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
                f'Add them to <code>pricing.py</code> to include their cost.</p>')
    category_help = "".join(
        f'<p><b>{esc(label)}:</b> {esc(help_text)}</p>'
        for _, label, help_text in pricing.CATEGORY_SPECS
    )
    return (
        '<details class="pricing"><summary>How is est. cost estimated?</summary>'
        '<div class="pricing-body">'
        "<p>Each model's tokens are multiplied by its list rate and summed. Tokens "
        "are attributed to the model that produced them, so a project that mixes "
        "models is priced correctly. Cache-read and cache-write tokens are included.</p>"
        f'{category_help}'
        f"<p>Rates are standard published list prices per 1M tokens, as of "
        f"<b>{esc(pricing.AS_OF)}</b>: no batch, priority, or long-context tiers and "
        "no volume/enterprise discounts, so read totals as an order-of-magnitude "
        "estimate.</p>"
        f'{_breakdown_table(by_model, scope)}'
        '<p class="sh">Rates used (per 1M tokens):</p>'
        f'{_grid_open(["model", *cat_labels])}{"".join(rate_rows)}{_GRID_CLOSE}'
        f'{excl}</div></details>')


def cost_breakdown_title(by_model):
    """Tooltip text: per-model cost split behind an est. cost figure."""
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
    """Per-entry 'work magnitude': active time if timed, else tokens out."""
    return m["activity"]["duration_ms"] or m["activity"]["tokens_out"]


def _sc_var(num):
    """CSS custom-property setting a session's color from the 8-color cycle."""
    return f"--sc:var(--s{(num - 1) % 8 + 1})"


def _stat_cards_html(cards):
    """Hero stat grid shared by the project page and the index-page hero.

    Each card is ``(number, label)`` or ``(number, label, tooltip)``.
    """
    out = []
    for c in cards:
        n, l = c[0], c[1]
        tip = f' title="{esc(c[2])}"' if len(c) > 2 and c[2] else ""
        out.append(f'<div class="stat"{tip}><div class="n">{esc(n)}</div>'
                   f'<div class="l lbl">{esc(l)}</div></div>')
    return "".join(out)


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
    """Group exec-only working directories under their interactive checkout.

    When an exec-only timeline uses a different working directory from the
    interactive checkout, grouping by cwd alone can split one repository across
    project cards. A repository's canonical path comes from an interactive
    timeline; exec-only timelines with the same recorded remote are assigned to it.
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
        if (tl["sessions"] and all(_is_codex_exec(s) for s in tl["sessions"])
                and repository in canonical):
            path = canonical[repository][1]
        grouped.setdefault(path, []).append(tl)
    return grouped


def _eyebrow(tools):
    """Human label for a set of tools, e.g. 'Claude Code + Codex'."""
    return " + ".join("Claude Code" if t == "claude" else t.capitalize()
                      for t in tools) or "Claude Code"


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
body{padding-right:56px}
@media (max-width:759px){.minimap{display:none}body{padding-right:0}}
.sessnav{display:flex;align-items:center;gap:8px;flex:0 0 auto;
  letter-spacing:.1em;text-transform:uppercase}
.sesscount{color:var(--dim);white-space:nowrap}
.sesscount b{color:var(--ink);font-weight:600}
.snav{width:19px;height:19px;display:inline-flex;align-items:center;justify-content:center;
  padding:0;border:1px solid var(--line);border-radius:4px;background:var(--panel);
  color:var(--dim);cursor:pointer;font-size:13px;line-height:1;
  transition:border-color .12s,color .12s}
.snav:hover{border-color:var(--machine);color:var(--ink)}
.snav:disabled{opacity:.35;cursor:default}
.snav:focus-visible{outline:2px solid var(--machine);outline-offset:2px}
.sessnav[hidden]{display:none}

/* ---- hero ---- */
header.hero{padding:46px 0 30px;border-bottom:1px solid var(--line)}
.eyebrow{font-size:10px;letter-spacing:.16em;text-transform:uppercase;color:var(--faint)}
.eyebrow::before{content:"";display:inline-block;width:7px;height:7px;
  background:var(--human);margin-right:9px}
h1{font-family:var(--serif);font-size:38px;font-weight:500;letter-spacing:-.01em;
  margin:12px 0 6px}
.path{font-size:11.5px;color:var(--faint);word-break:break-all}
.range{font-size:12px;color:var(--dim);margin-top:12px}
.range b{color:var(--ink);font-weight:600}
.age{color:var(--faint);white-space:nowrap}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:22px 26px;margin-top:30px}
.stat{border-top:1px solid var(--line);padding-top:9px}
.stat .n{font-size:21px;font-weight:600;letter-spacing:-.02em}
.stat .l{margin-top:2px}
.meta-row{display:flex;flex-wrap:wrap;gap:7px;margin-top:26px}
.chip{font-size:11px;padding:3px 9px;border:1px solid var(--line);border-radius:4px;
  background:var(--panel);color:var(--dim);white-space:nowrap}
.chip b{color:var(--ink);font-weight:600}
.chip.model{color:var(--machine)}
.chip.model.fam-claude{color:var(--claude)}
.chip.model.fam-gpt{color:var(--codex)}
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
.sessionfilter{display:inline-flex;align-items:center;gap:7px;margin-left:auto;
  padding:3px 9px;border:1px solid var(--line);border-radius:4px;background:var(--panel);
  color:var(--dim);font-size:11px;cursor:pointer;white-space:nowrap}
.sessionfilter:hover{border-color:var(--machine);color:var(--ink)}
.sessionfilter:focus-within{outline:2px solid var(--machine);outline-offset:2px}
.sessionfilter input{margin:0;accent-color:var(--bar)}
.sessionfilter .filter-note{color:var(--faint)}

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
.sess .stitle{font-size:15px;font-weight:600;margin-top:6px}
.sess .sstats{font-size:11px;color:var(--dim);margin-top:5px}
.gapnote{padding-left:92px;margin:-8px 0 16px;font-size:10.5px;color:var(--faint);
  letter-spacing:.08em}
.entry{position:relative;padding:0 0 34px 92px;scroll-margin-top:72px}
.entry.quiet{padding-bottom:20px}
.session-block[data-automated],.etick[data-automated],.sdot[data-automated]{display:none}
html.show-automated .session-block[data-automated]{display:block}
html.show-automated .etick[data-automated],html.show-automated .sdot[data-automated]{display:block}
/* clickable timeline marker; the one at the reading position is ringed (JS .current) */
.emark{position:absolute;left:56px;top:0;width:24px;height:24px;z-index:1;
  cursor:pointer;display:block;text-decoration:none}
.emark::after{content:"";position:absolute;left:9px;top:7px;width:7px;height:7px;
  background:var(--sc,var(--human));transition:transform .12s,box-shadow .12s}
.entry.session .emark::after{background:var(--bg);border:1px solid var(--faint)}
.emark:hover::after{transform:scale(1.5)}
.emark:focus-visible{outline:2px solid var(--machine);outline-offset:2px}
.entry.current .emark::after{box-shadow:0 0 0 3px color-mix(in srgb,var(--sc,var(--human)) 40%,transparent)}
.entry.current .clock{color:var(--human)}
.clock{position:absolute;left:0;top:3px;width:52px;text-align:right;
  font-size:11px;color:var(--faint);text-decoration:none}
a.clock:hover,a.clock:focus-visible{color:var(--human);outline:none}
.ask{font-family:var(--serif);font-size:16.5px;line-height:1.55;max-width:62ch;
  white-space:pre-wrap;overflow-wrap:anywhere}
.ask.clip{max-height:148px;overflow:hidden;cursor:pointer;
  -webkit-mask-image:linear-gradient(#000 64%,transparent);
  mask-image:linear-gradient(#000 64%,transparent)}
.ask .cmdname{font-family:var(--mono);font-size:13px;color:var(--human);
  padding-right:4px}
.ask-open{font-size:12px;color:var(--faint)}
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
.rotools .tn{color:var(--machine)}
details.more{margin-top:9px;border-top:1px dashed var(--line);padding-top:8px}
details.more>summary{cursor:pointer;font-size:10.5px;letter-spacing:.1em;
  text-transform:uppercase;color:var(--faint);list-style:none;user-select:none}
details.more>summary::-webkit-details-marker{display:none}
details.more>summary::before{content:"\\25B8  "}
details.more[open]>summary::before{content:"\\25BE  "}
details.more>summary:focus-visible{outline:2px solid var(--machine);outline-offset:2px}
.gist{margin:10px 0;padding-left:12px;border-left:2px solid var(--bar);
  font-size:12px;color:var(--dim);white-space:pre-wrap}
.files{margin:10px 0}
.files .fh{margin-bottom:5px}
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

@media (max-width:640px){
  .stats{grid-template-columns:repeat(2,1fr)}
  h1{font-size:30px}
  .sessionfilter{margin-left:0;white-space:normal}
  .log::before,.emark{display:none}
  .entry,.sess,.gapnote{padding-left:0}
  .clock{position:static;display:block;width:auto;text-align:left;margin-bottom:4px}
  .clock::before{content:"";display:inline-block;width:7px;height:7px;
    background:var(--sc,var(--human));margin-right:8px}
  .entry.session .clock::before{background:var(--bg);border:1px solid var(--faint)}
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
}
"""

JS = """
const automatedQuery='show-automated';
const showAutomated=new URL(location.href).searchParams.get(automatedQuery)==='1';
document.documentElement.classList.toggle('show-automated',showAutomated);
const automatedToggle=document.getElementById('automatedToggle');
if(automatedToggle){
  automatedToggle.checked=!showAutomated;
  automatedToggle.addEventListener('change',()=>{
    const url=new URL(location.href);
    if(automatedToggle.checked) url.searchParams.delete(automatedQuery);
    else url.searchParams.set(automatedQuery,'1');
    location.href=url;
  });
}
const included=el=>showAutomated||!el.closest('[data-automated]');
const smooth=matchMedia('(prefers-reduced-motion: reduce)').matches?'auto':'smooth';
const entries=[...document.querySelectorAll('.entry')].filter(included);
const sessions=[...document.querySelectorAll('.sess')].filter(included);
const heroSessionCount=document.getElementById('heroSessionCount');
const heroSessionLabel=document.getElementById('heroSessionLabel');
if(heroSessionCount) heroSessionCount.textContent=sessions.length;
if(heroSessionLabel) heroSessionLabel.textContent=sessions.length===1?'session':'sessions';
const docTop=el=>el.getBoundingClientRect().top+window.scrollY;
const docH=()=>document.documentElement.scrollHeight||1;

// The current entry is the latest one whose top has crossed the reading line,
// defaulting to the first before any one crosses. It changes when the next
// entry crosses the line and drives the URL anchor and ribbon playhead.
const topbar=document.querySelector('.topbar');
const entrySM=entries.length?parseFloat(getComputedStyle(entries[0]).scrollMarginTop)||0:0;
const headOff=()=>topbar?topbar.getBoundingClientRect().bottom:entrySM;  // reading-line offset
function currentId(){
  const line=headOff()+1;
  let id=entries.length?entries[0].id:null;
  for(const e of entries){ if(e.getBoundingClientRect().top<=line) id=e.id; else break; }
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
  if(!track) return;
  const H=docH();
  const root=getComputedStyle(document.documentElement);
  const col=['--s1','--s2','--s3','--s4','--s5','--s6','--s7','--s8'].map(v=>root.getPropertyValue(v).trim());
  const sessColor=n=>col[(n-1)%col.length];
  track.textContent='';
  sessions.forEach((s,i)=>{                       // one color block per session
    const n=parseInt(s.id.slice(1),10)||1;
    const top=docTop(s), bot=(i+1<sessions.length)?docTop(sessions[i+1]):H;
    const b=document.createElement('div'); b.className='mm-sess';
    b.style.top=(top/H*100)+'%'; b.style.height=Math.max(0,(bot-top)/H*100)+'%';
    b.style.background=sessColor(n); track.appendChild(b);
  });
  entries.forEach(e=>{                             // one tick per entry, length=work
    const n=parseInt(e.id.slice(1),10)||1;
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
  syncHash(id);
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
const dots=[...document.querySelectorAll('.sdot')].filter(included); // visible session starts
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
  const header=el.closest('.session-block')?.querySelector('.sess');
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
    if(e.metaKey||e.ctrlKey||e.altKey) return;
    const t=e.target; if(t&&(/^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName)||t.isContentEditable)) return;
    const k=e.key.toLowerCase();
    if(k==='j'){e.preventDefault();jump(1);} else if(k==='k'){e.preventDefault();jump(-1);}
  });
}
curSessIdx=posIdx(); paint();               // initial counter + crumb subtitle

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
            badge = '<span class="origintag" title="spawned Codex subagent">subagent</span>'
        elif _is_codex_exec(session):
            badge = '<span class="origintag" title="non-interactive Codex run">codex exec</span>'
        parent = session.get("parent_session_id")
        relation = session.get("parent_relation")
        if not parent or not relation:
            return badge
        if parent in sess_idx:
            parent_num = sess_idx[parent]
            link = (f'<a class="forktag" href="#s{parent_num:02d}"'
                    f' title="{esc(relation)} conversation {esc(parent[:8])}">'
                    f'{esc(relation)} session {parent_num:02d}</a>')
        else:
            link = (f'<span class="forktag" title="parent conversation is outside this page">'
                    f'{esc(relation)} {esc(parent[:8])}</span>')
        return badge + link

    real_models = list(s["models"])
    multi_model = len(real_models) > 1
    cost, cost_text, cost_label, cost_title = cost_display(
        s.get("tokens_by_model") or {})

    # ---- hero
    stat_cards = [
        (fmt_num(s["prompts"]), f'prompt{_s(s["prompts"])}'),
        (fmt_num(s["commands"]), f'command{_s(s["commands"])}'),
        (fmt_num(s["assistant_turns"]),
         f'assistant turn{_s(s["assistant_turns"])}'),
        (fmt_num(s["tool_calls"]), f'tool call{_s(s["tool_calls"])}'),
        (str(len(s["files_changed"])),
         f'file{_s(len(s["files_changed"]))} changed'),
        (fmt_dur(s["active_ms"]), "active time"),
        (fmt_num(s["tokens_out"]), "tokens out"),
        (str(len(days_active)), f'day{_s(len(days_active))} active'),
        (cost_text, cost_label, cost_title),
    ]
    if s.get("recovered_prompts"):
        stat_cards.insert(2, (
            fmt_num(s["recovered_prompts"]),
            f'recovered prompt{_s(s["recovered_prompts"])}',
            RECOVERED_PROMPT_EXPLANATION))
    stats_html = _stat_cards_html(stat_cards)

    chips = []
    for m in real_models:
        fam = model_family(m)
        cls = f"chip model fam-{fam}" if fam else "chip model"
        chips.append(f'<span class="{cls}">{esc(clean_model(m))} <b>{s["models"][m]}</b></span>')
    for b in list(tl.get("git_branches", {}))[:3]:
        chips.append(f'<span class="chip">&#x2387; {esc(b)}</span>')
    for k, v in list(s["tools"].items())[:6]:
        chips.append(f'<span class="chip"><b>{v}</b> {esc(k)}</span>')
    diagnostic_count = len(tl.get("diagnostics") or [])
    if diagnostic_count:
        chips.append(
            f'<span class="chip" title="The parser skipped malformed or non-UTF-8 '
            f'transcript records"><b>{diagnostic_count}</b> skipped transcript '
            f'record{_s(diagnostic_count)}</span>')

    rendered_sessions = [session for session in tl["sessions"]
                         if session["id"] in sess_agg]
    automated_sessions = [session for session in rendered_sessions
                          if _is_automated_codex(session)]
    total = len(rendered_sessions)
    visible_total = total - len(automated_sessions)
    session_filter = ""
    if automated_sessions:
        session_filter = (
            '<label class="sessionfilter"><input id="automatedToggle" '
            'type="checkbox" checked><span>Hide automated Codex sessions '
            '<span class="filter-note">(activity stays in project totals)</span>'
            '</span></label>')

    refreshed = refreshed_at or now_local()
    range_html = ""
    if first:
        n_days = (last_dt.date() - first_dt.date()).days + 1 if first_dt and last_dt else 1
        range_html = (f'<b>{esc(fmt_date(first))}</b> &rarr; <b>{esc(fmt_date(last))}</b>'
                      f' &middot; {n_days} day{_s(n_days)}'
                      f' &middot; <b id="heroSessionCount">{visible_total}</b> '
                      f'<span id="heroSessionLabel">session{_s(visible_total)}</span>'
                      f' &middot; refreshed {refresh_stamp(refreshed, bold=True)}')

    # ---- navigation: readable per-entry anchors "sNN-EE" (session number, entry
    # within session), a sticky session stepper, and the vertical minimap rail
    sess_idx = {x["id"]: i + 1 for i, x in enumerate(rendered_sessions)}
    sess_tool = {x["id"]: x["tool"] for x in rendered_sessions}
    sess_automated = {x["id"]: _is_automated_codex(x) for x in rendered_sessions}
    entry_ids, _seen, sess_first = [], Counter(), {}
    for m in ms:
        sid = m["session"]
        _seen[sid] += 1
        entry_ids.append(f's{sess_idx.get(sid, 1):02d}-{_seen[sid]:02d}')
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
        hidden = " hidden" if visible_total < 2 else ""
        stepper = (f'<div class="sessnav"{hidden}>'
                   f'<button class="snav" data-d="-1" title="previous session (k)"'
                   f' aria-label="previous session">&lsaquo;</button>'
                   f'<span class="sesscount">session <b id="sessCur">1</b> / '
                   f'<b id="sessTotal">{visible_total}</b></span>'
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
                    f'<button class="sdot" data-s="s{num:02d}"'
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
              f'<div class="tbtop">{crumb}{stepper}</div>{ribbon}</div></div>')
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
                nodes.append('</section>')
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
                bits.append(f'{fmt_dur(g["active"])} active')
            if g.get("files"):
                bits.append(f'{len(g["files"])} file{_s(len(g["files"]))}')
            if g.get("tok"):
                bits.append(f'{fmt_num(g["tok"])} tok out')
            scost, stext, _, _ = cost_display(g.get("by_model") or {})
            if scost or pricing.cost_breakdown(g.get("by_model") or {})[2]:
                bits.append(f'~{stext}')
            sid = f"s{num:02d}"
            auto_attr = " data-automated" if sess_automated.get(cur_session) else ""
            nodes.append(f'<section class="session-block"{auto_attr}>')
            # data-t: session title, surfaced live in the sticky crumb as this
            # header scrolls past the reading line (so it tracks the session stepper).
            nodes.append(
                f'<div class="sess" id="{sid}" data-t="{esc(stitle)}" style="{_sc_var(num)}">'
                f'<a class="lbl" href="#{sid}"><span class="sw"></span>'
                f'<span class="sn">session {num:02d}</span> '
                f'&middot; {esc(cur_session[:8])}</a> '
                f'{tool_pill(sess_tool.get(cur_session))}'
                f'{origin_tag(session)}'
                f'<div class="stitle">{esc(stitle)}</div>'
                f'<div class="sstats">{esc(" · ".join(bits))}</div></div>')
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
        else:
            txt = m["text"] or ""
            clip = " clip" if len(txt) > 700 else ""
            if kind == "command":
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
                stat_bits.append(f'<span><b>{esc(fmt_dur(a["duration_ms"]))}</b> active</span>')
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
            subs = a.get("subagents") or []
            if subs:
                sub_by_model = {}
                for run in subs:
                    merge_token_models(sub_by_model, run["by_model"])
                scost = pricing.estimate_cost(sub_by_model)
                bit = f'spawned <b>{len(subs)}</b> subagent{"s" if len(subs) != 1 else ""}'
                if scost and icost:  # share of THIS milestone's cost, not a second total
                    pct = scost / icost * 100
                    bit += f' · <b>{"&lt;1%" if pct < 1 else f"{round(pct)}%"}</b> of cost'
                stat_bits.append(f'<span class="sub">{bit}</span>')

            tools = sorted(a["tools"].items(), key=lambda kv: -kv[1])
            tool_bits = [f'<span><span class="tn">{esc(k)}</span> &times;{v}</span>'
                         for k, v in tools[:5]]
            if len(tools) > 5:
                tool_bits.append(f'<span>+{len(tools) - 5} more</span>')

            detail_bits = []
            if a.get("gist"):
                detail_bits.append(f'<div class="gist">{esc(a["gist"])}</div>')
            if a["files"]:
                rows = "".join(f'<code>{esc(short_path(f, tl))}</code>' for f in a["files"])
                detail_bits.append(
                    f'<div class="files"><div class="fh lbl">files changed</div>{rows}</div>')
            if a["tool_events"]:
                # collapse consecutive identical calls into one ×n row
                runs = [(n, l, sum(1 for _ in g)) for (n, l), g in
                        itertools.groupby(a["tool_events"],
                                          key=lambda e: (e["name"], e["label"]))]
                evs = "".join(
                    f'<span class="tn">{esc(n)}{" &times;" + str(c) if c > 1 else ""}</span>'
                    f'<span class="tl">{esc(l)}</span>'
                    for n, l, c in runs)
                detail_bits.append(f'<div class="telog">{evs}</div>')
            detail = ""
            if detail_bits:
                sumbits = ["log"]
                if a["files"]:
                    sumbits.append(
                        f'{len(a["files"])} file{_s(len(a["files"]))}')
                calls = sum(a["tools"].values())
                if calls:
                    sumbits.append(f'{calls} tool calls')
                detail = (f'<details class="more"><summary>{esc(" · ".join(sumbits))}</summary>'
                          f'{"".join(detail_bits)}</details>')

            ro = (f'<div class="ro"><div class="rostat">{"".join(stat_bits)}</div>'
                  f'<div class="rotools">{"".join(tool_bits)}</div>{detail}</div>')

        quiet = "" if ro else " quiet"
        w = math.sqrt(mag(m) / vmax_w) if vmax_w else 0.0
        nodes.append(
            f'<div class="entry {kind}{quiet}" id="{entry_ids[i]}" data-w="{w:.3f}"'
            f' data-rf="{rf(m["ts"])}"'
            f' style="{_sc_var(sess_idx.get(m["session"], 1))}">'
            f'<a class="emark" href="#{entry_ids[i]}" aria-label="scroll to this entry"></a>'
            f'<a class="clock" href="#{entry_ids[i]}" title="link to this entry">'
            f'{esc(fmt_clock(m["ts"]))}</a>'
            f'{ask}{ro}</div>')

    if session_open:
        nodes.append('</section>')

    return PAGE.format(
        generator_meta=GENERATOR_META,
        provenance=PAGE_PROVENANCE,
        title=esc(tl["project_name"]),
        css=CSS, js=JS + REFRESH_JS,
        eyebrow=esc(_eyebrow(_session_tools(tl["sessions"]))),
        project=esc(tl["project_name"]),
        path=esc(tl["project_path"]),
        range=range_html,
        stats=stats_html,
        chips="".join(chips),
        session_filter=session_filter,
        minimap=minimap, topbar=topbar,
        timeline="".join(nodes),
        last_activity=esc(fmt_ts(last)),
        refreshed=refresh_stamp(refreshed),
        n_inputs=_input_count(s),
        input_suffix=_s(_input_count(s)),
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
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'><path d='M1 8 H3 L4 4 L5 12 L6 8 H8.2' fill='none' stroke='%233f92c4' stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round'/><path d='M8.2 8 L9.2 3 L10.2 13 L11 8 H15' fill='none' stroke='%23cf9a3c' stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round'/></svg>">
<title>Project logs</title>
<style>{css}</style></head><body>
<div class="wrap">
<header class="hero">
  <div class="eyebrow">{eyebrow} &middot; project logs</div>
  <h1>Project logs</h1>
  <div class="path">{root}</div>
  <div class="range">{range}</div>
  <div class="stats">{stats}</div>
  {costnote}
</header>
<div class="axislbl"><span>{gfirst}</span><span class="lbl">all timeline entries, shared time axis
&middot; taller = more work after it</span><span>{glast}</span></div>
<div class="shelf">{rows}</div>
<footer>{n} projects &middot; {provenance} &middot; refreshed {refreshed}</footer>
</div>
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
    tot_cost, tot_cost_text, tot_cost_label, tot_cost_title = cost_display(all_by_model)
    n_days = (gl_dt.date() - gf_dt.date()).days + 1
    stat_cards = [
        (str(len(entries)), f'project{_s(len(entries))}'),
        (str(tot["sessions"]), f'session{_s(tot["sessions"])}'),
        (fmt_num(tot["inputs"]), f'input{_s(tot["inputs"])} typed'),
        (fmt_dur(tot["active"]), "active time"),
        (fmt_num(tot["tok"]), "tokens out"),
        (str(n_days), f'day{_s(n_days)} spanned'),
        (tot_cost_text, tot_cost_label, tot_cost_title),
    ]
    stats_html = _stat_cards_html(stat_cards)

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
            f'<b>{esc(fmt_dur(s["active_ms"]))}</b> active',
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

    all_tools = _session_tools(x for _, tl in entries for x in tl["sessions"])
    label = _eyebrow(all_tools)
    return INDEX_PAGE.format(
        generator_meta=GENERATOR_META,
        provenance=PAGE_PROVENANCE,
        css=CSS + INDEX_CSS,
        eyebrow=esc(label),
        root=esc(source_label or "Claude Code and Codex data"),
        range=(f'<b>{esc(fmt_date(gfirst))}</b> &rarr; <b>{esc(fmt_date(glast))}</b>'
               f' &middot; {len(entries)} projects'
               f' &middot; refreshed {refresh_stamp(refreshed, bold=True)}'),
        stats=stats_html,
        gfirst=esc(fmt_date(gfirst)), glast=esc(fmt_date(glast)),
        rows="".join(rows),
        n=len(entries),
        refreshed=refresh_stamp(refreshed),
        js=REFRESH_JS,
        costnote=cost_method_html(all_by_model, "all projects"),
    )


PAGE = """<!doctype html><html lang="en"><head>
{generator_meta}
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'><path d='M1 8 H3 L4 4 L5 12 L6 8 H8.2' fill='none' stroke='%233f92c4' stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round'/><path d='M8.2 8 L9.2 3 L10.2 13 L11 8 H15' fill='none' stroke='%23cf9a3c' stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round'/></svg>">
<title>{title} · project log</title>
<style>{css}</style></head><body>
{minimap}
{topbar}
<div class="wrap">
<header class="hero">
  <div class="eyebrow">{eyebrow} &middot; project log</div>
  <h1>{project}</h1>
  <div class="path">{path}</div>
  <div class="range">{range}</div>
  <div class="stats">{stats}</div>
  <div class="meta-row">{chips}{session_filter}</div>
  {costnote}
</header>
<div class="log">{timeline}</div>
<footer>{n_inputs} input{input_suffix} typed &middot; {provenance} &middot; last activity {last_activity} &middot; refreshed {refreshed}</footer>
</div>
<script>{js}</script>
</body></html>"""


def _merge_timelines(tls):
    """Merge timelines for the same project into one: sessions in chronological
    order, each session's milestone chunk kept together, stats recomputed."""
    if len(tls) == 1:
        return tls[0]
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
