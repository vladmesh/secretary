"""The two pages, rendered from the layer's own documents and from nothing else.

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
the client itself holds.
"""

from __future__ import annotations

from html import escape
from typing import Any

TITLE = "secretary"

#: Said on every page. This service has no authentication of any kind, so where it may listen is
#: not a deployment preference; see :mod:`secretary.web.server`.
LOOPBACK_NOTICE = (
    "local only — this service has no password, no TLS and no authorisation, and is refused a "
    "non-loopback address until the slice that adds them"
)

STYLE = """
:root { color-scheme: light dark; --line: #8884; --warn: #b3541e; --dim: #6b7280; }
* { box-sizing: border-box; }
body { margin: 0; font: 14px/1.5 ui-sans-serif, system-ui, sans-serif; }
main { max-width: 62rem; margin: 0 auto; padding: 1rem 1.25rem 4rem; }
header { border-bottom: 1px solid var(--line); padding: .75rem 1.25rem; }
header .notice { color: var(--warn); font-size: .8rem; }
h1 { font-size: 1.1rem; margin: 0; }
h2 { font-size: .95rem; margin: 1.75rem 0 .5rem; }
section { border-top: 1px solid var(--line); }
table { border-collapse: collapse; width: 100%; }
th, td { text-align: left; padding: .3rem .5rem .3rem 0; vertical-align: top; }
th { font-weight: 600; color: var(--dim); font-size: .8rem; }
tr + tr td { border-top: 1px solid var(--line); }
.empty { color: var(--dim); font-style: italic; }
.unavailable { border-left: 3px solid var(--warn); background: #b3541e14; padding: .5rem .75rem; margin: .25rem 0; }
.unavailable b { color: var(--warn); }
.age { color: var(--dim); font-size: .8rem; }
.state { font-family: ui-monospace, monospace; font-size: .85rem; }
.state-running { color: #15803d; }
.state-process_failed { color: #b91c1c; }
.state-source_unavailable, .state-unknown { color: var(--warn); }
.reason { color: var(--dim); }
form { display: flex; flex-wrap: wrap; gap: .5rem; align-items: flex-end; margin: .5rem 0; }
label { display: block; font-size: .8rem; color: var(--dim); }
input, select, textarea, button { font: inherit; padding: .3rem .4rem; }
button { cursor: pointer; }
#feedback:not(:empty) { border-left: 3px solid var(--line); padding: .5rem .75rem; margin: .5rem 0; }
#feedback.bad { border-left-color: var(--warn); }
ol.events { list-style: none; padding: 0; margin: 0; }
ol.events li { border-top: 1px solid var(--line); padding: .3rem 0; }
ol.events time { font-family: ui-monospace, monospace; color: var(--dim); margin-right: .5rem; }
pre { white-space: pre-wrap; overflow-x: auto; }
code { font-family: ui-monospace, monospace; }
"""


# -- the shell ----------------------------------------------------------------------------------


def _page(title: str, body: str, *, script: str = "") -> str:
    return "\n".join(
        [
            "<!doctype html>",
            '<html lang="en"><head><meta charset="utf-8">',
            '<meta name="viewport" content="width=device-width, initial-scale=1">',
            f"<title>{escape(title)}</title>",
            f"<style>{STYLE}</style>",
            "</head><body>",
            "<header>",
            f'<h1><a href="/">{escape(TITLE)}</a> — {escape(title)}</h1>',
            f'<p class="notice">{escape(LOOPBACK_NOTICE)}</p>',
            "</header>",
            "<main>",
            body,
            "</main>",
            f"<script>{script}</script>" if script else "",
            "</body></html>",
        ]
    )


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
    head = "".join(f"<th>{escape(name)}</th>" for name in headers)
    body = "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _state_cell(state: str, reason: str) -> str:
    return (
        f'<span class="state state-{escape(state)}">{escape(state)}</span>'
        f'<div class="reason">{escape(reason)}</div>'
    )


def _link(ref: str) -> str:
    return f'<a href="/tasks/{escape(ref)}">{escape(ref)}</a>'


def _or_dash(value: Any) -> str:
    return escape(str(value)) if value not in (None, "") else "—"


# -- the dashboard ------------------------------------------------------------------------------


