from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterator

import fcntl

from secretary.config import ConfigError, load_config, validate_instance
from secretary._fsutil import sha256_file
from secretary.data import (
    DataExport,
    export_all,
    init_layout,
    raw_kanboard_dump,
)
from secretary.backup_policy import (
    ARCHIVE_ROOT,
    BACKUP_KINDS,
    BACKUP_VERSION,
    BACKUPS_MAX_BYTES,
    BackupKind,
    build_components_manifest,
    policy_for,
    should_skip_data_entry,
)
from secretary.backup_retention import (
    BackupHealth,
    apply_retention as _apply_retention,
    backup_archives as _backup_archives,
    check_backup_health as _check_backup_health,
    remove_path_quietly as _remove_path_quietly,
)
from secretary.backup_verify import VerifyResult, verify_backup


PIPELINE_WORKTREE = Path("/home/dev/orca/workspaces/triggered-agents/pipeline")
ORCA_STATE_DIRS = (Path.home() / ".orca", Path.home() / ".config" / "orca")
PIPELINE_PAUSE_REASON = "secretary backup create"
PIPELINE_PAUSE_ACTOR = "secretary-backup"
PRODUCT_REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class BackupResult:
    archive: Path
    manifest: dict[str, Any]


def create_backup(
    instance_path: Path,
    *,
    data_dir: Path | None = None,
    recipient: str | None = None,
    copy_transcripts: bool = False,
    allow_claimed_worker: bool = False,
    caller_workspace: Path | None = None,
    pipeline_worktree: Path = PIPELINE_WORKTREE,
    pipeline_command: list[str] | None = None,
    age_command: str = "age",
    encrypt: Callable[[Path, Path, str], None] | None = None,
    backup_kind: BackupKind = "full",
) -> BackupResult:
    return create_backups(
        instance_path,
        data_dir=data_dir,
        recipient=recipient,
        copy_transcripts=copy_transcripts,
        allow_claimed_worker=allow_claimed_worker,
        caller_workspace=caller_workspace,
        pipeline_worktree=pipeline_worktree,
        pipeline_command=pipeline_command,
        age_command=age_command,
        encrypt=encrypt,
        backup_kinds=(backup_kind,),
    )[0]


