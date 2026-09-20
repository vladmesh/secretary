#!/usr/bin/env python3
"""Measure the dashboard numbers this sprint is judged on, against a running installation.

One command, from a checkout, printing the two measurements the Definition of Done names and the
threshold each of them is judged against:

  * warm sequential `GET /`, `GET /sprints` and `GET /projects` — 20 requests each after a
    discarded warm-up, reported as p95 (the judged number) with min, median and max beside it;
  * four concurrent `GET /` while a `/po/api/sessions/{session}` poll runs every three seconds,
    three rounds of it, judged on the worst.

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

That rule lives in exactly three places, and nowhere else:

  1. :func:`fetch` refuses a response that is not 2xx. It holds the only `urlopen` in this file, so
     every request in the script passes through it, and no caller re-implements the check or is
     able to forget it. A caller that genuinely wants to tolerate a status says so with `tolerate=`
     and a written reason.
  2. :func:`prepare` proves the whole scenario is reproducible — data directory, PO token, a
     session to poll, and one poll of it that actually answered — before any clock starts.
  3. :func:`measure_concurrent` proves *overlap*: each round's timed window has to overlap a
     selected-session poll that was genuinely in flight during it. The round and the poll are
     released from one barrier, so the overlap is produced rather than hoped for, and the per-round
     count in the output is computed from the recorded windows rather than asserted.

**This script only reads.** Every request it makes is a GET or a HEAD, and the whole list is
:data:`READ_REQUESTS` below. There is no POST, nothing that starts or stops a head, and nothing is
written to the board, the state directory or the instance repository.

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
POLL_INTERVAL_SECONDS = 3.0
#: How many times that scenario is repeated. One round of it is not a measurement: the first
#: baseline taken with this script came out 17 s, 27–29 s and 38–41 s on three runs of the same
#: unchanged installation, a factor of 2.4, and a later card cannot close or refuse a 2.0 s item on
#: one sample from an instrument with that spread. Three rounds, every one of them printed, and the
#: threshold judged on the worst: the DoD says *each* request answers within 2.0 s, so a scenario
#: that breaches it in one round of three has not met it.
CONCURRENT_ROUNDS = 3

#: How long a round waits at the barrier for the poll thread to join it. Bounded well under the
#: request timeout, because a poll that is not coming means the scenario is not running, and that
#: is an answer this script should reach quickly rather than after two minutes.
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
    """One round of the concurrent scenario, and the proof that it was that scenario."""

    samples: list[Sample]
    #: Selected-session polls that were genuinely in flight during this round's timed window.
    overlapping_polls: int

    @property
    def started_at(self) -> float:
        return min(sample.started_at for sample in self.samples)

    @property
    def ended_at(self) -> float:
        return max(sample.ended_at for sample in self.samples)


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


# -- the one place a request is made ---------------------------------------------------------


def fetch(
    base_url: str,
    route: str,
    *,
    method: str = "GET",
    cookie: str = "",
    tolerate: frozenset[int] = frozenset(),
) -> Sample:
    """One request, timed end to end from the client's side — and validated here, only here.

    This function holds the only `urlopen` in the file, which is what makes it the single place
    response validity can live. A response that is not 2xx raises :class:`Unmeasurable` naming the
    verb, the route and the status: the script measures answers, and a 404, a 401 or a contained
    500 is not one. The rule used to be re-implemented at five of the six call sites in four
    different spellings, and was simply missing at the sixth — three rounds of review found three
    different sites of the same omission, which is why it is here now and nowhere else.

    `tolerate` is the deliberate escape hatch: a caller that genuinely needs a non-2xx status names
    it, at the call site, with a written reason. The default is refusal, so a new call site cannot
    forget the rule — it can only decide, in writing, to exempt itself. Nothing in this script
    passes it today.

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
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            payload = response.read()
            status = int(response.status)
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        status = int(exc.code)
    except (urllib.error.URLError, OSError) as exc:
        raise Unmeasurable(f"{method} {route} on {base_url} could not be made: {exc}") from None
    ended = time.perf_counter()
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


def choose_poll_target(base_url: str, cookie: str) -> tuple[str, str]:
    """The session to poll and how it was chosen, or an empty target and why there is none.

    The first session the `/po` overview lists, which is the one an operator's browser would be
    polling. A `/po` that does not answer never reaches here — `fetch` refuses it — so the only way
    out with an empty target is the product's own state of having no open session. That is not an
    error of this installation, and the caller says so in the output; it is still not the specified
    scenario, so the run cannot come out green.
    """
    overview = fetch(base_url, PO_OVERVIEW, cookie=cookie)
    found = _SESSION_LINK.search(overview.body.decode("utf-8", "replace"))
    if not found:
        return "", f"{PO_OVERVIEW} answered, and lists no open session to poll"
    session = found.group(1)
    return PO_SESSION_JSON.format(session=session), (
        f"the first session {PO_OVERVIEW} lists ({session}), read with this installation's PO cookie"
    )