def dashboard(snapshot: dict[str, Any]) -> str:
    """Criterion 2: health with its reason and age, projects, current cards, running agents."""
    installation = snapshot.get("installation") or {}
    projects = snapshot.get("projects") or {}
    tasks = snapshot.get("tasks") or {}
    agents = snapshot.get("agents") or {}
    body = "\n".join(
        [
            f'<p class="age">read at {escape(str(snapshot.get("observed_at") or "an unknown time"))}</p>',
            "<section><h2>installation</h2>",
            _installation(installation),
            "</section>",
            "<section><h2>projects</h2>",
            _section(
                projects.get("source"),
                list(projects.get("items") or []),
                what="which projects are registered",
                empty="this installation has no registered project.",
                table=_project_table(list(projects.get("items") or [])),
            ),
            "</section>",
            "<section><h2>current tasks</h2>",
            _section(
                tasks.get("source"),
                list(tasks.get("items") or []),
                what="which cards the pipeline is carrying",
                empty="no card is in flight.",
                table=_task_table(list(tasks.get("items") or [])),
            ),
            "</section>",
            "<section><h2>agents</h2>",
            _section(
                agents.get("source"),
                list(agents.get("items") or []),
                what="which agents are running",
                empty="no agent is running.",
                table=_agent_table(list(agents.get("items") or []), with_ref=True),
            ),
            "</section>",
            "<section><h2>start a run</h2>",
            _start_form(list(projects.get("items") or []), list(tasks.get("items") or [])),
            '<p id="feedback"></p>',
            "</section>",
        ]
    )
    return _page("dashboard", body, script=_DASHBOARD_SCRIPT)


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
                escape(str(item.get("state") or "")),
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
        '<form id="start-form">'
        f'<div><label for="project">project</label><select id="project" name="project">{options}</select></div>'
        f'<div><label for="ref">card</label><select id="ref" name="ref">{cards}</select></div>'
        '<div><label for="profile">head profile</label>'
        '<input id="profile" name="profile" placeholder="a profile from the head registry" required></div>'
        '<div><label for="instruction">extra instruction</label>'
        '<input id="instruction" name="instruction" placeholder="optional"></div>'
        '<button type="submit">start a worker run</button>'
        "</form>"
        '<p class="empty">a repeated submission of the same card and profile carries the same request '
        "id, and the operation behind it answers it with the run that already exists.</p>"
    )


# -- the task page ------------------------------------------------------------------------------


def task(snapshot: dict[str, Any], *, runs: dict[str, Any]) -> str:
    """Criterion 3: state, recent events, the worker's and reviewer's output, and the result."""
    ref = str(snapshot.get("ref") or "")
    card = snapshot.get("card") or {}
    events = snapshot.get("events") or {}
    agents = snapshot.get("agents") or {}
    body = "\n".join(
        [
            f'<p class="age">read at {escape(str(snapshot.get("observed_at") or "an unknown time"))}</p>',
            "<section><h2>card</h2>",
            _source_block(card.get("source"), what="what this card is"),
            _card(card.get("value"), snapshot.get("project") or {}),
            "</section>",
            "<section><h2>attempt</h2>",
            _attempt(snapshot.get("attempt") or {}),
            "</section>",
            "<section><h2>agents</h2>",
            _section(
                agents.get("source"),
                list(agents.get("items") or []),
                what="which agents are working this card",
                empty="the dispatcher holds no head for this card.",
                table=_agent_table(list(agents.get("items") or []), with_ref=False),
            ),
            "</section>",
            "<section><h2>product runs</h2>",
            _runs(ref, runs),
            '<p id="feedback"></p>',
            "</section>",
            "<section><h2>work</h2>",
            _work(snapshot.get("work") or {}),
            "</section>",
            "<section><h2>events</h2>",
            _source_block(events.get("source"), what="this card's history"),
            _events(list(events.get("items") or [])),
            '<p id="events-notice"></p>',
            "</section>",
        ]
    )
    cursor = escape(str(events.get("next_cursor") or ""))
    script = _TASK_SCRIPT.replace("__REF__", _js(ref)).replace("__CURSOR__", _js(cursor))
    return _page(f"card {ref}", body, script=script)


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
            ]
        )
    workers = [item.get("run") or {} for item in items if (item.get("run") or {}).get("role") == "worker"]
    return _rows(["run", "role", "profile", "phase", "state"], rows) + _review_form(
        ref, worker=workers[-1] if workers else None
    )


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
            f"<pre>{escape(str(entry.get('body') or ''))}</pre>"
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