def create_backups(
    instance_path: Path,
    *,
    data_dir: Path | None = None,
    recipient: str | None = None,
    copy_transcripts: bool = False,
    allow_claimed_worker: bool = False,
    caller_workspace: Path | None = None,
    pipeline_worktree: Path = PIPELINE_WORKTREE,
    pipeline_command: list[str] | None = None,
    age_command: str = "age",
    encrypt: Callable[[Path, Path, str], None] | None = None,
    backup_kinds: tuple[BackupKind, ...] = ("full",),
) -> list[BackupResult]:
    kinds = tuple(dict.fromkeys(backup_kinds))
    invalid = [kind for kind in kinds if kind not in BACKUP_KINDS]
    if invalid:
        raise RuntimeError(f"unsupported backup kind: {invalid[0]}")
    if not kinds:
        raise RuntimeError("at least one backup kind is required")

    exclude_workspace: Path | None = None
    if not allow_claimed_worker:
        _reject_claimed_worker_context()
    elif caller_workspace is None:
        raise RuntimeError(
            "backup create with allow_claimed_worker needs caller_workspace so the "
            "pipeline freeze can exclude the calling worker"
        )
    else:
        exclude_workspace = caller_workspace.expanduser().resolve()
    instance_file = _instance_file(instance_path)
    data_dir = (data_dir or _load_data_dir(instance_file)).expanduser().resolve()
    recipient = recipient or _age_recipient(instance_file)
    if not recipient:
        raise RuntimeError("age recipient is not configured")

    backups_dir = data_dir / "backups"
    backups_dir.mkdir(parents=True, exist_ok=True)
    created_at = datetime.now(UTC).replace(microsecond=0).isoformat()
    stamp = created_at.replace("+00:00", "Z").replace("-", "").replace(":", "")
    final_archives: list[Path] = []
    results: list[BackupResult] = []

    paused_by_us = False
    completed = False
    temp_paths: list[Path] = []
    with _backup_create_lock(backups_dir):
        final_archives = [
            _unique_archive_path(backups_dir / f"secretary-backup-{kind}-{stamp}.tar.age")
            for kind in kinds
        ]
        try:
            pre_pause = _pipeline_status(pipeline_worktree=pipeline_worktree, command=pipeline_command)
            if pre_pause.get("paused"):
                raise RuntimeError("pipeline is already paused; backup create must own the freeze")
            pause_status = _pipeline_action(
                "pause",
                pipeline_worktree=pipeline_worktree,
                command=pipeline_command,
                exclude_workspace=exclude_workspace,
            )
            paused_by_us = pause_status is None or _pause_owned_by_backup(pause_status)
            if not paused_by_us:
                raise RuntimeError("pipeline pause was not owned by backup create")

            init_layout(data_dir)
            raw_dump = raw_kanboard_dump(data_dir)
            exports = export_all(data_dir, copy_transcripts=copy_transcripts)

            for kind, final_archive in zip(kinds, final_archives, strict=True):
                policy = policy_for(kind)
                if policy is None:
                    raise RuntimeError(f"unsupported backup kind: {kind}")
                staging = Path(tempfile.mkdtemp(prefix=".secretary-backup-", suffix=".tmp"))
                temp_paths.append(staging)
                payload = staging / ARCHIVE_ROOT
                payload.mkdir()

                manifest = _build_versions_manifest(
                    created_at=created_at,
                    backup_kind=kind,
                    instance_file=instance_file,
                    data_dir=data_dir,
                    raw_dump=raw_dump.dump_dir,
                    exports=exports,
                )
                _copy_instance_config(instance_file.parent, payload / "instance")
                _copy_data_snapshot(data_dir, payload / "secretary-data", backup_kind=kind)
                if kind == "core":
                    core_board_count = _filter_core_board_export(
                        payload / "secretary-data" / "board"
                    )
                    manifest["components"]["board"]["count"] = core_board_count
                if kind == "full":
                    _write_orca_debug_snapshot(payload / "debug" / "orca-state")
                manifest["checksums"] = _payload_checksums(payload)
                _write_json(payload / "versions.json", manifest)

                plain_archive = staging / f"{kind}.tar"
                _write_tar(plain_archive, payload)
                temp_paths.append(plain_archive)

                encrypted_archive = staging / f"{kind}.tar.age"
                temp_paths.append(encrypted_archive)
                if encrypt is None:
                    _encrypt_with_age(plain_archive, encrypted_archive, recipient, age_command=age_command)
                else:
                    encrypt(plain_archive, encrypted_archive, recipient)
                os.replace(encrypted_archive, final_archive)
                results.append(BackupResult(archive=final_archive, manifest=manifest))
            _apply_retention(backups_dir, keep=set(final_archives), now=datetime.now(UTC))
            completed = True
        finally:
            try:
                if paused_by_us:
                    _pipeline_action(
                        "resume",
                        pipeline_worktree=pipeline_worktree,
                        command=pipeline_command,
                    )
            finally:
                for path in temp_paths:
                    _remove_path_quietly(path)
            if not completed:
                for final_archive in final_archives:
                    _remove_path_quietly(final_archive)

    missing = [path for path in final_archives if not path.is_file()]
    if missing:
        raise RuntimeError("encrypted archive was not created")
    return results


def check_backup_health(
    data_dir: Path,
    *,
    now: datetime | None = None,
    max_bytes: int = BACKUPS_MAX_BYTES,
) -> BackupHealth:
    return _check_backup_health(
        data_dir,
        now=now,
        max_bytes=max_bytes,
        archive_loader=_backup_archives,
    )


def _reject_claimed_worker_context() -> None:
    if os.environ.get("BOARD_ROLE") == "worker" or _claimed_workspace_from_cwd() is not None:
        raise RuntimeError(
            "backup create must not run from a claimed worker; use an operator context"
        )


def _claimed_workspace_from_cwd(cwd: Path | None = None) -> Path | None:
    current = (cwd or Path.cwd()).expanduser().resolve()
    for candidate in (current, *current.parents):
        if candidate.parent.parent.name != "workspaces":
            continue
        if (candidate / "TASK.md").is_file():
            return candidate
    return None


@contextmanager
def _backup_create_lock(backups_dir: Path) -> Iterator[None]:
    lock_path = backups_dir / ".create.lock"
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as exc:
        raise RuntimeError(f"could not open backup create lock: {exc}") from None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("another backup create is already running") from None
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _build_versions_manifest(
    *,
    created_at: str,
    backup_kind: BackupKind,
    instance_file: Path,
    data_dir: Path,
    raw_dump: Path,
    exports: dict[str, DataExport],
) -> dict[str, Any]:
    policy = policy_for(backup_kind)
    if policy is None:
        raise RuntimeError(f"unsupported backup kind: {backup_kind}")
    return {
        "version": BACKUP_VERSION,
        "backup_kind": backup_kind,
        "restore_capability": policy.restore_capability,
        "created_at": created_at,
        "tool": "secretary",
        "python": sys.version.split()[0],
        "git_commit": _git_commit(PRODUCT_REPO_ROOT),
        "instance": {
            "path": str(instance_file),
            "identity": _instance_identity(instance_file),
        },
        "data_dir": str(data_dir),
        "components": build_components_manifest(
            policy=policy,
            data_dir=data_dir,
            raw_dump=raw_dump,
            exports=exports,
        ),
    }


