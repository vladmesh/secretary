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
import json
import os
import sys
import tempfile
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
        status, body, delay, location = plan(self.command, path)
        if delay:
            time.sleep(delay)
        payload = body.encode("utf-8")
        self.send_response(status)
        if location:
            self.send_header("Location", location)
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


class AgainstAStubDashboard(unittest.TestCase):
    """The script against a dashboard whose answers this test decides.

    Everything here is the fixture: the stub, the run helper, and the patches that keep the case
    off the host it happens to be running on.
    """

    def serve(
        self,
        *,
        missing: tuple[str, ...] = (),
        delays: dict[str, float] | None = None,
        status_after: tuple[str, int, int] | None = None,
        redirect: tuple[str, str] | None = None,
    ) -> str:
        """A dashboard with a decided answer, and a decided cost, for every route.

        `status_after` is `(path, nth, status)`: that path's GETs answer cleanly until the nth one,
        and from then on answer `status`, which is a dashboard that breaks part-way through a run.

        `redirect` is `(path, location)`: that path answers 302 and points somewhere else, which is
        how a case models a front, a proxy or a wrong `--base-url` trying to send the script to
        another installation.
        """
        seen: list[tuple[str, str]] = []
        lock = threading.Lock()

        def plan(method: str, path: str) -> tuple[int, str, float, str]:
            with lock:
                seen.append((method, path))
                counted = sum(1 for verb, seen_path in seen if verb == "GET" and seen_path == path)
            delay = (delays or {}).get(path, 0.0)
            if redirect is not None and path == redirect[0]:
                return 302, "moved", delay, redirect[1]
            if status_after is not None:
                failing_path, nth, status = status_after
                if method == "GET" and path == failing_path and counted >= nth:
                    return status, "not an answer", delay, ""
            if path in missing:
                return 404, "no such route", delay, ""
            return 200, "<html>dashboard</html>", delay, ""

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

    def setUp(self) -> None:
        """No case in this suite may read the host it happens to be running on.

        `tests/README.md`: no test needs a running installation. That is enforced here, once, for
        every case in the class — not by each case remembering to ask for it. A case that forgets
        is no longer possible, and a case that somehow reaches past these patches finds an
        environment with no installation in it and fails the same way on every host instead of
        resolving whatever this one happens to have.

        It is not a hypothetical. `test_an_unreachable_installation_…` did not stub the resolution,
        passed on a developer host that has `~/secretary-instance`, and turned CI red on a runner
        that does not (run 35542829308, `unit`).
        """
        self.enterContext(mock.patch.dict(os.environ, {}, clear=False))
        os.environ.pop("SECRETARY_DATA_DIR", None)
        os.environ.pop("SECRETARY_INSTANCE", None)
        self.enterContext(mock.patch("secretary.onboarding.DEFAULT_INSTANCE", "/nonexistent/instance"))
        # What this resolves to for real is exercised in `DataDirectoryResolutionTests`.
        self.enterContext(
            mock.patch.object(
                measure, "resolve_data_dir", return_value=(Path("/nonexistent/data"), "a test fixture")
            )
        )

    def run_main(self, base_url: str, *extra: str) -> tuple[int, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = measure.main(["--base-url", base_url, *extra])
        return code, out.getvalue() + err.getvalue()


class MeasurementScriptTests(AgainstAStubDashboard):
    """Statuses, routes, percentiles, verdicts and refusals of the warm measurement.

    No case here depends on how fast this host is. The stub answers instantly unless a case gives a
    route a delay, and the one case that does gives it a delay above the threshold, which a sleep
    can only overshoot.
    """

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

    def test_response_validity_lives_in_fetch_and_no_call_site_re_implements_it(self) -> None:
        """The structural half of the rule: a new request path cannot bypass it.

        Three rounds of review found three different call sites of the same missing status check,
        because the rule lived at call sites. It now lives in `fetch`, and this test is what keeps
        it there: the only transport call in the file is inside `fetch`, the only `Sample` is built
        inside `fetch`, and no code outside it reads a status off a response. A seventh call site
        added next month is therefore a request that went through the rule, or a red test here.

        The transport is the module-level opener, and there is one of it, built with redirects
        refused and with an empty `ProxyHandler`. A second opener — or a plain `urlopen`, which
        follows redirects and reads the proxy environment — would be a request that could leave the
        installation the caller named, so neither may exist anywhere in this file.
        """
        tree = ast.parse(SCRIPT.read_text(encoding="utf-8"), filename=str(SCRIPT))
        fetch = next(
            node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "fetch"
        )
        inside_fetch = set(map(id, ast.walk(fetch)))

        def calls(name: str) -> list[ast.Call]:
            return [
                node
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and (
                    (isinstance(node.func, ast.Attribute) and node.func.attr == name)
                    or (isinstance(node.func, ast.Name) and node.func.id == name)
                )
            ]

        transport = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and ast.unparse(node.func).endswith(("urlopen", "_OPENER.open"))
        ]
        self.assertEqual(len(transport), 1, "a second transport call would be a request outside the rule")
        self.assertIn(id(transport[0]), inside_fetch, "the only request must be the one inside fetch")
        self.assertEqual(ast.unparse(transport[0].func), "_OPENER.open")

        # `urlopen` follows redirects and cannot be told not to, so the file may not name it at
        # all — not as a call, not as an imported name, not as an attribute of anything.
        named = [
            node
            for node in ast.walk(tree)
            if (isinstance(node, ast.Name) and node.id == "urlopen")
            or (isinstance(node, ast.Attribute) and node.attr == "urlopen")
            or (isinstance(node, ast.alias) and node.name.split(".")[-1] == "urlopen")
        ]
        self.assertEqual(named, [], "every request goes through the opener that refuses redirects")

        openers = calls("build_opener")
        self.assertEqual(len(openers), 1, "one opener, or the redirect rule is not the only rule")
        self.assertEqual(
            [ast.unparse(argument) for argument in openers[0].args],
            ["_RefuseRedirects()", "urllib.request.ProxyHandler({})"],
            "the one opener refuses redirects and reads no proxy environment",
        )

        built = calls("Sample")
        self.assertEqual(len(built), 1, "a Sample built elsewhere is a response nothing validated")
        self.assertIn(id(built[0]), inside_fetch)

        # `status` is read exactly where it is decided. Anywhere else is a call site quietly
        # making its own judgement about a response again.
        outside = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and node.attr in {"status", "ok"}
            and id(node) not in inside_fetch
        ]
        self.assertEqual(
            [ast.unparse(node) for node in outside],
            [],
            "response status is judged in fetch and nowhere else",
        )

    def test_every_request_the_run_makes_is_refused_when_it_does_not_answer(self) -> None:
        """The behavioural half: each route on the inventory, made to 404, must exit 2.

        Sweeping every route rather than the two the reviewer happened to reproduce, because the
        defect this rule closes was never about a particular route — it was about which call site
        remembered to look.
        """
        cases = {
            "/": "a warm route and the reachability probe",
            "/sprints": "a warm route",
            "/projects": "a warm route",
        }
        for route, why in cases.items():
            with self.subTest(route=route, why=why):
                base = self.serve(missing=(route,))
                with mock.patch.object(measure, "WARM_REQUESTS", 2):
                    code, text = self.run_main(base)
                self.assertEqual(code, measure.EXIT_UNMEASURABLE, text)
                self.assertIn("404", text)
                self.assertNotIn("MEETS", text)

    def test_a_run_asks_for_nothing_but_the_method_and_route_pairs_on_the_list(self) -> None:
        """Pairs, not paths: `HEAD /` must not pass because `GET /` happens to be listed."""
        base = self.serve()
        with mock.patch.object(measure, "WARM_REQUESTS", 2):
            self.run_main(base)

        listed = set(measure.READ_REQUESTS)
        observed = set(self.server.seen)  # type: ignore[attr-defined]
        self.assertEqual(observed - listed, set(), "a request was made that the inventory omits")
        # And the inventory is not padded either: every pair on it was actually asked for.
        self.assertEqual(listed - observed, set(), "the inventory lists a request the run never made")

    # -- the named installation, and nothing else --------------------------------------------

    def elsewhere(self) -> str:
        """A second installation on this host, which no request of a run may ever reach."""
        outside = self.serve()
        self.outside = self.server  # type: ignore[attr-defined]
        return outside

    def test_a_redirect_on_any_route_of_the_inventory_is_unmeasurable(self) -> None:
        """A 3xx anywhere is a route that did not answer, and is refused.

        A front that redirects `/` to a login page would otherwise be timed as if it were the
        dashboard. The redirect target is a second local server, and it is never spoken to at all.
        """
        for route in measure.WARM_ROUTES:
            with self.subTest(route=route):
                outside = self.elsewhere()
                base = self.serve(redirect=(route, f"{outside}/elsewhere"))
                with mock.patch.object(measure, "WARM_REQUESTS", 2):
                    code, report = self.run_main(base)
                self.assertEqual(code, measure.EXIT_UNMEASURABLE, report)
                self.assertIn("302", report)
                self.assertIn("redirect", report)
                self.assertNotIn("MEETS", report)
                self.assertEqual(self.outside.seen, [])  # type: ignore[attr-defined]

    def load_under(self, environment: dict[str, str]) -> ModuleType:
        """A fresh copy of the script, imported with `environment` already in place.

        The opener is built once, when the module is imported, and the standard library's
        `ProxyHandler` reads the proxy variables when it is *constructed*. A case that patched the
        environment after import would therefore pass no matter what the code did. This imports the
        script again with the variables already set, which is the operator's situation: a shell
        that had `http_proxy` in it before the command started.
        """
        name = f"measure_dashboard_under_{len(sys.modules)}"
        spec = importlib.util.spec_from_file_location(name, SCRIPT)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        self.addCleanup(sys.modules.pop, name, None)
        with mock.patch.dict(os.environ, environment, clear=False):
            spec.loader.exec_module(module)
        return module

    def test_a_proxy_in_the_environment_never_receives_the_request_or_the_cookie(self) -> None:
        """`build_opener` keeps the default `ProxyHandler` unless it is replaced.

        With `http_proxy` set, that handler sends every request — and any `Cookie` header on it —
        to the proxy, while the output still names the base URL. Here the second local server is
        the proxy, the script is imported with the variables already set, and the assertion is
        that the proxy is never spoken to at all while the installation answers normally.
        """
        proxy = self.elsewhere()
        base = self.serve()
        fresh = self.load_under(
            {"http_proxy": proxy, "HTTP_PROXY": proxy, "https_proxy": proxy, "all_proxy": proxy}
        )

        sample = fresh.fetch(base, "/sprints", cookie="secretary_po=probe")

        self.assertEqual(sample.status, 200)
        self.assertIn(("GET", "/sprints"), self.server.seen)  # type: ignore[attr-defined]
        self.assertEqual(
            self.outside.seen,  # type: ignore[attr-defined]
            [],
            "the proxy received the request, and the cookie with it",
        )

    def test_a_whole_run_under_a_proxy_environment_stays_on_the_named_installation(self) -> None:
        """The same door, end to end: the command completes, and the proxy log is still empty."""
        proxy = self.elsewhere()
        base = self.serve()
        fresh = self.load_under({"http_proxy": proxy, "HTTP_PROXY": proxy})

        with (
            mock.patch.object(
                fresh, "resolve_data_dir", return_value=(Path("/nonexistent/data"), "a test fixture")
            ),
            mock.patch.object(fresh, "WARM_REQUESTS", 2),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            code = fresh.main(["--base-url", base])

        self.assertEqual(code, fresh.EXIT_MET)
        self.assertEqual(self.outside.seen, [])  # type: ignore[attr-defined]

    def test_the_fetcher_reports_the_redirect_rather_than_the_page_behind_it(self) -> None:
        """`fetch` itself, with no run around it: the 3xx is the answer it saw."""
        outside = self.elsewhere()
        base = self.serve(redirect=("/", f"{outside}/elsewhere"))

        with self.assertRaises(measure.Unmeasurable) as refused:
            measure.fetch(base, "/", cookie="secretary_po=deadbeef")

        self.assertIn("302", str(refused.exception))
        self.assertEqual(self.outside.seen, [])  # type: ignore[attr-defined]
        # And not even an explicit tolerance can turn a redirect into a measurement.
        with self.assertRaises(measure.Unmeasurable):
            measure.fetch(base, "/", tolerate=frozenset({302}))
        self.assertEqual(self.outside.seen, [])  # type: ignore[attr-defined]

    # -- the numbers -------------------------------------------------------------------------

    def test_p95_is_the_nearest_rank_over_the_twenty_samples(self) -> None:
        values = [float(index) for index in range(1, measure.WARM_REQUESTS + 1)]
        # Nearest rank over twenty samples is the nineteenth: the second slowest request.
        self.assertEqual(measure.p95(values), 19.0)
        self.assertEqual(measure.p95(list(reversed(values))), 19.0)
        self.assertEqual(measure.p95([5.0]), 5.0)

    def test_a_fast_installation_meets_every_threshold_and_exits_zero(self) -> None:
        base = self.serve()
        with mock.patch.object(measure, "WARM_REQUESTS", 3):
            code, text = self.run_main(base)

        self.assertEqual(code, measure.EXIT_MET, text)
        self.assertNotIn("EXCEEDS", text)
        self.assertEqual(text.count("MEETS"), len(measure.WARM_ROUTES))
        self.assertIn(f"base URL: {base}", text)
        self.assertIn("threshold 1000 ms", text)
        self.assertIn(f"all {len(measure.WARM_ROUTES)} measurements are at or under their threshold", text)

    def test_a_slow_route_exits_one_and_still_prints_the_whole_table(self) -> None:
        # Just over the warm threshold on one page, and only on that page. A sleep can only
        # overshoot, so this case's verdict does not depend on how fast the host is.
        base = self.serve(delays={"/sprints": (measure.WARM_P95_THRESHOLD_MS + 200) / 1000.0})
        with mock.patch.object(measure, "WARM_REQUESTS", 3):
            code, text = self.run_main(base)

        self.assertEqual(code, measure.EXIT_EXCEEDED, text)
        self.assertIn("EXCEEDS", text)
        # Red prints as much as green: every route is still there.
        for route in measure.WARM_ROUTES:
            self.assertIn(f"warm p95 GET {route}", text)
        self.assertIn(f"1 of {len(measure.WARM_ROUTES)} measurements exceed their threshold", text)

    def test_every_request_of_a_warm_route_is_timed_and_the_warm_up_is_discarded(self) -> None:
        base = self.serve()
        with mock.patch.object(measure, "WARM_REQUESTS", 4):
            report = measure.run(base, None)

        self.assertEqual([warm["route"] for warm in report.warm], list(measure.WARM_ROUTES))
        self.assertEqual([warm["count"] for warm in report.warm], [4] * len(measure.WARM_ROUTES))
        gets = [path for method, path in self.server.seen if method == "GET"]  # type: ignore[attr-defined]
        for route in measure.WARM_ROUTES:
            self.assertEqual(gets.count(route), measure.WARMUP_REQUESTS + 4, route)

    # -- what it says it measures ------------------------------------------------------------

    def test_the_output_says_it_measures_the_warm_items_only(self) -> None:
        """One line, and nothing in the output that reads as a verdict on concurrency."""
        base = self.serve()
        with mock.patch.object(measure, "WARM_REQUESTS", 2):
            code, text = self.run_main(base)

        self.assertEqual(code, measure.EXIT_MET, text)
        self.assertEqual(text.count(measure.SCOPE_LINE), 1, text)
        self.assertIn("warm items only", measure.SCOPE_LINE)
        self.assertIn("four concurrent GET /", measure.SCOPE_LINE)
        self.assertIn("measured separately", measure.SCOPE_LINE)
        # The scope line is the only place concurrency is mentioned, and it carries no verdict.
        others = [line for line in text.splitlines() if line != measure.SCOPE_LINE]
        self.assertFalse([line for line in others if "concurren" in line.lower()], text)
        self.assertNotIn("threshold 2000 ms", text)
        self.assertNotRegex(measure.SCOPE_LINE, r"MEETS|EXCEEDS|JUDGED")

        with mock.patch.object(measure, "WARM_REQUESTS", 2):
            code, document = self.run_main(base, "--json")
        self.assertEqual(code, measure.EXIT_MET, document)
        parsed = json.loads(document)
        self.assertEqual(parsed["scope"], measure.SCOPE_LINE)
        self.assertEqual(
            [item["label"] for item in parsed["measurements"]],
            [f"warm p95 GET {route}" for route in measure.WARM_ROUTES],
        )

    def test_the_scope_line_is_printed_on_an_unmeasurable_run_too(self) -> None:
        base = self.serve(missing=("/projects",))
        with mock.patch.object(measure, "WARM_REQUESTS", 2):
            code, text = self.run_main(base)

        self.assertEqual(code, measure.EXIT_UNMEASURABLE, text)
        self.assertEqual(text.count(measure.SCOPE_LINE), 1, text)

    # -- what it cannot measure --------------------------------------------------------------

    def test_a_late_404_after_clean_warm_reads_exits_two_and_prints_them_unjudged(self) -> None:
        """A route that answers for its first reads and then stops: nothing is judged."""
        warm = 2
        base = self.serve(status_after=("/sprints", 1 + warm, 404))
        with mock.patch.object(measure, "WARM_REQUESTS", warm):
            code, text = self.run_main(base)

        self.assertEqual(code, measure.EXIT_UNMEASURABLE, text)
        self.assertIn("404", text)
        self.assertNotIn("MEETS", text)
        # The route before it did succeed, and is shown — with no verdict beside it.
        self.assertIn("warm p95 GET /", text)
        self.assertIn("NOT MEASURED", text)
        self.assertIn("NOT JUDGED", text)

    def test_a_late_contained_500_exits_two(self) -> None:
        """The same, with the transport's own contained 500."""
        warm = 2
        base = self.serve(status_after=("/", 1 + warm, 500))
        with mock.patch.object(measure, "WARM_REQUESTS", warm):
            code, text = self.run_main(base)

        self.assertEqual(code, measure.EXIT_UNMEASURABLE, text)
        self.assertIn("500", text)
        self.assertNotIn("MEETS", text)

    def test_an_unreachable_installation_exits_two_rather_than_reporting_a_number(self) -> None:
        code, text = self.run_main("http://127.0.0.1:1")

        self.assertEqual(code, measure.EXIT_UNMEASURABLE, text)
        self.assertIn("not reachable", text)
        self.assertNotIn("MEETS", text)

    def test_the_precondition_runs_before_any_clock_starts(self) -> None:
        """An unresolvable data directory refuses the run before a single request is made."""
        base = self.serve()
        with mock.patch.object(
            measure, "resolve_data_dir", side_effect=measure.Unmeasurable("no data directory")
        ):
            code, text = self.run_main(base)

        self.assertEqual(code, measure.EXIT_UNMEASURABLE, text)
        self.assertIn("no data directory", text)
        self.assertEqual(self.server.seen, [])  # type: ignore[attr-defined]

    def test_a_missing_route_exits_two_rather_than_timing_a_404(self) -> None:
        base = self.serve(missing=("/projects",))
        with mock.patch.object(measure, "WARM_REQUESTS", 2):
            code, text = self.run_main(base)

        self.assertEqual(code, measure.EXIT_UNMEASURABLE, text)
        self.assertIn("/projects", text)
        self.assertIn("404", text)


class DataDirectoryResolutionTests(unittest.TestCase):
    """Where the documented command finds the installation, with nothing in the environment.

    The reviewer ran `python3 scripts/measure_dashboard.py` in an ordinary checkout shell on this
    host. That shell does not inherit the service unit's `SECRETARY_DATA_DIR` or
    `SECRETARY_INSTANCE`, so the script resolved no data directory at all. It now falls through to
    the instance the CLI itself defaults to, read with the product's own resolution.
    """

    def instance(self) -> tuple[Path, Path]:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        data = root / "data"
        data.mkdir()
        instance = root / "instance"
        instance.mkdir()
        (instance / "instance.yaml").write_text(
            "version: 1\nname: test\n"
            f"data_dir: {data}\n"
            "offsite:\n  instance_remote: git@example.invalid:x/y.git\n",
            encoding="utf-8",
        )
        return instance, data

    def test_with_neither_variable_set_it_falls_through_to_the_default_instance(self) -> None:
        instance, data = self.instance()
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch("secretary.onboarding.DEFAULT_INSTANCE", str(instance)),
        ):
            resolved, source = measure.resolve_data_dir(None)

        self.assertEqual(resolved, data)
        self.assertIn("the default instance", source)
        self.assertIn(str(instance), source)

    def test_the_documented_order_is_the_argument_then_the_two_variables(self) -> None:
        instance, data = self.instance()
        with mock.patch.dict(os.environ, {"SECRETARY_INSTANCE": str(instance)}, clear=True):
            resolved, source = measure.resolve_data_dir(None)
            self.assertEqual(resolved, data)
            self.assertIn("SECRETARY_INSTANCE", source)

        with mock.patch.dict(os.environ, {"SECRETARY_DATA_DIR": "/from/env"}, clear=True):
            self.assertEqual(measure.resolve_data_dir(None), (Path("/from/env"), "SECRETARY_DATA_DIR"))
            # The argument still wins over both.
            self.assertEqual(measure.resolve_data_dir("/from/flag"), (Path("/from/flag"), "--data-dir"))

    def test_an_unresolvable_installation_is_refused_rather_than_silently_dropped(self) -> None:
        """`except Exception: return None` was the same quiet downgrade a third time."""
        with (
            mock.patch.dict(os.environ, {"SECRETARY_INSTANCE": "/nowhere/at/all"}, clear=True),
            self.assertRaises(measure.Unmeasurable) as refused,
        ):
            measure.resolve_data_dir(None)

        self.assertIn("/nowhere/at/all", str(refused.exception))
        self.assertIn("--data-dir", str(refused.exception))


