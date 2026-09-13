"""The pages, rendered from the layer's own documents and from nothing else.

Every value on a page comes out of a `web-read` or `web-run` document. Nothing here recomputes a
state, decides whether an agent is alive, or fills a gap with a plausible value — a section whose
source refused says so, in its own words, where the list would have been.

That is the one rendering rule worth stating twice, because it is criterion 2: **an empty list and
"I could not find out" are different things, and they look different.** An empty section is a quiet
line saying there is nothing; an unavailable one is a marked block carrying the reason the source
gave and the age of whatever is being shown instead. A dashboard that drew both as blank space
would tell an operator that the pipeline is idle at the exact moment it has lost sight of it.

The pages are server-rendered, so what a source said is in the markup rather than assembled later
by a script that may not run. The only thing the script does is tail a card's events from a cursor
the client itself holds, and narrow the issue list to the product a sprint form has selected -- both
of which leave a page that works when it does not run.

The sprint form obeys the same rule twice over. Every choice on it is an entry of `sprint_options`,
so a registry or a board that holds something else offers something else, and nothing about a
product, an issue, a project or a head profile is written into this module. And a refused
submission is rendered from the very object that was submitted, so what a person typed comes back
on the screen exactly as they typed it.
"""

from __future__ import annotations

from html import escape
from typing import Any
from urllib.parse import quote

TITLE = "secretary"

#: Said on every page. This application still has no authentication of any kind of its own, so
#: where it may listen is not a deployment preference; see :mod:`secretary.web.server`. What
#: changed with DoD 5 is what stands in front of it, not what it is: a request that arrived from
#: off this host passed TLS and a password at the front (:mod:`secretary.webfront`) before it
#: reached this process, and there is no path here that does not.
LOOPBACK_NOTICE = (
    "local only — this application has no password, no TLS and no authorisation of its own and is "
    "refused a non-loopback address; anything reaching it from outside came through the guarded "
    "front"
)

STYLE = """
:root {
  color-scheme: light dark;
  --ground: #f3f5f8; --surface: #ffffff; --raised: #eef1f5; --line: #d9dfe7; --line-strong: #b9c2cd;
  --ink: #18212c; --muted: #5d6b7a; --faint: #8a95a3;
  --accent: #2456a6; --accent-ink: #ffffff; --accent-soft: #e4ecf9;
  --ok: #1e7f4f; --ok-soft: #dff3e8; --warn: #a8600f; --warn-soft: #fbeedb; --bad: #b3261e; --bad-soft: #fbe3e1;
  --mono: "IBM Plex Mono", ui-monospace, "SFMono-Regular", Menlo, monospace;
  --sans: "IBM Plex Sans", system-ui, -apple-system, "Segoe UI", sans-serif;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --ground: #11161d; --surface: #1a2028; --raised: #222a34; --line: #2c3541; --line-strong: #40495a;
    --ink: #e7ebf0; --muted: #a0abb8; --faint: #6f7b89;
    --accent: #8db1f0; --accent-ink: #0f1a2e; --accent-soft: #223252;
    --ok: #5fcf8f; --ok-soft: #173627; --warn: #f0b060; --warn-soft: #3a2a12; --bad: #f28b84; --bad-soft: #3d1c1a;
  }
}
:root[data-theme="dark"] {
  --ground: #11161d; --surface: #1a2028; --raised: #222a34; --line: #2c3541; --line-strong: #40495a;
  --ink: #e7ebf0; --muted: #a0abb8; --faint: #6f7b89;
  --accent: #8db1f0; --accent-ink: #0f1a2e; --accent-soft: #223252;
  --ok: #5fcf8f; --ok-soft: #173627; --warn: #f0b060; --warn-soft: #3a2a12; --bad: #f28b84; --bad-soft: #3d1c1a;
}
* { box-sizing: border-box; }
html { background: var(--ground); }
body { margin: 0; background: var(--ground); color: var(--ink); font: 14px/1.5 var(--sans); }
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
h1, h2, h3 { margin: 0; text-wrap: balance; }
h1 { font-size: 1.35rem; font-weight: 600; letter-spacing: -.01em; }
h2 { font-size: .8rem; font-weight: 600; text-transform: uppercase; letter-spacing: .06em; color: var(--muted); }
h3 { font-size: 1rem; font-weight: 600; }
code, .mono, time, .ref { font-family: var(--mono); font-size: .86em; }
pre { white-space: pre-wrap; overflow-x: auto; margin: 0; font: .86rem/1.5 var(--mono); }

/* the top bar: product, navigation, where you are */
.top { position: sticky; top: 0; z-index: 5; background: var(--surface); border-bottom: 1px solid var(--line); }
.top .row { max-width: 1280px; margin: 0 auto; padding: 0 20px; display: flex; align-items: center; gap: 1.5rem; min-height: 3rem; flex-wrap: wrap; }
.brand { font-weight: 600; letter-spacing: -.01em; color: var(--ink); }
.brand:hover { text-decoration: none; }
nav.primary { display: flex; gap: .25rem; }
nav.primary a { padding: .8rem .6rem; color: var(--muted); border-bottom: 2px solid transparent; }
nav.primary a:hover { color: var(--ink); text-decoration: none; }
nav.primary a[aria-current="page"] { color: var(--ink); border-bottom-color: var(--accent); }
.crumbs { display: flex; align-items: center; gap: .4rem; color: var(--muted); font-size: .85rem; }
.crumbs .sep { color: var(--faint); }
.crumbs .here { color: var(--ink); font-family: var(--mono); }
.top .notice { margin-left: auto; color: var(--faint); font-size: .72rem; max-width: 22rem; text-align: right; line-height: 1.25; }

/* the page */
main { max-width: 1280px; margin: 0 auto; padding-block: 1.25rem 4rem; padding-inline: 20px; }
.lead { display: flex; align-items: baseline; gap: 1rem; flex-wrap: wrap; margin-bottom: 1rem; }
.lead .age { margin-left: auto; }
.age { color: var(--faint); font-size: .8rem; font-family: var(--mono); }
.grid { display: grid; grid-template-columns: minmax(0, 7fr) minmax(0, 4fr); gap: 1rem; align-items: start; }
.grid > .col { display: grid; gap: 1rem; min-width: 0; }
@media (max-width: 900px) { .grid { grid-template-columns: minmax(0, 1fr); } }

/* panels: one surface per subject */
.panel { background: var(--surface); border: 1px solid var(--line); border-radius: 6px; min-width: 0; }
.panel > header { display: flex; align-items: center; gap: .6rem; padding: .6rem .9rem; border-bottom: 1px solid var(--line); }
.panel > header .count { font-family: var(--mono); font-size: .8rem; color: var(--muted); }
.panel > header .more { margin-left: auto; font-size: .85rem; }
.panel > .body { padding: .75rem .9rem; }
.panel > .body > * + * { margin-top: .6rem; }
.panel table { margin: -.25rem 0; }
.stack > * + * { margin-top: .6rem; }
details.panel > summary { list-style: none; cursor: pointer; padding: .6rem .9rem; font-weight: 600; color: var(--muted); font-size: .8rem; text-transform: uppercase; letter-spacing: .06em; }
details.panel > summary::-webkit-details-marker { display: none; }
details.panel > summary::before { content: "▸ "; color: var(--faint); }
details.panel[open] > summary::before { content: "▾ "; }
details.panel > .body { padding: .25rem .9rem .9rem; }

/* the pipeline strip */
.strip { display: flex; align-items: center; gap: 1rem; flex-wrap: wrap; padding: .7rem .9rem; }
.strip .facts { margin-left: auto; }
.light { display: inline-flex; align-items: center; gap: .45rem; padding: .3rem .7rem; border-radius: 999px; font-weight: 600; font-size: .85rem; border: 1px solid transparent; }
.light::before { content: ""; width: .55rem; height: .55rem; border-radius: 50%; background: currentColor; }
.light-running, .light-ok { color: var(--ok); background: var(--ok-soft); }
.light-drain, .light-attention { color: var(--warn); background: var(--warn-soft); }
.light-freeze, .light-bad { color: var(--bad); background: var(--bad-soft); }
.light-unknown { color: var(--muted); background: var(--raised); }

/* chips: state in form */
.chip { display: inline-block; padding: .05rem .5rem; border-radius: 999px; font-size: .78rem; font-weight: 600; line-height: 1.5; background: var(--raised); color: var(--muted); white-space: nowrap; }
.chip-ok { background: var(--ok-soft); color: var(--ok); }
.chip-warn { background: var(--warn-soft); color: var(--warn); }
.chip-bad { background: var(--bad-soft); color: var(--bad); }
.chip-accent { background: var(--accent-soft); color: var(--accent); }
.state { font-family: var(--mono); font-size: .85rem; }
.state-running { color: var(--ok); }
.state-process_failed, .state-stopped { color: var(--bad); }
.state-source_unavailable, .state-unknown, .state-not_started { color: var(--warn); }
.reason, .muted { color: var(--muted); }
.reason { font-size: .85rem; }
.empty { color: var(--faint); font-style: italic; }
.facts { color: var(--muted); font-size: .85rem; display: flex; flex-wrap: wrap; gap: .25rem 1rem; }
.facts b { color: var(--ink); font-weight: 600; font-family: var(--mono); font-size: .95em; }
.problems { margin: 0; padding: 0; list-style: none; display: grid; gap: .3rem; }
.problems li { padding-left: .9rem; position: relative; color: var(--ink); }
.problems li::before { content: ""; position: absolute; left: 0; top: .55em; width: .4rem; height: .4rem; border-radius: 50%; background: var(--warn); }
.unavailable { border-left: 3px solid var(--warn); background: var(--warn-soft); padding: .5rem .75rem; border-radius: 0 4px 4px 0; margin: 0; }
.unavailable b { color: var(--warn); }

/* tables */
table { border-collapse: collapse; width: 100%; font-variant-numeric: tabular-nums; }
th, td { text-align: left; padding: .4rem .6rem .4rem 0; vertical-align: top; }
th { font-weight: 600; color: var(--muted); font-size: .74rem; text-transform: uppercase; letter-spacing: .05em; border-bottom: 1px solid var(--line); }
tr + tr td { border-top: 1px solid var(--line); }
td:last-child, th:last-child { padding-right: 0; }
.scroll { overflow-x: auto; }
table.kv th { text-transform: none; letter-spacing: 0; font-size: .85rem; border-bottom: 0; width: 11rem; color: var(--muted); font-weight: 500; }
table.kv tr + tr th { border-top: 1px solid var(--line); }
.feed td:first-child { white-space: nowrap; color: var(--muted); }
.feed .actor { color: var(--muted); font-size: .85rem; }
.outcome-success { color: var(--ok); }
.outcome-failure, .outcome-refused, .outcome-error { color: var(--bad); }

/* sprints */
.sprint-card { border: 1px solid var(--line); border-radius: 6px; background: var(--surface); }
.sprint-card > header { display: flex; align-items: center; gap: .6rem; flex-wrap: wrap; padding: .6rem .9rem; border-bottom: 1px solid var(--line); }
.sprint-card > header h3 a { color: var(--ink); font-family: var(--mono); font-size: .95rem; }
.sprint-card > .body { padding: .6rem .9rem .75rem; }
.sprint-card .goal { margin: 0 0 .6rem; max-width: 65ch; }
.budget { display: inline-block; width: 7rem; height: .45rem; border: 1px solid var(--line-strong); border-radius: 999px; vertical-align: middle; overflow: hidden; background: var(--raised); }
.budget i { display: block; height: 100%; background: var(--ok); }
.budget.signal i { background: var(--warn); }
.budget.hard i { background: var(--bad); }
.hero { display: flex; align-items: flex-start; gap: 1rem; flex-wrap: wrap; margin-bottom: 1rem; }
.hero h1 { font-family: var(--mono); font-weight: 600; }
.hero .title { font-size: 1.05rem; color: var(--ink); flex-basis: 100%; max-width: 70ch; }
.hero .chips { display: flex; gap: .4rem; flex-wrap: wrap; align-items: center; }

/* forms and actions */
form { margin: 0; }
form.inline { display: flex; flex-wrap: wrap; gap: .5rem; align-items: flex-end; }
label { display: block; font-size: .78rem; color: var(--muted); margin-bottom: .15rem; }
input, select, textarea { font: inherit; color: var(--ink); background: var(--surface); border: 1px solid var(--line-strong); border-radius: 4px; padding: .35rem .5rem; max-width: 100%; }
textarea { width: 100%; min-height: 4rem; resize: vertical; }
button { font: inherit; font-weight: 600; cursor: pointer; border-radius: 4px; padding: .38rem .8rem; border: 1px solid var(--accent); background: var(--accent); color: var(--accent-ink); }
button.quiet { background: transparent; color: var(--accent); }
button.danger { background: transparent; border-color: var(--warn); color: var(--warn); }
button:hover { filter: brightness(1.08); }
form.act { display: block; }
form.act + form.act { border-top: 1px solid var(--line); padding-top: .75rem; margin-top: .75rem; }
form.act .row { display: flex; flex-wrap: wrap; gap: .6rem; align-items: flex-end; margin-top: .5rem; }
form.act .row label { margin: 0; display: flex; align-items: center; gap: .35rem; color: var(--ink); font-size: .85rem; }
form.act .hint { color: var(--faint); font-size: .78rem; margin: .4rem 0 0; }
.feedback:not(:empty) { border-left: 3px solid var(--accent); background: var(--accent-soft); padding: .4rem .75rem; margin-top: .5rem; border-radius: 0 4px 4px 0; font-size: .9rem; }
.feedback.bad { border-left-color: var(--bad); background: var(--bad-soft); }
#feedback:not(:empty) { border-left: 3px solid var(--accent); background: var(--accent-soft); padding: .4rem .75rem; margin-top: .5rem; }
#feedback.bad { border-left-color: var(--bad); background: var(--bad-soft); }
.refresh { display: flex; align-items: center; gap: .35rem; font-size: .8rem; color: var(--muted); margin-left: auto; }
.refresh input { margin: 0; }
.filters { display: flex; gap: .25rem; flex-wrap: wrap; }
.filters a { padding: .2rem .6rem; border-radius: 999px; border: 1px solid var(--line); color: var(--muted); font-size: .85rem; }
.filters a[aria-current="true"] { background: var(--accent-soft); color: var(--accent); border-color: transparent; }
.filters a:hover { text-decoration: none; color: var(--ink); }

/* long text folds; the first line is the summary */
details.text > summary { list-style: none; cursor: pointer; color: var(--ink); }
details.text > summary::-webkit-details-marker { display: none; }
details.text > summary::after { content: " more"; color: var(--accent); font-size: .8rem; }
details.text[open] > summary::after { content: " less"; }
details.text > pre { margin-top: .4rem; }

/* the transition timeline */
ol.timeline { list-style: none; margin: 0; padding: 0; }
ol.timeline > li { border-top: 1px solid var(--line); }
ol.timeline > li:first-child { border-top: 0; }
ol.timeline details > summary { list-style: none; cursor: pointer; display: grid; grid-template-columns: 5.2rem 1fr; gap: .6rem; padding: .35rem 0; align-items: baseline; }
ol.timeline details > summary::-webkit-details-marker { display: none; }
ol.timeline details > summary:hover { background: var(--raised); }
ol.timeline .arrow { color: var(--faint); margin: 0 .2rem; }
ol.timeline .why { color: var(--muted); font-size: .85rem; display: block; }
ol.timeline .detail { padding: .3rem 0 .6rem 5.8rem; display: grid; gap: .4rem; }
ol.timeline .record { border-left: 2px solid var(--line-strong); padding-left: .6rem; }
ol.timeline .record .who { font-size: .8rem; color: var(--muted); }
@media (max-width: 600px) { ol.timeline .detail { padding-left: 0; } }

/* events */
ol.events { list-style: none; padding: 0; margin: 0; }
ol.events li { display: grid; grid-template-columns: 11.5rem 1fr; gap: .6rem; padding: .35rem 0; border-top: 1px solid var(--line); }
ol.events li:first-child { border-top: 0; }
ol.events time { color: var(--muted); }
ol.events b { font-weight: 600; }

/* the sprint form */
form.sprint { display: block; max-width: 48rem; }
form.sprint .field { margin: .9rem 0; }
form.sprint textarea { min-height: 4.5rem; }
form.sprint select { min-width: 22rem; max-width: 100%; }
form.sprint .choices { border: 1px solid var(--line); border-radius: 4px; padding: .4rem .6rem; max-height: 14rem; overflow-y: auto; }
form.sprint .choices label { display: block; font-size: inherit; color: inherit; padding: .1rem 0; }
form.sprint .hint { color: var(--muted); font-size: .8rem; }
.bad-field { color: var(--bad); font-size: .85rem; }
.refused, .pending { border-left: 3px solid var(--warn); background: var(--warn-soft); padding: .5rem .75rem; margin: .5rem 0; border-radius: 0 4px 4px 0; }
.launch { font-weight: 600; }
@media (prefers-reduced-motion: no-preference) { .light::before { transition: background .2s; } }
"""


