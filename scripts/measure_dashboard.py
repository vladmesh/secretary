#!/usr/bin/env python3
"""Measure the dashboard numbers this sprint is judged on, against a running installation.

One command, from a checkout, printing the two measurements the Definition of Done names and the
threshold each of them is judged against:

  * warm sequential `GET /`, `GET /sprints` and `GET /projects` — 20 requests each after a
    discarded warm-up, reported as p95 (the judged number) with min, median and max beside it;
  * four concurrent `GET /` while a `/po/api/sessions/{session}` poll runs on the three-second
    cadence the `/po` page's own `setInterval` uses, three rounds of it, judged on the worst.

It exists so that later cards are assessed on its output rather than on a hand measurement: the
numbers before an optimisation and the numbers after it come out of the same procedure, with the
same warm-up, the same request count and the same percentile rule.

**The one rule this file is built around.** No number printed here may be judged MEETS unless it
was produced by exactly the scenario the Definition of Done names. Every other outcome is
`Unmeasurable` and exits 2. There is no third outcome in which this script reports a green result
for a scenario it did not run. Concretely:

  * exit 0 — every request answered 2xx, the complete specified scenario was reproduced, and every
    judged number is at or under its threshold;
  * exit 1 — the same, except that a judged number is over its threshold. A red number is still a
    real number;
  * exit 2 — everything else.

That rule lives in exactly four places, and nowhere else:

  1. :func:`fetch` refuses a response that is not 2xx, and reaches only the installation the
     caller named. It holds the only transport call in this file — :data:`_OPENER`, built with no
     redirect handler and with an empty `ProxyHandler` — so every request in the script passes
     through it, and no caller re-implements the check or is able to forget it. A caller that
     genuinely wants to tolerate a status says so with `tolerate=` and a written reason; a 3xx is
     refused even then, because a followed redirect would measure a different installation and
     would carry this installation's PO cookie to it, and a proxy would do the same without even a
     response to show for it.
  2. :func:`prepare` proves the whole scenario is reproducible — data directory, PO token, and a
     session whose own JSON says a turn is **running**, which is the only kind of session a `/po`
     page polls at all — before any clock starts.
  3. :func:`measure_concurrent` proves the round was *launched by a due poll*: the poll for the
     round falls due on its own cadence, is issued, and only then are the four requests released.
     That ordering is program order inside the poll thread, so it holds at any installation speed.
     How many polls were genuinely **in flight** during the round is measured from the recorded
     windows and printed per round — it is a fact about the run, not a condition on it, and on a
     fast installation it is legitimately zero.
  4. :func:`require_cadence` proves the *cadence*: the spacing actually observed between polls is
     what is judged and what is printed, never the constant. Nothing in this file can shorten an
     interval — :meth:`SessionPoll._sleep_until` waits out an absolute deadline and only a full
     stop can end that wait — and if a shorter spacing is observed anyway, the run is refused
     rather than reported. A previous version woke the poll thread when a round was armed, so the
     polls fired milliseconds apart while the output still said "every 3 s".

**This script only reads, and only from the installation it was given.** Every request it makes
is a GET or a HEAD, and the whole list is :data:`READ_REQUESTS` below. There is no POST, nothing
that starts or stops a head, and nothing is written to the board, the state directory or the
instance repository. Every request goes to `--base-url` and nowhere else: the opener follows no
redirect and reads no proxy variable, so neither a response nor the caller's environment can send
this script — or the PO cookie it carries — to another host or port, and the list below stays the
complete inventory of what a run asks for.

Standard library only. What it imports from the product is the product's own resolution of an
installation — where a data directory comes from, and how the PO cookie is derived — because a
second copy of those rules here would be a second thing to keep in step.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Self

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Where `secretary-web.service` listens. The unit binds this and only this
#: (`packaging/systemd/secretary-web.service`); `--base-url` is for a second installation or a
#: stand, not for reaching a published front, which adds TLS and a password to every number.
DEFAULT_BASE_URL = "http://127.0.0.1:8787"

#: The pages the warm sequential measurement covers, in the order it prints them.
WARM_ROUTES = ("/", "/sprints", "/projects")
#: The page the concurrency measurement asks four times at once.
CONCURRENT_ROUTE = "/"
#: Where the poll target is discovered, and the route that is polled once it is known.
PO_OVERVIEW = "/po"
PO_SESSION_JSON = "/po/api/sessions/{session}"

#: Every request this script is able to make, as a reviewer reads it: method and route, nothing
#: else. It is the complete surface, verbs included — the reachability probe is a HEAD and is on
#: this list for that reason. `tests.test_measurement_script` checks a whole run's request log
#: against these pairs in both directions, so a request that is not here fails there.
READ_REQUESTS = (
    ("HEAD", "/"),
    ("GET", "/"),
    ("GET", "/sprints"),
    ("GET", "/projects"),
    ("GET", PO_OVERVIEW),
    ("GET", PO_SESSION_JSON),
)
#: The only verbs :func:`fetch` will send. A POST would start a head on a live installation.
READ_METHODS = frozenset({"GET", "HEAD"})

#: How the warm measurement is taken. One warm-up request is discarded because the first request
#: after a deploy pays for import, connection and cache warming that no later request pays again,
#: and the DoD judges a warm page.
WARMUP_REQUESTS = 1
WARM_REQUESTS = 20

#: The concurrency measurement: four browsers' worth of the dashboard at once, while the PO page a
#: real operator leaves open keeps polling its session.
CONCURRENT_REQUESTS = 4
#: The cadence being modelled: `src/secretary/web/pages.py` polls the selected session from the
#: open `/po` page with `setInterval(..., 3000)`, anchored on the start of the previous poll rather
#: than on when its answer came back. This script reproduces that schedule and never shortens it.
POLL_INTERVAL_SECONDS = 3.0
#: How much shorter than the cadence an observed interval may be before the run is refused. The
#: schedule makes a short interval impossible by construction (:meth:`SessionPoll._sleep_until`
#: waits out an absolute deadline), so this is a guard on the fact rather than a working tolerance:
#: if the spacing is ever observed short, something shortened the cadence and the scenario the
#: thresholds judge did not happen.
CADENCE_TOLERANCE_SECONDS = 0.05
#: How many times that scenario is repeated. One round of it is not a measurement: the first
#: baseline taken with this script came out 17 s, 27–29 s and 38–41 s on three runs of the same
#: unchanged installation, a factor of 2.4, and a later card cannot close or refuse a 2.0 s item on
#: one sample from an instrument with that spread. Three rounds, every one of them printed, and the
#: threshold judged on the worst: the DoD says *each* request answers within 2.0 s, so a scenario
#: that breaches it in one round of three has not met it.
CONCURRENT_ROUNDS = 3

#: How long a round waits at the barrier for the poll thread to join it. It has to exceed the
#: cadence, because waiting out the rest of the interval is exactly what a round does: the four
#: requests are released by the poll that falls due next. It stays well under the request timeout,
#: because a poll that is not coming at all means the scenario is not running, and that is an
#: answer this script should reach quickly rather than after two minutes.
ROUND_GATE_TIMEOUT_SECONDS = 30.0

#: The Definition of Done, in milliseconds. Warm p95 is judged per route; each of the four
#: concurrent requests is judged on its own, because a browser that waits is a browser that waits.
WARM_P95_THRESHOLD_MS = 1000.0
CONCURRENT_THRESHOLD_MS = 2000.0

#: A request that has not answered by then is a failed measurement, not a slow one.
REQUEST_TIMEOUT_SECONDS = 120.0

#: Exit statuses, as the card names them.
EXIT_MET = 0
EXIT_EXCEEDED = 1
EXIT_UNMEASURABLE = 2

#: Session ids as the `/po` overview links them, so a page that lists several is read in its order.
_SESSION_LINK = re.compile(r'href="/po/sessions/([A-Za-z0-9_-]{1,128})"')


class Unmeasurable(Exception):
    """The installation could not be measured, which is not the same as measuring badly.

    It may carry the part of the report that had already been taken when it was raised, so a run
    that fails at the concurrent phase can still show what the warm phase found. Those numbers are
    printed without a verdict: they are real, and the run they belong to is not the specified one.
    """

    def __init__(self, message: str, report: Report | None = None) -> None:
        super().__init__(message)
        self.report = report


@dataclass
class Sample:
    """One request that was made and answered 2xx, and when it ran."""

    route: str
    status: int
    started_at: float
    ended_at: float
    body: bytes = b""

    @property
    def duration_ms(self) -> float:
        return (self.ended_at - self.started_at) * 1000.0


@dataclass
class Round:
    """One round of the concurrent scenario: what it cost, and what the poll beside it did.

    The two poll numbers here answer different kinds of question, and only one of them is a
    condition. `launched_at` is the scenario: a poll fell due on the cadence, was issued, and
    released this round. `polls_in_flight` is a measurement of what that produced — how much of the
    round genuinely had a session read running alongside it — which depends on how fast the
    installation answers and is reported rather than required.
    """

    samples: list[Sample]
    #: When the poll that released this round was issued, or None if none did. A round no poll
    #: launched is refused: it is four requests beside nothing, not the scenario.
    launched_at: float | None
    #: Selected-session polls genuinely in flight during this round's timed window. On a fast
    #: installation the launching poll has already finished before the round starts and the next
    #: one is seconds away, so 0 here is an ordinary reading and not a failure.
    polls_in_flight: int

    @property
    def started_at(self) -> float:
        return min(sample.started_at for sample in self.samples)

    @property
    def ended_at(self) -> float:
        return max(sample.ended_at for sample in self.samples)


@dataclass
class Launch:
    """The poll that releases one round, as the poll thread records it before releasing.

    Written by the poll thread and read by the round's own thread once the round is over, with the
    event as the handover: a round that finds `issued` set knows its poll was issued first, because
    that is the order the poll thread did the two things in.
    """

    issued: threading.Event = field(default_factory=threading.Event)
    started_at: float | None = None


@dataclass
class Measurement:
    """One judged number, its threshold, and the samples it was taken from."""

    label: str
    value_ms: float
    threshold_ms: float
    detail: str = ""

    @property
    def met(self) -> bool:
        return self.value_ms <= self.threshold_ms


@dataclass
class Scenario:
    """A reproducible run of the specified scenario, proved before a single clock starts."""

    base_url: str
    data_dir: Path
    data_dir_source: str
    cookie: str
    poll_target: str
    poll_explanation: str


@dataclass
class Report:
    """Everything the run measured, in the order it is printed."""

    base_url: str
    poll_target: str = ""
    poll_explanation: str = ""
    data_dir: str = ""
    warm: list[dict[str, Any]] = field(default_factory=list)
    #: One entry per round of the concurrent scenario.
    concurrent: list[Round] = field(default_factory=list)
    #: Which of those rounds the threshold was judged on: the one with the slowest request in it.
    worst_round: int = 0
    measurements: list[Measurement] = field(default_factory=list)
    #: Polls of the selected session that answered 2xx over the whole concurrent phase.
    poll_requests: int = 0
    #: The spacing observed between consecutive polls, in seconds. What the cadence is reported
    #: from: the constant states the schedule, these state the run.
    poll_intervals: list[float] = field(default_factory=list)


# -- the one place a request is made ---------------------------------------------------------


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """A redirect handler that never redirects.

    Returning None from `redirect_request` is urllib's own way of declining one: the response goes
    back down the error chain and arrives at `fetch` as an `HTTPError` carrying the 3xx status,
    which `fetch` then refuses like any other non-answer.
    """

    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        return None


#: The only transport in this file, and the whole of "only the installation the caller named".
#: `build_opener` keeps the default handlers except the ones a passed handler replaces, and both
#: passed here are replacements: the refusing subclass leaves an opener that cannot follow a
#: redirect, and `ProxyHandler({})` leaves one that reads no proxy environment at all. Without the
#: second, `http_proxy` in the caller's shell is enough to send every request — and the PO cookie
#: one of them carries — to a host the base URL never named, while the output still reports the
#: base URL. Built once, at module level, so a second opener would be as visible to a reader as a
#: second `urlopen` was.
_OPENER = urllib.request.build_opener(_RefuseRedirects(), urllib.request.ProxyHandler({}))


def fetch(
    base_url: str,
    route: str,
    *,
    method: str = "GET",
    cookie: str = "",
    tolerate: frozenset[int] = frozenset(),
) -> Sample:
    """One request, timed end to end from the client's side — and validated here, only here.

    This function holds the only transport call in the file, which is what makes it the single
    place response validity can live. A response that is not 2xx raises :class:`Unmeasurable`
    naming the verb, the route and the status: the script measures answers, and a 404, a 401 or a
    contained 500 is not one. The rule used to be re-implemented at five of the six call sites in
    four different spellings, and was simply missing at the sixth — three rounds of review found
    three different sites of the same omission, which is why it is here now and nowhere else.

    A 3xx is refused before anything else, and refused unconditionally. Nothing follows it: the
    opener has no redirect handler, so the request that was made is the request that was listed,
    and the only host and port this script ever speaks to is `base_url`. That is a trust boundary
    rather than a measurement nicety — a followed 302 on `/po` would be recorded as a 200 for the
    route that actually answered 302, and would hand this installation's PO cookie to whatever the
    redirect named.

    `tolerate` is the deliberate escape hatch for a status a caller genuinely needs, named at the
    call site with a written reason. The default is refusal, so a new call site cannot forget the
    rule — it can only decide, in writing, to exempt itself. Nothing in this script passes it
    today, and no value of it can exempt a redirect.

    The body is read before the clock stops: a response whose headers arrive quickly and whose
    body trickles is slow to the browser that is waiting for it, and the DoD is about that wait.
    """
    if method not in READ_METHODS:
        raise ValueError(f"{method} is not a read; this script makes no writing request")
    request = urllib.request.Request(base_url.rstrip("/") + route, method=method)
    if cookie:
        request.add_header("Cookie", cookie)
    started = time.perf_counter()
    try:
        with _OPENER.open(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            payload = response.read()
            status = int(response.status)
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        status = int(exc.code)
    except (urllib.error.URLError, OSError) as exc:
        raise Unmeasurable(f"{method} {route} on {base_url} could not be made: {exc}") from None
    ended = time.perf_counter()
    if 300 <= status < 400:
        raise Unmeasurable(
            f"{method} {route} on {base_url} answered {status}, a redirect. This script measures "
            f"the installation it was given and nothing else, so the redirect was not followed: "
            f"following it would time a different installation under this route's name and would "
            f"send this installation's PO cookie to wherever it points"
        )
    if not (200 <= status < 300) and status not in tolerate:
        raise Unmeasurable(
            f"{method} {route} on {base_url} answered {status}; this script measures answers, and "
            f"a {status} is not one, so nothing it could time here would be the specified scenario"
        )
    return Sample(route=route, status=status, started_at=started, ended_at=ended, body=payload)


# -- the one place the scenario is proved reproducible ----------------------------------------


def _product_path() -> None:
    """Put this checkout's `src` on the path, so the product's own rules can be imported."""
    root = str(REPO_ROOT / "src")
    if root not in sys.path:
        sys.path.insert(0, root)


def resolve_data_dir(argument: str | None) -> tuple[Path, str]:
    """Where this installation's data plane is, and which rule said so.

    The product's own order, and the product's own last step: the argument, then
    `SECRETARY_DATA_DIR`, then `SECRETARY_INSTANCE`, then the default instance the CLI documents
    (`secretary.onboarding.DEFAULT_INSTANCE`) — read through `secretary.config.instance_data_dir`
    rather than a second copy of the mapping. The default instance matters because the documented
    command is run from an ordinary checkout shell, which does not inherit the service unit's
    environment: without this step that shell resolved no data directory at all, could not read the
    PO token, and quietly measured a scenario the DoD does not name.

    A failure here raises rather than returning None. "An unresolvable data dir only costs the
    poll" was the old comment, and it was wrong: the poll is not a garnish, it is half the
    specified scenario.
    """
    if argument:
        return Path(argument).expanduser(), "--data-dir"
    configured = os.environ.get("SECRETARY_DATA_DIR")
    if configured:
        return Path(configured).expanduser(), "SECRETARY_DATA_DIR"
    _product_path()
    try:
        from secretary.config import DataDirError, instance_data_dir
        from secretary.onboarding import DEFAULT_INSTANCE
    except ImportError as exc:
        raise Unmeasurable(
            f"the product could not be imported from {REPO_ROOT / 'src'} ({exc}), so this "
            f"installation's data directory could not be resolved; pass --data-dir"
        ) from None
    selected = os.environ.get("SECRETARY_INSTANCE")
    source = "SECRETARY_INSTANCE" if selected else "the default instance"
    instance = Path(selected or DEFAULT_INSTANCE).expanduser()
    try:
        return instance_data_dir(instance), f"{source} {instance}"
    except DataDirError as exc:
        raise Unmeasurable(
            f"the data directory of {instance} could not be resolved ({exc}); set "
            f"SECRETARY_INSTANCE or SECRETARY_DATA_DIR, or pass --data-dir"
        ) from None


def po_cookie(data_dir: Path) -> str:
    """This installation's PO cookie, or a refusal naming what would fix it.

    The derivation is imported from the product rather than copied: the cookie is an HMAC keyed by
    the token file, and a second implementation of that rule here would be a second thing to keep
    in step with `secretary.po.token`.
    """
    _product_path()
    try:
        from secretary.po.token import COOKIE_NAME, TokenError, cookie_value, read_token
    except ImportError as exc:
        raise Unmeasurable(f"the PO cookie rule could not be imported from this checkout: {exc}") from None
    try:
        token = read_token(data_dir)
    except TokenError as exc:
        raise Unmeasurable(
            f"{exc}. The concurrent scenario is measured while a PO session is polled, so this "
            f"token is part of the measurement, not an extra"
        ) from None
    except OSError as exc:
        raise Unmeasurable(
            f"the PO token under {data_dir} is not readable by this user ({exc}); run this command "
            f"as the runtime user that owns the installation"
        ) from None
    return f"{COOKIE_NAME}={cookie_value(token)}"


def session_is_running(route: str, body: bytes) -> bool:
    """Whether the session's own JSON says a turn is running — read from the body, not the page.

    The product decides this and publishes it: `webproto.po_ops.po_session` sets `running` from
    whether any turn of the session is in the running state, and the session page installs its
    three-second `setInterval` only `if (__RUNNING__)`, clearing it when the turn ends
    (`src/secretary/web/pages.py`). So an idle session is one no browser polls, and a document
    this script cannot read `running` out of is one it cannot say either way about — which is a
    refusal here rather than a guess, because the guess would decide whether a scenario was
    reproduced.
    """
    try:
        document = json.loads(body.decode("utf-8", "replace"))
    except ValueError as exc:
        raise Unmeasurable(
            f"GET {route} did not answer with JSON ({exc}), so whether a PO page would be polling "
            f"this session could not be established"
        ) from None
    running = document.get("running") if isinstance(document, dict) else None
    if not isinstance(running, bool):
        raise Unmeasurable(
            f"GET {route} answered a document with no boolean `running` field, so whether a PO "
            f"page would be polling this session could not be established"
        )
    return running


def choose_poll_target(base_url: str, cookie: str) -> tuple[str, str]:
    """The session to poll and how it was chosen, or an empty target and why there is none.

    The first session the `/po` overview lists **whose own JSON reports a turn running**. That
    qualifier is the whole point: the page's poll exists only while a turn runs, so polling an idle
    session would be a request no `/po` page makes, and the concurrency measurement would be four
    requests beside a load the operator's browser is not producing. The overview's markup is not
    trusted for it — each candidate is read at the route that would actually be polled, and the
    answer comes out of that response body.

    A `/po` that does not answer never reaches here, and neither does a session whose JSON does
    not: `fetch` refuses both. So the only ways out with an empty target are the product's own
    states of having no open session at all and of having no turn running in any of them. Neither
    is an error of this installation, and the caller says which one in the output; neither is the
    specified scenario either, so the run cannot come out green.
    """
    overview = fetch(base_url, PO_OVERVIEW, cookie=cookie)
    sessions = _SESSION_LINK.findall(overview.body.decode("utf-8", "replace"))
    if not sessions:
        return "", f"{PO_OVERVIEW} answered, and lists no open session to poll"
    for session in sessions:
        route = PO_SESSION_JSON.format(session=session)
        # The probe is made at the polled route with the polled cookie, so a session that passes
        # it has been proved pollable by the same request the measurement will repeat.
        if session_is_running(route, fetch(base_url, route, cookie=cookie).body):
            return route, (
                f"the first session {PO_OVERVIEW} lists whose own JSON reports a running turn "
                f"({session}), read with this installation's PO cookie"
            )
    listed = f"{len(sessions)} open session(s)" if len(sessions) > 1 else "one open session"
    return "", (
        f"{PO_OVERVIEW} lists {listed} and none of them has a turn running, so no PO page on this "
        f"installation is polling anything"
    )


def prepare(base_url: str, data_dir_argument: str | None) -> Scenario:
    """Prove the specified scenario can be reproduced here, before a single clock starts.

    Every step raises :class:`Unmeasurable` on failure, so a run that gets past this point has an
    installation that answers, a data plane, a readable token, a session with a turn running, and
    one poll of that session that actually returned — the probe in :func:`choose_poll_target` is
    that poll, made at the route the measurement will repeat. Nothing downstream re-checks any of
    it.
    """
    data_dir, data_dir_source = resolve_data_dir(data_dir_argument)
    try:
        fetch(base_url, CONCURRENT_ROUTE, method="HEAD")
    except Unmeasurable as exc:
        raise Unmeasurable(f"{base_url} is not reachable: {exc}") from None
    cookie = po_cookie(data_dir)
    poll_target, poll_explanation = choose_poll_target(base_url, cookie)
    return Scenario(
        base_url=base_url,
        data_dir=data_dir,
        data_dir_source=data_dir_source,
        cookie=cookie,
        poll_target=poll_target,
        poll_explanation=poll_explanation,
    )


# -- the measurements ------------------------------------------------------------------------


def p95(values: list[float]) -> float:
    """Nearest-rank p95: the smallest sample at least 95% of the samples are at or below.

    Nearest rank rather than an interpolated quantile because the number has to mean one request
    that actually happened — over twenty samples it is the second slowest, and two runs of this
    script compare the same way every time.
    """
    ordered = sorted(values)
    rank = max(1, -(-len(ordered) * 95 // 100))
    return ordered[rank - 1]


def measure_warm(base_url: str, route: str) -> dict[str, Any]:
    """Warm sequential requests to one route: a discarded warm-up, then the judged twenty.

    No status check here. `fetch` refuses anything that is not an answer, so a route that is
    missing, refused or failing ends the run from inside it, with the verb, route and status named.
    """
    first = fetch(base_url, route)
    samples = [fetch(base_url, route) for _ in range(WARM_REQUESTS)]
    durations = [sample.duration_ms for sample in samples]
    return {
        "route": route,
        "count": len(durations),
        "warmup_ms": first.duration_ms,
        "min_ms": min(durations),
        "median_ms": statistics.median(durations),
        "p95_ms": p95(durations),
        "max_ms": max(durations),
    }


class SessionPoll:
    """The `/po` poll an operator's open session page makes, for the concurrency measurement.

    A read of the running session on the cadence the page itself uses. It is not measured itself:
    what is measured is what the dashboard costs beside it.

    Two properties carry the concurrency measurement, and both are things this class *does* rather
    than things it hopes for.

    *The cadence is real.* A poll is scheduled :data:`POLL_INTERVAL_SECONDS` after the previous
    poll **started** — the anchor `setInterval` uses, since a browser's timer does not wait for the
    answer — and :meth:`_sleep_until` waits that deadline out against the same clock the samples
    are timed with. There is no wake and no caller that can ask for a poll sooner; only stopping
    the poll altogether ends the wait, and then no further poll is made at all. So an observed
    interval cannot come out short, and :func:`require_cadence` checks that it did not anyway.

    *Every round is launched by a due poll.* A round hands its barrier to :meth:`arm`, and its four
    requests wait there. The poll thread does not touch that barrier until its next poll falls
    **due** and has been issued and recorded; only then does it release the round. That is program
    order inside one thread, so it is true at any installation speed, and it is what the round's
    `launched_at` records.

    What it deliberately does not promise is temporal overlap. An earlier version released the poll
    and the round from the barrier together to force a poll to be in flight during every round;
    with a two-millisecond poll and a three-millisecond round, whether the two requests are
    genuinely simultaneous is a coin toss, and one run in five failed on it. Overlap is therefore
    measured — :meth:`overlapping` — and reported per round, and it is zero on a fast installation
    because the poll has answered before the round starts, which is an accurate reading rather than
    a fault.

    The version before that one shortened the cadence instead: `arm` set an event the poll thread
    was sleeping on, so the rounds ran back to back, the polls fired two milliseconds apart, and
    the output still said "every 3 s".
    """

    def __init__(self, base_url: str, route: str, cookie: str) -> None:
        self.base_url = base_url
        self.route = route
        self.cookie = cookie
        #: Start and end of every poll that answered, as `time.perf_counter` readings.
        self.windows: list[tuple[float, float]] = []
        self.failure: Unmeasurable | None = None
        self._lock = threading.Lock()
        self._gate: threading.Barrier | None = None
        #: What the poll thread writes the launching poll into before it releases the round.
        self._launch: Launch | None = None
        #: The start of a poll that is under way and whose window is therefore not written down
        #: yet. A poll still in flight when a round ends would otherwise be missing from that
        #: round's count, because a window is only recorded once the answer is in. It is used for
        #: the in-flight question alone; no recorded window is ever back-dated to it.
        self._pending: float | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="po-poll", daemon=True)

    @property
    def successes(self) -> int:
        with self._lock:
            return len(self.windows)

    def intervals(self) -> list[float]:
        """The spacing actually observed between consecutive polls, in seconds.

        Start to start, because that is what the cadence schedules and what a `setInterval` does.
        This is the only thing the output is allowed to describe the cadence from: the constant
        says what was asked for, and these say what happened.
        """
        with self._lock:
            starts = [start for start, _end in self.windows]
        return [later - earlier for earlier, later in zip(starts, starts[1:], strict=False)]

    def _take_gate(self) -> tuple[threading.Barrier | None, Launch | None]:
        with self._lock:
            gate, self._gate = self._gate, None
            launch, self._launch = self._launch, None
            return gate, launch

    def arm(self, gate: threading.Barrier) -> Launch:
        """Hand the poll thread a round's barrier. Nothing here hurries the poll.

        The next poll to fall due is issued and recorded, and that poll's thread then releases this
        barrier. If the poll thread is mid-interval, the round waits out the rest of that interval;
        if it has already taken this loop's gate, the round waits for the poll after it. Either way
        the round is released by a poll that was going to happen then anyway, at the cadence, which
        is what makes the ordering true without anything being shortened.

        Returns the record the poll thread fills in before it releases the round.
        """
        launch = Launch()
        with self._lock:
            self._gate = gate
            self._launch = launch
        return launch

    def _sleep_until(self, due: float) -> bool:
        """Wait until `due` on the sample clock. False means the poll was stopped instead.

        Looped against `time.perf_counter` rather than trusting one timed wait, so this cannot
        return early and hand back an interval shorter than the cadence.
        """
        while True:
            remaining = due - time.perf_counter()
            if remaining <= 0:
                return True
            if self._stop.wait(remaining):
                return False

    def _run(self) -> None:
        # The first poll is due at once: the run has already proved in `prepare` that this session
        # is running and answers, and the cadence is anchored from here on the start of each poll.
        due = time.perf_counter()
        while not self._stop.is_set():
            if not self._sleep_until(due):
                return
            gate, launch = self._take_gate()
            with self._lock:
                self._pending = time.perf_counter()
            try:
                sample = fetch(self.base_url, self.route, cookie=self.cookie)
            except Unmeasurable as exc:
                # Recorded, not swallowed: `check` reports it on the caller's thread. Polling stops
                # because every further attempt would fail the same way and the run is already void.
                self.failure = exc
                with self._lock:
                    self._pending = None
                if gate is not None:
                    # A round is waiting at that barrier for a poll that is not coming.
                    gate.abort()
                self._stop.set()
                return
            with self._lock:
                self._pending = None
                self.windows.append((sample.started_at, sample.ended_at))
            if gate is not None:
                # The ordering the scenario is defined by, as plain program order: this poll fell
                # due, was issued and is recorded, and only now are the round's four requests let
                # go. Nothing about it depends on how the threads are scheduled afterwards.
                launch = launch if launch is not None else Launch()
                launch.started_at = sample.started_at
                launch.issued.set()
                try:
                    gate.wait(timeout=ROUND_GATE_TIMEOUT_SECONDS)
                except threading.BrokenBarrierError:
                    # The round gave up waiting or failed on its own. It reports that; the poll
                    # beside it was made on the cadence either way.
                    pass
            due = sample.started_at + POLL_INTERVAL_SECONDS

    def overlapping(self, started_at: float, ended_at: float) -> int:
        """How many polls were genuinely in flight during `[started_at, ended_at]`.

        In flight means started before that window ended and not finished before it began — the
        ordinary interval overlap, computed from recorded windows. A poll still under way when
        this is asked has no recorded end yet, so it counts when it started before the window
        closed: it cannot have finished before the window opened, since it has not finished.
        """
        with self._lock:
            windows = list(self.windows)
            pending = self._pending
        counted = sum(1 for start, end in windows if start < ended_at and end > started_at)
        if pending is not None and pending < ended_at:
            counted += 1
        return counted

    def check(self) -> None:
        """Raise whatever the poll thread hit, on the caller's thread."""
        if self.failure is not None:
            raise self.failure

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=REQUEST_TIMEOUT_SECONDS)


