from __future__ import annotations

import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import uuid
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from secretary.backup_policy import (
    ARCHIVE_ROOT,
    BACKUP_KINDS,
    BACKUP_VERSION,
    BackupPolicy,
    CORE_POLICY,
    is_memory_journal_git_runtime_entry,
    policy_for,
    restore_plan_components,
    should_skip_data_entry,
)
from secretary.backup_verify import _verify_plain_tar
from secretary.config import ConfigError, load_config, validate_instance
from secretary import state_repo
from secretary.data import init_layout
from secretary._fsutil import file_lock, write_text_atomic
from secretary.tasks import KanboardClient, TaskAudit, TaskError, TaskReader, TaskWriter


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
    """Populate an empty board from the normalized export and prove parity on every retry.

    Returns the number of restored Pipeline cards; the sprint entities restored
    alongside them are counted in `restore-state.json`.
    """
    data_dir = data_dir.expanduser().resolve()
    with file_lock(data_dir / "board" / ".restore.lock"):
        try:
            cards = _normalized_cards(data_dir)
            sprints = _normalized_sprints(data_dir)
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
            # One read of the sprint board before any write: it decides what already exists,
            # which records a retry must not append twice, and whether the audit this data dir
            # carries was written against this backend or an earlier one.
            existing_sprints = _existing_sprints(data_dir, client, sprints)
            prefix = _restore_request_prefix(
                data_dir, writer.audit, set(existing) | set(existing_sprints)
            )
            for card in sorted(cards, key=_restore_card_order):
                current = existing.get(card["reference"])
                if current is None:
                    _create_restored_card(writer, card, prefix)
                target = _state_for_column(card["column"])
                writer.restore_card(
                    reference=card["reference"], metadata=_restore_board_metadata(card), target=target or "",
                    position=_restore_position(card), swimlane=str(card.get("swimlane") or ""),
                    request_id=f"{prefix}card:{card['reference']}",
                )
                live_comments = Counter(
                    str(comment.get("body") or "")
                    for comment in reader.show(card["reference"]).get("comments", [])
                )
                occurrences: dict[str, int] = {}
                for index, comment in enumerate(_restore_comments(card)):
                    occurrence = occurrences.get(comment, 0)
                    occurrences[comment] = occurrence + 1
                    if live_comments[comment] > occurrence:
                        continue
                    writer.restore_comment(
                        reference=card["reference"], body=comment, occurrence=occurrence,
                        request_id=f"{prefix}comment:{card['reference']}:{index}",
                    )
            actual = {card["reference"]: reader.show(card["reference"]) for card in cards}
            if any(_core_from_live(actual[card["reference"]]) != _core_from_export(card) for card in cards):
                _update_restore_state(data_dir, board="failed", board_parity="failed")
                raise RestoreError("board parity check failed")
            _import_sprints(data_dir, client, sprints, existing_sprints, prefix)
        except TaskError as exc:
            raise RestoreError(exc.message) from None
        _update_restore_state(
            data_dir,
            board="complete",
            board_parity="complete",
            board_count=len(cards),
            sprints="complete",
            sprint_parity="complete",
            sprint_count=len(sprints),
        )
        return len(cards)