# -- the shell ----------------------------------------------------------------------------------


#: The primary navigation: where a person can go from anywhere. Keys are what a page names itself
#: as, so the current one is marked; the order is the order of use.
NAV: tuple[tuple[str, str, str], ...] = (
    ("dashboard", "/", "Dashboard"),
    ("sprints", "/sprints", "Sprints"),
    ("history", "/history", "History"),
    ("new-sprint", "/sprints/new", "New sprint"),
)

FONTS = "https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap"


def _page(
    title: str,
    body: str,
    *,
    script: str = "",
    nav: str = "",
    crumbs: tuple[tuple[str, str], ...] = (),
) -> str:
    """The shell every page shares: the top bar with the product, the navigation and the crumbs.

    `nav` names the primary entry this page belongs to, and the navigation marks it; `crumbs` is
    what is open under it -- normally one identifier, the card or the sprint -- shown beside the
    navigation and never repeating it.
    """
    current = ' aria-current="page"'
    links = "".join(
        f'<a href="{escape(href)}"{current if key == nav else ""}>{escape(label)}</a>'
        for key, href, label in NAV
    )
    trail = ""
    if crumbs:
        parts = []
        for index, (label, href) in enumerate(crumbs):
            if index == len(crumbs) - 1:
                parts.append(f'<span class="here">{escape(label)}</span>')
            else:
                parts.append(f'<a href="{escape(href)}">{escape(label)}</a><span class="sep">/</span>')
        trail = f'<div class="crumbs">{"".join(parts)}</div>'
    return "\n".join(
        [
            "<!doctype html>",
            '<html lang="en"><head><meta charset="utf-8">',
            '<meta name="viewport" content="width=device-width, initial-scale=1">',
            f"<title>{escape(title)} · {escape(TITLE)}</title>",
            '<link rel="preconnect" href="https://fonts.googleapis.com">',
            f'<link rel="stylesheet" href="{FONTS}">',
            f"<style>{STYLE}</style>",
            "</head><body>",
            '<header class="top"><div class="row">',
            f'<a class="brand" href="/">{escape(TITLE)}</a>',
            f'<nav class="primary" aria-label="primary">{links}</nav>',
            trail,
            f'<p class="notice" title="{escape(LOOPBACK_NOTICE)}">loopback transport behind the guarded front</p>',
            "</div></header>",
            "<main>",
            body,
            "</main>",
            f"<script>{script}</script>" if script else "",
            "</body></html>",
        ]
    )


def _panel(title: str, body: str, *, count: Any = None, more: str = "", open_: bool | None = None) -> str:
    """One surface for one subject: a header naming it, a count when there is one, and the body.

    `open_` turns the panel into a disclosure that starts open or closed; `None` is a plain panel.
    """
    counted = "" if count is None else f'<span class="count">{escape(str(count))}</span>'
    if open_ is None:
        return (
            f'<section class="panel"><header><h2>{escape(title)}</h2>{counted}{more}</header>'
            f'<div class="body">{body}</div></section>'
        )
    return (
        f'<details class="panel"{" open" if open_ else ""}><summary>{escape(title)}{" " + counted if counted else ""}</summary>'
        f'<div class="body">{body}</div></details>'
    )


def _chip(text: str, tone: str = "") -> str:
    tone_class = f" chip-{tone}" if tone else ""
    return f'<span class="chip{tone_class}">{escape(text)}</span>'


#: The tone a card state reads in. Semantic colour beside the state's own word, never instead.
STATE_TONES: dict[str, str] = {
    "in_progress": "accent",
    "validate": "accent",
    "reviewing": "accent",
    "assessment": "warn",
    "blocked": "bad",
    "done": "ok",
    "ready": "",
    "issues": "",
}


def _state_chip(state: Any) -> str:
    word = str(state or "unknown")
    return _chip(word.replace("_", " "), STATE_TONES.get(word, ""))


def error(status: int, code: str, message: str) -> str:
    """A refusal as a page: the status, the protocol code that produced it, and what it said."""
    body = "\n".join(
        [
            f"<h2>{status} — {escape(code)}</h2>",
            f'<p class="unavailable"><b>this request was refused.</b> {escape(message)}</p>',
            '<p><a href="/">back to the dashboard</a></p>',
        ]
    )
    return _page(f"{status} {code}", body)


# -- source rendering ---------------------------------------------------------------------------


def _source_block(source: dict[str, Any] | None, *, what: str) -> str:
    """The marked block an unavailable source gets, or nothing when it answered."""
    source = source or {}
    if source.get("state") == "available":
        return ""
    age = source.get("data_age_seconds")
    stale = "" if age is None else f' <span class="age">showing evidence {_age(age)} old</span>'
    reason = escape(str(source.get("reason") or "no reason was recorded"))
    return f'<p class="unavailable"><b>could not find out {escape(what)}:</b> {reason}{stale}</p>'


def _section(source: dict[str, Any] | None, items: list[Any], *, what: str, empty: str, table: str) -> str:
    """One section: the source's refusal if it refused, then the rows, or the empty line.

    Both are rendered when a source refused and stale rows are still worth showing, and the two are
    never confused: the refusal is above the rows, so nobody reads an old list as a current one.
    """
    parts = [_source_block(source, what=what)]
    if items:
        parts.append(table)
    elif (source or {}).get("state") == "available":
        parts.append(f'<p class="empty">{escape(empty)}</p>')
    return "\n".join(part for part in parts if part)


def _age(seconds: Any) -> str:
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        return "an unknown time"
    if value < 90:
        return f"{int(value)}s"
    if value < 5400:
        return f"{int(value // 60)}m"
    return f"{int(value // 3600)}h"


def _rows(headers: list[str], rows: list[list[str]]) -> str:
    """A table. Headerless two-column rows are a key/value list and are drawn as one."""
    if headers and not any(headers):
        body = "".join(f"<tr><th>{row[0]}</th><td>{row[1]}</td></tr>" for row in rows)
        return f'<table class="kv"><tbody>{body}</tbody></table>'
    head = "".join(f"<th>{escape(name)}</th>" for name in headers)
    body = "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows)
    return f'<div class="scroll"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def _state_cell(state: str, reason: str) -> str:
    return (
        f'<span class="state state-{escape(state)}">{escape(state)}</span>'
        f'<div class="reason">{escape(reason)}</div>'
    )


def _link(ref: str) -> str:
    return f'<a class="ref" href="/tasks/{quote(ref)}">{escape(ref)}</a>'


def _or_dash(value: Any) -> str:
    return escape(str(value)) if value not in (None, "") else "—"


# -- the dashboard ------------------------------------------------------------------------------


