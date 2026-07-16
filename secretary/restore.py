from __future__ import annotations

import json
import os
import shutil
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from secretary.backup_policy import (
    ARCHIVE_ROOT,
    BACKUP_KINDS,
    BACKUP_VERSION,
    CORE_POLICY,
    is_memory_journal_git_runtime_entry,
    policy_for,
    restore_plan_components,
    should_skip_data_entry,
)
from secretary.backup_verify import _decrypt_with_age, _verify_plain_tar
from secretary.config import ConfigError, load_config, validate_instance
from secretary.data import init_layout
from secretary._fsutil import sha256_stream, write_text_atomic
from secretary.tasks import KanboardClient, TaskError, TaskReader, TaskWriter


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


RESTORE_STATE_FILE = "restore-state.json"
MEMORY_REINDEX_TIMEOUT_SECONDS = 300


def restore_state(data_dir: Path) -> dict[str, Any]:
    """Read the derived restore progress record without treating it as canon."""
    try:
        value = json.loads((data_dir / RESTORE_STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def import_normalized_board(data_dir: Path, *, client: KanboardClient | None = None) -> int:
    """Populate an empty board from cards.json and prove parity on every retry."""
    data_dir = data_dir.expanduser().resolve()
    try:
        cards = _normalized_cards(data_dir)
        client = client or KanboardClient()
        reader = TaskReader(client)
        writer = TaskWriter(client, data_dir=data_dir)
        _, unresolved = writer.reconcile()
        if unresolved:
            raise RestoreError("board audit repair is required before restore")
        existing = {card["ref"]: card for card in reader.list()}
        unexpected = set(existing) - {card["reference"] for card in cards}
        if unexpected:
            raise RestoreError("board is not empty or does not match normalized restore data")
        for card in cards:
            current = existing.get(card["reference"])
            if current is None:
                _create_restored_card(writer, card)
            target = _state_for_column(card["column"])
            writer.restore_card(
                reference=card["reference"], metadata=_restore_board_metadata(card), target=target or ""
            )
        actual = {card["reference"]: reader.show(card["reference"]) for card in cards}
        if any(_core_from_live(actual[card["reference"]]) != _core_from_export(card) for card in cards):
            _update_restore_state(data_dir, board="failed", board_parity="failed")
            raise RestoreError("board parity check failed")
    except TaskError as exc:
        raise RestoreError(exc.message) from None
    _update_restore_state(data_dir, board="complete", board_parity="complete", board_count=len(cards))
    return len(cards)


def rebuild_memory_index(
    data_dir: Path, *, python: Path | None = None, script: Path | None = None, model: str | None = None,
    dim: int | None = None, runner=None,
) -> int:
    """Ask memory-mcp to replace its derived index from restored canon."""
    data_dir = data_dir.expanduser().resolve()
    memory_dir = data_dir / "memory"
    facts_dir = memory_dir / "facts"
    if not (facts_dir / ".git").is_dir():
        raise RestoreError("memory facts journal is not available for index rebuild")
    try:
        if runner is not None:
            result = runner(facts_dir, memory_dir / "export.ndjson", memory_dir / "index.sqlite")
            count = int(result["parity"]["indexed"])
        else:
            if python is None or script is None or not isinstance(model, str) or not model or not isinstance(dim, int):
                raise RuntimeError("memory-mcp rebuild contract is not configured")
            python = python.expanduser().absolute()
            script = script.expanduser().resolve()
            if not python.is_file() or not os.access(python, os.X_OK) or not script.is_file():
                raise RuntimeError("memory-mcp rebuild argv contract is unavailable")
            completed = subprocess.run(
                [
                    str(python), str(script), "--canon", str(facts_dir), "--export",
                    str(memory_dir / "export.ndjson"), "--target-db",
                    str(memory_dir / "index.sqlite"), "--model", model, "--dim", str(dim),
                ],
                text=True, capture_output=True, check=False, timeout=MEMORY_REINDEX_TIMEOUT_SECONDS,
            )
            if completed.returncode:
                raise RuntimeError(
                    "memory-mcp reindex command failed: " + _reindex_error_detail(completed)
                )
            result = json.loads(completed.stdout)
            if not isinstance(result, dict) or result.get("ok") is not True:
                raise RuntimeError("memory-mcp reindex command reported failure")
            count = int(result["parity"]["indexed"])
    except (OSError, RuntimeError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as exc:
        raise RestoreError(f"could not rebuild memory index: {exc}") from None
    _update_restore_state(data_dir, memory_index="complete", memory_index_count=count)
    return count


def restore_findings(data_dir: Path) -> list[str]:
    """Return stable, actionable restore findings for doctor."""
    state = restore_state(data_dir)
    findings: list[str] = []
    if not state:
        findings.append("restore is incomplete")
        return findings
    if state.get("board_parity") == "failed":
        findings.append("board restore parity failed")
    elif state.get("board") != "complete":
        findings.append("board restore is incomplete")
    if state.get("memory_index") != "complete":
        findings.append("memory index has not been rebuilt")
    if state.get("reconcile") != "complete":
        findings.append("managed reconcile has not been applied")
    return findings


def _reindex_error_detail(completed: subprocess.CompletedProcess[str]) -> str:
    """Return the public failure reason from memory-mcp's JSON contract."""
    try:
        result = json.loads(completed.stdout)
    except (TypeError, ValueError):
        return f"exit {completed.returncode}"
    error = result.get("error") if isinstance(result, dict) else None
    return error if isinstance(error, str) and error else f"exit {completed.returncode}"


def mark_reconcile_applied(data_dir: Path) -> None:
    """Mark the explicit live reconcile verification complete."""
    if not (data_dir / RESTORE_STATE_FILE).is_file():
        return
    _update_restore_state(data_dir, reconcile="complete")


def _normalized_cards(data_dir: Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads((data_dir / "board" / "cards.json").read_text(encoding="utf-8"))
        cards = payload["cards"]
    except (OSError, ValueError, KeyError, TypeError):
        raise RestoreError("normalized board export is unavailable") from None
    if not isinstance(cards, list) or any(not isinstance(card, dict) for card in cards):
        raise RestoreError("normalized board export is invalid")
    refs = [card.get("reference") for card in cards]
    if any(not isinstance(ref, str) or not ref for ref in refs) or len(set(refs)) != len(refs):
        raise RestoreError("normalized board export has invalid references")
    for card in cards:
        if not isinstance(card.get("column"), str) or _state_for_column(card["column"]) is None:
            raise RestoreError("normalized board export has an invalid column")
        if not isinstance(card.get("fields"), dict) or not isinstance(card.get("metadata"), dict):
            raise RestoreError("normalized board export has invalid task data")
        if not isinstance(card.get("title"), str) or not isinstance(card.get("description"), str):
            raise RestoreError("normalized board export has invalid task text")
    return sorted(cards, key=lambda card: str(card["reference"]))


def _create_restored_card(writer: TaskWriter, card: dict[str, Any]) -> None:
    fields = _restore_fields(card)
    writer.create(
        role="steward", actor="restore", project=fields["project"], task_type=fields["task_type"],
        title=card["title"], description=card["description"], reference=card["reference"],
        blocked_by=fields["blocked_by"], head=fields["head"], review_head=fields["review_head"],
        slug=fields["slug"], base_branch=fields["base_branch"], complexity=fields["complexity"],
        family_preference=fields["family_preference"], codex_launch_mode=fields["codex_launch_mode"],
        request_id=f"restore-create:{card['reference']}",
    )


def _restore_board_metadata(card: dict[str, Any]) -> dict[str, str]:
    result = {str(key): str(value) for key, value in card["metadata"].items()}
    for key, value in _restore_fields(card).items():
        result.setdefault(key, value)
    return result


def _restore_fields(card: dict[str, Any]) -> dict[str, str]:
    fields = card["fields"]
    metadata = card["metadata"]
    value = lambda name: str(metadata.get(name, fields.get(name, "")) or "")
    return {
        "project": value("project"), "task_type": value("task_type"), "blocked_by": value("blocked_by"),
        "head": value("head"), "review_head": value("review_head"), "slug": value("slug"),
        "base_branch": value("base_branch"),
        "complexity": _enum_or_default(value("complexity"), {"cheap", "standard", "hard", "frontier"}, "standard"),
        "family_preference": _enum_or_default(value("family_preference"), {"auto", "claude", "codex"}, "auto"),
        "codex_launch_mode": _enum_or_default(value("codex_launch_mode"), {"", "exec", "tui"}, ""),
    }


def _enum_or_default(value: str, allowed: set[str], default: str) -> str:
    return value if value in allowed else default


def _core_from_export(card: dict[str, Any]) -> dict[str, Any]:
    fields = _restore_fields(card)
    metadata = card["metadata"]
    return {
            "ref": card["reference"], "title": card["title"], "description": card["description"],
            "state": _state_for_column(card["column"]), "project": fields["project"],
            "type": fields["task_type"], "blocked_by": fields["blocked_by"] or None,
            "claim": {"worker": metadata.get("claim") or None, "claimed_at": None},
            "routing": {"complexity": fields["complexity"], "family_preference": fields["family_preference"], "head": fields["head"] or None, "review_head": fields["review_head"] or None, "resolved_head": metadata.get("resolved_head") or None, "resolved_review_head": metadata.get("resolved_review_head") or None, "codex_launch_mode": fields["codex_launch_mode"] or None},
            "workspace": {"slug": metadata.get("slug") or None, "base_branch": metadata.get("base_branch") or None},
    }


def _core_from_live(card: dict[str, Any]) -> dict[str, Any]:
    return {
        "ref": card.get("ref"), "title": card.get("title"), "description": card.get("description"),
        "state": card.get("state"), "project": card.get("project"), "type": card.get("type"),
        "blocked_by": card.get("blocked_by"), "claim": card.get("claim"),
        "routing": {"complexity": card["routing"].get("complexity"), "family_preference": card["routing"].get("family_preference"), "head": card["routing"].get("head_override"), "review_head": card["routing"].get("review_head_override"), "resolved_head": card["routing"].get("resolved_worker_head"), "resolved_review_head": card["routing"].get("resolved_review_head"), "codex_launch_mode": card["routing"].get("codex_launch_mode")},
        "workspace": card.get("workspace"),
    }


def _state_for_column(column: str) -> str | None:
    return {"Идеи": "ideas", "Ready": "ready", "In progress": "in_progress", "Validate": "validate", "Blocked": "blocked", "Done": "done"}.get(column)


def _update_restore_state(data_dir: Path, **changes: Any) -> None:
    state = restore_state(data_dir)
    state.update(changes)
    state["version"] = 1
    try:
        write_text_atomic(
            data_dir / RESTORE_STATE_FILE,
            json.dumps(state, indent=2, sort_keys=True) + "\n",
        )
    except RuntimeError as exc:
        raise RestoreError(f"could not record restore progress: {exc}") from None


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
        _update_restore_state(target, board="pending", memory_index="pending", reconcile="pending")
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
        raise RestoreError("invalid target instance: " + "; ".join(map(str, report.errors)))
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


def _validate_restore_payload(plain_archive: Path, manifest: dict[str, Any], policy: Any) -> None:
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
                relative = member.name.removeprefix(prefix)
                if member.name.startswith(data_prefix):
                    data_relative = relative.removeprefix("secretary-data/")
                    path = Path(data_relative)
                    if (
                        not _allowed_data_path(data_relative, policy)
                        and not is_memory_journal_git_runtime_entry(path)
                    ):
                        raise RestoreError(f"unexpected data component: {data_relative}")
                if member.isdir():
                    continue
                if not member.isfile():
                    raise RestoreError(f"unsupported archive entry type: {member.name}")
                if relative == "versions.json":
                    continue
                actual.add(relative)
                source = archive.extractfile(member)
                if source is None:
                    raise RestoreError(f"could not read archive entry: {member.name}")
                digest = sha256_stream(source)
                if checksums.get(relative) != digest:
                    raise RestoreError(f"checksum mismatch: {relative}")
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


def _allowed_data_path(relative: str, policy: Any) -> bool:
    return not should_skip_data_entry(Path(relative), policy=policy)


def _stage_and_publish(plain_archive: Path, target: Path, *, policy: Any) -> None:
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
                    if is_memory_journal_git_runtime_entry(relative):
                        continue
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
    except RestoreError:
        raise
    except (OSError, tarfile.TarError) as exc:
        raise RestoreError(f"restore staging failed: {exc}") from None