def _pipeline_action(
    action: str,
    *,
    pipeline_worktree: Path,
    command: list[str] | None,
    exclude_workspace: Path | None = None,
) -> dict[str, Any] | None:
    cmd = command or [sys.executable, "-m", "triggered_agents", "pipeline"]
    if action == "pause":
        args = [
            "--role",
            "steward",
            "pause",
            "freeze",
            "--reason",
            PIPELINE_PAUSE_REASON,
            "--actor",
            PIPELINE_PAUSE_ACTOR,
        ]
        if exclude_workspace is not None:
            args.extend(["--exclude-workspace", str(exclude_workspace)])
    elif action == "resume":
        args = ["--role", "steward", "resume"]
    else:
        raise RuntimeError(f"unknown pipeline action: {action}")
    env = os.environ.copy()
    pythonpath = str(pipeline_worktree)
    if env.get("PYTHONPATH"):
        pythonpath = f"{pythonpath}{os.pathsep}{env['PYTHONPATH']}"
    env["PYTHONPATH"] = pythonpath
    try:
        result = subprocess.run(
            [*cmd, *args],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        return json.loads(result.stdout) if result.stdout.strip() else None
    except FileNotFoundError:
        raise RuntimeError(f"pipeline command not found: {cmd[0]}") from None
    except subprocess.CalledProcessError as exc:
        if exclude_workspace is not None and _rejects_exclude_workspace(exc.stderr):
            raise RuntimeError(
                "pipeline dispatcher does not understand --exclude-workspace; it "
                "predates triggered-agents PR #85 and would relaunch the calling "
                "worker on resume, so refusing to pause instead of dropping the flag"
            ) from None
        reason = (exc.stderr or exc.stdout or "pipeline command failed").strip().splitlines()
        raise RuntimeError(reason[-1] if reason else "pipeline command failed") from None
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"pipeline command returned invalid JSON: {exc}") from None


def _rejects_exclude_workspace(stderr: str | None) -> bool:
    text = (stderr or "").lower()
    return "unrecognized arguments" in text and "--exclude-workspace" in text