def _existing_sprints(
    data_dir: Path, client: KanboardClient, sprints: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """The sprint entities the target already holds, read once before any write.

    An export without sprint entities never touches the sprint board, so it never
    asks the backend about one either.
    """
    if not sprints:
        return {}
    from secretary.sprints import SprintReader

    return {
        sprint["ref"]: sprint
        for sprint in SprintReader(client, data_dir=data_dir).export()
    }


def _import_sprints(
    data_dir: Path, client: KanboardClient, sprints: list[dict[str, Any]],
    existing: dict[str, dict[str, Any]], prefix: str,
) -> None:
    """Recreate the sprint entities and prove they match the export.

    Cards come first: a restored sprint names its current card, and the entity is
    only worth as much as the cards it points at.
    """
    if not sprints:
        # An installation that never opened a sprint has no board to create and
        # nothing to compare; creating an empty one would be recovery inventing state.
        return
    from secretary.data import normalize_sprint_entity
    from secretary.sprints import SprintReader, SprintWriter, ensure_sprint_board

    ensure_sprint_board(client)
    reader = SprintReader(client, data_dir=data_dir)
    writer = SprintWriter(client, data_dir=data_dir)
    unexpected = set(existing) - {sprint["reference"] for sprint in sprints}
    if unexpected:
        raise RestoreError("sprint board is not empty or does not match normalized restore data")
    for sprint in sprints:
        reference = sprint["reference"]
        if reference not in existing:
            writer.create(
                role="steward", actor="restore", goal=sprint["goal"],
                definition_of_done=sprint["definition_of_done"],
                repositories=list(sprint["repositories"]), reference=reference,
                request_id=f"{prefix}sprint-create:{reference}",
            )
        writer.restore(
            reference=reference, values=_restore_sprint_metadata(sprint),
            request_id=f"{prefix}sprint:{reference}",
        )
        live_comments = Counter(
            str(comment.get("body") or "")
            for comment in existing.get(reference, {}).get("comments", [])
        )
        occurrences: dict[str, int] = {}
        for index, comment in enumerate(str(entry["text"]) for entry in sprint["comments"]):
            occurrence = occurrences.get(comment, 0)
            occurrences[comment] = occurrence + 1
            if live_comments[comment] > occurrence:
                continue
            writer.restore_comment(
                reference=reference, body=comment, occurrence=occurrence,
                request_id=f"{prefix}sprint-comment:{reference}:{index}",
            )
    live = {entity["reference"]: entity for entity in map(normalize_sprint_entity, reader.export())}
    if any(_sprint_core(live.get(sprint["reference"], {})) != _sprint_core(sprint) for sprint in sprints):
        _update_restore_state(data_dir, sprints="failed", sprint_parity="failed")
        raise RestoreError("sprint parity check failed")


SPRINT_PARITY_FIELDS = (
    "reference", "goal", "definition_of_done", "repositories", "status", "budget",
    "current_task", "resume", "audit",
)


def _sprint_core(sprint: dict[str, Any]) -> dict[str, Any]:
    """The exported sprint contract, without what a rewrite cannot reproduce.

    Kanboard stamps its own creation time on a restored record, so records compare
    by body; the source timestamps of the entity travel in its audit metadata and
    do compare exactly.
    """
    core: dict[str, Any] = {field: sprint.get(field) for field in SPRINT_PARITY_FIELDS}
    core["comments"] = [
        str(comment.get("text") or "")
        for comment in sprint.get("comments", [])
        if isinstance(comment, dict)
    ]
    return core


def _restore_sprint_metadata(sprint: dict[str, Any]) -> dict[str, str]:
    resume = sprint.get("resume")
    return {
        "sprint_goal": str(sprint["goal"]),
        "sprint_definition_of_done": str(sprint["definition_of_done"]),
        "sprint_repositories": json.dumps(list(sprint["repositories"]), separators=(",", ":")),
        "sprint_status": str(sprint["status"]),
        "sprint_budget": json.dumps(
            {"by_type": sprint["budget"]["by_type"]}, sort_keys=True, separators=(",", ":")
        ),
        "sprint_current_task": str(sprint["current_task"]),
        "sprint_resume": (
            json.dumps(resume, sort_keys=True, separators=(",", ":")) if resume else ""
        ),
        "sprint_source_audit": json.dumps(sprint["audit"], sort_keys=True, separators=(",", ":")),
    }


DEFAULT_MEMORY_MODEL = "intfloat/multilingual-e5-large"
DEFAULT_MEMORY_DIM = 1024


def rebuild_memory_index(
    data_dir: Path, instance_dir: Path | None, *, python: Path | None = None,
    script: Path | None = None, model: str | None = None, dim: int | None = None,
    threads: int | None = None, runner=None,
) -> int:
    """Replace the derived index from restored canon.

    Canon is `state/memory/facts` in the private repo (docs/RECOVERY.md,
    "Layout"), so recovery rebuilds the index straight off the checkpoint the
    remote carries. The in-package implementation is the default; ``python`` and
    ``script`` keep the old memory-mcp argv contract available during the
    side-by-side window.
    """
    data_dir = data_dir.expanduser().resolve()
    memory_dir = data_dir / "memory"
    facts_dir = _memory_canon_dir(data_dir, instance_dir)
    try:
        if runner is not None:
            result = runner(facts_dir, memory_dir / "export.ndjson", memory_dir / "index.sqlite")
            count = int(result["parity"]["indexed"])
        elif python is not None or script is not None:
            if python is None or script is None or not isinstance(model, str) or not model or not isinstance(dim, int):
                raise RuntimeError("external memory rebuild contract is not configured")
            python = python.expanduser().absolute()
            script = script.expanduser().resolve()
            if not python.is_file() or not os.access(python, os.X_OK) or not script.is_file():
                raise RuntimeError("external memory rebuild argv contract is unavailable")
            completed = subprocess.run(
                [
                    str(python), str(script), "--canon", str(facts_dir), "--export",
                    str(memory_dir / "export.ndjson"), "--target-db",
                    str(memory_dir / "index.sqlite"), "--model", model, "--dim", str(dim),
                ],
                text=True,
                capture_output=True,
                check=False,
                timeout=MEMORY_REINDEX_TIMEOUT_SECONDS,
                env={
                    **os.environ,
                    "MEMORY_CACHE_DIR": str(memory_dir / "fastembed-cache"),
                    "MEMORY_THREADS": str(threads or 1),
                },
            )
            if completed.returncode:
                raise RuntimeError(
                    "memory reindex command failed: " + _reindex_error_detail(completed)
                )
            result = json.loads(completed.stdout)
            if not isinstance(result, dict) or result.get("ok") is not True:
                raise RuntimeError("memory reindex command reported failure")
            count = int(result["parity"]["indexed"])
        else:
            from .memory_reindex import rebuild
            from .memory_service import build_document_embedder

            selected_model = model or DEFAULT_MEMORY_MODEL
            result = rebuild(
                facts_dir,
                memory_dir / "export.ndjson",
                memory_dir / "index.sqlite",
                selected_model,
                dim or DEFAULT_MEMORY_DIM,
                document_embed=build_document_embedder(
                    selected_model, memory_dir / "fastembed-cache", threads or 1
                ),
            )
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
    if state.get("sprint_parity") == "failed":
        findings.append("sprint restore parity failed")
    elif "sprints" in state and state.get("sprints") != "complete":
        # A recovery staged before sprint entities joined the checkpoint records no
        # sprint state at all. Its board restore already finished against an export
        # that had none, so demanding the field now would only turn doctor red on an
        # instance that has nothing left to restore.
        findings.append("sprint restore is incomplete")
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
        if not isinstance(card.get("comments", []), list) or any(
            not isinstance(comment, dict) or not isinstance(comment.get("text"), str)
            for comment in card.get("comments", [])
        ):
            raise RestoreError("normalized board export has invalid comments")
    return sorted(cards, key=lambda card: str(card["reference"]))


def _normalized_sprints(data_dir: Path) -> list[dict[str, Any]]:
    """Read the exported sprint entities.

    A checkpoint written before sprints joined the export has no file at all; that
    reads as an installation without sprint entities, not as a broken export.
    """
    from secretary.sprints import SPRINT_REFERENCE_PREFIX

    path = data_dir / "board" / "sprints.json"
    if not path.is_file():
        return []
    try:
        sprints = json.loads(path.read_text(encoding="utf-8"))["sprints"]
    except (OSError, ValueError, KeyError, TypeError):
        raise RestoreError("normalized sprint export is unavailable") from None
    if not isinstance(sprints, list) or any(not isinstance(sprint, dict) for sprint in sprints):
        raise RestoreError("normalized sprint export is invalid")
    refs = [sprint.get("reference") for sprint in sprints]
    if any(
        not isinstance(ref, str) or not ref.startswith(SPRINT_REFERENCE_PREFIX) for ref in refs
    ) or len(set(refs)) != len(refs):
        raise RestoreError("normalized sprint export has invalid references")
    for sprint in sprints:
        if not isinstance(sprint.get("goal"), str) or not sprint["goal"].strip():
            raise RestoreError("normalized sprint export has an invalid goal")
        if sprint.get("status") not in {"open", "closed", "stopped"}:
            raise RestoreError("normalized sprint export has an invalid status")
        if not isinstance(sprint.get("definition_of_done"), str) or not isinstance(
            sprint.get("current_task"), str
        ):
            raise RestoreError("normalized sprint export has invalid entity text")
        if not isinstance(sprint.get("repositories"), list) or any(
            not isinstance(repo, str) for repo in sprint["repositories"]
        ):
            raise RestoreError("normalized sprint export has invalid repositories")
        budget = sprint.get("budget")
        if not isinstance(budget, dict) or not isinstance(budget.get("by_type"), dict) or any(
            not isinstance(count, int) for count in budget["by_type"].values()
        ):
            raise RestoreError("normalized sprint export has an invalid budget")
        if sprint.get("resume") is not None and not isinstance(sprint.get("resume"), dict):
            raise RestoreError("normalized sprint export has an invalid resume entry")
        if not isinstance(sprint.get("audit"), dict):
            raise RestoreError("normalized sprint export has invalid audit metadata")
        if not isinstance(sprint.get("comments", []), list) or any(
            not isinstance(comment, dict) or not isinstance(comment.get("text"), str)
            for comment in sprint.get("comments", [])
        ):
            raise RestoreError("normalized sprint export has invalid records")
        sprint.setdefault("comments", [])
    return sorted(sprints, key=lambda sprint: str(sprint["reference"]))


def _restore_request_prefix(data_dir: Path, audit: TaskAudit, live_refs: set[str]) -> str:
    """Return the request-id namespace this recovery writes its audit under.

    Restore events are durable: the checkpoint of a recovered instance carries them, so a
    second recovery from that checkpoint meets its own request ids again. Reusing them
    would short-circuit every write as already committed against a backend that holds
    nothing, and recovery would fail reading an entity it never created. A namespace whose
    committed events name entities the target does not have was written against an earlier
    backend, so this recovery takes a fresh one; `restore-state.json` keeps it so the
    retries of one recovery stay on the same namespace.
    """
    state = restore_state(data_dir)
    token = state.get("restore_namespace")
    if not isinstance(token, str) or not token or not _namespace_is_local(audit, token, live_refs):
        token = uuid.uuid4().hex
        # `sprints` is also what marks a restore state as one that tracks the sprint step
        # at all; a recovery staged before sprint entities joined the checkpoint has no
        # such key, and doctor reads its absence as nothing left to restore.
        _update_restore_state(
            data_dir, restore_namespace=token, sprints=state.get("sprints", "pending")
        )
    return f"restore:{token}:"


def _namespace_is_local(audit: TaskAudit, token: str, live_refs: set[str]) -> bool:
    prefix = f"restore:{token}:"
    return all(
        str(event.get("ref") or "") in live_refs
        for event in audit.events()
        if str(event.get("request_id") or "").startswith(prefix)
    )


def _create_restored_card(writer: TaskWriter, card: dict[str, Any], prefix: str) -> None:
    fields = _restore_fields(card)
    writer.create(
        role="steward", actor="restore", project=fields["project"], task_type=fields["task_type"],
        title=card["title"], description=card["description"], reference=card["reference"],
        blocked_by=fields["blocked_by"], head=fields["head"], review_head=fields["review_head"],
        slug=fields["slug"], base_branch=fields["base_branch"], complexity=fields["complexity"],
        family_preference=fields["family_preference"], codex_launch_mode=fields["codex_launch_mode"],
        request_id=f"{prefix}create:{card['reference']}",
    )


def _restore_board_metadata(card: dict[str, Any]) -> dict[str, str]:
    result = {str(key): str(value) for key, value in card["metadata"].items()}
    for key, value in _restore_fields(card).items():
        result.setdefault(key, value)
    return result


def _restore_comments(card: dict[str, Any]) -> list[str]:
    return [str(comment["text"]) for comment in card.get("comments", [])]


def _restore_position(card: dict[str, Any]) -> int | None:
    position = card.get("position")
    return position if isinstance(position, int) and position > 0 else None


def _restore_card_order(card: dict[str, Any]) -> tuple[str, str, int, str]:
    return (
        str(card["column"]),
        str(card.get("swimlane") or ""),
        _restore_position(card) or 0,
        str(card["reference"]),
    )


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
            "position": _restore_position(card),
            "swimlane": str(card.get("swimlane") or "") or None,
            "comments": [{"body": body} for body in _restore_comments(card)],
    }


