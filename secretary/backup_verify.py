from __future__ import annotations

import json
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
    BackupPolicy,
    component_archive_name,
    is_memory_journal_git_entry,
    policy_for,
)


@dataclass(frozen=True)
class VerifyResult:
    code: int
    findings: list[str]
    warnings: list[str]
    manifest: dict[str, Any] | None = None


def verify_backup(
    archive: Path,
    *,
    identity: Path | None = None,
    age_command: str = "age",
    decrypt=None,
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

    policy = _policy_from_manifest(manifest)
    required_entries = _required_entries_for_manifest(manifest, policy)
    missing = sorted(required_entries - names)
    findings.extend(f"missing required archive entry: {name}" for name in missing)

    if not isinstance(manifest, dict):
        findings.append("versions manifest must be an object")
    else:
        raw_kind = manifest.get("backup_kind", manifest.get("kind", "full"))
        if manifest.get("version") != BACKUP_VERSION:
            findings.append("unsupported backup version")
        if raw_kind not in BACKUP_KINDS:
            findings.append("unsupported backup kind")
        findings.extend(_verify_manifest_components(manifest, policy, members, names))

    if policy.kind == "core":
        findings.extend(_verify_core_archive(names, path, policy))

    forbidden_names = [name for name in sorted(names) if _is_forbidden_archive_entry(name)]
    findings.extend(f"forbidden archive entry: {name}" for name in forbidden_names)
    transcript_copy_names = [
        name
        for name in sorted(names)
        if Path(name).parts[:4]
        == (
            ARCHIVE_ROOT,
            "secretary-data",
            "transcripts",
            "copies",
        )
    ]
    findings.extend(
        f"unexpected transcript payload copy: {name}" for name in transcript_copy_names
    )
    return VerifyResult(
        1 if findings else 0,
        findings,
        warnings,
        manifest if isinstance(manifest, dict) else None,
    )


def _policy_from_manifest(manifest: Any) -> BackupPolicy:
    if not isinstance(manifest, dict):
        return policy_for("full")
    raw_kind = manifest.get("backup_kind", manifest.get("kind", "full"))
    return policy_for(raw_kind) or policy_for("full")


def _required_entries_for_manifest(manifest: Any, policy: BackupPolicy) -> set[str]:
    entries = set(policy.required_entries)
    components = manifest.get("components") if isinstance(manifest, dict) else None
    if (
        policy.kind == "full"
        and isinstance(components, dict)
        and _is_legacy_full_manifest(manifest, components)
    ):
        entries.discard(f"{ARCHIVE_ROOT}/secretary-data/runs/claims.json")
    return entries


def _verify_manifest_components(
    manifest: dict[str, Any],
    policy: BackupPolicy,
    members: list[tarfile.TarInfo],
    names: set[str],
) -> list[str]:
    findings: list[str] = []
    components = manifest.get("components")
    if not isinstance(components, dict):
        return ["versions manifest has no components object"]

    required_components = set(policy.required_components)
    if policy.kind == "full" and _is_legacy_full_manifest(manifest, components):
        required_components.discard("runs_state")
    missing_components = sorted(required_components - set(components))
    findings.extend(f"versions manifest missing component: {name}" for name in missing_components)
    component_policies = {component.name: component for component in policy.components}
    for name in sorted(required_components & set(components)):
        component = components.get(name)
        if not isinstance(component, dict) or not isinstance(component.get("path"), str):
            findings.append(f"versions manifest component has no path: {name}")
            continue
        archive_name = component_archive_name(component["path"])
        if not _archive_has_path(names, archive_name):
            findings.append(f"component path missing from archive: {name}")
            continue
        component_policy = component_policies[name]
        if component_policy.requires_raw_board_data:
            findings.extend(_verify_raw_board_component(members, names, archive_name))
        for field in component_policy.required_fields:
            value = component.get(field)
            if not isinstance(value, str):
                findings.append(f"{name} component has no {field} path")
                continue
            field_archive_name = component_archive_name(value)
            if not _archive_has_path(names, field_archive_name):
                findings.append(f"{name} component path missing from archive: {field}")
    return findings


def _is_legacy_full_manifest(manifest: dict[str, Any], components: dict[str, Any]) -> bool:
    return "backup_kind" not in manifest and "kind" not in manifest and "runs_state" not in components


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
    data_relative = Path(*parts[2:]) if parts[:2] == (ARCHIVE_ROOT, "secretary-data") else Path()
    return (
        (".git" in parts and not is_memory_journal_git_entry(data_relative))
        or any(part.startswith(".env") for part in parts)
        or "index.sqlite" in parts
        or "backups" in parts
        or any(part.endswith(".service") or part.endswith(".timer") for part in parts)
        or (len(parts) >= 3 and parts[1:3] == ("secretary-data", "worktrees"))
    )


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


def _verify_core_archive(names: set[str], path: Path, policy: BackupPolicy) -> list[str]:
    findings: list[str] = []
    raw_entries = [
        name
        for name in names
        if Path(name).parts[:3] == (ARCHIVE_ROOT, "secretary-data", "board")
        and len(Path(name).parts) > 3
        and Path(name).parts[3].startswith("kanboard-raw-")
    ]
    findings.extend(f"core archive contains raw board dump: {name}" for name in raw_entries)
    findings.extend(
        f"core archive contains full-only entry: {name}"
        for name in sorted(set(policy.forbidden_entries) & names)
    )
    try:
        with tarfile.open(path, "r") as archive:
            cards = _read_member_json(archive, f"{ARCHIVE_ROOT}/secretary-data/board/cards.json")
    except (tarfile.TarError, OSError, json.JSONDecodeError, UnicodeError) as exc:
        return [*findings, f"could not inspect core board export: {exc}"]
    card_list = cards.get("cards") if isinstance(cards, dict) else None
    if not isinstance(card_list, list):
        findings.append("core board export has no cards list")
        return findings
    done_refs = [
        str(card.get("reference") or card.get("id") or "(unknown)")
        for card in card_list
        if isinstance(card, dict) and str(card.get("column", "")).casefold() == "done"
    ]
    findings.extend(f"core archive contains Done card: {ref}" for ref in done_refs)
    return findings