def prepare(base_url: str, data_dir_argument: str | None) -> Scenario:
    """Prove the specified scenario can be reproduced here, before a single clock starts.

    Every step raises :class:`Unmeasurable` on failure, so a run that gets past this point has an
    installation that answers, a data plane, a readable token, a session, and one poll of that
    session that actually returned. Nothing downstream has to re-check any of it.
    """
    data_dir, data_dir_source = resolve_data_dir(data_dir_argument)
    try:
        fetch(base_url, CONCURRENT_ROUTE, method="HEAD")
    except Unmeasurable as exc:
        raise Unmeasurable(f"{base_url} is not reachable: {exc}") from None
    cookie = po_cookie(data_dir)
    poll_target, poll_explanation = choose_poll_target(base_url, cookie)
    if poll_target:
        # One poll, made here, so that "the session can be polled" is a fact this run established
        # rather than an assumption the concurrent phase inherits.
        fetch(base_url, poll_target, cookie=cookie)
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

    A read of the selected session every three seconds. It is not measured itself: what is being
    measured is what the dashboard costs *while* that poll is in flight, which is the whole reason
    the poll exists here.

    Which is why this class does two things beyond issuing requests. It records the window of every
    successful poll, so overlap with a round can be computed afterwards from facts rather than
    assumed. And it can be *armed* with a round's barrier: the poll thread then joins that barrier
    and issues its request the instant the barrier releases, alongside the four concurrent
    requests. A readiness gate that merely waits for a completed poll does not give that — it
    deterministically places the poll *before* the round, which is exactly how a previous version
    of this file could report "polled successfully 1 time(s)" for a poll that overlapped nothing.
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
        #: Set once the poll released by the current round has been recorded. Without it the
        #: caller could compute overlap before the poll thread had written its window down and
        #: read back a round that nothing overlapped — the record lagging the fact, not the fact.
        self._gated_done: threading.Event | None = None
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="po-poll", daemon=True)

    @property
    def successes(self) -> int:
        with self._lock:
            return len(self.windows)

    def _take_gate(self) -> tuple[threading.Barrier | None, threading.Event | None]:
        with self._lock:
            gate, self._gate = self._gate, None
            done, self._gated_done = self._gated_done, None
            return gate, done

    def arm(self, gate: threading.Barrier) -> threading.Event:
        """Hand the poll thread a round's barrier, and wake it if it is waiting out the cadence.

        Returns the event that is set once that round's poll has been made *and recorded*, which
        is what the caller waits on before it asks about overlap.
        """
        done = threading.Event()
        with self._lock:
            self._gate = gate
            self._gated_done = done
        self._wake.set()
        return done

    def _run(self) -> None:
        while not self._stop.is_set():
            gate, done = self._take_gate()
            try:
                if gate is not None:
                    # Meeting the round here is what produces the overlap: the four requests and
                    # this poll leave the barrier together.
                    gate.wait(timeout=ROUND_GATE_TIMEOUT_SECONDS)
                sample = fetch(self.base_url, self.route, cookie=self.cookie)
            except threading.BrokenBarrierError:
                # The round gave up waiting, or broke the barrier on its own failure. It reports
                # that; this thread just goes back round.
                if done is not None:
                    done.set()
                continue
            except Unmeasurable as exc:
                # Recorded, not swallowed: `check` reports it on the caller's thread. Polling stops
                # because every further attempt would fail the same way and the run is already void.
                self.failure = exc
                if done is not None:
                    done.set()
                self._stop.set()
                return
            with self._lock:
                self.windows.append((sample.started_at, sample.ended_at))
            if done is not None:
                done.set()
            self._wake.wait(POLL_INTERVAL_SECONDS)
            self._wake.clear()

    def overlapping(self, started_at: float, ended_at: float) -> int:
        """How many polls were genuinely in flight during `[started_at, ended_at]`.

        In flight means started before that window ended and not finished before it began — the
        ordinary interval overlap, computed from recorded windows.
        """
        with self._lock:
            windows = list(self.windows)
        return sum(1 for start, end in windows if start < ended_at and end > started_at)

    def check(self) -> None:
        """Raise whatever the poll thread hit, on the caller's thread."""
        if self.failure is not None:
            raise self.failure

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        self._wake.set()
        self._thread.join(timeout=REQUEST_TIMEOUT_SECONDS)


