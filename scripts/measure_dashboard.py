#!/usr/bin/env python3
"""Measure the dashboard's warm response times, against a running installation.

One command, from a checkout, printing the warm measurements the Definition of Done names and the
threshold each of them is judged against: warm sequential `GET /`, `GET /sprints` and
`GET /projects` — 20 requests each after a discarded warm-up, reported as p95 (the judged number)
with min, median and max beside it.

It measures the warm items only. The Definition-of-Done item about four concurrent `GET /` while a
`/po` page polls is measured separately, not by this command, and nothing it prints is a verdict
about concurrency.

It exists so that later cards are assessed on its output rather than on a hand measurement: the
numbers before an optimisation and the numbers after it come out of the same procedure, with the
same warm-up, the same request count and the same percentile rule.

**The one rule this file is built around.** No number printed here may be judged MEETS unless it
was produced by exactly the measurement the Definition of Done names. Every other outcome is
`Unmeasurable` and exits 2. Concretely:

  * exit 0 — every request answered 2xx and every judged number is at or under its threshold;
  * exit 1 — the same, except that a judged number is over its threshold. A red number is still a
    real number;
  * exit 2 — everything else.

That rule lives in exactly two places, and nowhere else:

  1. :func:`fetch` refuses a response that is not 2xx, and reaches only the installation the
     caller named. It holds the only transport call in this file — :data:`_OPENER`, built with no
     redirect handler and with an empty `ProxyHandler` — so every request in the script passes
     through it, and no caller re-implements the check or is able to forget it. A caller that
     genuinely wants to tolerate a status says so with `tolerate=` and a written reason; a 3xx is
     refused even then, because a followed redirect would measure a different installation, and a
     proxy would do the same without even a response to show for it.
  2. :func:`prepare` proves the installation can be measured — its data directory resolves, and
     it answers — before any clock starts.

**This script only reads, and only from the installation it was given.** Every request it makes
is a GET or a HEAD, and the whole list is :data:`READ_REQUESTS` below. There is no POST, nothing
that starts or stops a head, and nothing is written to the board, the state directory or the
instance repository. Every request goes to `--base-url` and nowhere else: the opener follows no
redirect and reads no proxy variable, so neither a response nor the caller's environment can send
this script to another host or port, and the list below stays the complete inventory of what a run
asks for.

Standard library only. What it imports from the product is the product's own resolution of where
an installation's data directory comes from, because a second copy of that rule here would be a
second thing to keep in step.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Where `secretary-web.service` listens. The unit binds this and only this
#: (`packaging/systemd/secretary-web.service`); `--base-url` is for a second installation or a
#: stand, not for reaching a published front, which adds TLS and a password to every number.
DEFAULT_BASE_URL = "http://127.0.0.1:8787"

#: The pages the warm sequential measurement covers, in the order it prints them.
WARM_ROUTES = ("/", "/sprints", "/projects")
#: The route the reachability probe asks with a HEAD before any clock starts.
PROBE_ROUTE = "/"

#: Every request this script is able to make, as a reviewer reads it: method and route, nothing
#: else. It is the complete surface, verbs included — the reachability probe is a HEAD and is on
#: this list for that reason. `tests.test_measurement_script` checks a whole run's request log
#: against these pairs in both directions, so a request that is not here fails there.
READ_REQUESTS = (
    ("HEAD", "/"),
    ("GET", "/"),
    ("GET", "/sprints"),
    ("GET", "/projects"),
)
#: The only verbs :func:`fetch` will send. A POST would start a head on a live installation.
READ_METHODS = frozenset({"GET", "HEAD"})

#: How the warm measurement is taken. One warm-up request is discarded because the first request
#: after a deploy pays for import, connection and cache warming that no later request pays again,
#: and the DoD judges a warm page.
WARMUP_REQUESTS = 1
WARM_REQUESTS = 20

#: The Definition of Done, in milliseconds. Warm p95 is judged per route.
WARM_P95_THRESHOLD_MS = 1000.0

#: A request that has not answered by then is a failed measurement, not a slow one.
REQUEST_TIMEOUT_SECONDS = 120.0

#: Exit statuses, as the card names them.
EXIT_MET = 0
EXIT_EXCEEDED = 1
EXIT_UNMEASURABLE = 2

#: The one line the output and `docs/OPERATIONS.md` both carry, so neither reads as a verdict on
#: the concurrent item.
SCOPE_LINE = (
    "scope: warm items only; the DoD item on four concurrent GET / while a /po page polls is "
    "measured separately, not by this command"
)


class Unmeasurable(Exception):
    """The installation could not be measured, which is not the same as measuring badly.

    It may carry the part of the report that had already been taken when it was raised, so a run
    that fails on a later route can still show what the earlier routes found. Those numbers are
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
    """An installation proved measurable before a single clock starts."""

    base_url: str
    data_dir: Path
    data_dir_source: str


