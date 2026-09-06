"""The owner's path through the sprint form, walked over a real socket, and written down.

This is not a third suite of assertions about the transport: `tests/test_web_sprint_transport.py`
already pins the transport against fakes and against the real layer, and
`tests/test_web_sprint_protocol.py` pins the layer. What was missing at the end of sprint 1428 was
the *path* — the eight things an owner actually does in order, each one made as an HTTP request on a
socket by a client that holds nothing but what the previous response gave it, with the answer of
each step recorded rather than retold.

Two properties are what make the record worth anything.

**Nothing live is touched.** The installation this walks is built in a temporary directory: its own
instance, its own data plane, its own installed head registry, and the repository's in-process board
fake in place of a Kanboard. No socket leaves loopback, no live data directory is read or written,
and the live `secretary-web.service` is neither restarted nor consulted. What this therefore does
*not* prove is stated in the same words wherever the record is quoted: the live board's behaviour on
the new `sprint_worker` / `sprint_reviewer` keys, and the readability of a sprint row the moment it
is written, are properties of a Kanboard and are here only as a fake's.

**The client is a browser's client.** The request id travels in the markup of the form, exactly as
it does in a browser, and every submission below re-sends what the previous page handed back. A
walkthrough that minted its own ids would pass while the double-click case failed.

Run it directly to get the transcript:

    python3 -P -m tests.sprint_user_path            # transcript on stdout, exit 1 on any surprise
    python3 -P -m tests.sprint_user_path --out FILE # and a copy in FILE

`tests/test_sprint_user_path.py` runs the same function, so a change that breaks the path breaks CI
rather than only the day somebody re-runs the script.
"""

from __future__ import annotations

import argparse
import re
from datetime import datetime, timezone
from dataclasses import dataclass, field
from http.client import HTTPConnection, HTTPResponse
from threading import Thread
from typing import Any
from unittest import mock
from urllib.parse import quote, urlencode

from secretary.sprints import SPRINT_BOARD_NAME
from secretary.web.app import WebApp
from secretary.web.server import build_server
from secretary.webproto.ops import OperationLayer
from secretary.webproto.reads import ReadLayer
from secretary.webproto.runs import RunStoreError
from secretary.webproto.sprint_requests import SprintRequestStore
from tests.webproto_sprint_fixtures import (
    OBSERVER_PROFILE,
    REVIEWER_PROFILE,
    WORKER_PROFILE,
    SprintProtocolFixture,
)

GOAL = "Open a sprint from the browser"
DEFINITION_OF_DONE = "the owner reaches a sprint page with an observer"
SECOND_GOAL = "Open a second sprint with both executors pinned"


class Surprise(AssertionError):
    """A step did not answer what the path says it answers."""


@dataclass
class Step:
    """One thing the owner did, and what came back."""

    number: int
    title: str
    request: str
    status: int
    notes: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    def expect(self, claim: str, held: bool) -> None:
        (self.notes if held else self.failures).append(claim)

    def render(self) -> str:
        lines = [f"{self.number}. {self.title}", f"   → {self.request}", f"   ← HTTP {self.status}"]
        lines += [f"   ✓ {note}" for note in self.notes]
        lines += [f"   ✗ {failure}" for failure in self.failures]
        return "\n".join(lines)


class Installation(SprintProtocolFixture):
    """The suites' own temporary installation, driven outside a test runner.

    Subclassed rather than copied on purpose: a walkthrough that built its own instance would drift
    from the fixture the two suites assert against, and the first thing to drift would be the head
    registry — which is exactly what the form offers.
    """

    def runTest(self) -> None:  # pragma: no cover - never run as a test
        raise NotImplementedError


class Browser:
    """An HTTP client that keeps nothing but the last page it was given."""

    def __init__(self, host: str, port: int) -> None:
        self.host, self.port = host, port
        self.origin = f"http://{host}:{port}"

    def request(self, method: str, path: str, *, body: bytes | None = None) -> tuple[int, dict, str]:
        connection = HTTPConnection(self.host, self.port, timeout=20)
        headers = {"Host": f"{self.host}:{self.port}"}
        if body is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            headers["Origin"] = self.origin
        try:
            connection.request(method, path, body=body or b"", headers=headers)
            response: HTTPResponse = connection.getresponse()
            payload = response.read().decode("utf-8", "replace")
            return response.status, dict(response.getheaders()), payload
        finally:
            connection.close()

    def get(self, path: str) -> tuple[int, dict, str]:
        return self.request("GET", path)

    def post(self, path: str, fields: list[tuple[str, str]]) -> tuple[int, dict, str]:
        return self.request("POST", path, body=urlencode(fields).encode("utf-8"))


