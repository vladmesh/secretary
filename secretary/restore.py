from __future__ import annotations

import hashlib
import json
import os
import shutil
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from secretary.backup_policy import (
    ARCHIVE_ROOT,
    BACKUP_KINDS,
    BACKUP_VERSION,
    BackupPolicy,
    CORE_POLICY,
    policy_for,
    restore_plan_components,
)
from secretary.backup_verify import _decrypt_with_age, _verify_plain_tar
from secretary.config import ConfigError, load_config, validate_instance
from secretary.data import init_layout


@dataclass(frozen=True)
class RestorePlan:
    archive: Path
    backup_kind: str
    backup_version: int
    data_dir: Path
    components: tuple[dict[str, str], ...]
    instance_identity: dict[str, str]


class RestoreError(RuntimeError):
    pass


def bootstrap_empty(instance_path: Path, *, dry_run: bool = False) -> RestorePlan:
    _, target, identity = _target(instance_path)
    _reject_existing_target(target)
    plan = RestorePlan(
        archive=Path(),
        backup_kind="empty",
        backup_version=BACKUP_VERSION,
        data_dir=target,
        components=restore_plan_components(CORE_POLICY, empty=True),
        instance_identity=identity,
    )
    if not dry_run:
        init_layout(target)
    return plan


def restore_backup(
    archive: Path,
    instance_path: Path,
    *,
    age_identity: Path | None,
    dry_run: bool = False,
    age_command: str = "age",
    decrypt=None,
) -> RestorePlan:
    _, target, target_identity = _target(instance_path)
    _reject_existing_target(target)
    archive = archive.expanduser()
    if not archive.is_file():
        raise RestoreError(f"archive not found: {archive}")
    if decrypt is None:
        if age_identity is None:
            raise RestoreError("age identity is not configured")
        age_identity = age_identity.expanduser()
        if not age_identity.is_file():
            raise RestoreError(f"age identity not found: {age_identity}")
        if shutil.which(age_command) is None:
            raise RestoreError(f"age command not found: {age_command}")

    with tempfile.TemporaryDirectory(prefix=".secretary-restore-") as temporary:
        plain = Path(temporary) / "payload.tar"
        try:
            if decrypt is None:
                _decrypt_with_age(archive, plain, identity=age_identity, age_command=age_command)
            else:
                decrypt(archive, plain)
        except RuntimeError as exc:
            raise RestoreError(str(exc)) from None

        verified = _verify_plain_tar(plain)
        if verified.code or verified.findings or not isinstance(verified.manifest, dict):
            findings = "; ".join(verified.findings) or "archive verification failed"
            raise RestoreError(findings)
        if not _has_memory_journal(plain):
            raise RestoreError("archive has no memory journal git metadata")
        manifest = verified.manifest
        archive_identity = _archive_identity(manifest)
        if archive_identity != target_identity:
            raise RestoreError("archive instance identity does not match target instance")
        kind = manifest.get("backup_kind")
        if kind not in BACKUP_KINDS or manifest.get("version") != BACKUP_VERSION:
            raise RestoreError("archive kind or version is not supported")
        policy = policy_for(kind)
        if policy is None:
            raise RestoreError("archive kind is not supported")
        plan = RestorePlan(
            archive=archive,
            backup_kind=kind,
            backup_version=BACKUP_VERSION,
            data_dir=target,
            components=restore_plan_components(policy),
            instance_identity=target_identity,
        )
        if dry_run:
            return plan
        _validate_restore_payload(plain, manifest, policy)
        _stage_and_publish(plain, target, policy=policy)
        return plan


def plan_as_json(plan: RestorePlan, *, action: str, dry_run: bool) -> dict[str, Any]:
    return {
        "ok": True,
        "action": action,
        "dry_run": dry_run,
        "archive": str(plan.archive) if action == "restore" else None,
        "backup_kind": plan.backup_kind,
        "backup_version": plan.backup_version,
        "data_dir": str(plan.data_dir),
        "components": list(plan.components),
        "instance_identity": plan.instance_identity,
        "next_steps": _next_steps(plan.components),
    }


def _target(instance_path: Path) -> tuple[Path, Path, dict[str, str]]:
    instance_file = instance_path.expanduser()
    if instance_file.is_dir():
        instance_file = instance_file / "instance.yaml"
    report = validate_instance(instance_file)
    if report.errors:
        raise RestoreError("invalid target instance: " + "; ".join(report.errors))
    try:
        config = load_config(instance_file)
    except ConfigError as exc:
        raise RestoreError(str(exc)) from None
    if not isinstance(config, dict):
        raise RestoreError("invalid target instance")
    configured_dir = config.get("data_dir")
    selected = Path(str(configured_dir)).expanduser()
    if not selected.is_absolute():
        raise RestoreError("target data root must be absolute")
    target = selected.resolve()
    return instance_file, target, _identity(config)


def _identity(config: dict[str, Any]) -> dict[str, str]:
    offsite = config.get("offsite")
    remote = offsite.get("instance_remote") if isinstance(offsite, dict) else None
    name = config.get("name")
    if not isinstance(name, str) or not isinstance(remote, str):
        raise RestoreError("target instance has no usable identity")
    return {"name": name, "instance_remote": remote}


def _archive_identity(manifest: dict[str, Any]) -> dict[str, str]:
    instance = manifest.get("instance")
    identity = instance.get("identity") if isinstance(instance, dict) else None
    if not isinstance(identity, dict):
        raise RestoreError("archive has no instance identity")
    name, remote = identity.get("name"), identity.get("instance_remote")
    if not isinstance(name, str) or not isinstance(remote, str):
        raise RestoreError("archive has invalid instance identity")
    return {"name": name, "instance_remote": remote}