@dataclass
class Report:
    """Everything the run measured, in the order it is printed."""

    base_url: str
    data_dir: str = ""
    warm: list[dict[str, Any]] = field(default_factory=list)
    measurements: list[Measurement] = field(default_factory=list)


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


# -- the one place the installation is proved measurable ---------------------------------------


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
    environment: without this step that shell resolved no data directory at all.

    A failure here raises rather than returning None: a run that cannot say which installation it
    measured is not the specified measurement.
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


def prepare(base_url: str, data_dir_argument: str | None) -> Scenario:
    """Prove the installation can be measured here, before a single clock starts.

    Every step raises :class:`Unmeasurable` on failure, so a run that gets past this point has a
    resolved data plane and an installation that answers. Nothing downstream re-checks either.
    """
    data_dir, data_dir_source = resolve_data_dir(data_dir_argument)
    try:
        fetch(base_url, PROBE_ROUTE, method="HEAD")
    except Unmeasurable as exc:
        raise Unmeasurable(f"{base_url} is not reachable: {exc}") from None
    return Scenario(base_url=base_url, data_dir=data_dir, data_dir_source=data_dir_source)


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


def run(base_url: str, data_dir_argument: str | None) -> Report:
    """Take the warm measurements against `base_url`, in the order they are printed."""
    scenario = prepare(base_url, data_dir_argument)
    report = Report(
        base_url=scenario.base_url,
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


# -- what it prints --------------------------------------------------------------------------


def render(report: Report, *, unmeasurable: str = "") -> list[str]:
    """The table, in full — a red result prints exactly as much as a green one.

    With `unmeasurable` set, the numbers that were taken are still printed, and none of them
    carries a verdict: they are real measurements of a run that was not the specified one, and a
    MEETS beside one of them would be this script reporting a measurement it did not complete.
    """
    lines = [f"base URL: {report.base_url}"]
    if report.data_dir:
        lines.append(f"data directory: {report.data_dir}")
    lines.append(SCOPE_LINE)
    if unmeasurable:
        lines.append("")
        lines.append(f"NOT MEASURED: {unmeasurable}")
        lines.append("  the numbers below were taken, but this run is not the specified measurement,")
        lines.append("  so none of them is judged and the command exits 2")
    if not report.measurements:
        return lines
    lines.append("")
    lines.append(
        f"warm sequential: {WARM_REQUESTS} requests per route after {WARMUP_REQUESTS} discarded warm-up request"
    )
    width = max(len(measurement.label) for measurement in report.measurements)
    for measurement in report.measurements:
        verdict = "NOT JUDGED" if unmeasurable else ("MEETS" if measurement.met else "EXCEEDS")
        line = (
            f"  {measurement.label:<{width}}  {measurement.value_ms:8.0f} ms  "
            f"threshold {measurement.threshold_ms:.0f} ms  {verdict}"
        )
        if measurement.detail:
            line = f"{line}\n  {'':<{width}}  ({measurement.detail})"
        lines.append(line)
    lines.append("")
    if unmeasurable:
        lines.append("no measurement was judged: this run was not the specified measurement")
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
        "scope": SCOPE_LINE,
        "unmeasurable": unmeasurable,
        "warm": report.warm,
        "measurements": [
            {
                "label": measurement.label,
                "value_ms": measurement.value_ms,
                "threshold_ms": measurement.threshold_ms,
                # A run that was not the specified measurement judges nothing, so there is no
                # verdict to publish either.
                "met": None if unmeasurable else measurement.met,
            }
            for measurement in report.measurements
        ],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Measure this installation's warm dashboard response times against the sprint's thresholds.",
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help=f"default {DEFAULT_BASE_URL}")
    parser.add_argument(
        "--data-dir",
        default=None,
        help=(
            "this installation's data directory; defaults to SECRETARY_DATA_DIR, then "
            "SECRETARY_INSTANCE, then the instance the CLI defaults to"
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