class MeasurementScriptDocumentationTests(unittest.TestCase):
    """The command an operator is told to run is the command that exists."""

    COMMAND: ClassVar[str] = "python3 scripts/measure_dashboard.py"

    def test_operations_names_the_command_and_the_three_places_the_durations_land(self) -> None:
        text = (REPO_ROOT / "docs" / "OPERATIONS.md").read_text(encoding="utf-8")

        self.assertIn(self.COMMAND, text)
        self.assertIn("journalctl -u secretary-web.service", text)
        self.assertIn("secretary status", text)

    def test_operations_says_in_one_line_that_the_command_measures_the_warm_items_only(self) -> None:
        text = (REPO_ROOT / "docs" / "OPERATIONS.md").read_text(encoding="utf-8")
        section = text.partition("### Measuring the dashboard")[2].partition("\n## ")[0]

        self.assertTrue(section, "the Operations section is missing")
        scope = [line for line in section.splitlines() if "warm items only" in line]
        self.assertEqual(len(scope), 1, scope)
        self.assertIn("four concurrent", scope[0])
        self.assertIn("measured separately", scope[0])
        # No threshold, cadence or verdict of the concurrent item survives in the section.
        for absent in ("2.0 s", "2000", "cadence", "worst round", "PO token", "po-web-token"):
            self.assertNotIn(absent, section, absent)

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
        # `secretary` is this repository, not a dependency: the data directory rule is imported
        # from the product rather than copied. Everything else has to be in the standard library.
        outside = imported - set(sys.stdlib_module_names) - {"secretary", ""}
        self.assertEqual(outside, set())


if __name__ == "__main__":
    unittest.main()