def measure_concurrent(base_url: str, poll: SessionPoll) -> Round:
    """One round: four `GET /` at once, released by the poll that fell due just before them.

    The barrier has five parties — the four request threads and the poll thread — and which of them
    waits is the whole point: the requests wait for the poll's own schedule, never the other way
    round. The poll thread arrives only after its due poll has been issued and recorded, so the
    round's `launched_at` is a fact of program order rather than of thread scheduling. The round
    costs up to one interval before its first clock starts, and that wait is outside every number.

    What that ordering does *not* buy is a poll in flight during the round; on a fast installation
    the poll has answered before the requests start. So the in-flight count is measured here from
    the recorded windows and reported, and nothing is arranged to make it come out above zero.
    """
    samples: list[Sample | None] = [None] * CONCURRENT_REQUESTS
    failures: list[BaseException] = []
    gate = threading.Barrier(CONCURRENT_REQUESTS + 1)

    def one(index: int) -> None:
        try:
            # The barrier is what makes this concurrency rather than four quick requests: every
            # thread is waiting here before any of them asks, and they are released together by
            # the poll thread arriving when its next poll falls due.
            gate.wait(timeout=ROUND_GATE_TIMEOUT_SECONDS)
            samples[index] = fetch(base_url, CONCURRENT_ROUTE)
        except BaseException as exc:  # noqa: BLE001 - reported below, on the caller's thread
            failures.append(exc)
            # A round that cannot reach the barrier must not leave the poll thread waiting there
            # for the full gate timeout: the cadence would stall behind a round that has already
            # failed. Aborting releases it to make the poll that is due.
            gate.abort()

    poll.check()
    # Armed before the threads start, so the round is already waiting when the poll comes due.
    launch = poll.arm(gate)
    threads = [threading.Thread(target=one, args=(index,)) for index in range(CONCURRENT_REQUESTS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=REQUEST_TIMEOUT_SECONDS * 2)
    # A poll that died is the likelier cause of a broken barrier than the requests were, and its
    # message names the route and the status, so it is reported first.
    poll.check()
    if failures:
        raise Unmeasurable(f"a concurrent GET {CONCURRENT_ROUTE} could not be made: {failures[0]}")
    measured = [sample for sample in samples if sample is not None]
    if len(measured) != CONCURRENT_REQUESTS:
        raise Unmeasurable(f"only {len(measured)} of {CONCURRENT_REQUESTS} concurrent requests were answered")
    started = min(sample.started_at for sample in measured)
    ended = max(sample.ended_at for sample in measured)
    return Round(
        samples=measured,
        launched_at=launch.started_at if launch.issued.is_set() else None,
        polls_in_flight=poll.overlapping(started, ended),
    )


def cadence_holds(intervals: list[float]) -> bool:
    """Did the polls actually run on the cadence this scenario models?

    One predicate, two callers: :func:`require_cadence` refuses a run it is false for, and
    :func:`render` prints the cadence as observed only where it is true. They cannot disagree, so
    the output cannot claim a cadence the run did not have — which is exactly what happened when
    the claim was a sentence in the code and the schedule was something else.

    Enough intervals to be a cadence at all means one per round beyond the first: each round is
    released by its own due poll, so a run that reproduced the scenario has at least that many.
    """
    if len(intervals) < CONCURRENT_ROUNDS - 1:
        return False
    return min(intervals) >= POLL_INTERVAL_SECONDS - CADENCE_TOLERANCE_SECONDS


def require_cadence(intervals: list[float]) -> None:
    """Refuse a run whose polls were not the cadence the scenario models.

    The four concurrent requests are judged against 2.0 s *while a session is polled every three
    seconds*. Polls that fired faster than that are a heavier workload than the DoD names, and
    polls that are too few to have a spacing are not a cadence at all. Either way the numbers are
    real and the scenario is not the specified one, so they are printed and not judged.
    """
    if cadence_holds(intervals):
        return
    if len(intervals) < CONCURRENT_ROUNDS - 1:
        raise Unmeasurable(
            f"only {len(intervals) + 1} poll(s) were made during the concurrent rounds, which is "
            f"too few to show the {POLL_INTERVAL_SECONDS:.0f} s cadence this scenario is defined "
            f"with, so the rounds cannot be judged as that scenario"
        )
    raise Unmeasurable(
        f"the polls were observed {min(intervals):.3f} s apart at the closest, shorter than the "
        f"{POLL_INTERVAL_SECONDS:.0f} s cadence this scenario models; the rounds therefore ran "
        f"against a busier installation than the thresholds judge, and nothing here may be reported "
        f"as that scenario"
    )


def require_launch(rounds: list[Round]) -> None:
    """Refuse rounds that no due poll launched.

    The DoD scenario is four concurrent requests beside a session being polled on the page's
    cadence. A round that no poll was issued for measured a quieter thing, so it may neither be
    judged nor compete to be the worst round — which is why this refuses any of them rather than
    only the one that happens to be judged.

    What it does not refuse is a round with no poll *in flight* during it. That number is measured
    and printed, and on an installation that answers a session read in two milliseconds it is
    legitimately zero; requiring it made the instrument's own proof a coin toss, which is a worse
    thing to publish than an honest zero.
    """
    barren = [index + 1 for index, item in enumerate(rounds) if item.launched_at is None]
    if barren:
        raise Unmeasurable(
            f"round(s) {', '.join(str(number) for number in barren)} ran without a "
            f"selected-session poll being issued for them, so they are not the scenario the "
            f"thresholds judge"
        )


def run(base_url: str, data_dir_argument: str | None) -> Report:
    """Take both measurements against `base_url`, in the order they are printed."""
    scenario = prepare(base_url, data_dir_argument)
    report = Report(
        base_url=scenario.base_url,
        poll_target=scenario.poll_target,
        poll_explanation=scenario.poll_explanation,
        data_dir=f"{scenario.data_dir} (from {scenario.data_dir_source})",
    )
    try:
        _measure(scenario, report)
    except Unmeasurable as exc:
        # Whatever was taken before the failure is still real, and a reader is owed it. It is
        # printed without verdicts: see `render`.
        exc.report = report
        raise
    return report


def _measure(scenario: Scenario, report: Report) -> None:
    for route in WARM_ROUTES:
        warm = measure_warm(scenario.base_url, route)
        report.warm.append(warm)
        report.measurements.append(
            Measurement(
                label=f"warm p95 GET {route}",
                value_ms=warm["p95_ms"],
                threshold_ms=WARM_P95_THRESHOLD_MS,
                detail=(
                    f"min {warm['min_ms']:.0f} ms, median {warm['median_ms']:.0f} ms, "
                    f"max {warm['max_ms']:.0f} ms over {warm['count']} requests"
                ),
            )
        )

    if not scenario.poll_target:
        # An installation with no PO session, or none with a turn running, is not broken, and the
        # warm half above is a real measurement of it. The concurrent half of the DoD cannot be
        # reproduced here at all: substituting an idle session would put a request no page makes
        # beside the four, and report it under the same heading. So the run stops, says which of
        # the two states it found, and cannot come out green.
        raise Unmeasurable(
            f"{scenario.poll_explanation}, so the concurrent scenario — four requests while a "
            f"running session is polled every {POLL_INTERVAL_SECONDS:.0f} s, the way the open page "
            f"polls it — was not reproduced on this installation; run this again while a PO turn "
            f"is running"
        )

    with SessionPoll(scenario.base_url, scenario.poll_target, scenario.cookie) as poll:
        report.concurrent = [measure_concurrent(scenario.base_url, poll) for _ in range(CONCURRENT_ROUNDS)]
    # Read after the block, so the poll thread has been stopped and joined and the record of what
    # it did is final rather than a snapshot of a thread still running.
    report.poll_requests = poll.successes
    report.poll_intervals = poll.intervals()
    poll.check()
    require_cadence(report.poll_intervals)
    require_launch(report.concurrent)

    # The judged round is the one holding the slowest single request: the DoD asks that *each* of
    # the four answers within 2.0 s, so a scenario that breaches it once in three rounds has not
    # met it, and judging the best or the average would report that it had.
    report.worst_round = max(
        range(len(report.concurrent)),
        key=lambda index: max(sample.duration_ms for sample in report.concurrent[index].samples),
    )
    for index, sample in enumerate(report.concurrent[report.worst_round].samples, start=1):
        report.measurements.append(
            Measurement(
                label=(
                    f"concurrent GET {CONCURRENT_ROUTE} #{index} of {CONCURRENT_REQUESTS} "
                    f"(worst of {CONCURRENT_ROUNDS} rounds)"
                ),
                value_ms=sample.duration_ms,
                threshold_ms=CONCURRENT_THRESHOLD_MS,
            )
        )


# -- what it prints --------------------------------------------------------------------------


def cadence_lines(intervals: list[float]) -> list[str]:
    """What the run is allowed to say about the cadence, which is only what it observed.

    The spacing is printed whether or not it held, because it is a fact of the run either way, and
    the sentence naming the cadence is printed only where :func:`cadence_holds` is true — the same
    predicate the run is refused by. There is no path through this file that prints the cadence as
    a claim and gets it from the constant.
    """
    if not intervals:
        return [
            (
                "  observed spacing: not observed — fewer than two polls were made, so this run "
                "shows no cadence at all"
            )
        ]
    observed = (
        f"  observed spacing: {min(intervals):.3f} s min, {statistics.median(intervals):.3f} s "
        f"median, {max(intervals):.3f} s max over {len(intervals)} interval(s)"
    )
    if cadence_holds(intervals):
        return [
            observed,
            (
                f"  the poll ran every {POLL_INTERVAL_SECONDS:.0f} s: no interval between two "
                f"polls was shorter than that"
            ),
        ]
    return [
        observed,
        (
            f"  the {POLL_INTERVAL_SECONDS:.0f} s cadence did NOT hold on this run, so nothing "
            f"here is the scenario the thresholds judge"
        ),
    ]


def render(report: Report, *, unmeasurable: str = "") -> list[str]:
    """The table, in full — a red result prints exactly as much as a green one.

    With `unmeasurable` set, the numbers that were taken are still printed, and none of them
    carries a verdict: they are real measurements of a run that was not the specified scenario, and
    a MEETS beside one of them would be this script reporting a scenario it did not run.
    """
    lines = [f"base URL: {report.base_url}"]
    if report.data_dir:
        lines.append(f"data directory: {report.data_dir}")
    if report.poll_target:
        lines.append(f"/po poll: GET {report.poll_target}")
        lines.append(f"  chosen as {report.poll_explanation}")
        lines.append(
            f"  scheduled {POLL_INTERVAL_SECONDS:.0f} s apart, which is the cadence the /po page "
            f"itself polls on"
        )
        lines.append(f"  polled successfully {report.poll_requests} time(s) during the concurrent rounds")
        lines.extend(cadence_lines(report.poll_intervals))
    else:
        lines.append(f"/po poll: none — {report.poll_explanation or 'no session was selected'}")
    if unmeasurable:
        lines.append("")
        lines.append(f"NOT MEASURED: {unmeasurable}")
        lines.append("  the numbers below were taken, but this run is not the specified scenario,")
        lines.append("  so none of them is judged and the command exits 2")
    if not report.measurements:
        return lines
    lines.append("")
    lines.append(
        f"warm sequential: {WARM_REQUESTS} requests per route after {WARMUP_REQUESTS} discarded warm-up request"
    )
    width = max(len(measurement.label) for measurement in report.measurements)

    def rows(measurements: list[Measurement]) -> list[str]:
        written: list[str] = []
        for measurement in measurements:
            verdict = "NOT JUDGED" if unmeasurable else ("MEETS" if measurement.met else "EXCEEDS")
            line = (
                f"  {measurement.label:<{width}}  {measurement.value_ms:8.0f} ms  "
                f"threshold {measurement.threshold_ms:.0f} ms  {verdict}"
            )
            if measurement.detail:
                line = f"{line}\n  {'':<{width}}  ({measurement.detail})"
            written.append(line)
        return written

    warm_count = len(report.warm)
    lines.extend(rows(report.measurements[:warm_count]))
    if report.concurrent:
        lines.append("")
        lines.append(
            f"concurrent: {CONCURRENT_REQUESTS} requests at once, {CONCURRENT_ROUNDS} rounds, "
            f"judged on the worst round"
        )
        lines.append(
            "  each round is released by a poll issued on the cadence just before it, which is the condition;"
        )
        lines.append(
            "  the in-flight count beside it is measured during the round, and is 0 whenever the "
            "poll answered first"
        )
        for index, item in enumerate(report.concurrent, start=1):
            marker = " <- judged" if index - 1 == report.worst_round and not unmeasurable else ""
            durations = ", ".join(f"{sample.duration_ms:.0f}" for sample in item.samples)
            launched = (
                "launched by a due poll"
                if item.launched_at is not None
                else "NO poll was issued for this round"
            )
            lines.append(
                f"  round {index}: {durations} ms "
                f"[{launched}; {item.polls_in_flight} poll(s) in flight]{marker}"
            )
    lines.extend(rows(report.measurements[warm_count:]))
    lines.append("")
    if unmeasurable:
        lines.append("no measurement was judged: this run was not the specified scenario")
        return lines
    exceeded = [measurement for measurement in report.measurements if not measurement.met]
    if exceeded:
        lines.append(f"{len(exceeded)} of {len(report.measurements)} measurements exceed their threshold")
    else:
        lines.append(f"all {len(report.measurements)} measurements are at or under their threshold")
    return lines


def as_json(report: Report, *, unmeasurable: str = "") -> dict[str, Any]:
    """The same facts, for a later card that wants to diff two runs rather than read two tables."""
    return {
        "base_url": report.base_url,
        "data_dir": report.data_dir,
        "unmeasurable": unmeasurable,
        "poll_target": report.poll_target,
        "poll_explanation": report.poll_explanation,
        "poll_requests": report.poll_requests,
        "poll_interval_seconds": POLL_INTERVAL_SECONDS,
        "poll_intervals_s": report.poll_intervals,
        "poll_cadence_held": cadence_holds(report.poll_intervals),
        "warm": report.warm,
        "concurrent_rounds_ms": [
            [sample.duration_ms for sample in item.samples] for item in report.concurrent
        ],
        # The condition, and then the measurement. They are separate keys because they answer
        # different questions: whether the round was the scenario, and what that scenario produced.
        "concurrent_launched_by_poll": [item.launched_at is not None for item in report.concurrent],
        "concurrent_polls_in_flight": [item.polls_in_flight for item in report.concurrent],
        "concurrent_worst_round": report.worst_round + 1,
        "measurements": [
            {
                "label": measurement.label,
                "value_ms": measurement.value_ms,
                "threshold_ms": measurement.threshold_ms,
                # A run that was not the specified scenario judges nothing, so there is no verdict
                # to publish either.
                "met": None if unmeasurable else measurement.met,
            }
            for measurement in report.measurements
        ],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Measure this installation's dashboard against the sprint's thresholds.",
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help=f"default {DEFAULT_BASE_URL}")
    parser.add_argument(
        "--data-dir",
        default=None,
        help=(
            "where the PO token lives; defaults to SECRETARY_DATA_DIR, then SECRETARY_INSTANCE, "
            "then the instance the CLI defaults to"
        ),
    )
    parser.add_argument("--json", action="store_true", help="print the same facts as one JSON document")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = run(args.base_url, args.data_dir)
    except Unmeasurable as exc:
        print(f"measure_dashboard: {exc}", file=sys.stderr)
        if exc.report is not None:
            if args.json:
                print(json.dumps(as_json(exc.report, unmeasurable=str(exc)), indent=2, sort_keys=True))
            else:
                for line in render(exc.report, unmeasurable=str(exc)):
                    print(line)
        return EXIT_UNMEASURABLE
    if args.json:
        print(json.dumps(as_json(report), indent=2, sort_keys=True))
    else:
        for line in render(report):
            print(line)
    return EXIT_MET if all(measurement.met for measurement in report.measurements) else EXIT_EXCEEDED


if __name__ == "__main__":
    raise SystemExit(main())