def request_id_of(markup: str) -> str:
    found = re.search(r'name="request_id" value="([^"]+)"', markup)
    if found is None:
        raise Surprise("the form this page returned carries no request id")
    return found.group(1)


def submission(request_id: str, **overrides: Any) -> list[tuple[str, str]]:
    """What the form posts, as a browser flattens it: repeated names for the two multi-selects."""
    values: dict[str, Any] = {
        "request_id": request_id,
        "product": "secretary",
        "goal": GOAL,
        "definition_of_done": DEFINITION_OF_DONE,
        "issues": ["issue:open"],
        "projects": ["secretary"],
        "observer": OBSERVER_PROFILE,
        "worker": "",
        "reviewer": "",
    }
    values.update(overrides)
    flat: list[tuple[str, str]] = []
    for name, value in values.items():
        if isinstance(value, (list, tuple)):
            flat += [(name, one) for one in value]
        else:
            flat.append((name, str(value)))
    return flat


def _shown(markup: str) -> str:
    """The page with its tags removed: what a person reads, rather than what the markup holds."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", markup))


def walk(record: list[Step]) -> list[Step]:
    """The path, in the order an owner walks it. Every step appends to ``record``.

    Each scenario gets its own installation and its own server. That is not fastidiousness: this
    product admits one open sprint per installation, so a walk that opened four sprints against one
    installation would be walking the refusal for the limit rather than the path. What every
    scenario shares is the fixture, so the form is offered off the same catalogue each time.
    """
    for scenario in (_the_owners_path, _pinning_both_executors, _a_profile_that_is_gone, _an_interrupted_create):
        installation = Installation()
        installation.setUp()
        try:
            app = WebApp(
                ReadLayer(installation.instance, data_dir=installation.data_dir, offline=True),
                OperationLayer(installation.instance, data_dir=installation.data_dir),
                installation.reads(),
                installation.ops(),
            )
            server = build_server(app, host="127.0.0.1", port=0)
            host, port = server.server_address[0], server.server_address[1]
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                scenario(Browser(str(host), int(port)), installation, record)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=10)
            _isolation(installation, app, record)
        finally:
            installation.doCleanups()
    return record


def _step(record: list[Step], title: str, request: str, status: int) -> Step:
    entry = Step(len(record) + 1, title, request, status)
    record.append(entry)
    return entry


def _isolation(installation: Installation, app: WebApp, record: list[Step]) -> None:
    """What this scenario touched, asked of the fixture rather than promised in a comment."""
    entry = _step(record, "Isolation, checked when the server is down", "(no request: the fixture is inspected)", 0)
    roots = {
        name: (getattr(app, name).instance, getattr(app, name).data_dir())
        for name in ("reads", "ops", "sprint_reads", "sprint_ops")
    }
    outside = {
        name: paths
        for name, paths in roots.items()
        if not all(path.is_relative_to(installation.tmp) for path in paths)
    }
    entry.expect(
        f"all four layers read and wrote inside {installation.tmp} only",
        outside == {},
    )
    if outside:
        entry.failures.append(f"layers pointing elsewhere: {outside}")
    entry.expect(
        "the board was the repository's in-process fake, so no Kanboard was reached",
        type(installation.board).__module__.startswith("tests."),
    )
    entry.notes.append(
        f"sprints this scenario wrote, all on the fake's {SPRINT_BOARD_NAME} board: "
        f"{[str(row['reference']) for row in installation.sprint_rows()]}"
    )


# -- the scenarios --------------------------------------------------------------------------------


def _the_owners_path(browser: Browser, installation: Installation, record: list[Step]) -> None:
    """Form, submission, page, repeat — and the two things the path refuses."""
    status, _headers, markup = browser.get("/sprints/new")
    one = _step(record, "The owner opens \u201cNew sprint\u201d", "GET /sprints/new", status)
    one.expect("the form is served", status == 200)
    one.expect(f"it offers this installation's own observer {OBSERVER_PROFILE}", OBSERVER_PROFILE in markup)
    one.expect("it offers the open issue issue:open", "issue:open" in markup)
    one.expect("it offers the registered project secretary", 'value="secretary"' in markup)
    observer_select = markup.split('<select id="observer"')[1].split("</select>")[0]
    one.expect("the observer select does not offer \u201cnone\u201d", 'value="none"' not in observer_select)
    one.expect(
        "worker and reviewer default to \u201cthe observer chooses\u201d",
        _shown(markup).count("the observer chooses") >= 2,
    )
    first_id = request_id_of(markup)
    one.expect(f"the page carries the request id {first_id} the submission will use", bool(first_id))

    fields = submission(first_id)
    status, headers, _body = browser.post("/sprints", fields)
    location = headers.get("Location", "")
    rows = installation.sprint_rows()
    two = _step(record, "The owner fills it in and presses \u201cStart this sprint\u201d", "POST /sprints", status)
    two.expect("the submission is accepted and redirects to the sprint", status == 303)
    two.expect(f"it lands on {location or '(no Location)'}", location.startswith("/sprints/"))
    two.expect("exactly one sprint row exists on the board", len(rows) == 1)
    reference = str(rows[0]["reference"]) if rows else ""
    two.expect(f"the row is {reference}", bool(reference))
    metadata = installation.metadata_of(reference) if reference else {}
    two.expect(
        "no executor was pinned, so the row carries neither sprint_worker nor sprint_reviewer",
        "sprint_worker" not in metadata and "sprint_reviewer" not in metadata,
    )

    status, _headers, markup = browser.get(location or f"/sprints/{quote(reference)}")
    shown = _shown(markup)
    three = _step(record, "The page it lands on", f"GET {location}", status)
    three.expect("the sprint page is served", status == 200)
    three.expect("it shows the goal that was typed", GOAL in shown)
    three.expect("it shows the definition of done that was typed", DEFINITION_OF_DONE in shown)
    three.expect(
        "the launch state reads \u201csaved \u2014 no observer is up for it yet\u201d",
        "saved \u2014 no observer is up for it yet" in shown,
    )
    three.expect(f"it names the observer {OBSERVER_PROFILE}", OBSERVER_PROFILE in shown)

    status, headers, _body = browser.post("/sprints", fields)
    four = _step(record, "The owner submits the same form again (a double click, a retry)", "POST /sprints", status)
    four.expect("it is accepted rather than refused", status == 303)
    four.expect("it lands on the sprint that already exists", headers.get("Location") == location)
    four.expect("the board still holds exactly one sprint", len(installation.sprint_rows()) == 1)

    status, _headers, markup = browser.get("/sprints/sprint%3A9999")
    five = _step(record, "A sprint reference the board does not hold", "GET /sprints/sprint:9999", status)
    five.expect("it is a 404 page rather than an empty one", status == 404)
    five.expect("the page says what was not found", "sprint:9999" in _shown(markup))

    status, _headers, _body = browser.request("DELETE", "/sprints")
    six = _step(record, "A verb no route publishes", "DELETE /sprints", status)
    six.expect(
        "it reaches no handler and writes nothing (the standard-library adapter answers 501 where "
        "the application's own table answers 405 \u2014 a deferred finding, not a defect of the path)",
        status in (405, 501),
    )
    six.expect("the board is unchanged", len(installation.sprint_rows()) == 1)

    status, _headers, markup = browser.get("/")
    seven = _step(record, "The dashboard the new routes were added beside", "GET /", status)
    seven.expect("it is still served by the same application", status == 200)
    seven.expect("and it offers the form", "/sprints/new" in markup)


def _pinning_both_executors(browser: Browser, installation: Installation, record: list[Step]) -> None:
    """The same path with the two selects moved off \u201cthe observer chooses\u201d."""
    status, _headers, markup = browser.get("/sprints/new")
    fields = submission(
        request_id_of(markup),
        goal=SECOND_GOAL,
        worker=WORKER_PROFILE,
        reviewer=REVIEWER_PROFILE,
    )
    status, headers, _body = browser.post("/sprints", fields)
    rows = installation.sprint_rows()
    reference = str(rows[0]["reference"]) if len(rows) == 1 else ""
    metadata = installation.metadata_of(reference) if reference else {}
    entry = _step(record, "A sprint opened with both executors chosen explicitly", "POST /sprints", status)
    entry.expect("the submission is accepted", status == 303)
    entry.expect(f"one sprint row exists: {reference or '(none)'}", bool(reference))
    entry.expect(f"its row pins sprint_worker={WORKER_PROFILE}", metadata.get("sprint_worker") == WORKER_PROFILE)
    entry.expect(f"its row pins sprint_reviewer={REVIEWER_PROFILE}", metadata.get("sprint_reviewer") == REVIEWER_PROFILE)
    status, _headers, markup = browser.get(headers.get("Location", ""))
    shown = _shown(markup)
    page = _step(record, "Its page", f"GET {headers.get('Location', '')}", status)
    page.expect("the sprint page is served", status == 200)
    page.expect("it shows the goal that was typed", SECOND_GOAL in shown)
    page.expect("it shows both pinned heads", WORKER_PROFILE in shown and REVIEWER_PROFILE in shown)


def _a_profile_that_is_gone(browser: Browser, installation: Installation, record: list[Step]) -> None:
    """A submission this installation cannot admit, and the form it hands back."""
    status, _headers, markup = browser.get("/sprints/new")
    typed = "A sprint naming a profile that left the registry"
    spent = request_id_of(markup)
    refused_fields = submission(spent, goal=typed, observer="retired-observer")
    status, _headers, markup = browser.post("/sprints", refused_fields)
    shown = _shown(markup)
    entry = _step(record, "A submission naming a profile this installation does not have", "POST /sprints", status)
    entry.expect("it is refused", status == 400)
    entry.expect("the refusal names the profile that is gone", "retired-observer" in shown)
    entry.expect("it comes back as a form, not as a blank error", "<form" in markup)
    entry.expect("everything typed is still on it", typed in markup)
    entry.expect("no sprint was opened", installation.sprint_rows() == [])
    reissued = request_id_of(markup)
    entry.expect("the spent request id is replaced", reissued != spent)

    corrected = [
        (name, {"request_id": reissued, "observer": OBSERVER_PROFILE}.get(name, value))
        for name, value in refused_fields
    ]
    status, headers, _body = browser.post("/sprints", corrected)
    again = _step(record, "The corrected form, submitted as it came back", "POST /sprints", status)
    again.expect("it is accepted", status == 303)
    again.expect("it opens exactly one sprint", len(installation.sprint_rows()) == 1)
    again.expect(f"and lands on {headers.get('Location', '(no Location)')}", headers.get("Location", "").startswith("/sprints/"))


def _an_interrupted_create(browser: Browser, installation: Installation, record: list[Step]) -> None:
    """A failure after the row exists, and the repeat that is the repair."""
    status, _headers, markup = browser.get("/sprints/new")
    held = request_id_of(markup)
    fields = submission(held, goal="A sprint whose create was interrupted")
    with mock.patch.object(
        SprintRequestStore, "record_reference", side_effect=RunStoreError("the disk went away")
    ):
        status, _headers, markup = browser.post("/sprints", fields)
    shown = _shown(markup)
    entry = _step(
        record,
        "A submission that fails after the sprint row was written",
        "POST /sprints (the request index is made to fail)",
        status,
    )
    entry.expect("the answer is the backend being unavailable, not a refusal", status == 503)
    entry.expect(
        "the page states the durable fact first: the sprint exists and the request did not finish",
        "this sprint exists and the request that opened it did not finish" in shown,
    )
    entry.expect("it says submitting the same form again is safe", "Submitting this form again is safe" in shown)
    entry.expect("the request id is kept, because only that id reaches the sprint", request_id_of(markup) == held)
    entry.expect("one sprint row was written", len(installation.sprint_rows()) == 1)

    status, headers, _body = browser.post("/sprints", fields)
    repeat = _step(record, "The same submission again, as the page says to", "POST /sprints", status)
    repeat.expect("it completes rather than refusing", status == 303)
    repeat.expect("it opens no second sprint", len(installation.sprint_rows()) == 1)
    repeat.expect(f"and lands on {headers.get('Location', '(no Location)')}", headers.get("Location", "").startswith("/sprints/"))


def transcript(record: list[Step]) -> str:
    lines = [
        "The sprint form, walked as a path over a real socket.",
        f"walked at {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} "
        f"by tests/sprint_user_path.py, on an isolated installation: temporary instance,",
        "temporary data plane, the repository's in-process board fake. No live installation, no "
        "Kanboard, no restart.",
        "",
    ]
    lines += [step.render() for step in record]
    failures = sum(len(step.failures) for step in record)
    checks = sum(len(step.notes) for step in record)
    lines.append("")
    lines.append(f"{len(record)} steps, {checks} checks held, {failures} did not")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", help="write the transcript here as well as to stdout")
    args = parser.parse_args(argv)
    record: list[Step] = []
    try:
        walk(record)
    finally:
        text = transcript(record)
        print(text)
        if args.out:
            with open(args.out, "w", encoding="utf-8") as handle:
                handle.write(text + "\n")
    return 1 if any(step.failures for step in record) else 0


if __name__ == "__main__":
    raise SystemExit(main())
