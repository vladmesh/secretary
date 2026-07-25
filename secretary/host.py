"""Desired host state and read-only host inventory.

This compares what an instance config describes against what is actually on the
host across three resource kinds: project repos, systemd units and Orca repo
registrations. Nothing here changes the host, and no source reads config values
or secrets: they only list resource *names*, so env files and secret material
are never opened.

Desired state has two inputs, both of them declarative. The instance config says
which components this installation runs and which foreign names under its unit
prefix it does not own; the product's own ``packaging/systemd`` directory says
what those components actually are. Because a unit file's content is part of the
desired state, its digest rides in the planned resource's spec, so editing a
shipped unit shows up as an ``update`` on the next plan.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

KINDS = ("projects", "units", "orca repos")
UNIT_SUFFIXES = (".service", ".timer")


@dataclass(frozen=True)
class PlannedResource:
    """One desired host resource and the evidence required to own it."""

    logical_id: str
    kind: str
    name: str
    spec: str
    fingerprint: str


@dataclass(frozen=True)
class PlanChange:
    logical_id: str
    kind: str
    name: str
    action: str


def _resource(logical_id: str, kind: str, name: str, payload: dict[str, str]) -> PlannedResource:
    spec = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    value = json.dumps([logical_id, kind, name, spec], separators=(",", ":"))
    return PlannedResource(logical_id, kind, name, spec, hashlib.sha256(value.encode()).hexdigest())


@dataclass(frozen=True)
class PackagedUnit:
    """One rendered systemd unit and the bytes that define its desired state."""

    component: str
    name: str
    path: Path
    content: bytes
    digest: str
    installable: bool
    oneshot: bool


def default_packaging_root() -> Path:
    """Where the running product keeps its shipped units."""
    return Path(__file__).resolve().parents[1] / "packaging" / "systemd"


@dataclass(frozen=True)
class SystemdLayout:
    """Installation-specific values substituted into shipped systemd templates."""

    product_root: Path
    instance_path: Path
    data_dir: Path
    runtime_user: str
    runtime_home: Path
    orca_executable: Path = Path("/usr/local/bin/orca")


def default_systemd_layout() -> SystemdLayout:
    root = Path(__file__).resolve().parents[1]
    home = Path.home()
    return SystemdLayout(root, home / "secretary-instance", home / "secretary-data", os.environ.get("USER", "dev"), home)


def render_systemd_unit(template: bytes, layout: SystemdLayout) -> bytes:
    """Compile one shipped unit template into its canonical host bytes."""
    values = {
        b"{{SECRETARY_PRODUCT_ROOT}}": os.fsencode(layout.product_root),
        b"{{SECRETARY_INSTANCE_PATH}}": os.fsencode(layout.instance_path),
        b"{{SECRETARY_DATA_DIR}}": os.fsencode(layout.data_dir),
        b"{{SECRETARY_RUNTIME_USER}}": layout.runtime_user.encode(),
        b"{{SECRETARY_RUNTIME_HOME}}": os.fsencode(layout.runtime_home),
        b"{{SECRETARY_ORCA_EXECUTABLE}}": os.fsencode(layout.orca_executable),
    }
    rendered = template
    for marker, value in values.items():
        rendered = rendered.replace(marker, value)
    if b"{{SECRETARY_" in rendered:
        raise ValueError("systemd template has an unknown placeholder")
    return rendered


def load_packaged_units(root: Path, prefix: str, layout: SystemdLayout | None = None) -> list[PackagedUnit]:
    """Read the shipped unit catalogue. A unit outside the prefix is not ours.

    Returns an empty catalogue when the directory is absent or unreadable rather
    than raising: callers that need the units to exist say so themselves, and a
    plan built without them must not crash a read-only doctor run.
    """
    if not prefix:
        return []
    try:
        entries = sorted(entry for entry in root.iterdir() if entry.is_file())
    except OSError:
        return []
    units: list[PackagedUnit] = []
    for entry in entries:
        if not entry.name.startswith(prefix) or not entry.name.endswith(UNIT_SUFFIXES):
            continue
        try:
            payload = render_systemd_unit(entry.read_bytes(), layout or default_systemd_layout())
        except (OSError, ValueError):
            continue
        suffix = entry.name[entry.name.rindex(".") :]
        component = entry.name[len(prefix) : -len(suffix)]
        if not component:
            continue
        units.append(
            PackagedUnit(
                component=component,
                name=entry.name,
                path=entry,
                content=payload,
                digest=hashlib.sha256(payload).hexdigest(),
                # Only a unit with [Install] can be enabled; the rest are pulled
                # in by a timer's Unit= and enabling them would fail.
                installable=b"[Install]" in payload,
                oneshot=b"Type=oneshot" in payload,
            )
        )
    return units


def component_enabled(host: dict[str, Any], component: str) -> bool:
    """Whether this installation runs a shipped component. Absent means yes.

    The product ships a component because it is part of the runtime; an
    installation opts out explicitly. That keeps a fresh install complete by
    default and makes a deliberately shed component a config fact rather than
    an undocumented gap on the host.
    """
    components = host.get("components") if isinstance(host, dict) else None
    if not isinstance(components, dict):
        return True
    entry = components.get(component)
    if not isinstance(entry, dict):
        return True
    return entry.get("enabled") is not False


def foreign_units(host: dict[str, Any]) -> set[str]:
    """Unit names under our prefix that this installation declares are not ours."""
    if not isinstance(host, dict):
        return set()
    return set(_str_list(host.get("foreign_units")))


def build_plan(
    instance: dict[str, Any],
    bindings: Iterable[dict[str, Any]],
    *,
    packaged: Iterable[PackagedUnit] | None = None,
) -> list[PlannedResource]:
    """Render the supported host surface without consulting the live host.

    Heads produce systemd services. Every enabled component of the shipped unit
    catalogue produces its unit. Project bindings with an explicit
    ``orca_binding`` produce Orca registrations, whose names are explicit
    binding data. This is independent from ``enabled``: that flag gates task
    routing after onboarding, while an inventory-only project may still need a
    durable Orca registration. The host block only supplies a namespace boundary
    and the component opt-outs; it never carries a second list of resources.
    """
    host = instance.get("host", {}) if isinstance(instance, dict) else {}
    prefix = host.get("unit_prefix", "") if isinstance(host, dict) else ""
    if packaged is None:
        packaged = load_packaged_units(default_packaging_root(), prefix)
    digests = {unit.name: unit.digest for unit in packaged}
    result: list[PlannedResource] = []
    heads = instance.get("heads", []) if isinstance(instance, dict) else []
    if isinstance(heads, list) and prefix:
        for head in heads:
            if not isinstance(head, dict) or not isinstance(head.get("role"), str):
                continue
            role = head["role"]
            logical_id = f"systemd:head:{role}"
            name = f"{prefix}{role}.service"
            model = head.get("model")
            if not isinstance(model, str):
                continue
            result.append(_resource(logical_id, "unit", name, {"model": model, "role": role}))
    if prefix:
        result.extend(_production_dispatcher_units(prefix, digests))
        result.extend(_packaged_component_units(host, packaged))
    for binding in bindings:
        if not isinstance(binding, dict):
            continue
        project_id = binding.get("id")
        name = binding.get("orca_binding")
        if not isinstance(project_id, str) or not isinstance(name, str):
            continue
        logical_id = f"orca:project:{project_id}"
        repo = binding.get("repo")
        if not isinstance(repo, str):
            continue
        result.append(_resource(logical_id, "orca", name, {"repo": repo, "binding": name}))
    # A declared foreign unit is outside this installation's ownership even
    # when its name overlaps a product-shipped unit. Keep that boundary in the
    # canonical desired state so reconcile and doctor cannot disagree about it.
    foreign = foreign_units(host)
    return sorted(
        (resource for resource in result if resource.kind != "unit" or resource.name not in foreign),
        key=lambda resource: (resource.kind, resource.logical_id),
    )


def _production_dispatcher_units(prefix: str, digests: dict[str, str]) -> list[PlannedResource]:
    """The dispatcher pair keeps its own logical ids and semantic spec.

    doctor reads these ids to tell an operator that the tick's own units drifted,
    so they are not folded into the generic packaged-component ids. The shipped
    file's digest still rides along, so editing the unit is an update here too.
    """
    service = f"{prefix}dispatcher-production.service"
    timer = f"{prefix}dispatcher-production.timer"
    service_spec = {
        "component": "dispatcher-production",
        "managed_by": "secretary",
        "runtime": "python3 -m secretary dispatcher production-tick --instance $SECRETARY_INSTANCE",
        "env": "SECRETARY_INSTANCE,KANBOARD_URL,KANBOARD_API_USER,KANBOARD_API_TOKEN,SECRETARY_DISPATCHER_OWNER",
    }
    timer_spec = {
        "component": "dispatcher-production",
        "managed_by": "secretary",
        "service": service,
        "on_boot_sec": "30s",
        "on_unit_active_sec": "60s",
    }
    for name, spec in ((service, service_spec), (timer, timer_spec)):
        if digest := digests.get(name):
            spec["digest"] = digest
    return [
        _resource("systemd:dispatcher:production.service", "unit", service, service_spec),
        _resource("systemd:dispatcher:production.timer", "unit", timer, timer_spec),
    ]


def _packaged_component_units(host: dict[str, Any], packaged: Iterable[PackagedUnit]) -> list[PlannedResource]:
    """Every shipped unit of an enabled component, keyed by its own name."""
    result: list[PlannedResource] = []
    for unit in packaged:
        if unit.component == "dispatcher-production":
            continue  # owned by _production_dispatcher_units, which carries its digest
        if not component_enabled(host, unit.component):
            continue
        result.append(
            _resource(
                f"systemd:unit:{unit.name}",
                "unit",
                unit.name,
                {
                    "component": unit.component,
                    "managed_by": "secretary",
                    "digest": unit.digest,
                    "installable": "yes" if unit.installable else "no",
                },
            )
        )
    return result


def plan_input_errors(
    instance: dict[str, Any],
    bindings: Iterable[dict[str, Any]],
    *,
    packaged: Iterable[PackagedUnit] | None = None,
) -> list[str]:
    """Reject incomplete desired-state inputs before a plan can fail open."""
    bindings = list(bindings)
    packaged = list(packaged) if packaged is not None else None
    host = instance.get("host", {}) if isinstance(instance, dict) else {}
    prefix = host.get("unit_prefix") if isinstance(host, dict) else None
    heads = instance.get("heads", []) if isinstance(instance, dict) else []
    if isinstance(heads, list) and heads and not isinstance(prefix, str):
        return ["host.unit_prefix is required when heads are configured"]
    errors: list[str] = []
    for binding in bindings:
        if isinstance(binding, dict) and binding.get("enabled") and not isinstance(binding.get("orca_binding"), str):
            errors.append("enabled binding requires explicit orca_binding")
    desired = build_plan(instance, bindings, packaged=packaged)
    logical_ids: set[str] = set()
    names: set[tuple[str, str]] = set()
    for resource in desired:
        if resource.logical_id in logical_ids:
            errors.append(f"duplicate desired logical_id: {resource.logical_id}")
        logical_ids.add(resource.logical_id)
        key = (resource.kind, resource.name)
        if key in names:
            errors.append(f"duplicate desired resource name: {resource.kind} {resource.name}")
        names.add(key)
    return errors


def load_managed_manifest(path: Path) -> list[PlannedResource]:
    """Load the applied state. Invalid or missing state proves no ownership."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    values = raw.get("resources", []) if isinstance(raw, dict) else []
    resources: list[PlannedResource] = []
    for value in values:
        if not isinstance(value, dict):
            continue
        fields = (value.get("logical_id"), value.get("kind"), value.get("name"), value.get("fingerprint"))
        if all(isinstance(field, str) and field for field in fields):
            spec = value.get("spec", "")
            resources.append(PlannedResource(fields[0], fields[1], fields[2], spec if isinstance(spec, str) else "", fields[3]))
    return resources


