"""Reconcile core: bring the host to the instance's desired state.

``plan`` renders the desired host surface and ``adopt`` records one already
correct resource as ours. This module is the write half, and it is the only one:
self-deploy, fresh install and recovery all reach the host through
``apply_host`` so there is a single materializer to reason about.

Two rules make that safe to run unattended.

Ownership. Every write is authorised by ``plan_changes`` against the managed
manifest, not by the desired plan alone. A name that exists on the host without
a matching managed record is a ``conflict``, and a conflict anywhere aborts the
whole run before the first write. Overwriting a unit we never installed is
exactly the failure mode that makes an unattended reconcile unsafe, so it fails
closed and asks the operator to adopt or declare the name instead.

Atomicity of record. The manifest is rewritten after each resource settles, so
an interrupted run leaves the manifest describing what is really installed. A
resource that failed to install is never recorded as managed.
"""

from __future__ import annotations

import json
import pwd
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from secretary._fsutil import directory_lock, write_text_atomic
from secretary.host import (
    HostInventory,
    PackagedUnit,
    PlanChange,
    PlannedResource,
    build_plan,
    default_packaging_root,
    foreign_units,
    load_packaged_units,
    SystemdLayout,
    manifest_text,
    plan_changes,
    plan_input_errors,
    strict_manifest,
)

SYSTEM_UNIT_DIR = Path("/etc/systemd/system")


@dataclass
class ApplyResult:
    """What one reconcile run changed, refused, or could not do."""

    changes: list[PlanChange] = field(default_factory=list)
    applied: list[str] = field(default_factory=list)
    conflicts: list[PlanChange] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    dry_run: bool = False

    @property
    def ok(self) -> bool:
        return not self.conflicts and not self.errors

    @property
    def changed(self) -> bool:
        return bool(self.applied)

    def render(self) -> list[str]:
        # Only what moves: an apply run that lists every unchanged resource buries
        # the two lines an operator actually has to read.
        lines = [
            f"{change.action} {change.logical_id} {change.kind} {change.name}"
            for change in self.changes
            if change.action != "unchanged"
        ]
        for conflict in self.conflicts:
            lines.append(f"conflict: {conflict.kind} {conflict.name} is not owned by this instance")
        lines.extend(f"error: {message}" for message in self.errors)
        return lines


class UnitInstaller(ABC):
    """The systemd side of a reconcile. Only this talks to the host."""

    @abstractmethod
    def installed(self, name: str) -> bytes | None:
        ...

    @abstractmethod
    def install(self, unit: PackagedUnit) -> None:
        ...

    @abstractmethod
    def remove(self, name: str) -> None:
        ...

    @abstractmethod
    def daemon_reload(self) -> None:
        ...

    @abstractmethod
    def enable(self, name: str) -> None:
        ...

    @abstractmethod
    def disable(self, name: str) -> None:
        ...

    @abstractmethod
    def restart(self, name: str) -> None:
        ...

    @abstractmethod
    def is_active(self, name: str) -> bool:
        ...


class HostCommandError(RuntimeError):
    """A host command failed. The message names the command, never its output."""


