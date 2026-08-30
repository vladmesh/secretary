#!/usr/bin/env python3
"""Validate and run the explicit top-level unittest suites used by GitHub CI."""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import traceback
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import TextIO

SUITES = (
    "unit",
    "component",
    "runtime-component",
    "integration-recovery",
    "integration-memory",
    "integration-board",
    "packaging",
)

FAST_MODULES = (
    "tests.test_hermetic_kanboard",
    "tests.test_hermetic_orca",
    "tests.test_hermetic_pipeline_state",
)
FAST_TIMEOUT_SECONDS = 120
_FAST_TERMINATE_GRACE_SECONDS = 5
EVIDENCE_SCHEMA_VERSION = 1
MAX_LOG_BYTES = 1_000_000
SLOWEST_TEST_LIMIT = 10
EVIDENCE_FILES = ("report.json", "junit.xml", "test-output.log")
OUTCOMES = (
    "success",
    "product_failure",
    "infrastructure_failure",
    "cancelled",
    "not_applicable",
)

# Loaded automatically by the fast profile's child interpreter. The selected
# tests still exercise their normal seams, while this makes an accidental new
# network or host-tool dependency fail at its boundary rather than consulting
# a control host's operational state.
_FAST_GUARD = """\
import os
import socket
import subprocess
import sys


def _deny(kind):
    raise RuntimeError(f"fast test profile forbids {kind}")


class _NoNetworkSocket(socket.socket):
    def connect(self, address):
        _deny("network access")

    def connect_ex(self, address):
        _deny("network access")


socket.socket = _NoNetworkSocket
socket.create_connection = lambda *args, **kwargs: _deny("network access")
_popen = subprocess.Popen


def _guarded_popen(args, *positional, **keyword):
    command = args[0] if isinstance(args, (list, tuple)) else args
    if os.path.realpath(os.fspath(command)) != os.path.realpath(sys.executable):
        if not (
            os.path.basename(os.fspath(command)) == "git"
            and isinstance(args, (list, tuple))
            and any(action in args for action in ("log", "ls-files", "check-ignore"))
        ):
            _deny("external command execution")
    return _popen(args, *positional, **keyword)


subprocess.Popen = _guarded_popen
os.system = lambda *args, **kwargs: _deny("external command execution")
"""


class ManifestError(ValueError):
    pass


class EvidenceError(ValueError):
    pass


@dataclasses.dataclass
class TestRecord:
    identifier: str
    classname: str
    name: str
    duration_seconds: float
    outcome: str = "passed"
    detail: str | None = None


@dataclasses.dataclass
class SuiteEvidence:
    suite: str
    candidate_sha: str
    outcome: str
    counts: dict[str, int]
    duration_seconds: float
    slowest_tests: list[TestRecord]
    failure_locations: list[str]
    log_truncated: bool = False
    detail: str | None = None
    test_records: list[TestRecord] = dataclasses.field(default_factory=list, repr=False)

    def as_json(self) -> dict[str, object]:
        return {
            "schema_version": EVIDENCE_SCHEMA_VERSION,
            "suite": self.suite,
            "candidate_sha": self.candidate_sha,
            "outcome": self.outcome,
            "counts": self.counts,
            "duration_seconds": round(self.duration_seconds, 3),
            "slowest_tests": [dataclasses.asdict(record) for record in self.slowest_tests],
            "failure_locations": self.failure_locations,
            "log_truncated": self.log_truncated,
            "detail": self.detail,
            "artifacts": {"junit": "junit.xml", "log": "test-output.log"},
        }