def dashboard(
    snapshot: dict[str, Any],
    *,
    pause: dict[str, Any] | None = None,
    sprints: dict[str, Any] | None = None,
    commands: dict[str, Any] | None = None,
) -> str:
    """The operator's one screen: the pipeline's state, the open sprints, what is in flight, and
    what happened last.

    Three of the four sections come from reads beside the snapshot, each handed in as
    ``{"available", "reason", "document"}`` by the transport; one that is not available is drawn
    as the marked block every unreadable source gets, and never as an empty section.
    """
    installation = snapshot.get("installation") or {}
    projects = snapshot.get("projects") or {}
    tasks = snapshot.get("tasks") or {}
    agents = snapshot.get("agents") or {}
    task_items = list(tasks.get("items") or [])
    agent_items = list(agents.get("items") or [])
    open_items = _sprint_items(sprints)
    body = "\n".join(
        [
            '<div class="lead"><h1>Dashboard</h1>',
            f'<span class="age">read at {escape(str(snapshot.get("observed_at") or "an unknown time"))}</span>',
            '<label class="refresh"><input type="checkbox" id="auto-refresh"> refresh every 30 s</label></div>',
            '<section class="panel">',
            _pipeline_strip(pause, installation),
            '<div class="body" id="pause-feedback-holder"><p id="pause-feedback" class="feedback"></p></div>',
            "</section>",
            '<div class="grid" style="margin-top:1rem">',
            '<div class="col">',
            _panel(
                "Open sprints",
                _open_sprints(sprints),
                count=len(open_items) if open_items else None,
                more='<a class="more" href="/sprints">all sprints</a>',
            ),
            _panel(
                "In flight",
                _section(
                    tasks.get("source"),
                    task_items,
                    what="which cards the pipeline is carrying",
                    empty="no card is in flight.",
                    table=_task_table(task_items),
                )
                + "<h3 style=\"margin-top:.9rem;font-size:.8rem;color:var(--muted)\">agents</h3>"
                + _section(
                    agents.get("source"),
                    agent_items,
                    what="which agents are running",
                    empty="no agent is running.",
                    table=_agent_table(agent_items, with_ref=True),
                ),
                count=len(task_items) if task_items else None,
            ),
            "</div>",
            '<div class="col">',
            _panel("Health", _health_panel(installation)),
            _panel(
                "Recent commands",
                _feed(commands, compact=True),
                more='<a class="more" href="/history">whole history</a>',
            ),
            _panel(
                "Start a run by hand",
                _start_form(list(projects.get("items") or []), task_items) + '<p id="feedback"></p>',
                open_=False,
            ),
            _panel(
                "Projects",
                _section(
                    projects.get("source"),
                    list(projects.get("items") or []),
                    what="which projects are registered",
                    empty="this installation has no registered project.",
                    table=_project_table(list(projects.get("items") or [])),
                ),
                count=len(projects.get("items") or []) or None,
                open_=False,
            ),
            _panel("Installation", _installation(installation), open_=False),
            "</div></div>",
        ]
    )
    return _page(
        "Dashboard", body, script=_DASHBOARD_SCRIPT + _ACTIONS_SCRIPT + _REFRESH_SCRIPT, nav="dashboard"
    )


def _sprint_items(section: dict[str, Any] | None) -> list[dict[str, Any]]:
    document = (section or {}).get("document") if (section or {}).get("available") else None
    if not isinstance(document, dict):
        return []
    return [item for item in (document.get("sprints") or {}).get("items") or [] if isinstance(item, dict)]


def _beside(section: dict[str, Any] | None, *, what: str) -> tuple[dict[str, Any] | None, str]:
    """A read made beside the page's own: its document, or the marked block saying why none."""
    if not section or not section.get("available") or not isinstance(section.get("document"), dict):
        reason = str((section or {}).get("reason") or "this section was not read")
        return None, (
            f'<div class="unavailable"><b>could not read {escape(what)}.</b> {escape(reason)}</div>'
        )
    return section["document"], ""


# -- the pipeline bar -----------------------------------------------------------------------------

#: The word for each pause mode, and the colour class that is its second reading.
PAUSE_WORDS: dict[str, tuple[str, str]] = {
    "running": ("running — the dispatcher claims cards and raises heads", "running"),
    "drain": ("drained — no new card is claimed; running heads finish their work", "drain"),
    "freeze": ("frozen — no new card is claimed and the heads were stopped", "freeze"),
    "soft": ("drained — no new card is claimed; running heads finish their work", "drain"),
    "hard": ("frozen — no new card is claimed and the heads were stopped", "freeze"),
}


def _pipeline_strip(section: dict[str, Any] | None, installation: dict[str, Any]) -> str:
    """The first thing on the screen: is the pipeline running, and is the installation healthy."""
    document, refused = _beside(section, what="whether the pipeline is paused")
    if document is None:
        return f'<div class="strip">{refused}{_health_light(installation)}</div>'
    state = document.get("state") or {}
    paused = bool(state.get("paused"))
    mode = str(state.get("mode") or "") if paused else "running"
    words, colour = PAUSE_WORDS.get(mode, (f"{mode or 'unknown'} — a pause mode this page does not know", "unknown"))
    facts = []
    if paused:
        facts.append(f"since <b>{_or_dash(state.get('since'))}</b>")
        facts.append(f"by <b>{_or_dash(state.get('actor'))}</b>")
        if state.get("pause_reason"):
            facts.append(f"because {escape(str(state.get('pause_reason')))}")
    dispatcher = document.get("dispatcher") or {}
    facts.append(f"dispatcher <b>{_or_dash(dispatcher.get('phase'))}</b>")
    if dispatcher.get("tracked_cards") is not None:
        facts.append(f"tracking <b>{escape(str(dispatcher.get('tracked_cards')))}</b> card(s)")
    if paused:
        action = (
            '<form class="pause inline" data-action="/api/pause/resume" data-confirm="Resume the pipeline? '
            'A frozen pipeline\'s heads are relaunched in their workspaces.">'
            '<button type="submit">Resume</button></form>'
        )
    else:
        action = (
            '<form class="pause inline" data-action="/api/pause/drain" data-confirm="Drain the pipeline? No '
            'new card is claimed until it is resumed; running heads keep working.">'
            '<input name="reason" id="drain-reason" placeholder="why" required size="26" aria-label="reason">'
            '<button type="submit" class="danger">Drain</button></form>'
        )
    return "\n".join(
        [
            '<div class="strip">',
            f'<span class="light light-{escape(colour)}" title="{escape(words)}">{escape(words.split(" — ")[0])}</span>',
            action,
            _health_light(installation),
            f'<span class="facts">{" · ".join(facts)}</span>',
            "</div>",
            _source_block(state.get("source"), what="the pause flag"),
        ]
    )


def _health_light(installation: dict[str, Any]) -> str:
    health = installation.get("health") or {}
    status = health.get("status")
    if not isinstance(status, dict) or not status:
        return '<span class="light light-unknown" title="health could not be read">health unknown</span>'
    state = str(status.get("state") or "unknown")
    problems = list(status.get("problems") or [])
    colour = "ok" if state == "ok" else ("attention" if state == "attention" else "unknown")
    suffix = f" · {len(problems)}" if problems else ""
    return f'<span class="light light-{escape(colour)}" title="installation health">health {escape(state)}{escape(suffix)}</span>'


def _health_panel(installation: dict[str, Any]) -> str:
    """Health as the read layer summarizes it: the problems by name, then the facts."""
    health = installation.get("health") or {}
    status = health.get("status")
    parts = [_source_block(health.get("source"), what="whether this installation is healthy")]
    if not isinstance(status, dict) or not status:
        if (health.get("source") or {}).get("state") == "available":
            parts.append('<p class="empty">the health collector answered with nothing.</p>')
        return "\n".join(part for part in parts if part)
    problems = [str(item) for item in status.get("problems") or []]
    if problems:
        parts.append('<ul class="problems">' + "".join(f"<li>{escape(item)}</li>" for item in problems) + "</ul>")
    else:
        parts.append('<p class="muted">nothing needs attention.</p>')
    checkpoint = status.get("checkpoint") or {}
    resources = status.get("resources") or {}
    cards = status.get("cards") or {}
    dispatcher = status.get("dispatcher") or {}
    rows = [
        [
            "checkpoint",
            f"{_or_dash(checkpoint.get('status'))}"
            + (
                f' <span class="muted">lag {escape(str(checkpoint.get("lag_minutes")))} min, {escape(str(checkpoint.get("lag_commits")))} commit(s)</span>'
                if checkpoint.get("lag_minutes") is not None
                else ""
            ),
        ],
        ["disk free", _gib(resources.get("disk_free_bytes"))],
        ["memory free", _gib(resources.get("memory_available_bytes"))],
        ["load", _load(resources.get("load_average"))],
        ["cards on the board", _or_dash(cards.get("total"))],
        ["active attempts", _or_dash(dispatcher.get("active_attempts"))],
        ["last tick", _or_dash(dispatcher.get("last_tick_finished_at"))],
        ["card backend", _or_dash(status.get("card_backend"))],
    ]
    parts.append(_rows(["", ""], rows))
    units = [
        unit
        for unit in status.get("units") or []
        if isinstance(unit, dict)
        and (unit.get("active") == "failed" or (str(unit.get("kind")) == "timer" and unit.get("active") not in (None, "active")))
    ]
    if units:
        parts.append(
            '<p class="facts">units not active: '
            + ", ".join(f"<b>{escape(str(unit.get('name')))}</b> ({escape(str(unit.get('active')))})" for unit in units)
            + "</p>"
        )
    return "\n".join(part for part in parts if part)


def _gib(value: Any) -> str:
    try:
        return f"{float(value) / (1024 ** 3):.1f} GiB"
    except (TypeError, ValueError):
        return "—"


def _load(value: Any) -> str:
    if not isinstance(value, list) or not value:
        return "—"
    try:
        return " ".join(f"{float(item):.2f}" for item in value[:3])
    except (TypeError, ValueError):
        return "—"


# -- the open sprints -----------------------------------------------------------------------------


def _open_sprints(section: dict[str, Any] | None) -> str:
    document, refused = _beside(section, what="the open sprints")
    if document is None:
        return refused
    listing = document.get("sprints") or {}
    items = [item for item in listing.get("items") or [] if isinstance(item, dict)]
    parts = [_source_block(listing.get("source"), what="which sprints are open")]
    if not items and (listing.get("source") or {}).get("state") == "available":
        parts.append('<p class="empty">no sprint is open. <a href="/sprints/new">Open one</a>.</p>')
    parts.append('<div class="stack">' + "\n".join(_sprint_card(item) for item in items) + "</div>")
    return "\n".join(part for part in parts if part)


def _sprint_card(item: dict[str, Any]) -> str:
    ref = str(item.get("ref") or "")
    goal = str(item.get("goal") or "")
    short = goal if len(goal) <= 240 else goal[:237].rstrip() + "…"
    return "\n".join(
        [
            '<article class="sprint-card">',
            "<header>",
            f'<h3><a href="/sprints/{quote(ref)}">{escape(ref)}</a></h3>',
            _chip(str(item.get("product") or "—")),
            _sprint_status_chip(item),
            _waiting_chip(item),
            "</header>",
            '<div class="body">',
            f'<p class="goal">{escape(short)}</p>',
            _sprint_work(item),
            _comment_form(f"/api/sprints/{quote(ref)}/comment", f"sprint.{ref}", "Tell the observer something"),
            "</div></article>",
        ]
    )


def _sprint_status_chip(item: dict[str, Any]) -> str:
    status = str(item.get("status") or "")
    return _chip(status or "unknown", {"open": "ok", "closed": "", "stopped": "bad"}.get(status, ""))


def _waiting_chip(item: dict[str, Any]) -> str:
    waiting = item.get("waiting") if isinstance(item.get("waiting"), dict) else {}
    state = str(waiting.get("state") or "")
    if not state:
        return ""
    tone = {"working": "accent", "waiting": "warn", "blocked": "bad", "ended": "", "unknown": "warn"}.get(state, "")
    return f'<span title="{escape(str(waiting.get("reason") or ""))}">{_chip("observer " + state, tone)}</span>'