def _has_memory_journal(plain_archive: Path) -> bool:
    required = f"{ARCHIVE_ROOT}/secretary-data/memory/facts/.git/HEAD"
    try:
        with tarfile.open(plain_archive, "r") as archive:
            return archive.getmember(required).isfile()
    except (KeyError, OSError, tarfile.TarError):
        return False


def _next_steps(components: tuple[dict[str, str], ...]) -> list[str]:
    labels = {
        "board_restore": "board restore",
        "memory_index": "memory index rebuild",
        "host_reconcile": "reconcile",
    }
    return [labels[component["name"]] for component in components if component["name"] in labels]


def _reject_existing_target(target: Path) -> None:
    if target.exists():
        raise RestoreError(f"target data root already exists: {target}")


def _validate_restore_payload(
    plain_archive: Path, manifest: dict[str, Any], policy: BackupPolicy
) -> None:
    checksums = manifest.get("checksums")
    if not isinstance(checksums, dict) or not checksums:
        raise RestoreError("versions manifest has no checksums")
    expected = {
        name for name, digest in checksums.items() if isinstance(name, str) and isinstance(digest, str)
    }
    if len(expected) != len(checksums) or any(len(digest) != 64 for digest in checksums.values()):
        raise RestoreError("versions manifest has invalid checksums")
    prefix = f"{ARCHIVE_ROOT}/"
    data_prefix = f"{ARCHIVE_ROOT}/secretary-data/"
    try:
        with tarfile.open(plain_archive, "r") as archive:
            actual: set[str] = set()
            for member in archive.getmembers():
                if member.name == ARCHIVE_ROOT and member.isdir():
                    continue
                if not member.name.startswith(prefix) or _unsafe_member(member):
                    raise RestoreError(f"unsafe archive entry: {member.name}")
                if member.isdir():
                    continue
                if not member.isfile():
                    raise RestoreError(f"unsupported archive entry type: {member.name}")
                relative = member.name.removeprefix(prefix)
                if relative == "versions.json":
                    continue
                actual.add(relative)
                source = archive.extractfile(member)
                if source is None:
                    raise RestoreError(f"could not read archive entry: {member.name}")
                digest = hashlib.sha256(source.read()).hexdigest()
                if checksums.get(relative) != digest:
                    raise RestoreError(f"checksum mismatch: {relative}")
                if member.name.startswith(data_prefix):
                    data_relative = relative.removeprefix("secretary-data/")
                    if not _allowed_data_path(data_relative, policy):
                        raise RestoreError(f"unexpected data component: {data_relative}")
            if actual != expected:
                missing = sorted(expected - actual)
                extra = sorted(actual - expected)
                detail = missing[0] if missing else extra[0]
                raise RestoreError(f"checksum manifest does not match archive: {detail}")
    except (OSError, tarfile.TarError) as exc:
        raise RestoreError(f"could not validate restore payload: {exc}") from None


def _unsafe_member(member: tarfile.TarInfo) -> bool:
    path = Path(member.name)
    return (
        path.is_absolute()
        or ".." in path.parts
        or member.issym()
        or member.islnk()
        or member.isdev()
        or member.isfifo()
    )


def _allowed_data_path(relative: str, policy: BackupPolicy) -> bool:
    if relative == "data-manifest.json":
        return True
    allowed = [
        path
        for component in policy.components
        if component.restore_action == "restore"
        for path in (component.path, *component.required_entries)
    ]
    # The memory component's exported file is not its journal directory, but the
    # journal is canonical state and is deliberately archived alongside it.
    allowed.append("memory/facts")
    allowed_paths = {Path(path) for path in allowed}
    allowed_paths.update(
        parent for path in tuple(allowed_paths) for parent in path.parents if parent != Path(".")
    )
    return any(
        relative == path.as_posix() or relative.startswith(f"{path.as_posix()}/")
        for path in allowed_paths
    )


def _stage_and_publish(plain_archive: Path, target: Path, *, policy: BackupPolicy) -> None:
    parent = target.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=f".{target.name}.restore-", dir=parent) as temporary:
            data_staging = Path(temporary) / "data"
            init_layout(data_staging)
            shutil.rmtree(data_staging / "memory" / "facts")
            with tarfile.open(plain_archive, "r") as archive:
                prefix = f"{ARCHIVE_ROOT}/secretary-data/"
                for member in archive.getmembers():
                    if not member.name.startswith(prefix):
                        continue
                    relative = Path(member.name.removeprefix(prefix))
                    if _unsafe_member(member) or not _allowed_data_path(relative.as_posix(), policy):
                        raise RestoreError(f"unsafe archive entry: {member.name}")
                    if member.isdir():
                        (data_staging / relative).mkdir(parents=True, exist_ok=True)
                        continue
                    if not member.isfile():
                        raise RestoreError(f"unsupported archive entry type: {member.name}")
                    destination = data_staging / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    source = archive.extractfile(member)
                    if source is None:
                        raise RestoreError(f"could not read archive entry: {member.name}")
                    with source, destination.open("wb") as output:
                        shutil.copyfileobj(source, output)
            _reject_existing_target(target)
            os.replace(data_staging, target)
    except (OSError, RuntimeError, tarfile.TarError) as exc:
        raise RestoreError(f"restore staging failed: {exc}") from None