class BoundedTee:
    """Copy test output to Actions while retaining one bounded artifact log."""

    def __init__(self, console: TextIO, path: Path, maximum_bytes: int = MAX_LOG_BYTES) -> None:
        self.console = console
        self.path = path
        self.maximum_bytes = maximum_bytes
        self.written_bytes = 0
        self.truncated = False
        self._log = path.open("w", encoding="utf-8")

    def write(self, value: str) -> int:
        self.console.write(value)
        encoded = value.encode("utf-8")
        remaining = self.maximum_bytes - self.written_bytes
        if remaining > 0:
            clipped = encoded[:remaining]
            self._log.write(clipped.decode("utf-8", errors="ignore"))
            self.written_bytes += len(clipped)
        if len(encoded) > max(remaining, 0):
            self.truncated = True
        return len(value)

    def flush(self) -> None:
        self.console.flush()
        self._log.flush()

    def close(self) -> None:
        if self.truncated:
            self._log.write("\n[CI evidence log truncated at 1000000 bytes]\n")
        self._log.close()


class EvidenceResult(unittest.TextTestResult):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.records: dict[str, TestRecord] = {}
        self._started: dict[str, float] = {}

    def startTest(self, test: unittest.case.TestCase) -> None:
        identifier = test.id()
        self._started[identifier] = time.monotonic()
        super().startTest(test)

    def stopTest(self, test: unittest.case.TestCase) -> None:
        identifier = test.id()
        duration = time.monotonic() - self._started.pop(identifier, time.monotonic())
        record = self.records.get(identifier)
        if record is None:
            classname, _, name = identifier.rpartition(".")
            record = TestRecord(identifier, classname, name, duration)
            self.records[identifier] = record
        else:
            record.duration_seconds = duration
        super().stopTest(test)

    def _mark(self, test: unittest.case.TestCase, outcome: str, detail: str | None = None) -> None:
        record = self.records.get(test.id())
        if record is None:
            classname, _, name = test.id().rpartition(".")
            record = TestRecord(test.id(), classname, name, 0.0)
            self.records[test.id()] = record
        record.outcome = outcome
        record.detail = detail

    def addFailure(self, test: unittest.case.TestCase, err: object) -> None:
        detail = self._exc_info_to_string(err, test)
        self._mark(test, "failed", detail)
        super().addFailure(test, err)

    def addError(self, test: unittest.case.TestCase, err: object) -> None:
        detail = self._exc_info_to_string(err, test)
        self._mark(test, "error", detail)
        super().addError(test, err)

    def addSkip(self, test: unittest.case.TestCase, reason: str) -> None:
        self._mark(test, "skipped", reason)
        super().addSkip(test, reason)

    def addExpectedFailure(self, test: unittest.case.TestCase, err: object) -> None:
        self._mark(test, "failed", self._exc_info_to_string(err, test))
        super().addExpectedFailure(test, err)

    def addUnexpectedSuccess(self, test: unittest.case.TestCase) -> None:
        self._mark(test, "failed", "unexpected success")
        super().addUnexpectedSuccess(test)


def _failure_location(identifier: str, detail: str | None) -> str:
    if detail:
        locations = re.findall(r'File "([^\"]+)", line (\d+)', detail)
        if locations:
            path, line = locations[-1]
            return f"{identifier}: {path}:{line}"
    return identifier


def _counts(result: EvidenceResult) -> dict[str, int]:
    failed = len(result.failures) + len(result.expectedFailures) + len(result.unexpectedSuccesses)
    errors = len(result.errors)
    skipped = len(result.skipped)
    return {
        "collected": result.testsRun,
        "passed": max(result.testsRun - failed - errors - skipped, 0),
        "failed": failed,
        "error": errors,
        "skipped": skipped,
    }


def _empty_counts() -> dict[str, int]:
    return {"collected": 0, "passed": 0, "failed": 0, "error": 0, "skipped": 0}