def strict_manifest(path: Path) -> tuple[list[PlannedResource], str]:
    """Load state for a write path. Unlike plan, a writer must fail closed.

    Returns ``(resources, reason)``; a non-empty reason means the manifest could
    not be trusted, and the caller must refuse to write rather than treat the
    unreadable state as "we own nothing".
    """
    if path.is_symlink():
        return [], "managed manifest must not be a symlink"
    if not path.exists():
        return [], ""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except UnicodeError:
        return [], "managed manifest is not valid UTF-8"
    except OSError:
        return [], "managed manifest is unreadable"
    except ValueError:
        return [], "managed manifest is not valid JSON"
    if not isinstance(payload, dict) or payload.get("version") != 1 or not isinstance(payload.get("resources"), list):
        return [], "managed manifest has an unsupported shape"
    resources = load_managed_manifest(path)
    if len(resources) != len(payload["resources"]):
        return [], "managed manifest contains invalid resource records"
    logical_ids: set[str] = set()
    names: set[tuple[str, str]] = set()
    for resource in resources:
        if resource.kind not in {"unit", "orca"} or not resource.spec:
            return [], "managed manifest contains non-canonical resource records"
        value = json.dumps(
            [resource.logical_id, resource.kind, resource.name, resource.spec],
            separators=(",", ":"),
        )
        if hashlib.sha256(value.encode()).hexdigest() != resource.fingerprint:
            return [], "managed manifest contains a fingerprint mismatch"
        if resource.logical_id in logical_ids:
            return [], "managed manifest has duplicate logical ids"
        logical_ids.add(resource.logical_id)
        key = (resource.kind, resource.name)
        if key in names:
            return [], "managed manifest has duplicate resource names"
        names.add(key)
    return resources, ""