def _sprint_work(item: dict[str, Any]) -> str:
    """What a sprint is doing right now, from the sections the listing and the page both carry."""
    rows: list[list[str]] = []
    current = item.get("current_task") if isinstance(item.get("current_task"), dict) else {}
    if isinstance(item.get("current_task"), str) and item.get("current_task"):
        # The sprint row's own spelling: the reference alone, before the work read says more.
        current = {"ref": item["current_task"], "live": None}
    if current.get("ref"):
        live = "" if current.get("live") is None else (" (live)" if current.get("live") else " (not live)")
        rows.append(["current card", f'{_link(str(current["ref"]))}<span class="muted">{escape(live)}</span>'])
    elif current:
        rows.append(["current card", f'<span class="empty">{escape(str(current.get("reason") or "none"))}</span>'])
    observer = item.get("observer") if isinstance(item.get("observer"), dict) else {}
    launch = observer.get("launch") or {}
    if launch:
        state = str(launch.get("state") or "")
        words = LAUNCH_WORDS.get(state, state or "unknown")
        rows.append(["observer", f'{_or_dash((observer.get("declared") or {}).get("profile"))} — <span class="state state-{escape(state)}">{escape(words)}</span>'])
    waiting = item.get("waiting") if isinstance(item.get("waiting"), dict) else {}
    if waiting:
        rows.append(["observer is", f'<b>{escape(str(waiting.get("state") or "unknown"))}</b> <span class="reason">{escape(_short(waiting.get("reason"), 140))}</span>'])
    checks = item.get("checks") if isinstance(item.get("checks"), dict) else {}
    if checks:
        gate = checks.get("gate") if isinstance(checks.get("gate"), dict) else {}
        rows.append(["checks", f'<b>{escape(str(gate.get("state") or "—"))}</b> <span class="reason">{escape(_short(checks.get("reason"), 140))}</span>'])
    budget = item.get("budget") if isinstance(item.get("budget"), dict) else {}
    if budget:
        rows.append(["budget", _budget(budget)])
    cards = item.get("cards") if isinstance(item.get("cards"), dict) else {}
    states = cards.get("states") if isinstance(cards.get("states"), dict) else {}
    if states:
        rows.append(
            [
                "cards",
                " · ".join(
                    f"{_state_chip(state)} " + ", ".join(_link(str(ref)) for ref in refs)
                    for state, refs in sorted(states.items())
                    if isinstance(refs, list) and refs
                ),
            ]
        )
    decision = item.get("decision") if isinstance(item.get("decision"), dict) else {}
    entry = decision.get("entry") if isinstance(decision.get("entry"), dict) else {}
    if entry:
        freshness = (decision.get("freshness") or {}).get("value") or {}
        stale = "" if freshness.get("fresh", True) else f' <span class="muted">(stale: {escape(str(freshness.get("error") or ""))})</span>'
        rows.append(["last decision", f'{_long(entry.get("selected_step"), chars=140)}{stale}<div class="reason">{_long(entry.get("selected_why"), chars=140)}</div>'])
        if entry.get("next_safe_step"):
            rows.append(["next step", _long(entry.get("next_safe_step"), chars=140)])
    if not rows:
        return '<p class="empty">nothing about this sprint\'s work could be read.</p>'
    return _rows(["", ""], rows)


def _short(text: Any, chars: int) -> str:
    value = str(text or "")
    return value if len(value) <= chars else value[: chars - 1].rstrip() + "…"


def _budget(budget: dict[str, Any]) -> str:
    total = int(budget.get("total") or 0)
    thresholds = budget.get("thresholds") or {}
    hard = int(thresholds.get("hard") or 0)
    signal = int(thresholds.get("signal") or 0)
    ratio = min(1.0, total / hard) if hard else 0.0
    colour = "hard" if budget.get("hard_reached") else ("signal" if budget.get("signal_reached") else "")
    by_type = budget.get("by_type") or {}
    spent = ", ".join(f"{escape(str(kind))} {escape(str(count))}" for kind, count in sorted(by_type.items()) if count)
    return (
        f'<span class="budget {colour}"><i style="width:{ratio * 100:.0f}%"></i></span> '
        f"{total} of {hard} (signal at {signal})"
        + (f' <span class="muted">— {spent}</span>' if spent else "")
    )


# -- the command feed -----------------------------------------------------------------------------


def _feed(section: dict[str, Any] | None, *, compact: bool = False) -> str:
    document, refused = _beside(section, what="the last commands")
    if document is None:
        return refused
    return _feed_table(document, compact=compact)


def _feed_table(document: dict[str, Any], *, compact: bool = False) -> str:
    commands = document.get("commands") or {}
    items = [item for item in commands.get("items") or [] if isinstance(item, dict)]
    parts = [_source_block(commands.get("source"), what="the command history")]
    if not items:
        if (commands.get("source") or {}).get("state") == "available":
            parts.append('<p class="empty">nothing has been recorded.</p>')
        return "\n".join(part for part in parts if part)
    rows = []
    for item in items:
        actor = item.get("actor") if isinstance(item.get("actor"), dict) else {}
        entity = item.get("entity") if isinstance(item.get("entity"), dict) else {}
        result = item.get("result") if isinstance(item.get("result"), dict) else {}
        outcome = str(result.get("outcome") or "")
        reason = str(result.get("reason") or "")
        cut = 90 if compact else 200
        shown = reason if len(reason) <= cut else reason[: cut - 3] + "…"
        when = str(item.get("occurred_at") or "")
        when_shown = when[11:19] if compact and len(when) >= 19 else when
        cells = [
            f'<time title="{escape(when)}">{escape(when_shown)}</time>',
            escape(str(item.get("action") or "")),
            _entity_link(entity),
            (
                f'<span class="outcome-{escape(outcome)}">{escape(outcome)}</span> '
                f'<span class="reason" title="{escape(reason)}">{escape(shown)}</span>'
            ),
        ]
        if not compact:
            cells.insert(1, f'<span class="actor">{escape(str(actor.get("role") or ""))} {escape(str(actor.get("id") or ""))}</span>')
        rows.append(cells)
    headers = ["when", "action", "on", "result"] if compact else ["when", "who", "action", "on", "result"]
    parts.append('<div class="feed">' + _rows(headers, rows) + "</div>")
    return "\n".join(part for part in parts if part)


def _entity_link(entity: dict[str, Any]) -> str:
    ref = str(entity.get("ref") or "")
    if not ref:
        return "—"
    if ref.startswith("sprint:"):
        return f'<a class="ref" href="/sprints/{quote(ref)}">{escape(ref)}</a>'
    if ref.startswith(("issue:", "product:")):
        return escape(ref)
    return _link(ref)


def commands(document: dict[str, Any]) -> str:
    """The whole command history, a page at a time, newest first."""
    listing = document.get("commands") or {}
    older = ""
    if listing.get("has_more") and listing.get("next_cursor"):
        older = f'<a class="more" href="/history?cursor={quote(str(listing["next_cursor"]))}&amp;limit={int(document.get("limit") or 25)}">older →</a>'
    body = "\n".join(
        [
            '<div class="lead"><h1>History</h1>',
            f'<span class="age">read at {escape(str(document.get("observed_at") or "an unknown time"))}</span></div>',
            _panel("Commands, newest first", _feed_table(document), more=older),
        ]
    )
    return _page("History", body, nav="history")


def sprints_page(document: dict[str, Any], *, statuses: list[str]) -> str:
    """Every sprint of the installation as a table, filtered by status."""
    listing = document.get("sprints") or {}
    items = [item for item in listing.get("items") or [] if isinstance(item, dict)]
    chosen = set(statuses)
    filters = []
    for value, label in (("open", "open"), ("stopped", "stopped"), ("closed", "closed"), ("", "all")):
        href = f"/sprints?status={value}" if value else "/sprints"
        active = chosen == {value} if value else not chosen
        marked = ' aria-current="true"' if active else ""
        filters.append(f'<a href="{href}"{marked}>{escape(label)}</a>')
    filters = "".join(filters)
    rows = []
    for item in items:
        ref = str(item.get("ref") or "")
        goal = str(item.get("goal") or "")
        current = item.get("current_task") if isinstance(item.get("current_task"), dict) else {}
        observer = item.get("observer") if isinstance(item.get("observer"), dict) else {}
        launch = observer.get("launch") or {}
        budget = item.get("budget") if isinstance(item.get("budget"), dict) else {}
        rows.append(
            [
                f'<a class="ref" href="/sprints/{quote(ref)}">{escape(ref)}</a>',
                _sprint_status_chip(item),
                escape(str(item.get("product") or "—")),
                escape(goal if len(goal) <= 110 else goal[:107].rstrip() + "…"),
                _link(str(current["ref"])) if current.get("ref") else "—",
                (
                    f'<span class="state state-{escape(str(launch.get("state") or ""))}">{escape(str(launch.get("state") or "—"))}</span>'
                    if launch
                    else "—"
                ),
                _budget(budget) if budget else "—",
            ]
        )
    table = _section(
        listing.get("source"),
        items,
        what="which sprints exist",
        empty="no sprint matches this filter.",
        table=_rows(["sprint", "status", "product", "goal", "current card", "observer", "budget"], rows),
    )
    body = "\n".join(
        [
            '<div class="lead"><h1>Sprints</h1>',
            f'<span class="age">read at {escape(str(document.get("observed_at") or "an unknown time"))}</span></div>',
            _panel("Sprints", f'<div class="filters">{filters}</div>' + table, count=len(items) or None,
                   more='<a class="more" href="/sprints/new">open a new sprint</a>'),
        ]
    )
    return _page("Sprints", body, nav="sprints")


# -- the owner's actions --------------------------------------------------------------------------


def _comment_form(action: str, key: str, label: str) -> str:
    return (
        f'<form class="act" data-action="{escape(action)}" data-key="{escape(key)}" data-kind="comment">'
        f'<label for="comment-{escape(key)}">{escape(label)}</label>'
        f'<textarea id="comment-{escape(key)}" name="body" required placeholder="a comment the head working this will read"></textarea>'
        '<div class="row"><button type="submit">Comment</button></div>'
        '<p class="feedback"></p>'
        "</form>"
    )


def _move_form(ref: str) -> str:
    options = "".join(f'<option value="{escape(target)}">{escape(target.replace("_", " "))}</option>' for target in MOVE_TARGETS)
    return (
        f'<form class="act" data-action="/api/tasks/{quote(ref)}/move" data-key="move.{escape(ref)}" data-kind="move">'
        '<label for="move-target">Move this card to</label>'
        f'<select id="move-target" name="target">{options}</select>'
        '<div class="row" style="display:block"><label for="move-reason" style="display:block">why</label>'
        '<textarea id="move-reason" name="reason" required placeholder="why the owner moves it"></textarea></div>'
        '<div class="row"><label><input type="checkbox" name="sprint_override" id="move-override"> past its sprint\'s reservation</label>'
        '<input name="sprint_override_reason" id="move-override-reason" placeholder="why the sprint is overridden" size="34"></div>'
        '<div class="row"><button type="submit" class="danger">Move</button></div>'
        '<p class="feedback"></p>'
        '<p class="hint">a decision on a parked card is the observer\'s; the owner\'s intervention is a move '
        "with a reason, and the audit says so.</p>"
        "</form>"
    )


def _close_form(ref: str) -> str:
    return (
        f'<form class="act" data-action="/api/sprints/{quote(ref)}/close" data-key="close.{escape(ref)}" data-kind="close">'
        '<label for="close-reason">Close this sprint</label>'
        '<input name="reason" id="close-reason" required placeholder="why the owner closes it" style="width:100%">'
        '<div class="row" style="display:block"><label for="close-closeout">closeout — what became of the work, written into state/knowledge</label>'
        '<textarea id="close-closeout" name="closeout" required></textarea></div>'
        '<div class="row" style="display:block"><label for="close-decisions">decisions, optional, as the CLI\'s decisions file</label>'
        '<textarea id="close-decisions" name="decisions" class="mono" placeholder="issues:\n  - {ref: issue:…, verdict: …, reason: …}\ncards:\n  - {ref: …, verdict: done|drop, reason: …}"></textarea></div>'
        '<div class="row"><button type="submit" class="danger">Close sprint</button></div>'
        '<p class="feedback"></p>'
        '<p class="hint">a close is not a completed Definition of Done; the sprint\'s own document says so.</p>'
        "</form>"
    )


#: The states a move may name, in the layer's spelling. Kept beside the form that offers them.
MOVE_TARGETS = ("ready", "in_progress", "done", "blocked", "issues", "validate", "assessment")


