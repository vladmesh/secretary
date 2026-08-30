#!/usr/bin/env python3
"""Validate and run the explicit top-level unittest suites used by GitHub CI."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

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
            raise ManifestError(
                f"{manifest}:{number}: {relative} already belongs to {owners[relative]!r}"
            )
        owners[relative] = suite
        grouped[suite].append(relative)

    discovered = {
        path.relative_to(root).as_posix() for path in (root / "tests").glob("test_*.py")
    }
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--check", action="store_true", help="validate and print the manifest")
    action.add_argument("--suite", choices=SUITES, help="validate, then run one CI suite")
    action.add_argument("--fast", action="store_true", help="run the bounded hermetic control-host profile")
    args = parser.parse_args(argv)

    root = Path(__file__).resolve().parents[1]
    if args.fast:
        return run_fast(root)

    try:
        grouped = load_manifest(root)
    except (OSError, ManifestError) as exc:
        print(f"CI test manifest is invalid: {exc}", file=sys.stderr)
        return 2

    if args.check:
        print(json.dumps({name: len(paths) for name, paths in grouped.items()}, sort_keys=True))
        return 0
    assert args.suite is not None
    return subprocess.call(
        [sys.executable, "-m", "unittest", "-v", *modules(grouped[args.suite])], cwd=root
    )


if __name__ == "__main__":
    raise SystemExit(main())
