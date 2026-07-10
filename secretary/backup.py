from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from secretary.config import ConfigError, load_config
from secretary.data import (
    DataExport,
    export_all,
    init_layout,
    raw_kanboard_dump,
)


PIPELINE_WORKTREE = Path("/home/dev/orca/workspaces/triggered-agents/pipeline")
ORCA_STATE_DIRS = (Path.home() / ".orca", Path.home() / ".config" / "orca")
ARCHIVE_ROOT = "secretary-backup"
BACKUP_VERSION = 1
PIPELINE_PAUSE_REASON = "secretary backup create"
PIPELINE_PAUSE_ACTOR = "secretary-backup"
PRODUCT_REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class BackupResult:
    archive: Path
    manifest: dict[str, Any]


@dataclass(frozen=True)
class VerifyResult:
    code: int
    findings: list[str]
    warnings: list[str]
    manifest: dict[str, Any] | None = None


def create_backup(
    instance_path: Path,
    *,
    data_dir: Path | None = None,
    recipient: str | None = None,
    copy_transcripts: bool = True,
    pipeline_worktree: Path = PIPELINE_WORKTREE,
    pipeline_command: list[str] | None = None,
    age_command: str = "age",
    encrypt: Callable[[Path, Path, str], None] | None = None,
) -> BackupResult:
    instance_file = _instance_file(instance_path)
    data_dir = (data_dir or _load_data_dir(instance_file)).expanduser().resolve()
    recipient = recipient or _age_recipient(instance_file)
    if not recipient:
        raise RuntimeError("age recipient is not configured")

    backups_dir = data_dir / "backups"
    backups_dir.mkdir(parents=True, exist_ok=True)
    created_at = datetime.now(UTC).replace(microsecond=0).isoformat()
    stamp = created_at.replace("+00:00", "Z").replace("-", "").replace(":", "")
    final_archive = _reserve_unique_path(backups_dir / f"secretary-backup-{stamp}.tar.age")

    paused_by_us = False
    completed = False
    temp_paths: list[Path] = []
    try:
        pre_pause = _pipeline_status(pipeline_worktree=pipeline_worktree, command=pipeline_command)
        if pre_pause.get("paused"):
            if pre_pause.get("mode") != "freeze" and pre_pause.get("internal_mode") != "hard":
                raise RuntimeError("pipeline is already paused in drain; backup create needs freeze")
        else:
            pause_status = _pipeline_action(
                "pause",
                pipeline_worktree=pipeline_worktree,
                command=pipeline_command,
            )
            paused_by_us = pause_status is None or _pause_owned_by_backup(pause_status)

        init_layout(data_dir)
        raw_dump = raw_kanboard_dump(data_dir)
        exports = export_all(data_dir, copy_transcripts=copy_transcripts)

        staging = Path(tempfile.mkdtemp(prefix=".secretary-backup-", suffix=".tmp"))
        temp_paths.append(staging)
        payload = staging / ARCHIVE_ROOT
        payload.mkdir()

        manifest = _build_versions_manifest(
            created_at=created_at,
            instance_file=instance_file,
            data_dir=data_dir,
            raw_dump=raw_dump.dump_dir,
            exports=exports,
        )
        _copy_instance_config(instance_file.parent, payload / "instance")
        _copy_data_snapshot(data_dir, payload / "secretary-data")
        _write_json(payload / "versions.json", manifest)
        _write_orca_debug_snapshot(payload / "debug" / "orca-state")

        plain_archive = staging / "payload.tar"
        _write_tar(plain_archive, payload)
        temp_paths.append(plain_archive)

        encrypted_archive = staging / "payload.tar.age"
        temp_paths.append(encrypted_archive)
        if encrypt is None:
            _encrypt_with_age(plain_archive, encrypted_archive, recipient, age_command=age_command)
        else:
            encrypt(plain_archive, encrypted_archive, recipient)
        os.replace(encrypted_archive, final_archive)
        completed = True
    finally:
        if paused_by_us:
            try:
                _pipeline_action(
                    "resume",
                    pipeline_worktree=pipeline_worktree,
                    command=pipeline_command,
                )
            finally:
                for path in temp_paths:
                    _remove_path_quietly(path)
        if not completed:
            _remove_path_quietly(final_archive)

    if not final_archive.is_file():
        raise RuntimeError("encrypted archive was not created")
    return BackupResult(archive=final_archive, manifest=manifest)