def _installation(installation: dict[str, Any]) -> str:
    health = installation.get("health") or {}
    status = health.get("status")
    rows = [
        ["instance", _or_dash(installation.get("instance"))],
        ["name", _or_dash(installation.get("name"))],
        ["data dir", _or_dash(installation.get("data_dir"))],
    ]
    parts = [
        _rows(["", ""], rows),
        _source_block(health.get("source"), what="whether this installation is healthy"),
    ]
    if isinstance(status, dict):
        summary = status.get("summary") if isinstance(status.get("summary"), dict) else {}
        overall = summary.get("state") or status.get("state") or "collected"
        parts.append(f'<p>health: <span class="state">{escape(str(overall))}</span></p>')
    elif (health.get("source") or {}).get("state") == "available":
        parts.append('<p class="empty">the health collector answered with nothing.</p>')
    return "\n".join(part for part in parts if part)


def _project_table(items: list[dict[str, Any]]) -> str:
    return _rows(
        ["project", "repo", "adapter", "branch", "enabled"],
        [
            [
                escape(str(item.get("id") or "")),
                _or_dash(item.get("repo")),
                _or_dash(item.get("adapter")),
                _or_dash(item.get("default_branch")),
                "yes" if item.get("enabled") else "no",
            ]
            for item in items
        ],
    )


def _task_table(items: list[dict[str, Any]]) -> str:
    return _rows(
        ["card", "state", "project", "title"],
        [
            [
                _link(str(item.get("ref") or "")),
                _state_chip(item.get("state")),
                _or_dash(item.get("project")),
                _or_dash(item.get("title")),
            ]
            for item in items
        ],
    )


def _agent_table(items: list[dict[str, Any]], *, with_ref: bool) -> str:
    headers = (["card", "project"] if with_ref else []) + ["role", "state", "head"]
    rows = []
    for item in items:
        row = []
        if with_ref:
            row += [_link(str(item.get("ref") or "")), _or_dash(item.get("project"))]
        row += [
            escape(str(item.get("role") or "")),
            _state_cell(str(item.get("state") or "unknown"), str(item.get("reason") or "")),
            _or_dash(item.get("head")),
        ]
        rows.append(row)
    return _rows(headers, rows)


def _start_form(projects: list[dict[str, Any]], tasks: list[dict[str, Any]]) -> str:
    if not projects:
        return '<p class="empty">no registered project, so there is nothing to start a run in.</p>'
    options = "".join(
        f'<option value="{escape(str(item.get("id")))}">{escape(str(item.get("id")))}</option>'
        for item in projects
    )
    cards = "".join(
        f'<option value="{escape(str(item.get("ref")))}" data-project="{escape(str(item.get("project") or ""))}">'
        f"{escape(str(item.get('ref')))} — {escape(str(item.get('title') or ''))}</option>"
        for item in tasks
    )
    return (
        '<form id="start-form" class="inline">'
        f'<div><label for="project">project</label><select id="project" name="project">{options}</select></div>'
        f'<div><label for="ref">card</label><select id="ref" name="ref">{cards}</select></div>'
        '<div><label for="profile">head profile</label>'
        '<input id="profile" name="profile" placeholder="a profile from the head registry" required></div>'
        '<div><label for="instruction">extra instruction</label>'
        '<input id="instruction" name="instruction" placeholder="optional"></div>'
        '<button type="submit">Start a worker run</button>'
        "</form>"
        '<p class="empty">a repeated submission of the same card and profile carries the same request '
        "id, and the operation behind it answers it with the run that already exists.</p>"
    )


# -- the task page ------------------------------------------------------------------------------


def task(snapshot: dict[str, Any], *, runs: dict[str, Any]) -> str:
    """Criterion 3: state, recent events, the worker's and reviewer's output, and the result."""
    ref = str(snapshot.get("ref") or "")
    card = snapshot.get("card") or {}
    value = card.get("value") or {}
    project = snapshot.get("project") or {}
    events = snapshot.get("events") or {}
    agents = snapshot.get("agents") or {}
    agent_items = list(agents.get("items") or [])
    chips = [_state_chip(value.get("state"))] if value else []
    if project.get("id") or value.get("project"):
        chips.append(_chip(str(project.get("id") or value.get("project"))))
    if value.get("claimed_by"):
        chips.append(_chip(f"claimed by {value.get('claimed_by')}", "accent"))
    event_items = list(events.get("items") or [])
    body = "\n".join(
        [
            '<div class="hero">',
            f"<h1>{escape(ref)}</h1>",
            f'<div class="chips">{"".join(chips)}</div>',
            f'<span class="age" style="margin-left:auto">read at {escape(str(snapshot.get("observed_at") or "an unknown time"))}</span>',
            (f'<p class="title">{escape(_short(value.get("title"), 160))}</p>' if value.get("title") else ""),
            "</div>",
            '<div class="grid">',
            '<div class="col">',
            _panel("Transitions", _source_block(events.get("source"), what="this card's history") + _timeline(event_items)),
            _panel("Work", _work(snapshot.get("work") or {})),
            _panel(
                "Every event",
                _events(event_items) + '<p id="events-notice"></p>',
                count=len(event_items) or None,
                open_=False,
            ),
            "</div>",
            '<div class="col">',
            _panel("Card", _source_block(card.get("source"), what="what this card is") + _card(card.get("value"), project)),
            _panel(
                "Agents",
                _section(
                    agents.get("source"),
                    agent_items,
                    what="which agents are working this card",
                    empty="the dispatcher holds no head for this card.",
                    table=_agent_table(agent_items, with_ref=False),
                ),
                count=len(agent_items) or None,
            ),
            _panel("Owner's actions", _comment_form(f"/api/tasks/{quote(ref)}/comment", f"card.{ref}", "Tell the head working this card something") + _move_form(ref)),
            _panel("Attempt", _attempt(snapshot.get("attempt") or {}), open_=False),
            _panel("Product runs", _runs(ref, runs) + '<p id="feedback"></p>', open_=False),
            "</div></div>",
        ]
    )
    cursor = escape(str(events.get("next_cursor") or ""))
    script = _TASK_SCRIPT.replace("__REF__", _js(ref)).replace("__CURSOR__", _js(cursor))
    return _page(
        f"Card {ref}",
        body,
        script=script + _ACTIONS_SCRIPT,
        nav="dashboard",
        crumbs=((ref, ""),),
    )


def _card(card: dict[str, Any] | None, project: dict[str, Any]) -> str:
    if card is None:
        return '<p class="empty">no card was read, so there is nothing to show here.</p>'
    registered = "registered" if project.get("registered") else "not registered on this installation"
    rows = [
        ["title", _or_dash(card.get("title"))],
        ["state", f'<span class="state">{_or_dash(card.get("state"))}</span>'],
        [
            "project",
            f'{_or_dash(project.get("id") or card.get("project"))} <span class="age">({escape(registered)})</span>',
        ],
        ["claimed by", _or_dash(card.get("claimed_by"))],
        ["updated", _or_dash(card.get("updated_at"))],
    ]
    return _rows(["", ""], rows)


def _attempt(attempt: dict[str, Any]) -> str:
    value = attempt.get("value")
    parts = [_source_block(attempt.get("source"), what="what the dispatcher holds for this card")]
    if value is None:
        if (attempt.get("source") or {}).get("state") == "available":
            parts.append('<p class="empty">the dispatcher holds no attempt for this card.</p>')
        return "\n".join(part for part in parts if part)
    paused = value.get("paused") or {}
    parts.append(
        _rows(
            ["", ""],
            [
                ["state", _or_dash(value.get("state"))],
                [
                    "attempt",
                    f"{_or_dash(value.get('attempt_id'))} (round {_or_dash(value.get('attempt_round'))})",
                ],
                ["gate", _or_dash(value.get("gate_state"))],
                ["workspace", _or_dash(value.get("workspace"))],
                [
                    "paused",
                    f"worker {'yes' if paused.get('worker') else 'no'}, reviewer {'yes' if paused.get('reviewer') else 'no'}",
                ],
            ],
        )
    )
    return "\n".join(part for part in parts if part)


def _runs(ref: str, runs: dict[str, Any]) -> str:
    if not runs.get("available"):
        reason = escape(str(runs.get("reason") or "no reason was recorded"))
        return f'<p class="unavailable"><b>could not find out this card\'s product runs:</b> {reason}</p>'
    items = list(runs.get("items") or [])
    if not items:
        return (
            '<p class="empty">this card has no product run. Start one from the dashboard.</p>'
            + _review_form(ref, worker=None)
        )
    rows = []
    for item in items:
        # Both facts, never one standing in for the other: `state` is what the evidence says this
        # run is — running, finished, failed, its source unreadable, or unknown — and `ended` is
        # whether it is over. A run that reads `unknown` while still open is a run nobody may treat
        # as running, so it must not look like one here.
        run = item.get("run") or {}
        state = item.get("state") or {}
        value = str(state.get("value") or "unknown")
        over = "over" if state.get("ended") else "open"
        rows.append(
            [
                f"<code>{escape(str(run.get('run_id') or ''))}</code>",
                escape(str(run.get("role") or "")),
                _or_dash(run.get("profile")),
                escape(str(run.get("phase") or "")),
                _state_cell(value, str(state.get("reason") or "no reason was recorded"))
                + f'<div class="age">({escape(over)})</div>',
                _outcome_cell(state),
            ]
        )
    workers = [item.get("run") or {} for item in items if (item.get("run") or {}).get("role") == "worker"]
    return _rows(["run", "role", "profile", "phase", "state", "outcome"], rows) + _review_form(
        ref, worker=workers[-1] if workers else None
    )


def _outcome_cell(state: dict[str, Any]) -> str:
    """What this run produced: the verdict it carries, its result, and the status it exited with.

    The state word beside this says how a run *ended*; this says what came of it, and the two are
    not the same question. A reviewer run that ended normally and a reviewer run that ended
    normally having called the work `red` read identically in the state column, which is the one
    thing a card page is read to find out — so the verdict is drawn here, off `state.result.verdict`
    (the field :func:`secretary.webproto.run_state.verdict_of` already publishes), and never
    re-derived from the result body by this module.

    The rule of this file applies unchanged: a result that is absent and a result that could not be
    read are different things and say different words. An open run has produced nothing yet and
    says exactly that, rather than borrowing the vocabulary of a run that finished empty.
    """
    result = state.get("result") or {}
    exit_status = state.get("exit") or {}
    parts: list[str] = []
    verdict = result.get("verdict")
    if verdict:
        parts.append(f'<div><b>verdict</b> <span class="state">{escape(str(verdict))}</span></div>')
    if result.get("present"):
        summary = _summary_of(result.get("value"))
        parts.append(f'<div>the head published a result{escape(summary)}</div>')
    elif result.get("reason"):
        parts.append(f'<div class="reason">{escape(str(result["reason"]))}</div>')
    elif state.get("ended"):
        parts.append('<div class="reason">the head published no result</div>')
    else:
        parts.append('<div class="empty">this run has produced nothing yet.</div>')
    if exit_status.get("code") is not None:
        parts.append(f'<div class="age">exit status {escape(str(exit_status["code"]))}</div>')
    elif exit_status.get("signal") is not None:
        parts.append(f'<div class="age">ended by signal {escape(str(exit_status["signal"]))}</div>')
    return "".join(parts)


def _summary_of(value: Any) -> str:
    """The one line a head's own result offers about itself, when it offers one."""
    if not isinstance(value, dict):
        return ""
    for name in ("summary", "status"):
        text = value.get(name)
        if isinstance(text, str) and text.strip():
            return ": " + text.strip()
    return ""


def _review_form(ref: str, worker: dict[str, Any] | None) -> str:
    if worker is None:
        return '<p class="empty">there is no worker run to review yet.</p>'
    return (
        '<form id="review-form">'
        f'<input type="hidden" id="worker-run" value="{escape(str(worker.get("run_id") or ""))}">'
        '<div><label for="review-profile">reviewer profile</label>'
        '<input id="review-profile" placeholder="a profile from the head registry" required></div>'
        f'<button type="submit">review {escape(str(worker.get("run_id") or ""))}</button>'
        "</form>"
        '<p class="empty">a review is refused while its worker run is still open, and a repeated '
        "submission returns the review that already exists.</p>"
    )


