"""`scripts/measure_dashboard.py`: the one command this sprint's numbers are read off.

The script measures a running installation, so what can be pinned here is everything about it that
is not the installation: that it only ever reads, that the routes it asks for are this product's own
read routes, that the percentile it prints is the one it says it prints, and that its exit status
and its table say the same thing. The measurements themselves run against a stub server this test
binds on loopback — the same hermetic rule the transport suite works under (`tests/README.md`): no
live installation, no real Orca, no network.

Hermetic in one more sense that matters for a timing script: the stub answers instantly, so a case
that wants a slow answer says how slow, and no assertion here depends on how fast this host is.
"""

from __future__ import annotations

import ast
import contextlib
import importlib.util
import io
import re
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import ClassVar
from unittest import mock

from secretary.web.app import ROUTES

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "measure_dashboard.py"


def _script() -> ModuleType:
    """The script, imported by path: `scripts/` is not an importable package (see `tests/broad.py`)."""
    spec = importlib.util.spec_from_file_location("secretary_measure_dashboard", SCRIPT)
    if spec is None or spec.loader is None:  # pragma: no cover - a missing script is a broken tree
        raise RuntimeError(f"the measurement script is unavailable at {SCRIPT}")
    cached = sys.modules.get(spec.name)
    if cached is not None:
        return cached
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[spec.name]
        raise
    return module


measure = _script()


class _Stub(BaseHTTPRequestHandler):
    """A dashboard that answers as fast, as slowly and as badly as a case tells it to."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *_args: object) -> None:
        """Quiet: this server's own journal is not what is under test."""

    def _answer(self, *, head: bool) -> None:
        plan = self.server.plan  # type: ignore[attr-defined]
        path = self.path.partition("?")[0]
        status, body, delay = plan(self.command, path)
        if delay:
            time.sleep(delay)
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if not head:
            self.wfile.write(payload)

    def do_GET(self) -> None:
        self._answer(head=False)

    def do_HEAD(self) -> None:
        self._answer(head=True)