def verify_backup(
    archive: Path,
    *,
    identity: Path | None = None,
    age_command: str = "age",
    decrypt: Callable[[Path, Path], None] | None = None,
) -> VerifyResult:
    archive = archive.expanduser()
    if not archive.is_file():
        return VerifyResult(2, [f"archive not found: {archive}"], [])

    if decrypt is None:
        if identity is None:
            return VerifyResult(2, ["age identity is not configured"], [])
        if not identity.expanduser().is_file():
            return VerifyResult(2, [f"age identity not found: {identity}"], [])
        if shutil.which(age_command) is None:
            return VerifyResult(2, [f"age command not found: {age_command}"], [])

    with tempfile.TemporaryDirectory(prefix=".secretary-verify-") as tmpdir:
        plain_archive = Path(tmpdir) / "payload.tar"
        try:
            if decrypt is None:
                _decrypt_with_age(
                    archive,
                    plain_archive,
                    identity=identity.expanduser(),
                    age_command=age_command,
                )
            else:
                decrypt(archive, plain_archive)
            return _verify_plain_tar(plain_archive)
        except RuntimeError as exc:
            return VerifyResult(1, [str(exc)], [])


def _verify_plain_tar(path: Path) -> VerifyResult:
    findings: list[str] = []
    warnings: list[str] = []
    try:
        with tarfile.open(path, "r") as archive:
            members = archive.getmembers()
            names = {member.name for member in members}
            manifest = _read_member_json(archive, f"{ARCHIVE_ROOT}/versions.json")
    except (tarfile.TarError, OSError, json.JSONDecodeError, UnicodeError) as exc:
        return VerifyResult(1, [f"invalid archive: {exc}"], [])

    required = {
        f"{ARCHIVE_ROOT}/versions.json",
        f"{ARCHIVE_ROOT}/instance/instance.yaml",
        f"{ARCHIVE_ROOT}/secretary-data/data-manifest.json",
        f"{ARCHIVE_ROOT}/secretary-data/board/cards.json",
        f"{ARCHIVE_ROOT}/secretary-data/board/cards.ndjson",
        f"{ARCHIVE_ROOT}/secretary-data/board/export.json",
        f"{ARCHIVE_ROOT}/secretary-data/memory/export.ndjson",
        f"{ARCHIVE_ROOT}/secretary-data/runs/runs.ndjson",
        f"{ARCHIVE_ROOT}/secretary-data/runs/watermarks.json",
        f"{ARCHIVE_ROOT}/secretary-data/runs/cards.json",
        f"{ARCHIVE_ROOT}/secretary-data/transcripts/inventory.json",
        f"{ARCHIVE_ROOT}/secretary-data/artifacts/inventory.json",
        f"{ARCHIVE_ROOT}/debug/orca-state/inventory.json",
    }
    missing = sorted(required - names)
    findings.extend(f"missing required archive entry: {name}" for name in missing)

    if not isinstance(manifest, dict):
        findings.append("versions manifest must be an object")
    else:
        if manifest.get("version") != BACKUP_VERSION:
            findings.append("unsupported backup version")
        components = manifest.get("components")
        if not isinstance(components, dict):
            findings.append("versions manifest has no components object")
        else:
            required_components = {
                "raw_board",
                "board",
                "memory",
                "runs",
                "transcripts",
                "artifacts",
                "debug_orca_state",
            }
            missing_components = sorted(required_components - set(components))
            findings.extend(
                f"versions manifest missing component: {name}" for name in missing_components
            )
            for name in sorted(required_components & set(components)):
                component = components.get(name)
                if not isinstance(component, dict) or not isinstance(component.get("path"), str):
                    findings.append(f"versions manifest component has no path: {name}")
                    continue
                archive_name = _component_archive_name(component["path"])
                if not _archive_has_path(names, archive_name):
                    findings.append(f"component path missing from archive: {name}")
                    continue
                if name == "raw_board":
                    findings.extend(_verify_raw_board_component(members, names, archive_name))

    forbidden_names = [
        name
        for name in sorted(names)
        if _is_forbidden_archive_entry(name)
    ]
    findings.extend(f"forbidden archive entry: {name}" for name in forbidden_names)
    return VerifyResult(1 if findings else 0, findings, warnings, manifest if isinstance(manifest, dict) else None)


def _build_versions_manifest(
    *,
    created_at: str,
    instance_file: Path,
    data_dir: Path,
    raw_dump: Path,
    exports: dict[str, DataExport],
) -> dict[str, Any]:
    return {
        "version": BACKUP_VERSION,
        "created_at": created_at,
        "tool": "secretary",
        "python": sys.version.split()[0],
        "git_commit": _git_commit(PRODUCT_REPO_ROOT),
        "instance": {
            "path": str(instance_file),
        },
        "data_dir": str(data_dir),
        "components": {
            "raw_board": {"path": _relative_to_data(data_dir, raw_dump)},
            **{
                name: {
                    "path": _relative_to_data(data_dir, export.path),
                    "count": export.count,
                    "source": export.source,
                }
                for name, export in sorted(exports.items())
            },
            "debug_orca_state": {"path": "debug/orca-state/inventory.json"},
        },
    }


