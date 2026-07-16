from __future__ import annotations

import json
import os
import shutil
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from secretary.backup_policy import ARCHIVE_ROOT, BACKUP_KINDS, BACKUP_VERSION, policy_for
from secretary.backup_verify import _decrypt_with_age, _verify_plain_tar
from secretary.config import ConfigError, load_config, validate_instance
from secretary.data import init_layout


@dataclass(frozen=True)
class RestorePlan:
    archive: Path
    backup_kind: str
    backup_version: int
    data_dir: Path
    components: tuple[str, ...]
    instance_identity: dict[str, str]


class RestoreError(RuntimeError):
    pass


def bootstrap_empty(
    instance_path: Path, *, data_dir: Path | None = None, dry_run: bool = False
) -> RestorePlan:
    _, target, identity = _target(instance_path, data_dir)
    _reject_existing_target(target)
    plan = RestorePlan(
        archive=Path(), backup_kind="empty", backup_version=BACKUP_VERSION,
        data_dir=target, components=("board", "memory", "runs"), instance_identity=identity,
    )
    if not dry_run:
        init_layout(target)
    return plan


def restore_backup(
    archive: Path,
    instance_path: Path,
    *,
    age_identity: Path | None,
    data_dir: Path | None = None,
    dry_run: bool = False,
    age_command: str = "age",
    decrypt=None,
) -> RestorePlan:
    _, target, target_identity = _target(instance_path, data_dir)
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
            archive=archive, backup_kind=kind, backup_version=BACKUP_VERSION,
            data_dir=target, components=policy.required_components, instance_identity=target_identity,
        )
        if dry_run:
            return plan
        _stage_and_publish(plain, target)
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
        "next_steps": ["board restore", "memory index rebuild", "reconcile"],
    }


def _target(instance_path: Path, data_dir: Path | None) -> tuple[Path, Path, dict[str, str]]:
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
    selected = data_dir if data_dir is not None else Path(str(configured_dir))
    target = selected.expanduser().resolve()
    if not target.is_absolute():
        raise RestoreError("target data root must be absolute")
    if data_dir is not None and target != Path(str(configured_dir)).expanduser().resolve():
        raise RestoreError("target data root must match instance data_dir")
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


def _reject_existing_target(target: Path) -> None:
    if target.exists():
        raise RestoreError(f"target data root already exists: {target}")


def _stage_and_publish(plain_archive: Path, target: Path) -> None:
    parent = target.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.restore-", dir=parent))
        data_staging = staging / "data"
        init_layout(data_staging)
        shutil.rmtree(data_staging / "memory" / "facts")
        with tarfile.open(plain_archive, "r") as archive:
            prefix = f"{ARCHIVE_ROOT}/secretary-data/"
            for member in archive.getmembers():
                if member.name.startswith(prefix) and (member.isfile() or member.isdir()):
                    relative = Path(member.name.removeprefix(prefix))
                    if relative.is_absolute() or ".." in relative.parts:
                        raise RestoreError(f"unsafe archive entry: {member.name}")
                    if relative.parts and relative.parts[0] != "backups":
                        destination = data_staging / relative
                        if member.isdir():
                            destination.mkdir(parents=True, exist_ok=True)
                            continue
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        source = archive.extractfile(member)
                        if source is None:
                            raise RestoreError(f"could not read archive entry: {member.name}")
                        with source, destination.open("wb") as output:
                            shutil.copyfileobj(source, output)
        os.replace(data_staging, target)
        shutil.rmtree(staging, ignore_errors=True)
    except (OSError, tarfile.TarError) as exc:
        raise RestoreError(f"restore staging failed: {exc}") from None