def _work(work: dict[str, Any]) -> str:
    parts = []
    for slot, title in (
        ("worker_report", "worker report"),
        ("review_verdict", "reviewer verdict"),
        ("decision", "observer decision"),
    ):
        entry = work.get(slot)
        if not isinstance(entry, dict):
            parts.append(f'<h3>{escape(title)}</h3><p class="empty">this round has produced none.</p>')
            continue
        classification = entry.get("classification")
        marked = f' <span class="age">({escape(str(classification))})</span>' if classification else ""
        parts.append(
            f"<h3>{escape(title)}</h3>"
            f'<p><span class="state">{escape(str(entry.get("marker") or ""))}</span>{marked} '
            f'<span class="age">at {_or_dash(entry.get("at"))}</span></p>'
            f"{_long(entry.get('body'), chars=200)}"
        )
    outcome = work.get("outcome")
    if isinstance(outcome, dict):
        terminal = "the card is Done" if outcome.get("terminal") else "the card is not finished"
        parts.append(
            f'<h3>result</h3><p><span class="state">{escape(str(outcome.get("kind")))}:'
            f"{escape(str(outcome.get('value')))}</span> at {_or_dash(outcome.get('at'))} "
            f'<span class="age">({escape(terminal)})</span></p>'
        )
    else:
        parts.append('<h3>result</h3><p class="empty">this card has produced no result yet.</p>')
    return "\n".join(parts)


def _events(items: list[dict[str, Any]]) -> str:
    if not items:
        return '<ol class="events" id="events"></ol><p class="empty" id="events-empty">no event has been recorded for this card.</p>'
    return '<ol class="events" id="events">' + "".join(_event(item) for item in items) + "</ol>"


def _event(item: dict[str, Any]) -> str:
    detail = item.get("reason") or item.get("outcome") or ""
    return (
        f'<li data-event-id="{escape(str(item.get("event_id") or ""))}">'
        f"<time>{escape(str(item.get('occurred_at') or ''))}</time>"
        f"<b>{escape(str(item.get('kind') or ''))}</b> {escape(str(detail))}</li>"
    )


def _long(text: Any, *, chars: int = 160) -> str:
    """Long text folded to its first line, opened on a click. Short text is shown as it is."""
    value = str(text or "")
    if not value.strip():
        return '<span class="empty">—</span>'
    first = value.strip().splitlines()[0]
    if len(value) <= chars and "\n" not in value.strip():
        return f"<pre>{escape(value)}</pre>"
    head = first if len(first) <= chars else first[: chars - 1].rstrip() + "…"
    return f'<details class="text"><summary>{escape(head)}</summary><pre>{escape(value)}</pre></details>'


#: The event kinds whose `data.body` is a record somebody wrote about the card: what a transition
#: is made of, read beside it.
RECORD_KINDS = {
    "card.reported": "worker report",
    "card.verdict": "reviewer verdict",
    "card.decided": "observer decision",
}


def _timeline(items: list[dict[str, Any]]) -> str:
    """Every transition of the card, oldest first, each opening on the records that made it.

    A transition is an event carrying `transition` (source and target). The records between the
    previous transition and this one -- the worker's report before a submit, the verdict before
    a park in Assessment, the decision before a rework -- are what made it, and they are read
    under it rather than found in the flat history.
    """
    ordered = sorted(items, key=lambda item: str(item.get("occurred_at") or ""))
    steps: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    pending: list[dict[str, Any]] = []
    for item in ordered:
        if item.get("kind") in RECORD_KINDS and (item.get("data") or {}).get("body"):
            pending.append(item)
        if isinstance(item.get("transition"), dict) and item["transition"]:
            steps.append((item, pending))
            pending = []
    if not steps:
        return '<p class="empty">no transition has been recorded for this card yet.</p>'
    parts = ['<ol class="timeline">']
    for event, records in steps:
        transition = event["transition"]
        when = str(event.get("occurred_at") or "")
        clock = when[11:19] if len(when) >= 19 else when
        actor = event.get("actor") if isinstance(event.get("actor"), dict) else {}
        reason = str(event.get("reason") or "")
        short = reason if len(reason) <= 120 else reason[:117].rstrip() + "…"
        detail = [
            f'<p class="why">{escape(when)} · {escape(str(actor.get("role") or ""))} {escape(str(actor.get("id") or ""))} · <span class="mono">{escape(str(event.get("kind") or ""))}</span></p>',
            (f"<div>{_long(reason, chars=300)}</div>" if reason else ""),
        ]
        for record in records:
            data = record.get("data") or {}
            who = record.get("actor") if isinstance(record.get("actor"), dict) else {}
            stamp = str(record.get("occurred_at") or "")
            marker = str(data.get("marker") or data.get("decision") or data.get("status") or "")
            detail.append(
                '<div class="record">'
                f'<div class="who"><b>{escape(RECORD_KINDS[str(record.get("kind"))])}</b>'
                f'{" · " + escape(marker) if marker else ""} · {escape(stamp[11:19] if len(stamp) >= 19 else stamp)}'
                f' · {escape(str(who.get("id") or who.get("role") or ""))}</div>'
                f'{_long(data.get("body"), chars=200)}'
                "</div>"
            )
        parts.append(
            f'<li><details><summary><time title="{escape(when)}">{escape(clock)}</time>'
            f"<span>{_state_chip(transition.get('source'))}<span class=\"arrow\">→</span>{_state_chip(transition.get('target'))}"
            f' <span class="why">{escape(short)}</span></span></summary>'
            f'<div class="detail">{"".join(part for part in detail if part)}</div></details></li>'
        )
    parts.append("</ol>")
    return "\n".join(parts)


def _js(value: str) -> str:
    """A string safe to paste into the script literal: no quote, no backslash, no tag opener."""
    return escape(value).replace("\\", "").replace("'", "").replace('"', "")


_DASHBOARD_SCRIPT = """
const feedback = document.getElementById('feedback');
const form = document.getElementById('start-form');
function say(text, bad) { feedback.textContent = text; feedback.className = bad ? 'bad' : ''; }
function requestId(key) {
  const stored = sessionStorage.getItem(key);
  if (stored) return stored;
  const made = 'web-' + (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random());
  sessionStorage.setItem(key, made);
  return made;
}
const project = document.getElementById('project');
const ref = document.getElementById('ref');
function filter() {
  let first = null;
  for (const option of ref.options) {
    const owned = !option.dataset.project || option.dataset.project === project.value;
    option.hidden = !owned;
    if (owned && first === null) first = option;
  }
  if (first && ref.selectedOptions[0] && ref.selectedOptions[0].hidden) ref.value = first.value;
}
if (project && ref) { project.addEventListener('change', filter); filter(); }
if (form) form.addEventListener('submit', async (event) => {
  event.preventDefault();
  const profile = document.getElementById('profile').value.trim();
  const instruction = document.getElementById('instruction').value;
  const card = ref.value;
  // The request id is the client's and it is kept: a repeated submission, a reload and a
  // reconnection all carry the same one, and the operation answers them with the same run.
  const id = requestId('secretary.web.start.' + card + '.' + profile);
  say('starting...', false);
  const response = await fetch('/api/runs/start', {
    method: 'POST',
    headers: {'content-type': 'application/json'},
    body: JSON.stringify({ref: card, request_id: id, profile: profile, instruction: instruction}),
  });
  const document_ = await response.json();
  if (!response.ok) { say(document_.error.code + ': ' + document_.error.message, true); return; }
  say('run ' + document_.run.run_id + ' — ' + document_.state.value, false);
  window.location.href = '/tasks/' + encodeURIComponent(card);
});
"""

_ACTIONS_SCRIPT = """
// The owner's actions: every form with a data-action posts a JSON body to that route, and shows
// the answer where the form is. The request id belongs to this browser and is kept until the
// operation answers success: a retry after a network failure repeats the same request, and the
// next comment is a new one.
function freshId() { return 'web-' + (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random()); }
function keptId(key) {
  const name = 'secretary.web.request.' + key;
  let id = sessionStorage.getItem(name);
  if (!id) { id = freshId(); sessionStorage.setItem(name, id); }
  return id;
}
function forgetId(key) { sessionStorage.removeItem('secretary.web.request.' + key); }
async function postJson(url, body) {
  const response = await fetch(url, {method: 'POST', headers: {'content-type': 'application/json'}, body: JSON.stringify(body)});
  let answer = null;
  try { answer = await response.json(); } catch (error) { answer = {error: {code: 'unreadable', message: 'the answer was not JSON (' + response.status + ')'}}; }
  return {ok: response.ok, status: response.status, answer: answer};
}
function tell(element, text, bad) { if (!element) return; element.textContent = text; element.className = 'feedback' + (bad ? ' bad' : ''); }
for (const form of document.querySelectorAll('form.act')) form.addEventListener('submit', async (event) => {
  event.preventDefault();
  const out = form.querySelector('.feedback');
  const kind = form.dataset.kind;
  const key = form.dataset.key;
  const body = {request_id: keptId(key)};
  for (const field of form.elements) {
    if (!field.name) continue;
    if (field.type === 'checkbox') body[field.name] = field.checked; else body[field.name] = field.value;
  }
  if (kind === 'move' && !body.sprint_override) { delete body.sprint_override; delete body.sprint_override_reason; }
  if (kind === 'close' && !body.decisions.trim()) delete body.decisions;
  if (kind === 'close' && !window.confirm('Close this sprint? Its remaining cards and issues get the decisions stated, and the closeout is written.')) return;
  tell(out, 'sending...', false);
  const result = await postJson(form.dataset.action, body);
  if (!result.ok) { tell(out, result.answer.error.code + ': ' + result.answer.error.message, true); return; }
  forgetId(key);
  const answer = result.answer;
  if (kind === 'comment') tell(out, (answer.saved === false ? 'already saved' : 'saved') + (answer.comment_id ? ' as ' + answer.comment_id : answer.event_id ? ' as ' + answer.event_id : ''), false);
  else if (kind === 'move') tell(out, 'moved (' + (answer.event_id || 'recorded') + '); reload to see the card\\'s new state', false);
  else if (kind === 'close') tell(out, 'closed; ' + ((answer.definition_of_done || {}).reason || ''), false);
  else tell(out, 'done', false);
  form.reset();
});
for (const form of document.querySelectorAll('form.pause')) form.addEventListener('submit', async (event) => {
  event.preventDefault();
  const out = document.getElementById('pause-feedback');
  if (!window.confirm(form.dataset.confirm)) return;
  const body = {};
  for (const field of form.elements) if (field.name) body[field.name] = field.value;
  tell(out, 'sending...', false);
  const result = await postJson(form.dataset.action, body);
  if (!result.ok) { tell(out, result.answer.error.code + ': ' + result.answer.error.message, true); return; }
  const answer = result.answer;
  tell(out, (answer.outcome || answer.kind || 'done') + (answer.state && answer.state.mode ? ' — ' + answer.state.mode : '') + '; reloading', false);
  window.setTimeout(() => window.location.reload(), 800);
});
"""

_REFRESH_SCRIPT = """
// Reload every 30 s while nobody is typing. The choice is this browser's and is remembered.
const box = document.getElementById('auto-refresh');
if (box) {
  try { box.checked = localStorage.getItem('secretary.web.refresh') !== 'off'; } catch (error) { box.checked = true; }
  box.addEventListener('change', () => { try { localStorage.setItem('secretary.web.refresh', box.checked ? 'on' : 'off'); } catch (error) {} });
  window.setInterval(() => {
    if (!box.checked) return;
    const active = document.activeElement;
    if (active && (active.tagName === 'TEXTAREA' || active.tagName === 'INPUT' || active.tagName === 'SELECT')) return;
    for (const field of document.querySelectorAll('textarea, input[type=text], input:not([type])')) if (field.value) return;
    window.location.reload();
  }, 30000);
}
"""