def _pipeline_action(
    action: str,
    *,
    pipeline_worktree: Path,
    command: list[str] | None,
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
        reason = (exc.stderr or exc.stdout or "pipeline command failed").strip().splitlines()
        raise RuntimeError(reason[-1] if reason else "pipeline command failed") from None
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"pipeline command returned invalid JSON: {exc}") from None


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


def _copy_data_snapshot(data_dir: Path, destination: Path) -> None:
    allowed_roots = {"board", "memory", "runs", "transcripts", "artifacts"}

    def skip(relative: Path) -> bool:
        if not relative.parts:
            return False
        if relative.name.startswith(".env") or relative.name == "index.sqlite":
            return True
        if relative.parts[0] == "backups":
            return True
        if relative.parts[0] in allowed_roots or relative.name == "data-manifest.json":
            return any(part.startswith(".") for part in relative.parts)
        return True

    _copy_tree_filtered(data_dir, destination, skip=skip)


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


def _decrypt_with_age(source: Path, destination: Path, *, identity: Path, age_command: str) -> None:
    try:
        subprocess.run(
            [age_command, "-d", "-i", str(identity), "-o", str(destination), str(source)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        reason = (exc.stderr or exc.stdout or "age decryption failed").strip().splitlines()
        raise RuntimeError(reason[-1] if reason else "age decryption failed") from None


def _read_member_json(archive: tarfile.TarFile, name: str) -> Any:
    try:
        member = archive.extractfile(name)
    except KeyError:
        return None
    if member is None:
        return None
    return json.loads(member.read().decode("utf-8"))


def _is_forbidden_archive_entry(name: str) -> bool:
    parts = Path(name).parts
    return (
        ".git" in parts
        or any(part.startswith(".env") for part in parts)
        or "index.sqlite" in parts
        or "backups" in parts
        or any(part.endswith(".service") or part.endswith(".timer") for part in parts)
        or (len(parts) >= 3 and parts[1:3] == ("secretary-data", "worktrees"))
    )


def _component_archive_name(path: str) -> str:
    if path.startswith("debug/"):
        return f"{ARCHIVE_ROOT}/{path}"
    return f"{ARCHIVE_ROOT}/secretary-data/{path}"


def _archive_has_path(names: set[str], archive_name: str) -> bool:
    return archive_name in names or any(
        member_name.startswith(f"{archive_name}/") for member_name in names
    )


def _verify_raw_board_component(
    members: list[tarfile.TarInfo],
    names: set[str],
    archive_name: str,
) -> list[str]:
    findings: list[str] = []
    manifest_name = f"{archive_name}/manifest.json"
    data_prefix = f"{archive_name}/data/"
    if manifest_name not in names:
        findings.append("raw board dump missing manifest.json")
    if not any(member.isfile() and member.name.startswith(data_prefix) for member in members):
        findings.append("raw board dump has no data files")
    return findings


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _load_data_dir(instance_file: Path) -> Path:
    try:
        instance = load_config(instance_file)
    except ConfigError as exc:
        raise RuntimeError(str(exc)) from None
    if not isinstance(instance, dict) or not isinstance(instance.get("data_dir"), str):
        raise RuntimeError("instance.yaml has no usable data_dir")
    return Path(instance["data_dir"])


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


def _instance_file(path: Path) -> Path:
    path = path.expanduser()
    return path / "instance.yaml" if path.is_dir() else path


def _relative_to_data(data_dir: Path, path: Path) -> str:
    try:
        return path.relative_to(data_dir).as_posix()
    except ValueError:
        return str(path)


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


def _reserve_unique_path(path: Path) -> Path:
    suffix = 1
    while True:
        candidate = path if suffix == 1 else _suffixed_path(path, suffix)
        try:
            fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            suffix += 1
            continue
        try:
            os.close(fd)
        except OSError:
            pass
        return candidate


def _suffixed_path(path: Path, suffix: int) -> Path:
    name = path.name
    if name.endswith(".tar.age"):
        return path.with_name(f"{name.removesuffix('.tar.age')}-{suffix}.tar.age")
    return path.with_name(f"{path.stem}-{suffix}{path.suffix}")


def _remove_path_quietly(path: Path) -> None:
    try:
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()
    except OSError:
        pass