class SystemdUnitInstaller(UnitInstaller):
    """The real host. Unit files are root-owned, so writes go through sudo."""

    timeout_seconds = 60

    def __init__(self, unit_dir: Path = SYSTEM_UNIT_DIR, sudo: bool = True) -> None:
        self.unit_dir = unit_dir
        self.sudo = sudo

    def _run(self, cmd: list[str], label: str) -> subprocess.CompletedProcess[str]:
        argv = (["sudo", "-n"] if self.sudo else []) + cmd
        try:
            result = subprocess.run(argv, capture_output=True, text=True, timeout=self.timeout_seconds)
        except FileNotFoundError:
            raise HostCommandError(f"{label}: {cmd[0]} not found") from None
        except subprocess.TimeoutExpired:
            raise HostCommandError(f"{label}: {cmd[0]} timed out") from None
        except OSError:
            raise HostCommandError(f"{label}: {cmd[0]} could not run") from None
        if result.returncode != 0:
            raise HostCommandError(f"{label}: {cmd[0]} exited {result.returncode}")
        return result

    def installed(self, name: str) -> bytes | None:
        try:
            return (self.unit_dir / name).read_bytes()
        except OSError:
            return None

    def install(self, unit: PackagedUnit) -> None:
        argv = (["sudo", "-n"] if self.sudo else []) + [
            "install", "-m", "0644", "-o", "root", "-g", "root", "/dev/stdin", str(self.unit_dir / unit.name),
        ]
        try:
            result = subprocess.run(argv, input=unit.content, capture_output=True, timeout=self.timeout_seconds)
        except FileNotFoundError:
            raise HostCommandError(f"install {unit.name}: install not found") from None
        except subprocess.TimeoutExpired:
            raise HostCommandError(f"install {unit.name}: install timed out") from None
        except OSError:
            raise HostCommandError(f"install {unit.name}: install could not run") from None
        if result.returncode != 0:
            raise HostCommandError(f"install {unit.name}: install exited {result.returncode}")

    def remove(self, name: str) -> None:
        self._run(["rm", "-f", str(self.unit_dir / name)], f"remove {name}")

    def daemon_reload(self) -> None:
        self._run(["systemctl", "daemon-reload"], "daemon-reload")

    def enable(self, name: str) -> None:
        self._run(["systemctl", "enable", "--now", name], f"enable {name}")

    def disable(self, name: str) -> None:
        self._run(["systemctl", "disable", "--now", name], f"disable {name}")

    def restart(self, name: str) -> None:
        self._run(["systemctl", "restart", name], f"restart {name}")

    def is_active(self, name: str) -> bool:
        argv = ["systemctl", "is-active", name]
        try:
            result = subprocess.run(argv, capture_output=True, text=True, timeout=self.timeout_seconds)
        except (OSError, subprocess.TimeoutExpired):
            return False
        return result.stdout.strip() == "active"


class OrcaRegistrar(ABC):
    @abstractmethod
    def add(self, name: str, repo: str) -> None:
        ...


class LiveOrcaRegistrar(OrcaRegistrar):
    timeout_seconds = 60

    def __init__(self, user: str | None = None):
        self.user = user

    def add(self, name: str, repo: str) -> None:
        argv = ["orca", "repo", "add", "--path", repo]
        if self.user:
            argv = ["runuser", "--user", self.user, "--", *argv]
        try:
            result = subprocess.run(argv, capture_output=True, text=True, timeout=self.timeout_seconds)
        except FileNotFoundError:
            raise HostCommandError(f"register {name}: orca not found") from None
        except subprocess.TimeoutExpired:
            raise HostCommandError(f"register {name}: orca timed out") from None
        except OSError:
            raise HostCommandError(f"register {name}: orca could not run") from None
        if result.returncode != 0:
            raise HostCommandError(f"register {name}: orca exited {result.returncode}")


@dataclass(frozen=True)
class ApplyInputs:
    """Everything a reconcile needs, resolved once by the caller."""

    instance: dict[str, Any]
    bindings: list[dict[str, Any]]
    inventory: HostInventory
    managed: list[PlannedResource]
    manifest_path: Path
    packaged: list[PackagedUnit]


def resolve_packaged(
    instance: dict[str, Any],
    packaging_root: Path | None = None,
    *,
    product_root: Path | None = None,
    instance_path: Path,
    runtime_user: str | None = None,
    orca_executable: Path | None = None,
) -> list[PackagedUnit]:
    """Compile shipped templates for this installation's user and filesystem layout."""
    layout = resolve_systemd_layout(
        instance,
        packaging_root=packaging_root,
        product_root=product_root,
        instance_path=instance_path,
        runtime_user=runtime_user,
        orca_executable=orca_executable,
    )
    host = instance.get("host", {}) if isinstance(instance, dict) else {}
    prefix = host.get("unit_prefix", "") if isinstance(host, dict) else ""
    root = packaging_root or default_packaging_root()
    return load_packaged_units(root, prefix if isinstance(prefix, str) else "", layout)


def find_orca_executable(runtime_user: str, runtime_home: Path | None = None) -> Path | None:
    """Find the pinned runtime or the legacy CLI owned by the runtime user."""
    if runtime_home is None:
        try:
            runtime_home = Path(pwd.getpwnam(runtime_user).pw_dir).expanduser().resolve(strict=False)
        except KeyError:
            return None
    for candidate in (Path("/usr/local/bin/orca"), runtime_home / ".local" / "bin" / "orca"):
        if _is_executable(candidate):
            return candidate
    return None


