"""Read-only host inventory for ``doctor --dry-run --host``.

This compares what an instance config describes against what is actually on the
host across three resource kinds: project repos, systemd units and Orca repo
registrations. It never changes the host and never reads config values or
secrets. Every source here only lists resource *names*; env files and secret
material are never opened.
"""

from __future__ import annotations

import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

KINDS = ("projects", "units", "orca repos")


@dataclass(frozen=True)
class HostInventory:
    """The set of resource names actually present on the host."""

    projects: set[str] = field(default_factory=set)
    units: set[str] = field(default_factory=set)
    orca_repos: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class Expectations:
    """Resource names an instance config says it owns."""

    projects: set[str] = field(default_factory=set)
    units: set[str] = field(default_factory=set)
    orca_repos: set[str] = field(default_factory=set)
    unit_prefix: str = ""
    projects_root: str = ""


@dataclass(frozen=True)
class KindDiff:
    """Three-way comparison for one resource kind. Sorted, name-only."""

    matched: list[str]
    missing_on_host: list[str]
    unmanaged_on_host: list[str]


def _project_name(binding: dict[str, Any]) -> str:
    """Pick the host-facing name of a project binding.

    A repo given as a path maps to a directory (and Orca repo) named after its
    last path segment; otherwise the stable ``id`` is used.
    """
    repo = binding.get("repo")
    if isinstance(repo, str) and "/" in repo:
        name = PurePosixPath(repo).name
        if name.endswith(".git"):
            name = name[: -len(".git")]
        if name:
            return name
    identifier = binding.get("id")
    return identifier if isinstance(identifier, str) else ""


def _str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def build_expectations(bindings: Iterable[dict[str, Any]], host: dict[str, Any]) -> Expectations:
    """Derive expected resource names from bindings and the instance ``host`` block."""
    host = host if isinstance(host, dict) else {}
    projects = {name for name in (_project_name(b) for b in bindings) if name}
    return Expectations(
        projects=projects,
        units=set(_str_list(host.get("units"))),
        orca_repos=set(_str_list(host.get("orca_repos"))),
        unit_prefix=host.get("unit_prefix", "") if isinstance(host.get("unit_prefix"), str) else "",
        projects_root=host.get("projects_root", "") if isinstance(host.get("projects_root"), str) else "",
    )


def _diff(expected: set[str], actual: set[str]) -> KindDiff:
    return KindDiff(
        matched=sorted(expected & actual),
        missing_on_host=sorted(expected - actual),
        unmanaged_on_host=sorted(actual - expected),
    )


def inventory(expected: Expectations, actual: HostInventory) -> dict[str, KindDiff]:
    """Compare expectations against a host inventory, one KindDiff per kind."""
    return {
        "projects": _diff(expected.projects, actual.projects),
        "units": _diff(expected.units, actual.units),
        "orca repos": _diff(expected.orca_repos, actual.orca_repos),
    }


class HostSource(ABC):
    """Something that can enumerate host resources without changing them."""

    @abstractmethod
    def collect(self, expected: Expectations) -> HostInventory:
        ...


def _names_from_dir(directory: Path) -> set[str]:
    if not directory.is_dir():
        return set()
    return {entry.name for entry in directory.iterdir() if entry.is_dir()}


class FixtureHostSource(HostSource):
    """A host modelled by a fixture directory. Used by tests and offline checks.

    Layout under ``root``::

        projects/<name>/     one directory per project repo on the host
        units.txt            one systemd unit name per line
        orca-repos.txt       one Orca repo name per line

    Reads only. Missing files mean an empty set for that kind.
    """

    def __init__(self, root: Path):
        self.root = root

    def _lines(self, name: str) -> set[str]:
        path = self.root / name
        if not path.is_file():
            return set()
        names: set[str] = set()
        for line in path.read_text(encoding="utf-8").splitlines():
            token = line.strip()
            if token and not token.startswith("#"):
                names.add(token)
        return names

    def collect(self, expected: Expectations) -> HostInventory:
        return HostInventory(
            projects=_names_from_dir(self.root / "projects"),
            units=self._lines("units.txt"),
            orca_repos=self._lines("orca-repos.txt"),
        )


class LiveHostSource(HostSource):
    """The real host: the projects directory, systemd and Orca, all read-only.

    Enumeration failures (a tool missing, a directory absent) degrade to an
    empty set for that kind rather than raising, so doctor still reports the
    kinds it could read.
    """

    def collect(self, expected: Expectations) -> HostInventory:
        return HostInventory(
            projects=self._projects(expected),
            units=self._units(expected),
            orca_repos=self._orca_repos(),
        )

    def _projects(self, expected: Expectations) -> set[str]:
        if not expected.projects_root:
            return set()
        return _names_from_dir(Path(expected.projects_root))

    def _units(self, expected: Expectations) -> set[str]:
        prefix = expected.unit_prefix
        if not prefix:
            # Without a namespace we cannot tell which host units are ours, so
            # scope to the declared names and report only presence.
            return {u for u in expected.units if self._unit_exists(u)}
        out = self._run(["systemctl", "list-unit-files", "--no-legend", f"{prefix}*"])
        names: set[str] = set()
        for line in out.splitlines():
            token = line.split()[0] if line.split() else ""
            if token.startswith(prefix):
                names.add(token)
        return names

    def _unit_exists(self, unit: str) -> bool:
        out = self._run(["systemctl", "list-unit-files", "--no-legend", unit])
        return bool(out.strip())

    def _orca_repos(self) -> set[str]:
        out = self._run(["orca", "repo", "list"])
        names: set[str] = set()
        for line in out.splitlines():
            parts = line.split()
            # Format: <uuid> <name> <path>. The name is the second column.
            if len(parts) >= 2:
                names.add(parts[1])
        return names

    @staticmethod
    def _run(cmd: list[str]) -> str:
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
            )
        except (OSError, ValueError):
            return ""
        return result.stdout or ""