_TASK_SCRIPT = """
const REF = '__REF__';
const KEY = 'secretary.web.cursor.' + REF;
const notice = document.getElementById('events-notice');
const list = document.getElementById('events');
const seen = new Set(Array.from(list.children).map((li) => li.dataset.eventId));
// The cursor is the client's, and it is the only thing that decides where watching resumes. A
// reload or a reconnection reads the one this browser stored; only a first visit falls back to the
// end of the tail the server rendered. The server keeps nothing.
let cursor = sessionStorage.getItem(KEY) || '__CURSOR__';
let polling = true;
function render(item) {
  if (seen.has(item.event_id)) return;
  seen.add(item.event_id);
  const li = document.createElement('li');
  li.dataset.eventId = item.event_id;
  const time = document.createElement('time');
  time.textContent = item.occurred_at || '';
  const kind = document.createElement('b');
  kind.textContent = item.kind || '';
  li.append(time, kind, ' ' + (item.reason || item.outcome || ''));
  list.append(li);
  const empty = document.getElementById('events-empty');
  if (empty) empty.remove();
}
async function tail() {
  if (!polling) return;
  const url = '/api/tasks/' + encodeURIComponent(REF) + '/events?cursor=' + encodeURIComponent(cursor);
  let response;
  try { response = await fetch(url); } catch (error) { notice.textContent = 'the page could not reach this service: ' + error; return; }
  if (response.status === 400) {
    // A cursor this installation will not honour is not silently reset to the beginning: watching
    // stops, the reason is shown, and starting again from the current tail is the reader's choice.
    polling = false;
    const refused = await response.json();
    notice.className = 'unavailable';
    notice.textContent = 'this browser\\'s position in the history was refused (' + refused.error.message + '). Reload to start from the current tail.';
    sessionStorage.removeItem(KEY);
    return;
  }
  if (!response.ok) { notice.textContent = 'the history could not be read just now (' + response.status + '); retrying.'; return; }
  const page = await response.json();
  notice.className = page.source.state === 'available' ? '' : 'unavailable';
  notice.textContent = page.source.state === 'available' ? '' : 'could not read this card\\'s history: ' + page.source.reason;
  page.items.forEach(render);
  cursor = page.next_cursor;
  sessionStorage.setItem(KEY, cursor);
}
tail();
setInterval(tail, 4000);

const reviewForm = document.getElementById('review-form');
const feedback = document.getElementById('feedback');
if (reviewForm) reviewForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  const worker = document.getElementById('worker-run').value;
  const profile = document.getElementById('review-profile').value.trim();
  // Same rule as a start: the id belongs to this browser and is kept, so a repeated submission
  // and a reconnection are answered with the review that already exists.
  const key = 'secretary.web.review.' + worker + '.' + profile;
  let id = sessionStorage.getItem(key);
  if (!id) {
    id = 'web-' + (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random());
    sessionStorage.setItem(key, id);
  }
  feedback.textContent = 'starting the review...';
  feedback.className = '';
  const response = await fetch('/api/runs/review', {
    method: 'POST',
    headers: {'content-type': 'application/json'},
    body: JSON.stringify({request_id: id, profile: profile, worker_run_id: worker, ref: REF}),
  });
  const answer = await response.json();
  if (!response.ok) {
    feedback.className = 'bad';
    feedback.textContent = answer.error.code + ': ' + answer.error.message;
    return;
  }
  feedback.textContent = 'review run ' + answer.review.run.run_id + ' — ' + answer.review.state.value;
});
"""


# -- the sprint form ------------------------------------------------------------------------------

#: What the observer's launch state is called on the page, in words. The colour is a second reading
#: of the same fact and never the only one: three of these -- saved and not yet raised, really
#: running, and nothing could be established -- are the three an operator opens this page to tell
#: apart, and a page that drew them as three shades would be unreadable to half the people who open
#: it and to every screen reader. The words come from here; the sentence beside them is the layer's
#: own reason and is never rewritten.
LAUNCH_WORDS: dict[str, str] = {
    "not_started": "saved — no observer is up for it yet",
    "running": "running — an observer head is up",
    "unavailable": "not established — this could not be read at all",
    "stopped": "stopped — an observer was raised for it and is not alive",
    "not_declared": "no observer — this sprint declared none, so none is raised",
}

#: Said under the submit button, because it is the one thing about this form that surprises people:
#: there is no second "start" action anywhere. See `secretary.webproto.sprint_ops`.
START_NOTICE = (
    "starting a sprint is opening it with an observer: there is no separate launch action, and the "
    "production tick raises one observer head for each open sprint that has none"
)

#: The word on the empty option of the two executor selects, and the answer that leaves the role
#: unpinned. Submitting it sends the layer nothing about that role at all.
EXECUTOR_CHOICE = "the observer chooses"


def redirect(location: str) -> str:
    """The body of a 303. A browser follows the header; anything that does not gets the link."""
    return _page(
        "opened",
        f'<p>this sprint is open. <a href="{escape(location)}">{escape(location)}</a></p>',
    )


def sprint_form(
    options: dict[str, Any] | None,
    *,
    submitted: dict[str, Any],
    errors: dict[str, str],
    refusal: dict[str, Any] | None = None,
    catalogue: str | None = None,
    reissued: bool = False,
) -> str:
    """The "new sprint" form, on this installation's own catalogue and on what was typed into it.

    `options` is a `sprint_options` document, or `None` when the catalogue could not be read at all
    while a refusal was being shown — the refusal is the thing being answered, so it is rendered
    over a form that says its choices are missing rather than replaced by a page about the
    catalogue.
    """
    options = options or {}
    heads = options.get("heads") or {}
    products = options.get("products") or {}
    issues = options.get("issues") or {}
    projects = options.get("projects") or {}
    body = "\n".join(
        part
        for part in [
            "<h2>new sprint</h2>",
            _refusal_block(refusal, reissued=reissued),
            _errors_block(errors),
            ""
            if catalogue is None
            else f'<p class="unavailable"><b>could not find out what this installation offers:</b> '
            f"{escape(catalogue)}</p>",
            '<form class="sprint" id="sprint-form" method="post" action="/sprints">',
            f'<input type="hidden" name="request_id" value="{escape(str(submitted.get("request_id") or ""))}">',
            _product_field(products, submitted, errors),
            _issue_field(issues, submitted, errors),
            _project_field(projects, submitted, errors),
            _text_field(
                "goal",
                "goal",
                submitted,
                errors,
                hint="what this sprint is for, in the owner's own words",
            ),
            _text_field(
                "definition_of_done",
                "definition of done",
                submitted,
                errors,
                hint="what would make this sprint done",
            ),
            _observer_field(heads, submitted, errors),
            _executor_field("worker", "worker", heads, submitted),
            _executor_field("reviewer", "reviewer", heads, submitted),
            '<button type="submit">start this sprint</button>',
            "</form>",
            f'<p class="hint empty">{escape(START_NOTICE)}</p>',
            (
                '<p class="hint empty">this form carries one request id for as long as it is open, '
                "so a double click, a retry and a reconnection all reach the same sprint rather "
                "than opening a second one.</p>"
            ),
        ]
        if part
    )
    return _page(
        "New sprint",
        body,
        script=_SPRINT_FORM_SCRIPT,
        nav="new-sprint",
    )


def _refusal_block(refusal: dict[str, Any] | None, *, reissued: bool = False) -> str:
    """What the layer said about this submission, and what may safely be done about it.

    Two refusals, two opposite instructions, and the page has to give the right one. A part-done
    create is the one that is not simply "no": a sprint exists, and the only safe move is to submit
    this same form again, which carries the same request id and therefore picks that sprint up
    instead of opening a second one.

    Everything else left no sprint behind and has spent its request id below this transport, so the
    form now carries a new one. That is said out loud rather than done quietly, because it is the
    difference between "fix the field and send this again" and "send exactly this again", and a
    person who read the wrong one either opens a second sprint or reaches a dead end.
    """
    if not refusal:
        return ""
    message = escape(str(refusal.get("message") or "this submission was refused"))
    data = refusal.get("data") or {}
    action = data.get("action") or {}
    if not action.get("repeat_request"):
        reissue = (
            " Nothing was created. This form now carries a new request id, so correcting a field "
            "and submitting it again is a new request and is safe."
            if reissued
            else ""
        )
        return f'<p class="refused"><b>this sprint was not opened.</b> {message}{reissue}</p>'
    reference = str(action.get("reference") or "")
    named = (
        f' It is <a href="/sprints/{quote(reference)}">{escape(reference)}</a>.' if reference else ""
    )
    return (
        '<p class="pending"><b>this sprint exists and the request that opened it did not finish.</b> '
        f"{message}{named} Submitting this form again is safe: it carries the same request id, so it "
        "picks up the sprint that already exists rather than opening a second one. Do not start over "
        "with a new form.</p>"
    )


def _errors_block(errors: dict[str, str]) -> str:
    if not errors:
        return ""
    items = "".join(
        f"<li><b>{escape(name.replace('_', ' '))}</b>: {escape(reason)}</li>"
        for name, reason in errors.items()
    )
    return (
        '<div class="refused"><b>this form is not complete, so nothing was opened.</b>'
        f"<ul>{items}</ul></div>"
    )


def _field(name: str, label: str, control: str, errors: dict[str, str], hint: str = "") -> str:
    said = f'<p class="bad-field">{escape(errors[name])}</p>' if name in errors else ""
    note = f'<p class="hint">{escape(hint)}</p>' if hint else ""
    return (
        f'<div class="field"><label for="{escape(name)}">{escape(label)}</label>'
        f"{note}{control}{said}</div>"
    )


def _text_field(name: str, label: str, submitted: dict[str, Any], errors: dict[str, str], *, hint: str) -> str:
    value = escape(str(submitted.get(name) or ""))
    control = f'<textarea id="{escape(name)}" name="{escape(name)}">{value}</textarea>'
    return _field(name, label, control, errors, hint)


def _product_field(products: dict[str, Any], submitted: dict[str, Any], errors: dict[str, str]) -> str:
    items = list(products.get("items") or [])
    chosen = str(submitted.get("product") or "")
    unavailable = _source_block(products.get("source"), what="which products this installation has")
    if not items and not chosen:
        empty = unavailable or '<p class="empty">this installation has no product to open a sprint for.</p>'
        return _field("product", "product", empty, errors)
    options = ['<option value="">choose a product</option>']
    for item in items:
        value = str(item.get("id") or "")
        options.append(
            f'<option value="{escape(value)}"{_selected(value == chosen)}>'
            f"{escape(str(item.get('label') or value))} ({escape(value)})</option>"
        )
    options += _kept_option(chosen, [str(item.get("id") or "") for item in items])
    control = unavailable + f'<select id="product" name="product">{"".join(options)}</select>'
    return _field("product", "product", control, errors, "the product whose issues this sprint serves")


def _issue_field(issues: dict[str, Any], submitted: dict[str, Any], errors: dict[str, str]) -> str:
    """The open issues, each carrying the product that owns it.

    The product is on every row rather than only in a script's memory: the list is narrowed to the
    selected product by the page's own script, and when that script does not run the whole list is
    there with each row saying whose it is, which is a usable form rather than a blank one.
    """
    items = list(issues.get("items") or [])
    chosen = set(submitted.get("issues") or [])
    unavailable = _source_block(issues.get("source"), what="which issues are open")
    if not items and not chosen:
        empty = unavailable or '<p class="empty">no open issue is on this board, so no sprint can serve one.</p>'
        return _field("issues", "issues this sprint serves", empty, errors)
    rows = []
    for item in items:
        ref = str(item.get("ref") or "")
        product = str(item.get("product") or "")
        rows.append(
            f'<label data-product="{escape(product)}">'
            f'<input type="checkbox" name="issues" value="{escape(ref)}"{_checked(ref in chosen)}> '
            f"{escape(ref)} — {escape(str(item.get('label') or ref))} "
            f'<span class="age">({escape(product) or "no product"})</span></label>'
        )
    rows += _kept_rows("issues", chosen, [str(item.get("ref") or "") for item in items])
    control = unavailable + f'<div class="choices" id="issues">{"".join(rows)}</div>'
    return _field(
        "issues", "issues this sprint serves", control, errors, "the open issues of the product above"
    )