def _core_from_live(card: dict[str, Any]) -> dict[str, Any]:
    return {
        "ref": card.get("ref"), "title": card.get("title"), "description": card.get("description"),
        "state": card.get("state"), "project": card.get("project"), "type": card.get("type"),
        "blocked_by": card.get("blocked_by"), "claim": card.get("claim"),
        "routing": {"complexity": card["routing"].get("complexity"), "family_preference": card["routing"].get("family_preference"), "head": card["routing"].get("head_override"), "review_head": card["routing"].get("review_head_override"), "resolved_head": card["routing"].get("resolved_worker_head"), "resolved_review_head": card["routing"].get("resolved_review_head"), "codex_launch_mode": card["routing"].get("codex_launch_mode")},
        "workspace": card.get("workspace"),
        "position": card.get("position"),
        "swimlane": card.get("extensions", {}).get("kanboard", {}).get("swimlane"),
        "comments": [{"body": str(comment.get("body") or "")} for comment in card.get("comments", [])],
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
    dry_run: bool = False,
) -> RestorePlan:
    _, target, target_identity = _target(instance_path)
    _reject_existing_target(target)
    archive = archive.expanduser()
    if not archive.is_file():
        raise RestoreError(f"archive not found: {archive}")

    try:
        verified = _verify_plain_tar(archive)
    except RuntimeError as exc:
        raise RestoreError(str(exc)) from None
    else:
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
            archive=archive,
            backup_kind=kind,
            backup_version=BACKUP_VERSION,
            data_dir=target,
            components=restore_plan_components(policy),
            instance_identity=target_identity,
        )
        if dry_run:
            return plan
        _stage_and_publish(archive, target, policy=policy)
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
    return not should_skip_data_entry(Path(relative), policy=policy)