class StubDashboard(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class MeasurementScriptTests(unittest.TestCase):
    """The script against a dashboard whose answers this test decides."""

    SESSION = "11111111-2222-3333-4444-555555555555"

    def serve(
        self,
        *,
        missing: tuple[str, ...] = (),
        sessions: bool = True,
        session_status: int = 200,
        overview_status: int = 200,
        delays: dict[str, float] | None = None,
        slow_get_after: tuple[str, int, float] | None = None,
    ) -> str:
        """A dashboard with a decided answer, and a decided cost, for every route.

        `slow_get_after` is `(path, nth, seconds)`: that path's GETs become slow from the nth one
        on. It is how a case makes one *round* of the concurrent scenario slow while the earlier
        one stays fast, which is the only way to see which round the threshold is judged on.
        """
        seen: list[tuple[str, str]] = []
        lock = threading.Lock()

        def plan(method: str, path: str) -> tuple[int, str, float]:
            with lock:
                seen.append((method, path))
                counted = sum(1 for verb, seen_path in seen if verb == "GET" and seen_path == path)
            delay = (delays or {}).get(path, 0.0)
            if slow_get_after is not None:
                slow_path, nth, seconds = slow_get_after
                if method == "GET" and path == slow_path and counted >= nth:
                    delay = seconds
            if path in missing:
                return 404, "no such route", delay
            if path == measure.PO_OVERVIEW:
                link = f'<a class="ref" href="/po/sessions/{self.SESSION}">session</a>' if sessions else ""
                return overview_status, f"<html>{link}</html>", delay
            if path.startswith("/po/api/sessions/"):
                return session_status, '{"session": {}}', delay
            return 200, "<html>dashboard</html>", delay

        server = StubDashboard(("127.0.0.1", 0), _Stub)
        server.plan = plan  # type: ignore[attr-defined]
        server.seen = seen  # type: ignore[attr-defined]
        self.server = server
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}"

    def cookie(self, value: str = "secretary_po=deadbeef") -> None:
        """Stand in for the installation's PO token, which no hermetic test may read from a host."""
        patch = mock.patch.object(measure, "po_cookie", return_value=(value, ""))
        patch.start()
        self.addCleanup(patch.stop)

    def run_main(self, base_url: str) -> tuple[int, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = measure.main(["--base-url", base_url])
        return code, out.getvalue() + err.getvalue()

    # -- read-only ---------------------------------------------------------------------------

    def test_every_request_it_can_make_is_a_read_on_a_read_route_of_this_product(self) -> None:
        """Criterion 8, confirmed from the request list rather than from a run.

        Each entry is checked against the product's own route table, so a route that later became
        a write, or a pattern that stopped existing, fails here instead of on a live installation.
        A HEAD is checked against the GET route it is served by, because that is how the transport
        answers one (`_Handler.do_HEAD`) — there is no separate HEAD row to match.
        """
        installed = {(route.method, route.pattern) for route in ROUTES}
        for method, route in measure.READ_REQUESTS:
            with self.subTest(method=method, route=route):
                self.assertIn(method, measure.READ_METHODS)
                self.assertIn(("GET", route), installed)
        self.assertIn(("HEAD", "/"), measure.READ_REQUESTS, "the reachability probe is a request too")

    def test_the_fetcher_refuses_any_verb_that_is_not_a_read(self) -> None:
        self.assertEqual(set(measure.READ_METHODS), {"GET", "HEAD"})
        with self.assertRaises(ValueError):
            measure.fetch("http://127.0.0.1:1", "/", method="POST")

    def test_no_call_in_the_script_names_a_writing_verb(self) -> None:
        """A grep a reviewer would do, done here so it stays true."""
        tree = ast.parse(SCRIPT.read_text(encoding="utf-8"), filename=str(SCRIPT))
        verbs = {
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        for forbidden in ("POST", "PUT", "PATCH", "DELETE"):
            self.assertNotIn(forbidden, verbs, f"{forbidden} appears as a literal in the script")

    def test_a_run_asks_for_nothing_but_the_method_and_route_pairs_on_the_list(self) -> None:
        """Pairs, not paths: `HEAD /` must not pass because `GET /` happens to be listed."""
        self.cookie()
        base = self.serve()
        with mock.patch.object(measure, "WARM_REQUESTS", 2):
            self.run_main(base)

        listed = set(measure.READ_REQUESTS)
        observed = set()
        for method, path in self.server.seen:  # type: ignore[attr-defined]
            # The one templated route: the session id is chosen at run time, so the observed path
            # is folded back onto the template it came from rather than matched literally.
            canonical = measure.PO_SESSION_JSON if path.startswith("/po/api/sessions/") else path
            observed.add((method, canonical))
        self.assertEqual(observed - listed, set(), "a request was made that the inventory omits")
        # And the inventory is not padded either: every pair on it was actually asked for.
        self.assertEqual(listed - observed, set(), "the inventory lists a request the run never made")

    # -- the numbers -------------------------------------------------------------------------

    def test_p95_is_the_nearest_rank_over_the_twenty_samples(self) -> None:
        values = [float(index) for index in range(1, measure.WARM_REQUESTS + 1)]
        # Nearest rank over twenty samples is the nineteenth: the second slowest request.
        self.assertEqual(measure.p95(values), 19.0)
        self.assertEqual(measure.p95(list(reversed(values))), 19.0)
        self.assertEqual(measure.p95([5.0]), 5.0)

    def test_a_fast_installation_meets_every_threshold_and_exits_zero(self) -> None:
        self.cookie()
        base = self.serve()
        with mock.patch.object(measure, "WARM_REQUESTS", 3):
            code, text = self.run_main(base)

        self.assertEqual(code, measure.EXIT_MET, text)
        self.assertNotIn("EXCEEDS", text)
        self.assertEqual(text.count("MEETS"), len(measure.WARM_ROUTES) + measure.CONCURRENT_REQUESTS)
        self.assertIn(f"base URL: {base}", text)
        self.assertIn("threshold 1000 ms", text)
        self.assertIn("threshold 2000 ms", text)

    def test_a_slow_route_exits_one_and_still_prints_the_whole_table(self) -> None:
        self.cookie()
        # Just over the warm threshold on one page, and only on that page.
        base = self.serve(delays={"/sprints": (measure.WARM_P95_THRESHOLD_MS + 200) / 1000.0})
        with mock.patch.object(measure, "WARM_REQUESTS", 3):
            code, text = self.run_main(base)

        self.assertEqual(code, measure.EXIT_EXCEEDED, text)
        self.assertIn("warm p95 GET /sprints", text)
        self.assertIn("EXCEEDS", text)
        # Red prints as much as green: every route and every concurrent request is still there.
        for route in measure.WARM_ROUTES:
            self.assertIn(f"warm p95 GET {route}", text)
        for index in range(1, measure.CONCURRENT_REQUESTS + 1):
            self.assertIn(f"concurrent GET / #{index} of {measure.CONCURRENT_REQUESTS}", text)
        self.assertIn("1 of 7 measurements exceed their threshold", text)

    def test_the_poll_target_is_named_with_how_it_was_chosen(self) -> None:
        self.cookie()
        base = self.serve()
        with mock.patch.object(measure, "WARM_REQUESTS", 2):
            code, text = self.run_main(base)

        self.assertEqual(code, measure.EXIT_MET, text)
        self.assertIn(f"/po poll: GET /po/api/sessions/{self.SESSION}", text)
        self.assertIn(f"the first session /po lists ({self.SESSION})", text)

    def test_no_session_is_said_out_loud_and_the_concurrency_numbers_still_come(self) -> None:
        """Criterion 7: a different scenario is never measured quietly under the same heading."""
        self.cookie()
        base = self.serve(sessions=False)
        with mock.patch.object(measure, "WARM_REQUESTS", 2):
            code, text = self.run_main(base)

        self.assertEqual(code, measure.EXIT_MET, text)
        self.assertIn("/po poll: none", text)
        self.assertIn("lists no session to poll", text)
        self.assertIn("WITHOUT the poll", text)
        self.assertIn("concurrent GET / #4 of 4", text)
        polled = [path for _method, path in self.server.seen if path.startswith("/po/api/")]  # type: ignore[attr-defined]
        self.assertEqual(polled, [])

    def test_the_concurrent_scenario_is_repeated_and_every_round_is_printed(self) -> None:
        self.cookie()
        base = self.serve()
        with mock.patch.object(measure, "WARM_REQUESTS", 2):
            code, text = self.run_main(base)

        self.assertEqual(code, measure.EXIT_MET, text)
        self.assertEqual(measure.CONCURRENT_ROUNDS, 3)
        for round_number in range(1, measure.CONCURRENT_ROUNDS + 1):
            self.assertIn(f"  round {round_number}: ", text)
        # Exactly one round is the judged one, and the four judged rows say which.
        self.assertEqual(text.count("<- judged"), 1)
        self.assertIn(f"(worst of {measure.CONCURRENT_ROUNDS} rounds)", text)

    def test_the_threshold_is_judged_on_the_worst_round_not_the_first(self) -> None:
        """A scenario that breaches 2.0 s in one round of three has not met it.

        The observer's finding on the first submission: the same unchanged installation produced
        17 s, 27 s and 38 s on three runs, so a single round could have reported either verdict.
        Here round one is fast and the later rounds are slow, and the run must come out red.
        """
        self.cookie()
        warm = 2
        # Requests on `/`: one warm-up, then `warm` timed ones, then four per round. Slowing the
        # eighth GET onwards leaves the warm phase and round one fast and makes round two slow.
        first_slow = 1 + warm + measure.CONCURRENT_REQUESTS + 1
        base = self.serve(slow_get_after=("/", first_slow, (measure.CONCURRENT_THRESHOLD_MS + 200) / 1000.0))
        with mock.patch.object(measure, "WARM_REQUESTS", warm):
            code, text = self.run_main(base)

        self.assertEqual(code, measure.EXIT_EXCEEDED, text)
        rounds = re.findall(r"^  round (\d+): (.*?) ms( <- judged)?$", text, re.MULTILINE)
        self.assertEqual(len(rounds), measure.CONCURRENT_ROUNDS, text)
        fastest = max(float(value) for value in rounds[0][1].split(", "))
        self.assertLess(fastest, measure.CONCURRENT_THRESHOLD_MS, "round one was meant to be fast")
        self.assertNotEqual(rounds[0][2], " <- judged", "the fast first round must not be the judged one")
        for index in range(1, measure.CONCURRENT_REQUESTS + 1):
            self.assertIn(f"concurrent GET / #{index} of {measure.CONCURRENT_REQUESTS}", text)
        self.assertIn("EXCEEDS", text)

    def test_a_po_overview_that_does_not_answer_is_unmeasurable_not_no_session(self) -> None:
        """The reviewer's first reproduction: a 404 from `/po` used to exit 0 as "no session"."""
        self.cookie()
        base = self.serve(missing=(measure.PO_OVERVIEW,))
        with mock.patch.object(measure, "WARM_REQUESTS", 2):
            code, text = self.run_main(base)

        self.assertEqual(code, measure.EXIT_UNMEASURABLE, text)
        self.assertIn(measure.PO_OVERVIEW, text)
        self.assertIn("404", text)
        self.assertNotIn("MEETS", text)
        self.assertNotIn("WITHOUT the poll", text)

    def test_a_selected_session_that_does_not_answer_is_unmeasurable(self) -> None:
        """The reviewer's second reproduction: `/po` lists a session whose JSON then 404s.

        The four requests would still have been made, and would still have produced numbers — but
        not the numbers this scenario is defined as, because nothing was being polled beside them.
        """
        self.cookie()
        base = self.serve(session_status=404)
        with mock.patch.object(measure, "WARM_REQUESTS", 2):
            code, text = self.run_main(base)

        self.assertEqual(code, measure.EXIT_UNMEASURABLE, text)
        self.assertIn("/po/api/sessions/", text)
        self.assertIn("404", text)
        self.assertNotIn("MEETS", text)

    def test_the_rounds_never_start_before_one_poll_has_succeeded(self) -> None:
        """An explicit readiness signal, not a started thread: the poll has to be in flight."""
        self.cookie()
        base = self.serve()
        with mock.patch.object(measure, "WARM_REQUESTS", 2):
            code, text = self.run_main(base)

        self.assertEqual(code, measure.EXIT_MET, text)
        seen = self.server.seen  # type: ignore[attr-defined]
        polls = [index for index, (_method, path) in enumerate(seen) if path.startswith("/po/api/")]
        concurrent = [
            index
            for index, (method, path) in enumerate(seen)
            if method == "GET" and path == "/" and index > polls[0]
        ]
        self.assertTrue(polls, "the selected session was never polled")
        self.assertTrue(concurrent, "no concurrent request followed the first poll")
        self.assertLess(polls[0], concurrent[0], "a round started before the first poll")
        # And the printed count is of successful polls only.
        count = re.search(r"polled successfully (\d+) time\(s\)", text)
        self.assertIsNotNone(count, text)
        self.assertGreaterEqual(int(count.group(1)), 1)

    # -- what it cannot measure --------------------------------------------------------------

    def test_an_unreachable_installation_exits_two_rather_than_reporting_a_number(self) -> None:
        code, text = self.run_main("http://127.0.0.1:1")

        self.assertEqual(code, measure.EXIT_UNMEASURABLE, text)
        self.assertIn("not reachable", text)
        self.assertNotIn("MEETS", text)

    def test_a_missing_route_exits_two_rather_than_timing_a_404(self) -> None:
        self.cookie()
        base = self.serve(missing=("/projects",))
        with mock.patch.object(measure, "WARM_REQUESTS", 2):
            code, text = self.run_main(base)

        self.assertEqual(code, measure.EXIT_UNMEASURABLE, text)
        self.assertIn("/projects", text)
        self.assertIn("404", text)


class MeasurementScriptDocumentationTests(unittest.TestCase):
    """The command an operator is told to run is the command that exists."""

    COMMAND: ClassVar[str] = "python3 scripts/measure_dashboard.py"

    def test_operations_names_the_command_and_the_three_places_the_durations_land(self) -> None:
        text = (REPO_ROOT / "docs" / "OPERATIONS.md").read_text(encoding="utf-8")

        self.assertIn(self.COMMAND, text)
        self.assertIn("journalctl -u secretary-web.service", text)
        self.assertIn("secretary status", text)

    def test_the_script_adds_no_dependency(self) -> None:
        """Criterion 9: standard library only, and nothing new in `pyproject.toml`."""
        source = SCRIPT.read_text(encoding="utf-8")
        imported = {
            (node.module or "").split(".")[0]
            for node in ast.walk(ast.parse(source, filename=str(SCRIPT)))
            if isinstance(node, ast.ImportFrom)
        } | {
            alias.name.split(".")[0]
            for node in ast.walk(ast.parse(source, filename=str(SCRIPT)))
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        # `secretary` is this repository, not a dependency: the PO cookie rule is imported from the
        # product rather than copied. Everything else has to be in the standard library.
        outside = imported - set(sys.stdlib_module_names) - {"secretary", ""}
        self.assertEqual(outside, set())


if __name__ == "__main__":
    unittest.main()