def run_reported_suite(
    suite_name: str, paths: list[str], candidate_sha: str, log: BoundedTee
) -> SuiteEvidence:
    """Run one manifest suite and retain the unittest facts needed by CI evidence."""
    started = time.monotonic()
    loader = unittest.defaultTestLoader
    try:
        selected = unittest.TestSuite(loader.loadTestsFromName(module) for module in modules(paths))
        runner = unittest.TextTestRunner(stream=log, verbosity=2, resultclass=EvidenceResult)
        with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
            result = runner.run(selected)
    except KeyboardInterrupt:
        return SuiteEvidence(
            suite_name,
            candidate_sha,
            "cancelled",
            _empty_counts(),
            time.monotonic() - started,
            [],
            ["test execution cancelled"],
        )
    except Exception as exc:  # noqa: BLE001 - this boundary must report unexpected runner failures.
        traceback.print_exc(file=log)
        return SuiteEvidence(
            suite_name,
            candidate_sha,
            "infrastructure_failure",
            _empty_counts(),
            time.monotonic() - started,
            [],
            [f"test runner error: {type(exc).__name__}"],
            detail=str(exc),
        )

    records = list(result.records.values())
    failures = [record for record in records if record.outcome in {"failed", "error"}]
    counts = _counts(result)
    return SuiteEvidence(
        suite_name,
        candidate_sha,
        "success" if result.wasSuccessful() else "product_failure",
        counts,
        time.monotonic() - started,
        sorted(records, key=lambda record: record.duration_seconds, reverse=True)[:SLOWEST_TEST_LIMIT],
        [_failure_location(record.identifier, record.detail) for record in failures],
        test_records=records,
    )


def _write_junit(path: Path, evidence: SuiteEvidence) -> None:
    testsuite = ET.Element(
        "testsuite",
        {
            "name": evidence.suite,
            "tests": str(evidence.counts["collected"]),
            "failures": str(evidence.counts["failed"]),
            "errors": str(evidence.counts["error"]),
            "skipped": str(evidence.counts["skipped"]),
            "time": f"{evidence.duration_seconds:.3f}",
            "hostname": evidence.candidate_sha,
        },
    )
    for record in evidence.test_records:
        case = ET.SubElement(
            testsuite,
            "testcase",
            {"classname": record.classname, "name": record.name, "time": f"{record.duration_seconds:.3f}"},
        )
        if record.outcome == "skipped":
            ET.SubElement(case, "skipped", {"message": record.detail or "skipped"})
        elif record.outcome in {"failed", "error"}:
            node = ET.SubElement(case, "failure" if record.outcome == "failed" else "error")
            node.text = record.detail or record.outcome
    ET.ElementTree(testsuite).write(path, encoding="utf-8", xml_declaration=True)