def pinned_orca_executable() -> Path | None:
    """Return the runtime installed by Secretary, never a user's legacy CLI."""
    candidate = Path("/usr/local/bin/orca")
    return candidate if _is_executable(candidate) else None


def _is_executable(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_mode & 0o111 != 0
    except OSError:
        return False


def resolve_runtime_owner(
    instance_path: Path, runtime_user: str | None = None
) -> tuple[str, Path]:
    """The account that owns an installation, and the home its paths hang off.

    The instance checkout is durable installation state. When a command runs
    as root after recovery, its owner identifies the runtime account. Failure
    to resolve that owner is an error: falling back to the invoking account
    would turn a read or repair command into a different desired state, and a
    repair run as root would materialize skills, entry points and worktrees
    under ``/root`` while rendering units that name the owner's home.
    """
    target = instance_path.expanduser().resolve(strict=False)
    if runtime_user is None:
        try:
            runtime_user = pwd.getpwuid(target.stat().st_uid).pw_name
        except (KeyError, OSError):
            raise ValueError(f"could not resolve installation user from {target}") from None
    try:
        home = Path(pwd.getpwnam(runtime_user).pw_dir).expanduser().resolve(strict=False)
    except KeyError:
        raise ValueError(f"installation user does not exist: {runtime_user}") from None
    return runtime_user, home


def resolve_systemd_layout(
    instance: dict[str, Any],
    packaging_root: Path | None = None,
    *,
    product_root: Path | None = None,
    instance_path: Path,
    runtime_user: str | None = None,
    orca_executable: Path | None = None,
) -> SystemdLayout:
    """Resolve the one systemd layout used for an installation command."""
    root = (packaging_root or default_packaging_root()).resolve(strict=False)
    # Units run with the product checkout as their working directory. Keep every
    # rendered filesystem value absolute so a caller's relative spelling cannot
    # change the service's interpretation of its own layout.
    target = instance_path.expanduser().resolve(strict=False)
    user, home = resolve_runtime_owner(target, runtime_user)
    executable = orca_executable or find_orca_executable(user, home) or Path("/usr/local/bin/orca")
    host = instance.get("host", {}) if isinstance(instance.get("host"), dict) else {}
    return SystemdLayout(
        product_root=(product_root or root.parents[1]).expanduser().resolve(strict=False),
        instance_path=target,
        data_dir=Path(instance.get("data_dir", home / "secretary-data")).expanduser().resolve(strict=False),
        runtime_user=user,
        runtime_home=home,
        orca_executable=executable,
        memory_model=host.get("memory_model", "intfloat/multilingual-e5-large"),
        memory_dim=host.get("memory_dim", 1024),
        memory_threads=host.get("memory_threads", 1),
    )


def apply_host(
    inputs: ApplyInputs,
    *,
    units: UnitInstaller,
    orca: OrcaRegistrar,
    dry_run: bool = False,
) -> ApplyResult:
    """Reconcile the host to the instance. Fails closed on any conflict."""
    host = inputs.instance.get("host", {}) if isinstance(inputs.instance, dict) else {}
    prefix = host.get("unit_prefix", "") if isinstance(host, dict) else ""
    errors = plan_input_errors(inputs.instance, inputs.bindings, packaged=inputs.packaged)
    if errors:
        return ApplyResult(errors=list(errors), dry_run=dry_run)

    desired = build_plan(inputs.instance, inputs.bindings, packaged=inputs.packaged)
    changes = plan_changes(
        desired,
        inputs.inventory,
        inputs.managed,
        prefix if isinstance(prefix, str) else "",
        foreign_units(host),
    )
    result = ApplyResult(changes=changes, dry_run=dry_run)
    result.conflicts = [change for change in changes if change.action == "conflict"]
    if result.conflicts:
        # Nothing is written: a conflict means at least one name in our namespace
        # is not provably ours, and a partial reconcile around it would leave the
        # host in a state neither the plan nor the manifest describes.
        return result
    desired_by_id = {resource.logical_id: resource for resource in desired}
    packaged_by_name = {unit.name: unit for unit in inputs.packaged}
    # Check the whole batch before the first write. A unit the plan wants but the
    # product does not ship would otherwise fail halfway through, leaving some
    # units installed and the rest not.
    unshipped = sorted(
        change.name
        for change in changes
        if change.kind == "unit" and change.action in {"create", "update"} and change.name not in packaged_by_name
    )
    if unshipped:
        result.errors.append("no unit file is shipped for: " + ", ".join(unshipped))
        return result
    if dry_run:
        return result

    managed_by_id = {resource.logical_id: resource for resource in inputs.managed}
    reload_needed = False

    for change in changes:
        if change.action == "unchanged":
            continue
        try:
            touched_units = _apply_change(
                change, desired_by_id, managed_by_id.get(change.logical_id), packaged_by_name, units, orca
            )
        except HostCommandError as exc:
            result.errors.append(str(exc))
            break
        reload_needed = reload_needed or touched_units
        if change.action == "delete":
            managed_by_id.pop(change.logical_id, None)
        else:
            managed_by_id[change.logical_id] = desired_by_id[change.logical_id]
        result.applied.append(f"{change.action} {change.logical_id} {change.kind} {change.name}")
        _write_manifest(inputs.manifest_path, managed_by_id.values())

    if reload_needed:
        try:
            units.daemon_reload()
            _settle_units(changes, desired_by_id, packaged_by_name, units)
        except HostCommandError as exc:
            result.errors.append(str(exc))
    return result


def _apply_change(
    change: PlanChange,
    desired_by_id: dict[str, PlannedResource],
    owned: PlannedResource | None,
    packaged_by_name: dict[str, PackagedUnit],
    units: UnitInstaller,
    orca: OrcaRegistrar,
) -> bool:
    """Materialize one change. Returns True when systemd needs a reload."""
    if change.kind == "unit":
        if change.action == "delete":
            # Only a unit with [Install] was ever enabled; `disable` on one
            # without it fails, which would turn a clean removal into an error.
            if _was_installable(owned):
                units.disable(change.name)
            units.remove(change.name)
            return True
        unit = packaged_by_name.get(change.name)
        if unit is None:
            raise HostCommandError(f"install {change.name}: no unit of that name is shipped by this product")
        units.install(unit)
        return True
    if change.kind == "orca":
        if change.action == "delete":
            # Orca has no repo-removal command, so pretending to delete would
            # silently leave the registration in place and record it as gone.
            raise HostCommandError(f"unregister {change.name}: Orca has no repo removal command; remove it by hand")
        resource = desired_by_id[change.logical_id]
        orca.add(change.name, _repo_of(resource))
        return False
    raise HostCommandError(f"{change.kind} {change.name}: unsupported resource kind")


def _was_installable(owned: PlannedResource | None) -> bool:
    """Whether the record we are deleting says the unit had an [Install] section."""
    if owned is None:
        return False
    try:
        return json.loads(owned.spec).get("installable") == "yes"
    except (ValueError, TypeError):
        return False


def _settle_units(
    changes: Iterable[PlanChange],
    desired_by_id: dict[str, PlannedResource],
    packaged_by_name: dict[str, PackagedUnit],
    units: UnitInstaller,
) -> None:
    """Enable what we just installed. Idempotent: enable --now is a no-op twice."""
    for change in changes:
        if change.kind != "unit" or change.action in {"delete", "unchanged"}:
            continue
        unit = packaged_by_name.get(change.name)
        if unit is None or not unit.installable:
            continue
        units.enable(change.name)


def _repo_of(resource: PlannedResource) -> str:
    try:
        repo = json.loads(resource.spec)["repo"]
    except (ValueError, KeyError, TypeError):
        raise HostCommandError(f"register {resource.name}: desired repo path is missing") from None
    if not isinstance(repo, str) or not repo:
        raise HostCommandError(f"register {resource.name}: desired repo path is missing")
    return repo


def _write_manifest(path: Path, resources: Iterable[PlannedResource]) -> None:
    if path.is_symlink():
        raise HostCommandError("managed manifest must not be a symlink")
    path.parent.mkdir(parents=True, exist_ok=True)
    with directory_lock(path.parent):
        write_text_atomic(path, manifest_text(resources))


__all__ = [
    "ApplyInputs",
    "ApplyResult",
    "HostCommandError",
    "LiveOrcaRegistrar",
    "OrcaRegistrar",
    "SystemdUnitInstaller",
    "UnitInstaller",
    "apply_host",
    "pinned_orca_executable",
    "resolve_packaged",
    "resolve_systemd_layout",
]