def manifest_text(resources: Iterable[PlannedResource]) -> str:
    records = [
        {
            "fingerprint": resource.fingerprint,
            "kind": resource.kind,
            "logical_id": resource.logical_id,
            "name": resource.name,
            "spec": resource.spec,
        }
        for resource in sorted(resources, key=lambda item: (item.kind, item.logical_id))
    ]
    return json.dumps({"version": 1, "resources": records}, indent=2, sort_keys=True) + "\n"


def plan_changes(
    desired: Iterable[PlannedResource],
    actual: HostInventory,
    managed: Iterable[PlannedResource],
    unit_prefix: str = "",
    declared_foreign: Iterable[str] = (),
) -> list[PlanChange]:
    """Classify changes. A name match is a conflict unless exact state owns it."""
    declared_foreign = set(declared_foreign)
    actual_names = {"unit": actual.units, "orca": actual.orca_repos}
    # Do not let an older manifest record pull a now-declared foreign unit back
    # under management through the deletion pass below.
    managed_by_id = {
        resource.logical_id: resource
        for resource in managed
        if resource.kind != "unit" or resource.name not in declared_foreign
    }
    desired_by_id = {
        resource.logical_id: resource
        for resource in desired
        if resource.kind != "unit" or resource.name not in declared_foreign
    }
    changes: list[PlanChange] = []
    for resource in desired_by_id.values():
        present = resource.name in actual_names[resource.kind]
        owned = managed_by_id.get(resource.logical_id)
        if not present:
            action = "create"
        elif owned and owned.kind == resource.kind and owned.name == resource.name:
            action = "update" if owned.fingerprint != resource.fingerprint else "unchanged"
        else:
            action = "conflict"
        changes.append(PlanChange(resource.logical_id, resource.kind, resource.name, action))
    for logical_id, resource in managed_by_id.items():
        desired_resource = desired_by_id.get(logical_id)
        renamed = desired_resource and (resource.kind != desired_resource.kind or resource.name != desired_resource.name)
        if (desired_resource is None or renamed) and resource.name in actual_names.get(resource.kind, set()):
            changes.append(PlanChange(logical_id, resource.kind, resource.name, "delete"))
    known_units = {resource.name for resource in desired_by_id.values() if resource.kind == "unit"}
    known_units.update(resource.name for resource in managed_by_id.values() if resource.kind == "unit")
    # A name the instance declares foreign stays out of our namespace: it is not
    # a conflict to resolve, it is somebody else's unit we have agreed to leave
    # alone. Declaring it is the only way to say so, so silence here is still
    # fail-closed.
    known_units.update(declared_foreign)
    if unit_prefix:
        for name in actual.units:
            if name.startswith(unit_prefix) and name not in known_units:
                changes.append(PlanChange(f"systemd:conflict:{name}", "unit", name, "conflict"))
    return sorted(changes, key=lambda change: (change.kind, change.logical_id))