def _pipeline_status(
    *,
    pipeline_worktree: Path,
    command: list[str] | None,
) -> dict[str, Any]:
    cmd = command or [sys.executable, "-m", "triggered_agents", "pipeline"]
    env = os.environ.copy()
    pythonpath = str(pipeline_worktree)
    if env.get("PYTHONPATH"):
        pythonpath = f"{pythonpath}{os.pathsep}{env['PYTHONPATH']}"
    env["PYTHONPATH"] = pythonpath
    try:
        result = subprocess.run(
            [*cmd, "--role", "steward", "pause-status"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
    except FileNotFoundError:
        raise RuntimeError(f"pipeline command not found: {cmd[0]}") from None
    except subprocess.CalledProcessError as exc:
        reason = (exc.stderr or exc.stdout or "pipeline command failed").strip().splitlines()
        raise RuntimeError(reason[-1] if reason else "pipeline command failed") from None
    try:
        status = json.loads(result.stdout) if result.stdout.strip() else {}
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"pipeline command returned invalid JSON: {exc}") from None
    return status if isinstance(status, dict) else {}


def _pause_owned_by_backup(status: dict[str, Any]) -> bool:
    return (
        status.get("paused") is True
        and status.get("actor") == PIPELINE_PAUSE_ACTOR
        and status.get("reason") == PIPELINE_PAUSE_REASON
        and (status.get("mode") == "freeze" or status.get("internal_mode") == "hard")
    )


def _copy_instance_config(source: Path, destination: Path) -> None:
    _copy_tree_filtered(
        source,
        destination,
        skip=lambda relative: ".git" in relative.parts or relative.name.startswith(".env"),
    )


def _copy_data_snapshot(data_dir: Path, destination: Path, *, backup_kind: BackupKind) -> None:
    policy = policy_for(backup_kind)
    if policy is None:
        raise RuntimeError(f"unsupported backup kind: {backup_kind}")

    def skip(relative: Path) -> bool:
        return should_skip_data_entry(relative, policy=policy)

    _copy_tree_filtered(data_dir, destination, skip=skip)


def _filter_core_board_export(board_dir: Path) -> int:
    cards_path = board_dir / "cards.json"
    ndjson_path = board_dir / "cards.ndjson"
    export_path = board_dir / "export.json"
    try:
        payload = json.loads(cards_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError) as exc:
        raise RuntimeError(f"could not read core board export: {exc}") from None
    cards = payload.get("cards") if isinstance(payload, dict) else None
    if not isinstance(cards, list):
        raise RuntimeError("core board export has no cards list")
    filtered = [
        card for card in cards
        if not (isinstance(card, dict) and str(card.get("column", "")).casefold() == "done")
    ]
    payload["cards"] = filtered
    _write_json(cards_path, payload)
    ndjson_path.write_text(
        "".join(json.dumps(card, sort_keys=True) + "\n" for card in filtered),
        encoding="utf-8",
    )
    try:
        export_payload = json.loads(export_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        export_payload = {"version": 1}
    if isinstance(export_payload, dict):
        export_payload["card_count"] = len(filtered)
        export_payload["policy"] = {
            **(export_payload.get("policy") if isinstance(export_payload.get("policy"), dict) else {}),
            "done_cards": "excluded",
        }
        _write_json(export_path, export_payload)
    return len(filtered)


def _copy_tree_filtered(source: Path, destination: Path, *, skip: Callable[[Path], bool]) -> None:
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if skip(relative):
            continue
        if path.is_symlink():
            continue
        target = destination / relative
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif path.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target, follow_symlinks=False)


def _write_orca_debug_snapshot(destination: Path) -> None:
    entries: list[dict[str, Any]] = []
    for root in ORCA_STATE_DIRS:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if path.is_symlink() or not path.is_file():
                continue
            stat = path.stat()
            entries.append(
                {
                    "root": str(root),
                    "relative_path": path.relative_to(root).as_posix(),
                    "bytes": stat.st_size,
                    "mtime": int(stat.st_mtime),
                }
            )
    destination.mkdir(parents=True, exist_ok=True)
    _write_json(destination / "inventory.json", {"version": 1, "files": entries})


def _write_tar(destination: Path, source: Path) -> None:
    try:
        with tarfile.open(destination, "w") as archive:
            archive.add(source, arcname=ARCHIVE_ROOT, recursive=True)
    except (tarfile.TarError, OSError) as exc:
        raise RuntimeError(f"could not write backup tar: {exc}") from None


def _encrypt_with_age(source: Path, destination: Path, recipient: str, *, age_command: str) -> None:
    try:
        subprocess.run(
            [age_command, "-r", recipient, "-o", str(destination), str(source)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError:
        raise RuntimeError(f"age command not found: {age_command}") from None
    except subprocess.CalledProcessError as exc:
        reason = (exc.stderr or exc.stdout or "age encryption failed").strip().splitlines()
        raise RuntimeError(reason[-1] if reason else "age encryption failed") from None


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _payload_checksums(payload: Path) -> dict[str, str]:
    """Return checksums for every regular payload file except its manifest."""
    checksums: dict[str, str] = {}
    for path in sorted(payload.rglob("*")):
        if not path.is_file() or path.is_symlink() or path.name == "versions.json":
            continue
        checksums[path.relative_to(payload).as_posix()] = sha256_file(path)
    return checksums


def _load_data_dir(instance_file: Path) -> Path:
    report = validate_instance(instance_file)
    if not report.ok:
        raise RuntimeError("instance config is invalid")
    data_dir = report.instance.get("data_dir")
    if not isinstance(data_dir, str):
        raise RuntimeError("instance.yaml has no usable data_dir")
    return Path(data_dir)


def _age_recipient(instance_file: Path) -> str | None:
    if os.environ.get("SECRETARY_AGE_RECIPIENT"):
        return os.environ["SECRETARY_AGE_RECIPIENT"]
    try:
        instance = load_config(instance_file)
    except ConfigError as exc:
        raise RuntimeError(str(exc)) from None
    if not isinstance(instance, dict):
        return None
    offsite = instance.get("offsite")
    if not isinstance(offsite, dict):
        return None
    recipient = offsite.get("age_recipient")
    return recipient if isinstance(recipient, str) and recipient else None


def _instance_identity(instance_file: Path) -> dict[str, str]:
    try:
        instance = load_config(instance_file)
    except ConfigError as exc:
        raise RuntimeError(str(exc)) from None
    offsite = instance.get("offsite") if isinstance(instance, dict) else None
    name = instance.get("name") if isinstance(instance, dict) else None
    remote = offsite.get("instance_remote") if isinstance(offsite, dict) else None
    if not isinstance(name, str) or not isinstance(remote, str):
        raise RuntimeError("instance.yaml has no usable identity")
    return {"name": name, "instance_remote": remote}


def _instance_file(path: Path) -> Path:
    path = path.expanduser()
    return path / "instance.yaml" if path.is_dir() else path


def _git_commit(repo_root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=repo_root,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None
    return result.stdout.strip() or None


def _unique_archive_path(path: Path) -> Path:
    suffix = 1
    while True:
        candidate = path if suffix == 1 else _suffixed_path(path, suffix)
        if not candidate.exists():
            return candidate
        suffix += 1


def _suffixed_path(path: Path, suffix: int) -> Path:
    name = path.name
    if name.endswith(".tar.age"):
        return path.with_name(f"{name.removesuffix('.tar.age')}-{suffix}.tar.age")
    return path.with_name(f"{path.stem}-{suffix}{path.suffix}")