def measure_concurrent(base_url: str, poll: SessionPoll) -> Round:
    """One round: four `GET /` at once, with a poll released alongside them.

    The barrier has five parties — the four request threads and the poll thread — so the round and
    a selected-session poll begin together. Overlap is then a property of the mechanism, and the
    count this returns is read back off the recorded windows as evidence of it.
    """
    samples: list[Sample | None] = [None] * CONCURRENT_REQUESTS
    failures: list[BaseException] = []
    gate = threading.Barrier(CONCURRENT_REQUESTS + 1)

    def one(index: int) -> None:
        try:
            # The barrier is what makes this concurrency rather than four quick requests: every
            # thread has its connection set up and is waiting before any of them asks.
            gate.wait(timeout=ROUND_GATE_TIMEOUT_SECONDS)
            samples[index] = fetch(base_url, CONCURRENT_ROUTE)
        except BaseException as exc:  # noqa: BLE001 - reported below, on the caller's thread
            failures.append(exc)

    poll.check()
    polled = poll.arm(gate)
    threads = [threading.Thread(target=one, args=(index,)) for index in range(CONCURRENT_REQUESTS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=REQUEST_TIMEOUT_SECONDS * 2)
    # A poll that died is the likelier cause of a broken barrier than the requests were, and its
    # message names the route and the status, so it is reported first.
    poll.check()
    # The round's own poll may still be in flight: a dashboard that answers four page requests
    # faster than one session read is exactly the case this measurement is heading towards. Wait
    # for it to be recorded before asking about overlap, or the answer would be about the record
    # rather than about what happened.
    polled.wait(timeout=ROUND_GATE_TIMEOUT_SECONDS)
    poll.check()
    if failures:
        raise Unmeasurable(f"a concurrent GET {CONCURRENT_ROUTE} could not be made: {failures[0]}")
    measured = [sample for sample in samples if sample is not None]
    if len(measured) != CONCURRENT_REQUESTS:
        raise Unmeasurable(f"only {len(measured)} of {CONCURRENT_REQUESTS} concurrent requests were answered")
    started = min(sample.started_at for sample in measured)
    ended = max(sample.ended_at for sample in measured)
    return Round(samples=measured, overlapping_polls=poll.overlapping(started, ended))


def require_overlap(rounds: list[Round]) -> None:
    """Refuse rounds that were not the specified scenario.

    The DoD scenario is four concurrent requests *while* the session is polled. A round no poll
    overlapped measured a quieter thing, so it may neither be judged nor compete to be the worst
    round — which is why this refuses any of them rather than only the one that happens to be
    judged.
    """
    barren = [index + 1 for index, item in enumerate(rounds) if item.overlapping_polls < 1]
    if barren:
        raise Unmeasurable(
            f"round(s) {', '.join(str(number) for number in barren)} were measured with no "
            f"selected-session poll in flight, so they are not the scenario the thresholds judge"
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
        # An installation with no open PO session is not broken, and the warm half above is a real
        # measurement of it. The concurrent half of the DoD cannot be reproduced here at all, and
        # measuring four requests against an idle dashboard instead would be a different scenario
        # reported under the same heading — so the run stops, says so, and cannot come out green.
        raise Unmeasurable(
            f"{scenario.poll_explanation}, so the concurrent scenario — four requests while a "
            f"session is polled every {POLL_INTERVAL_SECONDS:.0f} s — cannot be reproduced on this "
            f"installation right now; open a PO session and run this again"
        )

    with SessionPoll(scenario.base_url, scenario.poll_target, scenario.cookie) as poll:
        report.concurrent = [measure_concurrent(scenario.base_url, poll) for _ in range(CONCURRENT_ROUNDS)]
        report.poll_requests = poll.successes
    poll.check()
    require_overlap(report.concurrent)

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
        lines.append(f"/po poll: GET {report.poll_target} every {POLL_INTERVAL_SECONDS:.0f} s")
        lines.append(f"  chosen as {report.poll_explanation}")
        lines.append(f"  polled successfully {report.poll_requests} time(s) during the concurrent rounds")
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
        for index, item in enumerate(report.concurrent, start=1):
            marker = " <- judged" if index - 1 == report.worst_round and not unmeasurable else ""
            durations = ", ".join(f"{sample.duration_ms:.0f}" for sample in item.samples)
            lines.append(
                f"  round {index}: {durations} ms [{item.overlapping_polls} poll(s) in flight]{marker}"
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
        "warm": report.warm,
        "concurrent_rounds_ms": [
            [sample.duration_ms for sample in item.samples] for item in report.concurrent
        ],
        "concurrent_overlapping_polls": [item.overlapping_polls for item in report.concurrent],
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