def _write_evidence(report_dir: Path, evidence: SuiteEvidence, log: BoundedTee) -> None:
    report_dir.mkdir(parents=True, exist_ok=True)
    log.close()
    evidence.log_truncated = log.truncated
    _write_junit(report_dir / "junit.xml", evidence)
    (report_dir / "report.json").write_text(
        json.dumps(evidence.as_json(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _read_evidence(report_dir: Path) -> SuiteEvidence:
    required = {name: report_dir / name for name in EVIDENCE_FILES}
    missing = [name for name, path in required.items() if not path.is_file() or path.stat().st_size == 0]
    if missing:
        raise EvidenceError(f"missing required evidence: {', '.join(missing)}")
    try:
        data = json.loads(required["report.json"].read_text(encoding="utf-8"))
        if data.get("schema_version") != EVIDENCE_SCHEMA_VERSION:
            raise EvidenceError("unsupported report schema")
        if data.get("outcome") not in OUTCOMES:
            raise EvidenceError("invalid report outcome")
        counts = data["counts"]
        if set(counts) != {"collected", "passed", "failed", "error", "skipped"}:
            raise EvidenceError("invalid report counts")
        if not data.get("suite") or not re.fullmatch(r"[0-9a-f]{40,64}", data.get("candidate_sha", "")):
            raise EvidenceError("report lacks an exact candidate SHA")
        ET.parse(required["junit.xml"])
    except (KeyError, TypeError, ValueError, ET.ParseError) as exc:
        raise EvidenceError(f"malformed evidence: {exc}") from exc
    return SuiteEvidence(
        data["suite"],
        data["candidate_sha"],
        data["outcome"],
        counts,
        float(data["duration_seconds"]),
        [TestRecord(**record) for record in data.get("slowest_tests", [])],
        list(data.get("failure_locations", [])),
        bool(data.get("log_truncated")),
        data.get("detail"),
    )


def _summary(evidence: SuiteEvidence) -> str:
    counts = evidence.counts
    lines = [
        f"## CI suite: {evidence.suite}",
        "",
        f"- Candidate SHA: `{evidence.candidate_sha}`",
        f"- Outcome: `{evidence.outcome}`",
        f"- Counts: collected {counts['collected']}, passed {counts['passed']}, failed {counts['failed']}, errors {counts['error']}, skipped {counts['skipped']}",
        f"- Duration: {evidence.duration_seconds:.3f}s",
        f"- Artifacts: `junit.xml`, `test-output.log`{' (truncated at 1,000,000 bytes)' if evidence.log_truncated else ''}",
    ]
    if evidence.failure_locations:
        lines.extend(
            ["- Failure locations:", *[f"  - `{location}`" for location in evidence.failure_locations]]
        )
    if evidence.slowest_tests:
        lines.extend(
            [
                "- Slowest tests:",
                *[
                    f"  - `{record.identifier}` ({record.duration_seconds:.3f}s)"
                    for record in evidence.slowest_tests
                ],
            ]
        )
    if evidence.detail:
        lines.append(f"- Detail: {evidence.detail}")
    return "\n".join(lines) + "\n"


def _outcome_code(outcome: str) -> int:
    return {
        "success": 0,
        "not_applicable": 0,
        "product_failure": 1,
        "infrastructure_failure": 3,
        "cancelled": 130,
    }[outcome]


def report_summary(report_dir: Path) -> int:
    try:
        evidence = _read_evidence(report_dir)
    except EvidenceError as exc:
        print(f"## CI suite evidence infrastructure failure\n\n- {exc}")
        return 3
    print(_summary(evidence), end="")
    return _outcome_code(evidence.outcome)


def aggregate_evidence(evidence_dir: Path, needs_result: str) -> int:
    reports: dict[str, SuiteEvidence] = {}
    for path in evidence_dir.rglob("report.json") if evidence_dir.is_dir() else ():
        try:
            evidence = _read_evidence(path.parent)
        except EvidenceError:
            continue
        reports[evidence.suite] = evidence
    outcomes: dict[str, str] = {}
    for suite in SUITES:
        evidence = reports.get(suite)
        if evidence:
            outcomes[suite] = evidence.outcome
        elif needs_result == "cancelled":
            outcomes[suite] = "cancelled"
        elif needs_result == "skipped":
            outcomes[suite] = "not_applicable"
        else:
            outcomes[suite] = "infrastructure_failure"
    lines = ["## Required CI test aggregate", "", *[f"- `{suite}`: `{outcomes[suite]}`" for suite in SUITES]]
    print("\n".join(lines))
    if "infrastructure_failure" in outcomes.values():
        return 3
    if "product_failure" in outcomes.values():
        return 1
    if "cancelled" in outcomes.values():
        return 130
    return 0


def load_manifest(root: Path, manifest: Path | None = None) -> dict[str, list[str]]:
    manifest = manifest or root / "tests" / "ci-shards.txt"
    grouped = {name: [] for name in SUITES}
    owners: dict[str, str] = {}
    for number, raw in enumerate(manifest.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) != 2:
            raise ManifestError(f"{manifest}:{number}: expected '<suite> <test-file>'")
        suite, relative = parts
        if suite not in grouped:
            raise ManifestError(f"{manifest}:{number}: unknown suite {suite!r}")
        if not relative.startswith("tests/test_") or not relative.endswith(".py"):
            raise ManifestError(f"{manifest}:{number}: invalid top-level test path {relative!r}")
        if relative in owners:
            raise ManifestError(f"{manifest}:{number}: {relative} already belongs to {owners[relative]!r}")
        owners[relative] = suite
        grouped[suite].append(relative)

    discovered = {path.relative_to(root).as_posix() for path in (root / "tests").glob("test_*.py")}
    declared = set(owners)
    missing = sorted(discovered - declared)
    stale = sorted(declared - discovered)
    empty = [name for name, paths in grouped.items() if not paths]
    problems = []
    if missing:
        problems.append(f"unclaimed tests: {', '.join(missing)}")
    if stale:
        problems.append(f"missing declared tests: {', '.join(stale)}")
    if empty:
        problems.append(f"empty suites: {', '.join(empty)}")
    if problems:
        raise ManifestError("; ".join(problems))
    return grouped


def modules(paths: list[str]) -> list[str]:
    return [path.removesuffix(".py").replace("/", ".") for path in paths]


def validate_fast_profile(root: Path) -> None:
    if len(FAST_MODULES) != len(set(FAST_MODULES)):
        raise ManifestError("fast profile contains duplicate modules")
    if not FAST_MODULES:
        raise ManifestError("fast profile is empty")
    for module in FAST_MODULES:
        if not module.startswith("tests.test_"):
            raise ManifestError(f"fast profile has invalid test module {module!r}")
        path = root / (module.replace(".", "/") + ".py")
        if not path.is_file():
            raise ManifestError(f"fast profile names missing test module {module!r}")


def fast_environment(root: Path, fixture_root: Path) -> dict[str, str]:
    """Build the only environment the bounded hermetic child may inherit."""
    home = fixture_root / "home"
    guard = fixture_root / "guard"
    paths = {
        "HOME": home,
        "TA_CODEX_HOME": fixture_root / "codex-home",
        "TA_PIPELINE_STATE_DIR": fixture_root / "pipeline-state",
        "TEMP": fixture_root / "tmp",
        "TMP": fixture_root / "tmp",
        "TMPDIR": fixture_root / "tmp",
        "XDG_CACHE_HOME": fixture_root / "cache",
        "XDG_CONFIG_HOME": fixture_root / "config",
        "XDG_DATA_HOME": fixture_root / "data",
    }
    for path in (*paths.values(), guard):
        path.mkdir(parents=True, exist_ok=True)
    (guard / "sitecustomize.py").write_text(_FAST_GUARD, encoding="utf-8")
    return {
        **{name: str(path) for name, path in paths.items()},
        "PATH": os.defpath,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONPATH": os.pathsep.join((str(guard), str(root), str(root / "src"))),
        "TA_SECRETARY_REPO": str(root),
    }


def _terminate_fast_child(child: subprocess.Popen[object]) -> None:
    try:
        os.killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        child.wait(timeout=_FAST_TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait()


def run_bounded(
    command: list[str], *, root: Path, environment: dict[str, str], timeout_seconds: float
) -> int:
    child = subprocess.Popen(command, cwd=root, env=environment, start_new_session=True)
    try:
        return child.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        _terminate_fast_child(child)
        print(
            f"Fast test profile timed out after {timeout_seconds:g} seconds; test child was stopped.",
            file=sys.stderr,
        )
        return 124


def run_fast(root: Path) -> int:
    try:
        validate_fast_profile(root)
    except ManifestError as exc:
        print(f"Fast test profile is invalid: {exc}", file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory(prefix="secretary-fast-tests.") as temporary:
        environment = fast_environment(root, Path(temporary))
        return run_bounded(
            [sys.executable, "-P", "-m", "unittest", "-v", *FAST_MODULES],
            root=root,
            environment=environment,
            timeout_seconds=FAST_TIMEOUT_SECONDS,
        )


def run_suite_with_evidence(root: Path, suite_name: str, report_dir: Path, candidate_sha: str) -> int:
    """Make all three required suite artifacts or return an infrastructure result."""
    try:
        report_dir.mkdir(parents=True, exist_ok=True)
        log = BoundedTee(sys.stdout, report_dir / "test-output.log")
    except OSError as exc:
        print(f"CI evidence infrastructure failure: cannot create {report_dir}: {exc}", file=sys.stderr)
        return 3
    started = time.monotonic()
    if not re.fullmatch(r"[0-9a-f]{40,64}", candidate_sha):
        print("CI evidence infrastructure failure: candidate SHA is not exact", file=log)
        evidence = SuiteEvidence(
            suite_name,
            candidate_sha,
            "infrastructure_failure",
            _empty_counts(),
            time.monotonic() - started,
            [],
            ["candidate SHA is not exact"],
        )
    else:
        try:
            grouped = load_manifest(root)
        except (OSError, ManifestError) as exc:
            print(f"CI test manifest is invalid: {exc}", file=log)
            evidence = SuiteEvidence(
                suite_name,
                candidate_sha,
                "infrastructure_failure",
                _empty_counts(),
                time.monotonic() - started,
                [],
                ["CI test manifest is invalid"],
                detail=str(exc),
            )
        else:
            sys.path.insert(0, str(root))
            try:
                evidence = run_reported_suite(suite_name, grouped[suite_name], candidate_sha, log)
            finally:
                sys.path.pop(0)
    try:
        _write_evidence(report_dir, evidence, log)
    except OSError as exc:
        print(f"CI evidence infrastructure failure: cannot write required report: {exc}", file=sys.stderr)
        return 3
    return _outcome_code(evidence.outcome)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--check", action="store_true", help="validate and print the manifest")
    action.add_argument("--suite", choices=SUITES, help="validate, then run one CI suite")
    action.add_argument("--fast", action="store_true", help="run the bounded hermetic control-host profile")
    action.add_argument("--summary", action="store_true", help="validate evidence and print a step summary")
    action.add_argument("--aggregate", action="store_true", help="summarize all downloaded suite evidence")
    parser.add_argument("--report-dir", type=Path, help="directory containing one suite's CI evidence")
    parser.add_argument("--candidate-sha", help="exact candidate SHA recorded in suite evidence")
    parser.add_argument("--evidence-dir", type=Path, help="download directory containing suite artifacts")
    parser.add_argument(
        "--needs-result",
        choices=("success", "failure", "cancelled", "skipped"),
        help="GitHub matrix result used to classify unavailable suite evidence",
    )
    args = parser.parse_args(argv)

    root = Path(__file__).resolve().parents[1]
    if args.summary:
        if args.report_dir is None:
            parser.error("--summary requires --report-dir")
        return report_summary(args.report_dir)
    if args.aggregate:
        if args.evidence_dir is None or args.needs_result is None:
            parser.error("--aggregate requires --evidence-dir and --needs-result")
        return aggregate_evidence(args.evidence_dir, args.needs_result)
    if args.fast:
        return run_fast(root)

    if args.suite is not None and (args.report_dir is not None or args.candidate_sha is not None):
        if args.report_dir is None or args.candidate_sha is None:
            parser.error("reported suite execution requires --report-dir and --candidate-sha")
        return run_suite_with_evidence(root, args.suite, args.report_dir, args.candidate_sha)

    try:
        grouped = load_manifest(root)
    except (OSError, ManifestError) as exc:
        print(f"CI test manifest is invalid: {exc}", file=sys.stderr)
        return 2

    if args.check:
        print(json.dumps({name: len(paths) for name, paths in grouped.items()}, sort_keys=True))
        return 0
    assert args.suite is not None
    return subprocess.call([sys.executable, "-m", "unittest", "-v", *modules(grouped[args.suite])], cwd=root)


if __name__ == "__main__":
    raise SystemExit(main())
