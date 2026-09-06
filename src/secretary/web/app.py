"""The routes, and what each of them calls. One layer operation per route, and no route without one.

This module is the transport in the literal sense: it turns a request line into arguments, calls
one operation of :mod:`secretary.webproto`, and turns what comes back into JSON or into a page. It
holds no session, no cache and no state between requests — the cursor a client watches a card with
is the client's, which is why a reload and a reconnect resume rather than restart, and why two
browsers watching the same card cannot disturb each other. The same rule covers the sprint form:
the request id that makes a submission idempotent lives in the form the browser holds, and this
process remembers nothing between the two requests that would let it invent a second one.

The one thing this module decides for itself is who may make a mutation, and it decides it in one
place on the POST path rather than per route (:func:`cross_origin_reason`). Everything else it is
handed.

The route table is the surface, all of it, and it is a table so that it can be read and asserted
against. Every entry names an operation that already exists. There is no entry that takes a
command, a shell, a script, a path or a module to run, and there is no catch-all: an unrouted path
is 404 and an unrouted method on a routed path is 405, neither of which reaches any handler.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, quote, unquote

from secretary.web import pages
from secretary.web.statuses import status_for
from secretary.webproto.errors import OperationPending, ReadError, ValidationRefused
from secretary.webproto.journal import DEFAULT_LIMIT, MAX_LIMIT
from secretary.webproto.reads import TASK_SNAPSHOT_EVENTS
from secretary.webproto.sprint_reads import NONE_SPELLING

#: The largest request body this transport reads. Every body it accepts is a handful of short
#: fields, so anything above this is a mistake or an attempt, and reading it would be neither.
MAX_BODY_BYTES = 64 * 1024

JSON_TYPE = "application/json; charset=utf-8"
HTML_TYPE = "text/html; charset=utf-8"
FORM_TYPE = "application/x-www-form-urlencoded"

#: The two body encodings a route may declare. A JSON object is what a program sends; a submitted
#: form is what a browser sends, and it is the encoding the sprint form uses so that the page works
#: as a page -- the request id it carries is in the markup the browser holds, which is exactly what
#: makes a double click, a retry and a reconnection one sprint rather than three.
JSON_BODY = "json"
FORM_BODY = "form"


@dataclass(frozen=True, slots=True)
class Response:
    """What a handler produced, before any HTTP machinery has touched it."""

    status: int
    body: bytes
    content_type: str
    headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Route:
    """One reachable thing, and the operation behind it."""

    method: str
    pattern: str
    handler: str
    #: The layer call this route is a transport for, named so the table reads as the contract it is.
    operation: str
    #: How a body arrives here: a JSON object, or a submitted HTML form. Both are read into the
    #: same shape and both are held to the same closed field list; what differs is the decoding.
    body: str = JSON_BODY
    #: Whether this route answers a person or a program. A refusal on a page route is rendered as
    #: a page carrying the same status, so a browser shows the reason instead of a blank body.
    page: bool = False

    @property
    def segments(self) -> tuple[str, ...]:
        return tuple(part for part in self.pattern.split("/") if part)


#: The whole externally reachable surface of this service.
ROUTES: tuple[Route, ...] = (
    Route("GET", "/", "dashboard", "reads.system_snapshot", page=True),
    Route("GET", "/tasks/{ref}", "task_page", "reads.task_snapshot", page=True),
    Route("GET", "/sprints/new", "sprint_form", "sprint_reads.sprint_options", page=True),
    Route("POST", "/sprints", "sprint_create", "sprint_ops.sprint_create", body=FORM_BODY, page=True),
    Route("GET", "/sprints/{ref}", "sprint_page", "sprint_reads.sprint_state", page=True),
    Route("GET", "/api/system", "system", "reads.system_snapshot"),
    Route("GET", "/api/tasks/{ref}", "task", "reads.task_snapshot"),
    Route("GET", "/api/tasks/{ref}/events", "events", "reads.task_events"),
    Route("GET", "/api/tasks/{ref}/runs", "task_runs", "ops.run_list"),
    Route("GET", "/api/runs/{run_id}", "run", "ops.run_state"),
    Route("POST", "/api/runs/start", "start", "ops.run_start"),
    Route("POST", "/api/runs/review", "review", "ops.run_review"),
)

#: The fields each POST accepts, and the only ones. A body carrying anything else is refused rather
#: than silently ignored: an unknown field is a client that believes this endpoint does something it
#: does not, and answering it as though the request had been understood is how a transport grows a
#: second, undocumented surface.
START_FIELDS = frozenset({"ref", "request_id", "profile", "instruction"})
REVIEW_FIELDS = frozenset({"ref", "request_id", "profile", "worker_run_id"})
SPRINT_FIELDS = frozenset(
    {
        "request_id",
        "product",
        "goal",
        "definition_of_done",
        "issues",
        "projects",
        "observer",
        "worker",
        "reviewer",
    }
)

#: The role and the actor a sprint opened from here is opened under. The web has no identity of its
#: own -- the front checks one password belonging to the owner -- so it says which of the roles
#: `SprintWriter.create` admits it is acting as, and names itself as the actor so the sprint's audit
#: says where the create came from rather than pretending to be a CLI.
SPRINT_ROLE = "po"
SPRINT_ACTOR = "web"

#: The value the two executor selects carry when the owner leaves the role to the observer. It is
#: the empty option of an HTML select and never a profile name: the handler turns it into `None`,
#: which is the layer's spelling for "nothing was said about this role". See criterion 4.
EXECUTOR_UNPINNED = ""


class WebApp:
    """The routing half, built over the read, operation and sprint layers.

    All four are handed in rather than constructed here, which is what lets a test drive every
    route against fakes with no socket, no Orca and no live installation. None of them is
    optional: a layer a route needs is a fact about how this application was built, so an
    application missing one fails where it was built rather than on the first request that
    reaches that route.
    """

    def __init__(self, reads: Any, ops: Any, sprint_reads: Any, sprint_ops: Any) -> None:
        self.reads = reads
        self.ops = ops
        self.sprint_reads = sprint_reads
        self.sprint_ops = sprint_ops

    # -- the entry point -------------------------------------------------------------------

    def handle(
        self,
        method: str,
        path: str,
        *,
        query: str = "",
        body: bytes = b"",
        headers: Any = None,
    ) -> Response:
        """One request, answered. The only place a protocol code becomes a status.

        The cross-origin check is here and only here. It is asked once, of every POST, before a
        handler is chosen and therefore before any operation of the layer can run -- which is the
        whole of it: a rule written per route is a rule the next route forgets, and the two routes
        that already start heads would have been exactly the ones nobody went back to.
        """
        route, params = self.match(method, path)
        if route is None:
            return self._refuse(
                method,
                path,
                status=params["status"],
                code=params["code"],
                message=params["message"],
            )
        if route.method == "POST":
            reason = cross_origin_reason(headers)
            if reason is not None:
                return self._deny(route, status=403, code="cross_origin", message=reason)
        handler: Callable[..., Response] = getattr(self, f"_{route.handler}")
        try:
            payload = _payload(route, body)
            return handler(params, _query(query), payload)
        except ReadError as exc:
            return self._error(route, exc)

    def match(self, method: str, path: str) -> tuple[Route | None, dict[str, Any]]:
        """The route for this request, or why there is none: 404 for a path, 405 for a method.

        A path a literal route matches belongs to that route, and a route with a placeholder is
        never considered beside it: `/api/runs/start` is the start operation and not a read of a
        run named "start". Without that rule the two would be decided by the order of the table,
        which is not a contract anybody should have to know.
        """
        wanted = [unquote(part) for part in path.split("/") if part]
        candidates = [
            (route, params)
            for route, params in ((route, _bind(route.segments, wanted)) for route in ROUTES)
            if params is not None
        ]
        literal = [pair for pair in candidates if not pair[1]]
        for route, params in literal or candidates:
            if route.method == method.upper():
                return route, params
        if candidates:
            allowed = ", ".join(sorted({route.method for route, _ in (literal or candidates)}))
            return None, {
                "status": 405,
                "code": "method_not_allowed",
                "message": f"{method} is not one of the methods this route answers ({allowed})",
            }
        return None, {
            "status": 404,
            "code": "not_found",
            "message": "this service answers only the named routes it publishes; there is no route here",
        }

    # -- read routes -----------------------------------------------------------------------

    def _system(self, _params, _query, _body) -> Response:
        return _json(200, self.reads.system_snapshot())

    def _task(self, params, query, _body) -> Response:
        return _json(200, self.reads.task_snapshot(params["ref"], events=_events_count(query)))

    def _events(self, params, query, _body) -> Response:
        return _json(
            200,
            self.reads.task_events(params["ref"], _one(query, "cursor"), limit=_limit(query)),
        )

    def _task_runs(self, params, _query, _body) -> Response:
        return _json(200, self.ops.run_list(params["ref"]))

    def _run(self, params, _query, _body) -> Response:
        return _json(200, self.ops.run_state(params["run_id"]))

    # -- run routes ------------------------------------------------------------------------

    def _start(self, _params, _query, body) -> Response:
        _fields(body, START_FIELDS, "start")
        return _json(
            200,
            self.ops.run_start(
                _required(body, "ref"),
                request_id=_required(body, "request_id"),
                profile=_required(body, "profile"),
                instruction=_text(body.get("instruction")),
            ),
        )

    def _review(self, _params, _query, body) -> Response:
        _fields(body, REVIEW_FIELDS, "review")
        return _json(
            200,
            self.ops.run_review(
                request_id=_required(body, "request_id"),
                profile=_required(body, "profile"),
                ref=_text(body.get("ref")),
                worker_run_id=_text(body.get("worker_run_id")),
            ),
        )

    # -- pages -----------------------------------------------------------------------------

    def _dashboard(self, _params, _query, _body) -> Response:
        return _html(200, pages.dashboard(self.reads.system_snapshot()))

    def _task_page(self, params, query, _body) -> Response:
        ref = params["ref"]
        snapshot = self.reads.task_snapshot(ref, events=_events_count(query))
        return _html(200, pages.task(snapshot, runs=self._runs_or_reason(ref)))

    def _runs_or_reason(self, ref: str) -> dict[str, Any]:
        """The card's product runs for the page, or the reason there are none to show.

        A run listing that refused must not take the card page down with it: the state, the events
        and the result are read from other sources and are still worth showing. This is the same
        rule the layer applies inside a snapshot, applied by the transport to the one call it makes
        beside the snapshot.
        """
        try:
            return {"available": True, "reason": None, "items": self.ops.run_list(ref)["items"]}
        except ReadError as exc:
            return {"available": False, "reason": exc.message, "items": []}

    # -- sprint routes ---------------------------------------------------------------------

    def _sprint_form(self, _params, _query, _body) -> Response:
        """The empty form, on this installation's own catalogue.

        The request id is minted here rather than by the browser, and it is minted once per form:
        it is what the submission carries back, so it is a property of *this* form and not of each
        POST somebody makes from it. A reload of this page is a new intention and gets a new id; a
        second submission of the page already open is the same intention and gets the same one.
        """
        return _html(
            200,
            pages.sprint_form(
                self.sprint_reads.sprint_options(),
                submitted=_blank_submission(_request_id()),
                errors={},
            ),
        )

    def _sprint_create(self, _params, _query, body) -> Response:
        """One submission: refuse what is incomplete, hand the rest down, and go to the sprint.

        Two kinds of refusal, and they are not the same kind of thing. A field the form itself
        requires -- no goal, no definition of done, no observer, no issue, no project -- is answered
        here, named field by field, because the person is looking at the form and can fix it. What a
        sprint *may be* is never decided here: an unknown profile, a closed issue, an unregistered
        project and a project another sprint holds are the writer's judgements, reached through the
        layer, and what this does with them is show what it was told beside the values the person
        typed.

        Success is a redirect and not a rendered page, so the address bar ends up on the sprint and
        a refresh re-reads it rather than re-posting the form.

        **A refusal decides what happens to the request id, and the two answers are opposite.**
        `sprint_create` claims the id, with a digest of the inputs, *before* the writer judges them
        (see its docstring): so a refusal that leaves nothing behind has still spent that id, and a
        corrected resubmission under it would be answered `validation: different inputs` -- a dead
        end with no sprint and no way forward. A refusal that is not an
        :class:`~secretary.webproto.errors.OperationPending` therefore comes back on a form carrying
        a *new* id, because a corrected submission really is a new request and nothing durable was
        created. An `OperationPending` is the exact opposite and must keep the same id and the same
        values: a sprint exists, and only that id reaches it.
        """
        _fields(body, SPRINT_FIELDS, "sprint create")
        submitted = _submission(body)
        errors = _incomplete(submitted)
        if errors:
            # Nothing reached the layer, so this id was never claimed and is still the right one.
            return self._form_again(submitted, errors=errors, status=400)
        try:
            created = self.sprint_ops.sprint_create(
                request_id=submitted["request_id"],
                actor=SPRINT_ACTOR,
                role=SPRINT_ROLE,
                product=submitted["product"],
                goal=submitted["goal"],
                definition_of_done=submitted["definition_of_done"],
                issues=list(submitted["issues"]),
                projects=list(submitted["projects"]),
                observer=submitted["observer"],
                worker=_pin(submitted["worker"]),
                reviewer=_pin(submitted["reviewer"]),
            )
        except OperationPending as exc:
            return self._form_again(submitted, errors={}, status=status_for(exc.code), refusal=exc)
        except ReadError as exc:
            return self._form_again(
                submitted, errors={}, status=status_for(exc.code), refusal=exc, fresh=True
            )
        reference = str((created.get("sprint") or {}).get("ref") or "")
        return _redirect(f"/sprints/{quote(reference)}")

    def _sprint_page(self, params, _query, _body) -> Response:
        return _html(200, pages.sprint(self.sprint_reads.sprint_state(params["ref"])))

    def _form_again(
        self,
        submitted: dict[str, Any],
        *,
        errors: dict[str, str],
        status: int,
        refusal: ReadError | None = None,
        fresh: bool = False,
    ) -> Response:
        """The form the person just submitted, with what was refused and everything they typed.

        `fresh` mints a new request id for the form, and it is the only thing that ever replaces a
        value the person's submission carried. It is set exactly when the layer refused without
        leaving a sprint behind, for the reason :meth:`_sprint_create` gives: that id is spent, and
        a form that handed it back would let the owner correct a field and be told the correction
        is a different request. Every other value comes back untouched -- including one the
        catalogue no longer offers, which the page marks rather than drops, because a form that
        quietly changed a submitted choice would be asking for a repeat of something else.

        The catalogue is read again because the form is rendered again, and a catalogue that cannot
        be read must not replace the refusal on the screen with its own: the reason the submission
        was refused is the thing being answered, so an unreadable catalogue is shown beside it as a
        section that could not be read rather than raised over the top of it.
        """
        shown = dict(submitted)
        if fresh:
            shown["request_id"] = _request_id()
        try:
            options, catalogue = self.sprint_reads.sprint_options(), None
        except ReadError as exc:
            options, catalogue = None, exc.message
        return _html(
            status,
            pages.sprint_form(
                options,
                submitted=shown,
                errors=errors,
                refusal=None if refusal is None else refusal.to_json(),
                catalogue=catalogue,
                reissued=fresh,
            ),
        )

    # -- failures --------------------------------------------------------------------------

    def _error(self, route: Route, exc: ReadError) -> Response:
        status = status_for(exc.code)
        if route.page:
            return _html(status, pages.error(status, exc.code, exc.message))
        return _json(status, {"error": exc.to_json()})

    def _deny(self, route: Route, *, status: int, code: str, message: str) -> Response:
        """A refusal this transport made itself, in the shape the route answers in."""
        if route.page:
            return _html(status, pages.error(status, code, message))
        return _json(status, {"error": {"code": code, "message": message}})

    def _refuse(self, method: str, path: str, *, status: int, code: str, message: str) -> Response:
        if status == 404 and method.upper() == "GET" and not path.startswith("/api/"):
            return _html(status, pages.error(status, code, message))
        return _json(status, {"error": {"code": code, "message": message}})


# -- request parsing ----------------------------------------------------------------------------


def _bind(pattern: tuple[str, ...], wanted: list[str]) -> dict[str, Any] | None:
    if len(pattern) != len(wanted):
        return None
    params: dict[str, Any] = {}
    for expected, given in zip(pattern, wanted, strict=True):
        if expected.startswith("{") and expected.endswith("}"):
            params[expected[1:-1]] = given
        elif expected != given:
            return None
    return params


def _query(raw: str) -> dict[str, list[str]]:
    return parse_qs(raw or "", keep_blank_values=True)


def _one(query: dict[str, list[str]], name: str) -> str | None:
    values = query.get(name) or []
    return values[-1] if values and values[-1] != "" else None


def _int(query: dict[str, list[str]], name: str, default: int, *, ceiling: int) -> int:
    raw = _one(query, name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValidationRefused(f"{name} must be a whole number, not {raw!r}") from None
    if value < 1 or value > ceiling:
        raise ValidationRefused(f"{name} must be between 1 and {ceiling}")
    return value


def _limit(query: dict[str, list[str]]) -> int:
    return _int(query, "limit", DEFAULT_LIMIT, ceiling=MAX_LIMIT)


def _events_count(query: dict[str, list[str]]) -> int:
    return _int(query, "events", TASK_SNAPSHOT_EVENTS, ceiling=MAX_LIMIT)


def _payload(route: Route, raw: bytes) -> dict[str, Any]:
    """The body of this request, decoded the way this route says its clients send one."""
    if route.method != "POST":
        return {}
    return _form(raw) if route.body == FORM_BODY else _body(raw)


def _form(raw: bytes) -> dict[str, Any]:
    """A submitted HTML form, as the fields it carries.

    A field a form submits more than once -- the issues and the projects a sprint serves -- is a
    list, and one submitted once is the string it carries. `keep_blank_values` is on because an
    empty field is an answer: the executor selects are submitted empty when the owner leaves the
    role to the observer, and dropping them here would make "nothing was said" indistinguishable
    from "this browser sent no such field at all".
    """
    if len(raw) > MAX_BODY_BYTES:
        raise ValidationRefused(f"this request body is larger than the {MAX_BODY_BYTES} bytes accepted here")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationRefused(f"this form is not UTF-8 text: {exc}") from None
    return {name: values for name, values in parse_qs(text, keep_blank_values=True).items()}


def _body(raw: bytes) -> dict[str, Any]:
    if len(raw) > MAX_BODY_BYTES:
        raise ValidationRefused(f"this request body is larger than the {MAX_BODY_BYTES} bytes accepted here")
    if not raw.strip():
        raise ValidationRefused("this route takes a JSON object body")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValidationRefused(f"this request body is not JSON: {exc}") from None
    if not isinstance(payload, dict):
        raise ValidationRefused("this route takes a JSON object body")
    return payload


def _fields(body: dict[str, Any], allowed: frozenset[str], operation: str) -> None:
    unknown = sorted(set(body) - allowed)
    if unknown:
        raise ValidationRefused(
            f"the {operation} operation takes only {', '.join(sorted(allowed))}; "
            f"it was given {', '.join(unknown)}"
        )


def _required(body: dict[str, Any], name: str) -> str:
    value = _text(body.get(name))
    if not value:
        raise ValidationRefused(f"{name} is required")
    return value


def _text(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValidationRefused("every field of these requests is a string")
    return value


# -- the sprint submission ----------------------------------------------------------------------


def _submission(form: dict[str, Any]) -> dict[str, Any]:
    """One form as the sprint create's own vocabulary, and as what to put back in the boxes.

    This is deliberately the only shape the handler and the page both know: the person's answers,
    whatever became of them. So a refused submission is re-rendered from the same object that was
    sent down, and no field can be lost on the way back by being read out of two different places.
    """
    return {
        "request_id": _first(form, "request_id"),
        "product": _first(form, "product"),
        "goal": _first(form, "goal"),
        "definition_of_done": _first(form, "definition_of_done"),
        "issues": _all(form, "issues"),
        "projects": _all(form, "projects"),
        "observer": _first(form, "observer"),
        "worker": _first(form, "worker"),
        "reviewer": _first(form, "reviewer"),
    }


def _blank_submission(request_id: str) -> dict[str, Any]:
    submission = _submission({})
    submission["request_id"] = request_id
    return submission


def _first(form: dict[str, Any], name: str) -> str:
    values = _all(form, name)
    return values[-1] if values else ""


def _all(form: dict[str, Any], name: str) -> list[str]:
    raw = form.get(name)
    values = raw if isinstance(raw, list) else [] if raw is None else [raw]
    for value in values:
        if not isinstance(value, str):
            raise ValidationRefused("every field of a submitted form is text")
    return [value.strip() for value in values if str(value).strip()]


#: What the form itself requires, and the words each one is refused with. These are the four
#: emptinesses a person can see on their own screen; everything about whether a filled-in value is
#: *admissible* belongs to the writer and is never re-decided here.
_REQUIRED: tuple[tuple[str, str], ...] = (
    ("request_id", "this submission carries no request id, so it cannot be repeated safely; open the form again"),
    ("product", "choose the product this sprint serves"),
    ("goal", "say what this sprint is for; a sprint with no goal cannot be reviewed against one"),
    ("definition_of_done", "say what would make this sprint done"),
    ("issues", "choose at least one open issue of that product for this sprint to serve"),
    ("projects", "choose at least one registered project for this sprint to reserve"),
    ("observer", "choose the observer head that will run this sprint"),
)


#: Why the one answer that is not a profile is not an answer *here*. `none` opens a sprint the
#: production tick deliberately raises no observer for, so on this route it would be a button
#: labelled "start" that starts nothing. It stays a legal answer for `secretary sprint create` and
#: for the rows that already carry it -- the sprint page renders those unchanged -- and what is
#: narrowed is this client, not the contract. The spelling is the layer's own rather than a word
#: repeated here, so a layer that ever spelled it differently would be refused under its own name.
OBSERVER_MUST_BE_A_PROFILE = (
    "a sprint opened from here names the head that will run it: opening one with no observer means "
    "nothing is raised for it, which is not what this page's button says. Choose a profile, or open "
    "such a sprint with `secretary sprint create --observer none`"
)


def _incomplete(submitted: dict[str, Any]) -> dict[str, str]:
    errors = {name: reason for name, reason in _REQUIRED if not submitted.get(name)}
    if "observer" not in errors and submitted.get("observer") == NONE_SPELLING:
        errors["observer"] = OBSERVER_MUST_BE_A_PROFILE
    return errors


def _pin(value: str) -> str | None:
    """One executor select, as the layer spells it: a profile, or nothing said about the role.

    The empty option means the observer chooses, and `None` is how the layer is told so -- the row
    is then written with no field for that role at all. An empty string must never travel down as
    if it were a profile name, which is the whole reason this is a function and not an inline
    `or`.
    """
    text = str(value or "").strip()
    return None if text == EXECUTOR_UNPINNED else text


def _request_id() -> str:
    """The id one form carries for its whole life. See :meth:`WebApp._sprint_form`."""
    return f"web-sprint-{uuid.uuid4()}"


# -- who may make a mutation ----------------------------------------------------------------------

#: Said to a browser whose page came from somewhere else. Quoted into the refusal so the reason is
#: on the screen rather than only in a status number.
CROSS_ORIGIN_REFUSAL = (
    "this request was made from a page this service did not serve, so it is refused before any "
    "operation runs; open the page from this service's own address and submit it there"
)


def cross_origin_reason(headers: Any) -> str | None:
    """Why this POST is refused as cross-origin, or `None` if it may proceed.

    The rule is the one a browser makes checkable: a browser sends `Origin` on every request whose
    method is not GET or HEAD, on its own requests as much as on somebody else's, so a POST that
    carries an origin naming a host other than the one it was addressed to came from a page this
    service did not serve. That is refused here, before a handler exists.

    Two properties of the shape are load-bearing:

    **A request with no `Origin` at all is not a browser**, and it keeps working. `secretary
    web-run`, `curl` and the diagnostics in OPERATIONS.md send none, and refusing them would break
    the loopback client this service is operated with while defending nothing: cross-origin is a
    browser's problem precisely because a browser is the thing that attaches somebody else's
    credentials to a request the person did not make.

    **The comparison is host and port, never scheme.** The front terminates TLS and proxies to
    `127.0.0.1` over plain HTTP, so a genuine `https://host` origin arrives at a process that would
    call itself `http`. Comparing schemes would refuse every real request through the published
    front; comparing the authority is what the check is actually about.
    """
    origin = _header(headers, "Origin")
    if not origin:
        return None
    if origin.strip().lower() == "null":
        return CROSS_ORIGIN_REFUSAL
    host = _header(headers, "Host")
    if not host:
        return CROSS_ORIGIN_REFUSAL
    return None if _authority(origin) == host.strip().lower() else CROSS_ORIGIN_REFUSAL


def _authority(origin: str) -> str:
    """The `host:port` of an origin, with the scheme dropped. See :func:`cross_origin_reason`."""
    text = origin.strip().lower()
    _scheme, separator, rest = text.partition("://")
    return (rest if separator else text).split("/")[0]


def _header(headers: Any, name: str) -> str:
    """One header, from whatever the caller was handed: a mapping, or `http.client.HTTPMessage`."""
    if headers is None:
        return ""
    getter = getattr(headers, "get", None)
    if getter is None:
        return ""
    value = getter(name)
    if value is None:
        value = getter(name.lower())
    return str(value or "")


# -- responses ----------------------------------------------------------------------------------


def _json(status: int, document: Any) -> Response:
    return Response(status, json.dumps(document, sort_keys=True).encode("utf-8"), JSON_TYPE)


def _html(status: int, markup: str) -> Response:
    return Response(status, markup.encode("utf-8"), HTML_TYPE)


def _redirect(location: str) -> Response:
    """See the thing that was made, at its own address.

    303 and not 302: the browser is told to *get* what the POST produced, so the address bar ends
    on the sprint and a refresh re-reads it. A form that answered a submission with a rendered page
    would leave the browser holding a POST it can be asked to repeat, which is the one thing the
    request id exists to make harmless and the one thing a person should not have to rely on it
    for.
    """
    return Response(303, pages.redirect(location).encode("utf-8"), HTML_TYPE, {"Location": location})
