#!/usr/bin/env python3
"""Validate and run the explicit top-level unittest suites used by GitHub CI."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--check", action="store_true", help="validate and print the manifest")
    action.add_argument("--suite", choices=SUITES, help="validate, then run one CI suite")
    args = parser.parse_args(argv)

    root = Path(__file__).resolve().parents[1]
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
