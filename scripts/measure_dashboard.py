#!/usr/bin/env python3
"""Measure the dashboard numbers this sprint is judged on, against a running installation.

One command, from a checkout, printing the two measurements the Definition of Done names and the
threshold each of them is judged against:

  * warm sequential `GET /`, `GET /sprints` and `GET /projects` — 20 requests each after a
    discarded warm-up, reported as p95 (the judged number) with min, median and max beside it;
  * four concurrent `GET /` while a `/po/api/sessions/{session}` poll runs every three seconds,
    reported as each of the four durations.

It exists so that later cards are assessed on its output rather than on a hand measurement: the
numbers before an optimisation and the numbers after it come out of the same procedure, with the
same warm-up, the same request count and the same percentile rule.

**This script only reads.** Every request it makes is a GET or a HEAD, and the whole list is
:data:`READ_REQUESTS` below — three dashboard pages, the `/po` overview it finds a session id on,
and that session's JSON. There is no POST, nothing that starts or stops a head, and nothing is
written to the board, the state directory or the instance repository.

Standard library only. The one thing it imports from the product is the PO cookie derivation, so
that the poll uses this installation's own rule rather than a copy of it.
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

#: Every request this script is able to make, as a reviewer reads it: method and route, nothing else.
READ_REQUESTS = (
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
    """The installation could not be measured, which is not the same as measuring badly."""


@dataclass
class Sample:
    """One request that was made and answered."""

    route: str
    status: int
    duration_ms: float


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
class Report:
    """Everything the run measured, in the order it is printed."""

    base_url: str
    poll_target: str
    poll_explanation: str
    warm: list[dict[str, Any]] = field(default_factory=list)
    concurrent: list[Sample] = field(default_factory=list)
    measurements: list[Measurement] = field(default_factory=list)
    poll_requests: int = 0


def fetch(base_url: str, route: str, *, method: str = "GET", cookie: str = "") -> Sample:
    """One request, timed end to end from the client's side.

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
            response.read()
            status = int(response.status)
    except urllib.error.HTTPError as exc:
        exc.read()
        status = int(exc.code)
    except (urllib.error.URLError, OSError) as exc:
        raise Unmeasurable(f"{method} {route} on {base_url} could not be made: {exc}") from None
    return Sample(route=route, status=status, duration_ms=(time.perf_counter() - started) * 1000.0)


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
    """Warm sequential requests to one route: a discarded warm-up, then the judged twenty."""
    first = fetch(base_url, route)
    if first.status == 404:
        raise Unmeasurable(f"{base_url} does not serve {route} (404); this installation cannot be measured")
    if first.status >= 400:
        raise Unmeasurable(f"GET {route} answered {first.status}; the measurement would be of a refusal")
    samples = [fetch(base_url, route) for _ in range(WARM_REQUESTS)]
    bad = [sample for sample in samples if sample.status >= 400]
    if bad:
        raise Unmeasurable(f"GET {route} answered {bad[0].status} during the measurement")
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
    """The `/po` poll a real operator's open session page makes, for the concurrency measurement.

    A read, every three seconds, on the one route the PO page polls. It runs beside the four
    concurrent requests rather than being measured itself: what is being measured is what the
    dashboard costs while that poll is in flight.
    """

    def __init__(self, base_url: str, route: str, cookie: str) -> None:
        self.base_url = base_url
        self.route = route
        self.cookie = cookie
        self.requests = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="po-poll", daemon=True)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                fetch(self.base_url, self.route, cookie=self.cookie)
            except Unmeasurable:
                # The poll is the scenario, not the measurement: a poll that fails mid-run is
                # reported by its count being short, and does not fail the four requests.
                pass
            self.requests += 1
            self._stop.wait(POLL_INTERVAL_SECONDS)

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=REQUEST_TIMEOUT_SECONDS)


