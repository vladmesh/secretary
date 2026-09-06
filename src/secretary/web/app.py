"""The routes, and what each of them calls. One layer operation per route, and no route without one.

This module is the transport in the literal sense: it turns a request line into arguments, calls
one operation of :mod:`secretary.webproto`, and turns what comes back into JSON or into a page. It
holds no session, no cache and no state between requests — the cursor a client watches a card with
is the client's, which is why a reload and a reconnect resume rather than restart, and why two
browsers watching the same card cannot disturb each other.

The route table is the surface, all of it, and it is a table so that it can be read and asserted
against. Every entry names an operation that already exists. There is no entry that takes a
command, a shell, a script, a path or a module to run, and there is no catch-all: an unrouted path
is 404 and an unrouted method on a routed path is 405, neither of which reaches any handler.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, unquote

from secretary.web import pages
from secretary.web.statuses import status_for
from secretary.webproto.errors import ReadError, ValidationRefused
from secretary.webproto.journal import DEFAULT_LIMIT, MAX_LIMIT
from secretary.webproto.reads import TASK_SNAPSHOT_EVENTS

#: The largest request body this transport reads. Every body it accepts is a handful of short
#: fields, so anything above this is a mistake or an attempt, and reading it would be neither.
MAX_BODY_BYTES = 64 * 1024

JSON_TYPE = "application/json; charset=utf-8"
HTML_TYPE = "text/html; charset=utf-8"


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

    @property
    def segments(self) -> tuple[str, ...]:
        return tuple(part for part in self.pattern.split("/") if part)


#: The whole externally reachable surface of this service.
ROUTES: tuple[Route, ...] = (
    Route("GET", "/", "dashboard", "reads.system_snapshot"),
    Route("GET", "/tasks/{ref}", "task_page", "reads.task_snapshot"),
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


class WebApp:
    """The routing half, built over one read layer and one operation layer.

    Both are handed in rather than constructed here, which is what lets a test drive every route
    against a fake board and a fake head backend with no socket, no Orca and no live installation.
    """

    def __init__(self, reads: Any, ops: Any) -> None:
        self.reads = reads
        self.ops = ops

    # -- the entry point -------------------------------------------------------------------

    def handle(self, method: str, path: str, *, query: str = "", body: bytes = b"") -> Response:
        """One request, answered. The only place a protocol code becomes a status."""
        route, params = self.match(method, path)
        if route is None:
            return self._refuse(
                method,
                path,
                status=params["status"],
                code=params["code"],
                message=params["message"],
            )
        handler: Callable[..., Response] = getattr(self, f"_{route.handler}")
        try:
            payload = _body(body) if route.method == "POST" else {}
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

    # -- failures --------------------------------------------------------------------------

    def _error(self, route: Route, exc: ReadError) -> Response:
        status = status_for(exc.code)
        if route.handler in {"dashboard", "task_page"}:
            return _html(status, pages.error(status, exc.code, exc.message))
        return _json(status, {"error": exc.to_json()})

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


# -- responses ----------------------------------------------------------------------------------


def _json(status: int, document: Any) -> Response:
    return Response(status, json.dumps(document, sort_keys=True).encode("utf-8"), JSON_TYPE)


def _html(status: int, markup: str) -> Response:
    return Response(status, markup.encode("utf-8"), HTML_TYPE)
