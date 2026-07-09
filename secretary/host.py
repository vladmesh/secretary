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


@dataclass(frozen=True)
class CollectResult:
    """What a source found on the host, plus why any kind could not be read.

    ``errors`` maps a kind ("projects" / "units" / "orca repos") to a reason
    when that kind could not be inspected. An unreadable kind is never reported
    as an empty host: doctor marks it unavailable instead of comparing against
    an empty set, so "could not inspect" never masquerades as "nothing there".
    """

    inventory: HostInventory
    errors: dict[str, str] = field(default_factory=dict)


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
    def collect(self, expected: Expectations) -> CollectResult:
        ...


def _names_from_dir(directory: Path) -> set[str]:
    return {entry.name for entry in directory.iterdir() if entry.is_dir()}


class FixtureHostSource(HostSource):
    """A host modelled by a fixture directory. Used by tests and offline checks.

    Layout under ``root``::

        projects/<name>/     one directory per project repo on the host
        units.txt            one systemd unit name per line
        orca-repos.txt       one Orca repo name per line

    Reads only. A missing file means an empty set for that kind, since the
    fixture is authored deliberately and absence is not an inspection failure.
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

    def _projects(self) -> set[str]:
        projects_dir = self.root / "projects"
        if not projects_dir.is_dir():
            return set()
        return _names_from_dir(projects_dir)

    def collect(self, expected: Expectations) -> CollectResult:
        return CollectResult(
            inventory=HostInventory(
                projects=self._projects(),
                units=self._lines("units.txt"),
                orca_repos=self._lines("orca-repos.txt"),
            )
        )


@dataclass(frozen=True)
class _CmdResult:
    """Outcome of one host probe.

    ``ran`` is False only when the process could not execute at all (missing
    binary, timeout, OS error); ``reason`` is set then. When ``ran`` is True the
    caller interprets ``returncode``/``stderr`` itself, because a non-zero exit
    is not always a failure (``systemctl list-unit-files`` exits 1 on no match).
    """

    ran: bool
    returncode: int
    stdout: str
    stderr: str
    reason: str = ""


class LiveHostSource(HostSource):
    """The real host: the projects directory, systemd and Orca, all read-only.

    Inspecting a kind can fail: a tool is missing, exits non-zero, hangs, or a
    directory is unreadable. Such a failure is recorded per kind in the
    CollectResult rather than silently turning into an empty set, so doctor can
    tell the operator "could not inspect" instead of a false "nothing there".
    """

    # Cap each host probe so a hung systemctl or orca cannot wedge doctor.
    timeout_seconds = 10

    def collect(self, expected: Expectations) -> CollectResult:
        inventory = HostInventory()
        errors: dict[str, str] = {}

        projects, reason = self._projects(expected)
        if reason:
            errors["projects"] = reason
        else:
            inventory = HostInventory(projects, inventory.units, inventory.orca_repos)

        units, reason = self._units(expected)
        if reason:
            errors["units"] = reason
        else:
            inventory = HostInventory(inventory.projects, units, inventory.orca_repos)

        repos, reason = self._orca_repos()
        if reason:
            errors["orca repos"] = reason
        else:
            inventory = HostInventory(inventory.projects, inventory.units, repos)

        return CollectResult(inventory=inventory, errors=errors)

    def _projects(self, expected: Expectations) -> tuple[set[str], str]:
        root = expected.projects_root
        if not root:
            # No declared projects to place means nothing to inspect; declared
            # projects with no root is an inspection gap, not an empty host.
            if expected.projects:
                return set(), "host.projects_root not set"
            return set(), ""
        path = Path(root)
        if not path.is_dir():
            # Never echo the configured value: it comes from private instance
            # config and could carry a secret-like path. Name the field only.
            return set(), "host.projects_root is not a directory"
        try:
            return _names_from_dir(path), ""
        except OSError:
            return set(), "host.projects_root is not readable"

    def _units(self, expected: Expectations) -> tuple[set[str], str]:
        prefix = expected.unit_prefix
        if not prefix:
            if not expected.units:
                return set(), ""
            # Without a namespace we can only check the declared units exist.
            names: set[str] = set()
            for unit in expected.units:
                result = self._run(["systemctl", "list-unit-files", "--no-legend", unit])
                reason = self._systemctl_error(result)
                if reason:
                    return set(), reason
                if result.stdout.strip():
                    names.add(unit)
            return names, ""
        result = self._run(["systemctl", "list-unit-files", "--no-legend", f"{prefix}*"])
        reason = self._systemctl_error(result)
        if reason:
            return set(), reason
        names = set()
        for line in result.stdout.splitlines():
            fields = line.split()
            token = fields[0] if fields else ""
            if token.startswith(prefix):
                names.add(token)
        return names, ""

    @staticmethod
    def _systemctl_error(result: _CmdResult) -> str:
        """Real failure vs. an empty match.

        ``list-unit-files`` exits 1 with no stderr when a pattern matches no
        units. That is a legitimately empty result, not an inspection failure,
        so only a failed exec or a non-empty stderr counts as an error.
        """
        if not result.ran:
            return result.reason
        if result.returncode != 0 and result.stderr.strip():
            return f"systemctl exited {result.returncode}"
        return ""

    def _orca_repos(self) -> tuple[set[str], str]:
        result = self._run(["orca", "repo", "list"])
        if not result.ran:
            return set(), result.reason
        if result.returncode != 0:
            # orca reports an empty registry as exit 0, so non-zero is a failure.
            return set(), f"orca exited {result.returncode}"
        names: set[str] = set()
        for line in result.stdout.splitlines():
            parts = line.split()
            # Format: <uuid> <name> <path>. The name is the second column.
            if len(parts) >= 2:
                names.add(parts[1])
        return names, ""

    def _run(self, cmd: list[str]) -> _CmdResult:
        tool = cmd[0]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
                timeout=self.timeout_seconds,
            )
        except FileNotFoundError:
            return _CmdResult(False, -1, "", "", f"{tool} not found")
        except subprocess.TimeoutExpired:
            return _CmdResult(False, -1, "", "", f"{tool} timed out after {self.timeout_seconds}s")
        except OSError:
            return _CmdResult(False, -1, "", "", f"{tool} could not run")
        return _CmdResult(True, result.returncode, result.stdout or "", result.stderr or "")