def measure_concurrent(base_url: str) -> list[Sample]:
    """Four `GET /` at once, each timed on its own thread."""
    samples: list[Sample | None] = [None] * CONCURRENT_REQUESTS
    failures: list[BaseException] = []
    ready = threading.Barrier(CONCURRENT_REQUESTS)

    def one(index: int) -> None:
        try:
            # The barrier is what makes this concurrency rather than four quick requests: every
            # thread has its connection set up and is waiting before any of them asks.
            ready.wait(timeout=REQUEST_TIMEOUT_SECONDS)
            samples[index] = fetch(base_url, CONCURRENT_ROUTE)
        except BaseException as exc:  # noqa: BLE001 - reported below, on the caller's thread
            failures.append(exc)

    threads = [threading.Thread(target=one, args=(index,)) for index in range(CONCURRENT_REQUESTS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=REQUEST_TIMEOUT_SECONDS * 2)
    if failures:
        raise Unmeasurable(f"a concurrent GET {CONCURRENT_ROUTE} could not be made: {failures[0]}")
    measured = [sample for sample in samples if sample is not None]
    if len(measured) != CONCURRENT_REQUESTS:
        raise Unmeasurable(f"only {len(measured)} of {CONCURRENT_REQUESTS} concurrent requests were answered")
    return measured


def po_cookie(data_dir: Path | None) -> tuple[str, str]:
    """This installation's PO cookie, or an empty one and the reason there is none.

    The derivation is imported from the product rather than copied: the cookie is an HMAC keyed by
    the token file, and a second implementation of that rule here would be a second thing to keep
    in step with `secretary.po.token`.
    """
    if data_dir is None:
        return "", "no data directory was resolved, so the PO token could not be read"
    sys.path.insert(0, str(REPO_ROOT / "src"))
    try:
        from secretary.po.token import COOKIE_NAME, TokenError, cookie_value, read_token
    except ImportError as exc:
        return "", f"the PO cookie rule could not be imported from this checkout: {exc}"
    try:
        token = read_token(data_dir)
    except TokenError as exc:
        return "", str(exc)
    except OSError as exc:
        return "", f"the PO token under {data_dir} is not readable by this user: {exc}"
    return f"{COOKIE_NAME}={cookie_value(token)}", ""


def choose_poll_target(base_url: str, cookie: str) -> tuple[str, str]:
    """The session to poll and how it was chosen, or an empty target and why there is none.

    The first session the `/po` overview lists, which is the one an operator's browser would be
    polling. No session is an ordinary state of this installation, not a failure: the concurrency
    numbers are then reported without the poll and say so, rather than quietly measuring an idle
    dashboard and calling it the same scenario.
    """
    if not cookie:
        return "", ""
    try:
        overview = urllib.request.Request(base_url.rstrip("/") + PO_OVERVIEW, method="GET")
        overview.add_header("Cookie", cookie)
        with urllib.request.urlopen(overview, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            markup = response.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as exc:
        return "", f"{PO_OVERVIEW} could not be read: {exc}"
    found = _SESSION_LINK.search(markup)
    if not found:
        return "", f"{PO_OVERVIEW} lists no session to poll"
    session = found.group(1)
    return PO_SESSION_JSON.format(session=session), (
        f"the first session {PO_OVERVIEW} lists ({session}), read with this installation's PO cookie"
    )


def run(base_url: str, data_dir: Path | None) -> Report:
    """Take both measurements against `base_url`, in the order they are printed."""
    try:
        reachable = fetch(base_url, CONCURRENT_ROUTE, method="HEAD")
    except Unmeasurable as exc:
        raise Unmeasurable(f"{base_url} is not reachable: {exc}") from None
    if reachable.status >= 400:
        raise Unmeasurable(f"{base_url} answered HEAD / with {reachable.status}")

    cookie, cookie_problem = po_cookie(data_dir)
    poll_target, poll_explanation = choose_poll_target(base_url, cookie)
    if not poll_target:
        poll_explanation = cookie_problem or poll_explanation or "no PO session was found"

    report = Report(base_url=base_url, poll_target=poll_target, poll_explanation=poll_explanation)
    for route in WARM_ROUTES:
        warm = measure_warm(base_url, route)
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

    if poll_target:
        with SessionPoll(base_url, poll_target, cookie) as poll:
            report.concurrent = measure_concurrent(base_url)
        report.poll_requests = poll.requests
    else:
        report.concurrent = measure_concurrent(base_url)
    for index, sample in enumerate(report.concurrent, start=1):
        report.measurements.append(
            Measurement(
                label=f"concurrent GET {CONCURRENT_ROUTE} #{index} of {CONCURRENT_REQUESTS}",
                value_ms=sample.duration_ms,
                threshold_ms=CONCURRENT_THRESHOLD_MS,
            )
        )
    return report


def render(report: Report) -> list[str]:
    """The table, in full — a red result prints exactly as much as a green one."""
    lines = [
        f"base URL: {report.base_url}",
    ]
    if report.poll_target:
        lines.append(f"/po poll: GET {report.poll_target} every {POLL_INTERVAL_SECONDS:.0f} s")
        lines.append(f"  chosen as {report.poll_explanation}")
        lines.append(f"  polled {report.poll_requests} time(s) during the concurrent requests")
    else:
        lines.append(f"/po poll: none — {report.poll_explanation}")
        lines.append("  the concurrency numbers below were measured WITHOUT the poll")
    lines.append("")
    lines.append(
        f"warm sequential: {WARM_REQUESTS} requests per route "
        f"after {WARMUP_REQUESTS} discarded warm-up request"
    )
    width = max(len(measurement.label) for measurement in report.measurements)
    for measurement in report.measurements:
        verdict = "MEETS" if measurement.met else "EXCEEDS"
        line = (
            f"  {measurement.label:<{width}}  {measurement.value_ms:8.0f} ms  "
            f"threshold {measurement.threshold_ms:.0f} ms  {verdict}"
        )
        if measurement.detail:
            line = f"{line}\n  {'':<{width}}  ({measurement.detail})"
        lines.append(line)
    exceeded = [measurement for measurement in report.measurements if not measurement.met]
    lines.append("")
    if exceeded:
        lines.append(f"{len(exceeded)} of {len(report.measurements)} measurements exceed their threshold")
    else:
        lines.append(f"all {len(report.measurements)} measurements are at or under their threshold")
    return lines


def as_json(report: Report) -> dict[str, Any]:
    """The same facts, for a later card that wants to diff two runs rather than read two tables."""
    return {
        "base_url": report.base_url,
        "poll_target": report.poll_target,
        "poll_explanation": report.poll_explanation,
        "poll_requests": report.poll_requests,
        "warm": report.warm,
        "concurrent_ms": [sample.duration_ms for sample in report.concurrent],
        "measurements": [
            {
                "label": measurement.label,
                "value_ms": measurement.value_ms,
                "threshold_ms": measurement.threshold_ms,
                "met": measurement.met,
            }
            for measurement in report.measurements
        ],
    }


def resolve_data_dir(argument: str | None) -> Path | None:
    """Where the PO token lives: the argument, the environment, or the selected instance.

    The same order `secretary` itself resolves a data plane in (`secretary.session`), so this
    script measures the installation the shell is already pointed at.
    """
    if argument:
        return Path(argument).expanduser()
    configured = os.environ.get("SECRETARY_DATA_DIR")
    if configured:
        return Path(configured).expanduser()
    instance = os.environ.get("SECRETARY_INSTANCE")
    if not instance:
        return None
    sys.path.insert(0, str(REPO_ROOT / "src"))
    try:
        from secretary.config import instance_data_dir

        return instance_data_dir(Path(instance).expanduser())
    except Exception:  # noqa: BLE001 - an unresolvable data dir only costs the poll
        return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Measure this installation's dashboard against the sprint's thresholds.",
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help=f"default {DEFAULT_BASE_URL}")
    parser.add_argument(
        "--data-dir",
        default=None,
        help="where the PO token lives; defaults to SECRETARY_DATA_DIR, then the selected instance",
    )
    parser.add_argument("--json", action="store_true", help="print the same facts as one JSON document")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = run(args.base_url, resolve_data_dir(args.data_dir))
    except Unmeasurable as exc:
        print(f"measure_dashboard: {exc}", file=sys.stderr)
        return EXIT_UNMEASURABLE
    if args.json:
        print(json.dumps(as_json(report), indent=2, sort_keys=True))
    else:
        for line in render(report):
            print(line)
    return EXIT_MET if all(measurement.met for measurement in report.measurements) else EXIT_EXCEEDED


if __name__ == "__main__":
    raise SystemExit(main())
