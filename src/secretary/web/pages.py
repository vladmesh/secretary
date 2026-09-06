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
form.sprint { display: block; }
form.sprint .field { margin: .9rem 0; }
form.sprint textarea { width: 100%; min-height: 4.5rem; }
form.sprint select { min-width: 22rem; max-width: 100%; }
form.sprint .choices { border: 1px solid var(--line); padding: .4rem .6rem; max-height: 14rem; overflow-y: auto; }
form.sprint .choices label { display: block; font-size: inherit; color: inherit; padding: .1rem 0; }
form.sprint .hint { color: var(--dim); font-size: .8rem; }
.bad-field { color: var(--warn); font-size: .85rem; }
.refused { border-left: 3px solid var(--warn); background: #b3541e14; padding: .5rem .75rem; margin: .5rem 0; }
.pending { border-left: 3px solid var(--warn); background: #b3541e14; padding: .5rem .75rem; margin: .5rem 0; }
.launch { font-weight: 600; }
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
            "<section><h2>sprints</h2>",
            '<p><a href="/sprints/new">open a new sprint</a></p>',
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
    return _page("new sprint", body, script=_SPRINT_FORM_SCRIPT)


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
    body = "\n".join(
        part
        for part in [
            f'<p class="age">read at {escape(str(document.get("observed_at") or "an unknown time"))}</p>',
            "<section><h2>sprint</h2>",
            _source_block(sprint_section.get("source"), what="what this sprint is"),
            _sprint_fields(value),
            "</section>",
            "<section><h2>observer</h2>",
            _observer_section(observer),
            "</section>",
            "<section><h2>the heads its cards run on</h2>",
            _executor_section((value or {}).get("executors") or {}),
            "</section>",
            "<section><h2>current card</h2>",
            _current_task(value),
            "</section>",
            "<section><h2>the observer's last resume</h2>",
            _resume((value or {}).get("resume")),
            "</section>",
        ]
        if part
    )
    return _page(f"sprint {ref}", body)


def _sprint_fields(value: dict[str, Any] | None) -> str:
    if value is None:
        return '<p class="empty">no sprint was read, so there is nothing to show here.</p>'
    return _rows(
        ["", ""],
        [
            ["reference", _or_dash(value.get("ref"))],
            ["status", f'<span class="state">{_or_dash(value.get("status"))}</span>'],
            ["goal", f"<pre>{escape(str(value.get('goal') or ''))}</pre>"],
            ["definition of done", f"<pre>{escape(str(value.get('definition_of_done') or ''))}</pre>"],
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
    rows = [[escape(str(name).replace("_", " ")), f"<pre>{escape(str(resume[name]))}</pre>"] for name in sorted(resume)]
    return _rows(["", ""], rows)