@dataclass(frozen=True)
class HostInventory:
    """The set of resource names actually present on the host."""

    projects: set[str] = field(default_factory=set)
    units: set[str] = field(default_factory=set)
    orca_repos: set[str] = field(default_factory=set)
    unit_states: dict[str, tuple[str, str]] = field(default_factory=dict)


@dataclass(frozen=True)
class Expectations:
    """Resource names an instance config says it owns."""

    projects: set[str] = field(default_factory=set)
    units: set[str] = field(default_factory=set)
    orca_repos: set[str] = field(default_factory=set)
    unit_prefix: str = ""
    projects_root: str = ""
    foreign_units: set[str] = field(default_factory=set)
    unit_runtime: dict[str, tuple[bool, bool]] = field(default_factory=dict)
    project_error: str = ""


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


def _normalized_repo_path(repo: str) -> str:
    return str(Path(repo).expanduser().resolve(strict=False))


def build_doctor_expectations(
    instance: dict[str, Any],
    bindings: Iterable[dict[str, Any]],
    *,
    packaged: Iterable[PackagedUnit] | None = None,
) -> Expectations:
    """Derive doctor parity from reconcile's canonical desired state."""
    bindings = list(bindings)
    host = instance.get("host", {}) if isinstance(instance, dict) else {}
    host = host if isinstance(host, dict) else {}
    projects: set[str] = set()
    project_error = ""
    for binding in bindings:
        if not isinstance(binding, dict) or not isinstance(binding.get("repo"), str):
            continue
        try:
            projects.add(_normalized_repo_path(binding["repo"]))
        except (OSError, RuntimeError):
            # A symlink loop or unreadable binding path is not evidence that the
            # checkout is absent. Leave this kind unavailable for doctor.
            project_error = "expected project checkout path could not be normalized"
    prefix = host.get("unit_prefix", "") if isinstance(host.get("unit_prefix"), str) else ""
    packaged = list(packaged) if packaged is not None else load_packaged_units(default_packaging_root(), prefix)
    desired = build_plan(instance, bindings, packaged=packaged)
    units = {resource.name for resource in desired if resource.kind == "unit"}
    packaged_by_name = {unit.name: unit for unit in packaged}
    runtime: dict[str, tuple[bool, bool]] = {}
    for name in units:
        unit = packaged_by_name.get(name)
        if name.endswith(".timer"):
            runtime[name] = (True, True)
        elif unit is None or not unit.oneshot:
            runtime[name] = (True, True)
    return Expectations(
        projects=projects,
        units=units,
        orca_repos={resource.name for resource in desired if resource.kind == "orca"},
        unit_prefix=prefix,
        projects_root=host.get("projects_root", "") if isinstance(host.get("projects_root"), str) else "",
        foreign_units=foreign_units(host),
        unit_runtime=runtime,
        project_error=project_error,
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
        "units": _diff(expected.units, actual.units - expected.foreign_units),
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

        projects/<name>/     one directory per project repo on the fixture host
        units.txt            one systemd unit name per line
        orca-repos.txt       one Orca repo name per line

    Reads only. The root itself must exist: a missing root is an inspection
    failure (nothing was read), so every kind is marked unavailable rather than
    reported as an empty host. Within an existing root a missing per-kind file
    means an empty set, since the fixture is authored deliberately and a file's
    absence is not an inspection failure.
    """

    def __init__(self, root: Path):
        self.root = root

    def _lines(self, name: str) -> tuple[set[str], str]:
        try:
            path = self.root / name
            if not path.is_file():
                return set(), ""
            names = {
                token for line in path.read_text(encoding="utf-8").splitlines()
                if (token := line.strip()) and not token.startswith("#")
            }
            return names, ""
        except UnicodeError:
            return set(), "fixture host file is not valid UTF-8"
        except OSError:
            return set(), "fixture host file is unreadable"

    def _projects(self, expected: Expectations) -> tuple[set[str], str]:
        try:
            paths, error = self._lines("projects.txt")
            if error or paths:
                return {_normalized_repo_path(path) for path in paths}, error
            projects_dir = self.root / "projects"
            if not projects_dir.is_dir():
                return set(), ""
            # Legacy fixtures model checkouts beneath their own root. Their
            # directory names are observed host facts, not aliases for an
            # expected binding with the same basename.
            return {_normalized_repo_path(str(projects_dir / name)) for name in _names_from_dir(projects_dir)}, ""
        except OSError:
            return set(), "fixture projects directory is unreadable"

    def collect(self, expected: Expectations) -> CollectResult:
        if not self.root.is_dir():
            # The root was never read, so this is "could not inspect", not an
            # empty host. Marking every kind unavailable stops doctor from
            # reporting all expected resources as missing against a phantom host.
            reason = "fixture host directory not found"
            return CollectResult(
                inventory=HostInventory(),
                errors={kind: reason for kind in KINDS},
            )
        projects, project_error = self._projects(expected)
        units, unit_error = self._lines("units.txt")
        states, state_error = self._unit_states()
        repos, repo_error = self._lines("orca-repos.txt")
        errors = {
            kind: reason for kind, reason in (
                ("projects", project_error), ("units", unit_error or state_error), ("orca repos", repo_error)
            ) if reason
        }
        return CollectResult(HostInventory(projects, units, repos, states), errors)

    def _unit_states(self) -> tuple[dict[str, tuple[str, str]], str]:
        """Optional fixture runtime states: ``unit enabled active`` per line."""
        try:
            path = self.root / "unit-states.txt"
            if not path.is_file():
                return {}, ""
            states: dict[str, tuple[str, str]] = {}
            for line in path.read_text(encoding="utf-8").splitlines():
                fields = line.split()
                if not fields or fields[0].startswith("#"):
                    continue
                if len(fields) != 3:
                    return {}, "fixture unit states are invalid"
                states[fields[0]] = (fields[1], fields[2])
            return states, ""
        except UnicodeError:
            return {}, "fixture unit states are not valid UTF-8"
        except OSError:
            return {}, "fixture unit states are unreadable"


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

    def __init__(self, orca_user: str | None = None):
        self.orca_user = orca_user

    def collect(self, expected: Expectations) -> CollectResult:
        inventory = HostInventory()
        errors: dict[str, str] = {}

        projects, reason = self._projects(expected)
        if reason:
            errors["projects"] = reason
        else:
            inventory = HostInventory(projects, inventory.units, inventory.orca_repos, inventory.unit_states)

        units, unit_states, reason = self._units(expected)
        if reason:
            errors["units"] = reason
        else:
            inventory = HostInventory(inventory.projects, units, inventory.orca_repos, unit_states)

        repos, reason = self._orca_repos()
        if reason:
            errors["orca repos"] = reason
        else:
            inventory = HostInventory(inventory.projects, inventory.units, repos, inventory.unit_states)

        return CollectResult(inventory=inventory, errors=errors)

    def _projects(self, expected: Expectations) -> tuple[set[str], str]:
        if expected.project_error:
            return set(), expected.project_error
        root = expected.projects_root
        actual: set[str] = set()
        for project in expected.projects:
            try:
                mode = Path(project).stat().st_mode
            except FileNotFoundError:
                continue
            except (OSError, RuntimeError):
                return set(), "expected project checkout path could not be inspected"
            if stat.S_ISDIR(mode):
                actual.add(project)
        if not root:
            return (actual, "host.projects_root not set") if expected.projects else (actual, "")
        path = Path(root).expanduser()
        if not path.is_dir():
            # Never echo the configured value: it comes from private instance
            # config and could carry a secret-like path. Name the field only.
            return set(), "host.projects_root is not a directory"
        try:
            actual.update(str(entry.resolve(strict=False)) for entry in path.iterdir() if entry.is_dir())
            return actual, ""
        except OSError:
            return set(), "host.projects_root is not readable"

    def _units(self, expected: Expectations) -> tuple[set[str], dict[str, tuple[str, str]], str]:
        prefix = expected.unit_prefix
        if not prefix:
            # unmanaged-on-host can only be computed by enumerating a namespace.
            # Declared units with no prefix would let us confirm the declared
            # ones and silently miss every undescribed host unit, so we refuse
            # to emit a diff that cannot include unmanaged-on-host.
            if expected.units:
                return set(), {}, "host.unit_prefix is required to compute unmanaged-on-host"
            return set(), {}, ""
        result = self._run(["systemctl", "list-unit-files", "--no-legend", f"{prefix}*"])
        reason = self._systemctl_error(result)
        if reason:
            return set(), {}, reason
        names: set[str] = set()
        for line in result.stdout.splitlines():
            fields = line.split()
            token = fields[0] if fields else ""
            if token.startswith(prefix):
                names.add(token)
        states: dict[str, tuple[str, str]] = {}
        for name in expected.units:
            if name not in names:
                continue
            enabled = self._run(["systemctl", "is-enabled", name])
            active = self._run(["systemctl", "is-active", name])
            if not enabled.ran or not active.ran:
                return set(), {}, enabled.reason or active.reason
            if enabled.stderr.strip() or active.stderr.strip():
                return set(), {}, "systemctl runtime status unavailable"
            states[name] = (enabled.stdout.strip() or "disabled", active.stdout.strip() or "inactive")
        return names, states, ""

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
        result = self._run(self._orca_command(["orca", "repo", "list"]))
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

    def orca_repo_paths(self) -> tuple[dict[str, str], str]:
        """Return Orca registration name -> normalized repo path.

        Adoption needs stronger evidence than the name-only inventory used by
        ``plan``. JSON output lets us compare the registered path with the
        explicit binding without reading project files or secrets.
        """
        result = self._run(self._orca_command(["orca", "repo", "list", "--json"]))
        if not result.ran:
            return {}, result.reason
        if result.returncode != 0:
            return {}, f"orca exited {result.returncode}"
        try:
            payload = json.loads(result.stdout)
        except ValueError:
            return {}, "orca returned invalid JSON"
        repos = payload.get("result", {}).get("repos") if isinstance(payload, dict) else None
        if not isinstance(repos, list):
            return {}, "orca JSON has no repo inventory"
        paths: dict[str, str] = {}
        for repo in repos:
            if not isinstance(repo, dict):
                continue
            name, path = repo.get("displayName"), repo.get("path")
            if not isinstance(name, str) or not name or not isinstance(path, str) or not path:
                continue
            candidate = Path(path).expanduser()
            if not candidate.is_absolute():
                return {}, "orca returned a non-absolute repo path"
            try:
                normalized = str(candidate.resolve(strict=False))
            except (OSError, RuntimeError):
                return {}, "orca repo path could not be normalized"
            if name in paths and paths[name] != normalized:
                return {}, "orca returned duplicate registration names"
            paths[name] = normalized
        return paths, ""

    def _orca_command(self, command: list[str]) -> list[str]:
        if os.geteuid() == 0 and self.orca_user:
            return ["runuser", "--user", self.orca_user, "--", *command]
        return command

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