def _stage_and_publish(plain_archive: Path, target: Path, *, policy: BackupPolicy) -> None:
    parent = target.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=f".{target.name}.restore-", dir=parent) as temporary:
            data_staging = Path(temporary) / "data"
            init_layout(data_staging)
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
            _update_restore_state(
                data_staging,
                board="pending",
                sprints="pending",
                memory_index="pending",
                reconcile="pending",
                # The archive carries the audit and the restore progress of whatever
                # recovery produced it; this data dir restores into its own backend.
                restore_namespace=uuid.uuid4().hex,
            )
            _reject_existing_target(target)
            os.replace(data_staging, target)
    except RestoreError:
        raise
    except (OSError, tarfile.TarError) as exc:
        raise RestoreError(f"restore staging failed: {exc}") from None


def _memory_canon_dir(data_dir: Path, instance_dir: Path | None) -> Path:
    """Canon facts, from the private repo; the data dir is the pre-flatten path."""
    if instance_dir is not None:
        facts_dir = state_repo.memory_facts_dir(instance_dir)
        if facts_dir.is_dir():
            return facts_dir
        raise RestoreError(f"memory canon is not available for index rebuild: {facts_dir}")
    legacy = data_dir / "memory" / "facts"
    if not legacy.is_dir():
        raise RestoreError("memory facts are not available for index rebuild")
    return legacy