def _project_field(projects: dict[str, Any], submitted: dict[str, Any], errors: dict[str, str]) -> str:
    """The registered projects, each saying whether an open sprint already holds it.

    `reserved_by` is three answers and not two: a list of sprints, an empty list, and `null` for a
    reservation index nobody could read. The third is said in its own words, because "held by
    nobody" and "nobody could say" would otherwise look identical on the one row where the
    difference decides whether a create is about to be refused.
    """
    items = list(projects.get("items") or [])
    chosen = set(submitted.get("projects") or [])
    unavailable = _source_block(projects.get("source"), what="which projects are registered")
    if not items and not chosen:
        empty = unavailable or '<p class="empty">this installation has no registered project to reserve.</p>'
        return _field("projects", "projects this sprint reserves", empty, errors)
    rows = []
    for item in items:
        value = str(item.get("id") or "")
        reserved = item.get("reserved_by")
        if reserved is None:
            held = "whether an open sprint holds it could not be established"
        elif reserved:
            held = "an open sprint already holds it: " + ", ".join(str(one) for one in reserved)
        else:
            held = ""
        note = f' <span class="age">({escape(held)})</span>' if held else ""
        rows.append(
            f"<label><input type=\"checkbox\" name=\"projects\" value=\"{escape(value)}\""
            f"{_checked(value in chosen)}> {escape(str(item.get('label') or value))}{note}</label>"
        )
    rows += _kept_rows("projects", chosen, [str(item.get("id") or "") for item in items])
    control = unavailable + f'<div class="choices">{"".join(rows)}</div>'
    return _field(
        "projects",
        "projects this sprint reserves",
        control,
        errors,
        "a project an open sprint already holds is refused by the board, not by this page",
    )


def _observer_field(heads: dict[str, Any], submitted: dict[str, Any], errors: dict[str, str]) -> str:
    """The observer, which is the one head an operator must name, chosen and never typed.

    Only the profiles the layer marked as observers are offered, because that flag is
    `check_observer_profile` — the create's own check — asked of each profile rather than a rule
    restated here.

    The one answer that is not a profile is deliberately *not* offered. `none` opens a sprint the
    production tick raises no observer for, so on a page whose button says "start this sprint" it
    would be an option that starts nothing; it stays a legal answer for `secretary sprint create`
    and for the rows that already carry it, which the sprint page renders unchanged.
    """
    items = [item for item in (heads.get("items") or []) if item.get("observer")]
    chosen = str(submitted.get("observer") or "")
    unavailable = _source_block(heads.get("source"), what="which head profiles this installation has")
    if not items and not chosen:
        empty = unavailable or '<p class="empty">this installation offers no profile that may observe a sprint.</p>'
        return _field("observer", "observer", empty, errors)
    options = ['<option value="">choose an observer</option>']
    options += [_profile_option(item, chosen) for item in items]
    options += _kept_option(chosen, [str(item.get("id") or "") for item in items])
    control = unavailable + f'<select id="observer" name="observer">{"".join(options)}</select>'
    return _field(
        "observer", "observer", control, errors, "the head that runs this sprint; it is required"
    )


def _executor_field(name: str, label: str, heads: dict[str, Any], submitted: dict[str, Any]) -> str:
    """One optional pin, whose first and default answer is that the observer picks the head.

    The empty option is not decoration: it is submitted as the empty string and the transport turns
    it into `None`, which is how the row is written with no field for this role at all. That is a
    different thing from a role pinned to a profile and a different thing again from one pinned to
    nothing, and the three must not be able to look alike here.
    """
    items = list(heads.get("items") or [])
    chosen = str(submitted.get(name) or "")
    options = [f'<option value=""{_selected(not chosen)}>{escape(EXECUTOR_CHOICE)}</option>']
    options += [_profile_option(item, chosen) for item in items]
    options += _kept_option(chosen, [str(item.get("id") or "") for item in items])
    control = f'<select id="{escape(name)}" name="{escape(name)}">{"".join(options)}</select>'
    return _field(
        name,
        f"{label} (optional)",
        control,
        {},
        f"leave this as “{EXECUTOR_CHOICE}” and the role stays unpinned",
    )


def _profile_option(item: dict[str, Any], chosen: str) -> str:
    """One head profile as a person picks it: what it is called, its model and its effort.

    None of the three is composed here from a rule about naming: `label`, `model` and `effort` are
    fields of the profile the registry actually holds, and a profile that pins neither says so in
    the words the layer used rather than showing an empty column.
    """
    value = str(item.get("id") or "")
    model = str(item.get("model") or "") or "the adapter's default model"
    effort = str(item.get("effort") or "") or "the adapter's default effort"
    return (
        f'<option value="{escape(value)}"{_selected(value == chosen)}>'
        f"{escape(str(item.get('label') or value))} — model {escape(model)}, effort {escape(effort)}"
        f" ({escape(value)})</option>"
    )


#: Said beside a value that was submitted and that the catalogue no longer offers. It is kept on
#: the form rather than dropped for one reason: a form that quietly changed a submitted choice
#: would then be asking for a repeat of something the person never sent -- which is exactly wrong
#: after a part-done create, where the safe move is to submit *this* form again unchanged.
NO_LONGER_OFFERED = "this installation no longer offers this choice"


def _kept_option(chosen: str, offered: list[str]) -> list[str]:
    """The submitted choice as an option of its own, when the catalogue stopped offering it."""
    if not chosen or chosen in offered:
        return []
    return [f'<option value="{escape(chosen)}" selected>{escape(chosen)} — {escape(NO_LONGER_OFFERED)}</option>']


def _kept_rows(name: str, chosen: set[str], offered: list[str]) -> list[str]:
    """The same, for the fields a person ticks rather than picks."""
    return [
        f'<label><input type="checkbox" name="{escape(name)}" value="{escape(value)}" checked> '
        f'{escape(value)} <span class="age">({escape(NO_LONGER_OFFERED)})</span></label>'
        for value in sorted(chosen)
        if value not in offered
    ]


def _selected(is_selected: bool) -> str:
    return " selected" if is_selected else ""


def _checked(is_checked: bool) -> str:
    return " checked" if is_checked else ""


_SPRINT_FORM_SCRIPT = """
// The only thing this does is narrow the issue list to the product that is selected. Every row is
// already on the page with the product that owns it, so a browser that does not run this shows the
// whole list rather than an empty one.
const product = document.getElementById('product');
const issues = document.getElementById('issues');
function narrow() {
  if (!product || !issues) return;
  for (const row of issues.querySelectorAll('label')) {
    const owned = !product.value || row.dataset.product === product.value;
    row.hidden = !owned;
    if (!owned) row.querySelector('input').checked = false;
  }
}
if (product) { product.addEventListener('change', narrow); narrow(); }
"""


# -- the sprint page ------------------------------------------------------------------------------


def sprint(document: dict[str, Any]) -> str:
    """One sprint: what it was opened with, what it pinned, and whether its observer is up."""
    ref = str(document.get("ref") or "")
    sprint_section = document.get("sprint") or {}
    value = sprint_section.get("value")
    observer = document.get("observer") or {}
    work = {**(value or {}), **(document.get("work") or {})}
    chips = []
    if value:
        chips.append(_sprint_status_chip(value))
        if value.get("product"):
            chips.append(_chip(str(value.get("product"))))
        chips.append(_waiting_chip(work))
    is_open = str((value or {}).get("status") or "") == "open"
    body = "\n".join(
        part
        for part in [
            '<div class="hero">',
            f"<h1>{escape(ref)}</h1>",
            f'<div class="chips">{"".join(chips)}</div>',
            f'<span class="age" style="margin-left:auto">read at {escape(str(document.get("observed_at") or "an unknown time"))}</span>',
            (f'<div class="title">{_long((value or {}).get("goal"), chars=200)}</div>' if value and value.get("goal") else ""),
            "</div>",
            '<div class="grid">',
            '<div class="col">',
            _panel("Work", _sprint_work(work)),
            _panel("Definition of done", f"<pre>{escape(str((value or {}).get('definition_of_done') or ''))}</pre>" if value else '<p class="empty">no sprint was read.</p>', open_=False),
            _panel("The observer's last resume", _resume((value or {}).get("resume")), open_=False),
            "</div>",
            '<div class="col">',
            _panel("Sprint", _source_block(sprint_section.get("source"), what="what this sprint is") + _sprint_fields(value)),
            _panel("Observer", _observer_section(observer) + _executor_section((value or {}).get("executors") or {})),
            _panel(
                "Owner's actions",
                _comment_form(f"/api/sprints/{quote(ref)}/comment", f"sprint.{ref}", "Tell the observer something")
                + (_close_form(ref) if is_open else ""),
            ),
            "</div></div>",
        ]
        if part
    )
    return _page(
        f"Sprint {ref}",
        body,
        script=_ACTIONS_SCRIPT,
        nav="sprints",
        crumbs=((ref, ""),),
    )


def _sprint_fields(value: dict[str, Any] | None) -> str:
    if value is None:
        return '<p class="empty">no sprint was read, so there is nothing to show here.</p>'
    return _rows(
        ["", ""],
        [
            ["reference", f'<span class="ref">{_or_dash(value.get("ref"))}</span>'],
            ["status", _sprint_status_chip(value)],
            ["product", _or_dash(value.get("product"))],
            ["issues", _listed(value.get("issues"), "this sprint declares no issue")],
            ["projects", _listed(value.get("reservations"), "this sprint reserves no project")],
            ["repositories", _listed(value.get("repositories"), "this sprint names no repository")],
        ],
    )


def _listed(values: Any, empty: str) -> str:
    items = [str(one) for one in (values or []) if str(one)]
    if not items:
        return f'<span class="empty">{escape(empty)}</span>'
    return ", ".join(escape(one) for one in items)


def _observer_section(observer: dict[str, Any]) -> str:
    """The declared observer, and separately whether one is up. Two sources, said apart."""
    declared = observer.get("declared") or {}
    launch = observer.get("launch") or {}
    state = str(launch.get("state") or "")
    words = LAUNCH_WORDS.get(state, "this launch state is one this page does not know")
    profile = declared.get("profile")
    if profile:
        said = escape(str(profile))
    elif declared.get("state") == "malformed":
        said = '<span class="empty">this sprint carries an observer value that is not one of the known forms</span>'
    elif (declared.get("value") or {}).get("kind") == "none":
        said = '<span class="empty">this sprint declared no observer</span>'
    else:
        said = '<span class="empty">the row of this sprint carries no observer field</span>'
    record = launch.get("record") or {}
    rows = [
        ["declared observer", said],
        [
            "state",
            (
                f'<span class="launch state state-{escape(state)}">{escape(words)}</span>'
                f'<div class="reason">{escape(str(launch.get("reason") or "no reason was recorded"))}</div>'
            ),
        ],
    ]
    if record:
        rows.append(["the head the dispatcher holds", _or_dash(record.get("head"))])
        rows.append(["its heartbeat", _or_dash(record.get("heartbeat_state"))])
    return _source_block(launch.get("source"), what="whether this sprint's observer is up") + _rows(
        ["", ""], rows
    )


def _executor_section(executors: dict[str, Any]) -> str:
    """Both roles, always, and each in the state it is really in.

    A role nobody pinned is not a blank cell: it is the observer's to choose, which is a decision
    somebody made, and the page says so in those words.
    """
    rows = []
    for role in ("worker", "reviewer"):
        entry = executors.get(role) if isinstance(executors.get(role), dict) else {}
        state = str(entry.get("state") or "")
        if state == "pinned":
            said = f"pinned to {escape(str(entry.get('profile') or ''))}"
        elif state == "malformed":
            said = "this row carries a pin that is not a profile"
        else:
            said = escape(EXECUTOR_CHOICE)
        rows.append([escape(role), said])
    return _rows(["role", "which head runs it"], rows)


def _current_task(value: dict[str, Any] | None) -> str:
    current = (value or {}).get("current_task")
    if not current:
        return '<p class="empty">the observer has cut no card for this sprint yet.</p>'
    return f"<p>{_link(str(current))}</p>"


def _resume(resume: Any) -> str:
    if not isinstance(resume, dict) or not resume:
        return '<p class="empty">the observer has recorded no resume for this sprint yet.</p>'
    rows = [[escape(str(name).replace("_", " ")), _long(resume[name], chars=200)] for name in sorted(resume)]
    return _rows(["", ""], rows)
