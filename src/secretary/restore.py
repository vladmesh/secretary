from __future__ import annotations

import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from secretary import _proc, state_repo
from secretary._fsutil import file_lock, write_text_atomic
from secretary.backup_policy import (
    ARCHIVE_ROOT,
    BACKUP_KINDS,
    BACKUP_VERSION,
    CORE_POLICY,
    BackupPolicy,
    is_memory_journal_git_runtime_entry,
    policy_for,
    restore_plan_components,
    should_skip_data_entry,
)
from secretary.backup_verify import _verify_plain_tar
from secretary.board.normalized_checkpoint import NormalizedBoardError, validated_normalized_cards
from secretary.config import DataDirError, instance_data_dir, validate_instance
from secretary.data import init_layout
from secretary.product_issues import (
    ensure_swimlane,
    registered_projects,
)
from secretary.sprint_observer import (
    ObserverMetadataError,
    check_observer_profile,
    encode_observer,
    installed_observer_profiles,
    is_executable,
    parse_observer,
)
from secretary.tasks import (
    _STATE_BY_COLUMN,
    KanboardClient,
    TaskAudit,
    TaskError,
    TaskReader,
    TaskWriter,
    _matching_swimlane,
    _positive_int,
    _task_is_active,
    all_project_cards,
)
from triggered_agents.runtime.head import CODEX_LAUNCH_MODES


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
# Every card needs a kind; refuse rows the current board cannot place.
_RECORD_TYPES = {"task", "issue", "product"}
_PRODUCT_ISSUE_METADATA = (
    "record_type",
    "product_id",
    "product_projects",
    "issue_product",
    "issue_kind",
    "issue_priority",
    "issue_closed_reason",
)


def restore_state(data_dir: Path) -> dict[str, Any]:
    """Read the derived restore progress record without treating it as canon."""
    try:
        value = json.loads((data_dir / RESTORE_STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def import_normalized_board(
    data_dir: Path, *, client: KanboardClient | None = None, instance: Path | None = None
) -> int:
    """Populate an empty board from the normalized export and prove parity on every retry."""
    from secretary.sprints import sprint_admission_lock

    data_dir = data_dir.expanduser().resolve()
    # Restoring open sprints is set admission against create and reopen.
    with file_lock(data_dir / "board" / ".restore.lock"), sprint_admission_lock(data_dir):
        try:
            cards = _normalized_cards(
                data_dir, registered_project_ids=(registered_projects(instance) if instance else None)
            )
            sprints = _normalized_sprints(data_dir)
            # Validate both sets before the first backend write.
            _check_restored_observers(sprints, instance)
            _check_restored_admission(sprints, instance)
            if client is None:
                if instance is None:
                    raise RestoreError("restore requires the target instance to bind its board")
                client = KanboardClient.for_instance(instance)
            reader = TaskReader(client)
            writer = TaskWriter(client, data_dir=data_dir)
            _, unresolved = writer.reconcile(defer_restore_comments=True)
            if unresolved:
                raise RestoreError("board audit repair is required before restore")
            existing = _existing_board_cards(reader)
            unexpected = set(existing) - {card["reference"] for card in cards}
            if unexpected:
                raise RestoreError("board is not empty or does not match normalized restore data")
            # Read once before writes for idempotency and backend-audit binding.
            existing_sprints = _existing_sprints(data_dir, client, sprints)
            prefix = _restore_request_prefix(data_dir, writer.audit, set(existing) | set(existing_sprints))
            _validate_deferred_restore_comments(writer.audit, cards, sprints, prefix)
            ordered_cards = sorted(cards, key=_restore_card_order)
            for card in ordered_cards:
                current = existing.get(card["reference"])
                if current is None:
                    _create_restored_card(writer, card, prefix)
                target = _state_for_column(card["column"])
                writer.restore_card(
                    reference=card["reference"],
                    metadata=_restore_board_metadata(card),
                    target=target or "",
                    position=_restore_position(card),
                    swimlane=str(card.get("swimlane") or ""),
                    request_id=f"{prefix}card:{card['reference']}",
                )
            setup = reader.restore_snapshot()
            _restore_card_comments_batched(writer, ordered_cards, setup, prefix)
            for card in ordered_cards:
                if card.get("closed") and not setup[card["reference"]].get("closed"):
                    task_id = int(setup[card["reference"]]["id"].removeprefix("task_kanboard_"))
                    if client.call("closeTask", task_id=task_id) is not True:
                        raise RestoreError("could not close restored card")
            actual = reader.restore_snapshot()
            if any(_core_from_live(actual[card["reference"]]) != _core_from_export(card) for card in cards):
                _update_restore_state(data_dir, board="failed", board_parity="failed")
                raise RestoreError("board parity check failed")
            if _restored_order_mismatch(cards, actual):
                _update_restore_state(data_dir, board="failed", board_parity="failed")
                raise RestoreError("board parity check failed: restored card order")
            _import_sprints(data_dir, client, sprints, existing_sprints, prefix)
            pending_comments = [
                event for event in writer.audit.pending_events() if event.get("kind") == "restored_comment"
            ]
            if pending_comments:
                raise RestoreError("board comment audit repair is required before restore can complete")
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
    """The sprint entities the target already holds, read once before any write."""
    if not sprints:
        return {}
    from secretary.sprints import SprintReader

    return {sprint["ref"]: sprint for sprint in SprintReader(client, data_dir=data_dir).export()}


def _existing_board_cards(reader: TaskReader) -> dict[str, dict[str, Any]]:
    """Read both active and closed Pipeline records before deciding a restore is empty."""
    board_id, _, _ = reader._board()
    raw_cards = all_project_cards(reader.client, board_id)
    result: dict[str, dict[str, Any]] = {}
    for card in raw_cards:
        if not isinstance(card, dict):
            continue
        reference = card.get("reference")
        if isinstance(reference, str) and reference:
            previous = result.get(reference)
            if previous is None or (_task_is_active(card) and not _task_is_active(previous)):
                result[reference] = card
    return result


def _restore_card_comments_batched(
    writer: TaskWriter,
    cards: list[dict[str, Any]],
    live: dict[str, dict[str, Any]],
    prefix: str,
) -> None:
    from secretary.task_restore import RestoreCommentOccurrence, restore_comments_batched

    intended: list[RestoreCommentOccurrence] = []
    for card in cards:
        reference = card["reference"]
        current = live.get(reference)
        if current is None:
            raise RestoreError(f"restored card disappeared before comment recovery: {reference}")
        task_id = int(str(current["id"]).removeprefix("task_kanboard_"))
        occurrences: dict[str, int] = {}
        for index, body in enumerate(_restore_comments(card)):
            occurrence = occurrences.get(body, 0)
            occurrences[body] = occurrence + 1
            intended.append(
                RestoreCommentOccurrence(
                    reference,
                    task_id,
                    body,
                    occurrence,
                    f"{prefix}comment:{reference}:{index}",
                )
            )
    restore_comments_batched(writer, intended)


def _validate_deferred_restore_comments(
    audit: TaskAudit,
    cards: list[dict[str, Any]],
    sprints: list[dict[str, Any]],
    prefix: str,
) -> None:
    """Fail before board writes unless every deferred event belongs to this canon."""
    from secretary.tasks import _digest

    expected: dict[str, tuple[str, str, int]] = {}
    for subject, bodies, label in (
        *(
            (card["reference"], _restore_comments(card), "comment")
            for card in sorted(cards, key=_restore_card_order)
        ),
        *(
            (
                sprint["reference"],
                [str(entry["text"]) for entry in sprint["comments"]],
                "sprint-comment",
            )
            for sprint in sprints
        ),
    ):
        seen: dict[str, int] = {}
        for index, body in enumerate(bodies):
            occurrence = seen.get(body, 0)
            seen[body] = occurrence + 1
            expected[f"{prefix}{label}:{subject}:{index}"] = (subject, _digest(body), occurrence)
    pending = audit.pending_events()
    if audit.status()["pending"] != len(pending):
        raise RestoreError("board audit repair is required before restore")
    for event in pending:
        request_id = str(event.get("request_id") or "")
        payload = event.get("payload")
        identity = expected.get(request_id)
        if (
            event.get("kind") != "restored_comment"
            or identity is None
            or event.get("ref") != identity[0]
            or not isinstance(payload, dict)
            or payload.get("body_sha256") != identity[1]
            or payload.get("restore_occurrence") != identity[2]
        ):
            raise RestoreError("board audit repair is required before restore")


def _import_sprints(
    data_dir: Path,
    client: KanboardClient,
    sprints: list[dict[str, Any]],
    existing: dict[str, dict[str, Any]],
    prefix: str,
) -> None:
    """Recreate the sprint entities and prove they match the export."""
    if not sprints:
        # Do not invent an empty sprint board.
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
            writer.restore_create(
                goal=sprint["goal"],
                definition_of_done=sprint["definition_of_done"],
                repositories=list(sprint["repositories"]),
                reference=reference,
                request_id=f"{prefix}sprint-create:{reference}",
                # Publish status and observer before a readable reference.
                observer=sprint.get("observer"),
                status=str(sprint["status"]),
            )
        writer.restore(
            reference=reference,
            values=_restore_sprint_metadata(sprint),
            request_id=f"{prefix}sprint:{reference}",
        )
    setup = {entity["ref"]: entity for entity in reader.export()}
    _restore_sprint_comments_batched(writer, sprints, setup, prefix)
    live = {entity["reference"]: entity for entity in map(normalize_sprint_entity, reader.export())}
    if any(_sprint_core(live.get(sprint["reference"], {})) != _sprint_core(sprint) for sprint in sprints):
        _update_restore_state(data_dir, sprints="failed", sprint_parity="failed")
        raise RestoreError("sprint parity check failed")


def _restore_sprint_comments_batched(
    writer: Any,
    sprints: list[dict[str, Any]],
    live: dict[str, dict[str, Any]],
    prefix: str,
) -> None:
    from secretary.task_restore import RestoreCommentOccurrence, restore_comments_batched

    intended: list[RestoreCommentOccurrence] = []
    for sprint in sprints:
        reference = sprint["reference"]
        current = live.get(reference)
        if current is None:
            raise RestoreError(f"restored sprint disappeared before comment recovery: {reference}")
        task_id = int(str(current["id"]).removeprefix("sprint_kanboard_"))
        occurrences: dict[str, int] = {}
        for index, body in enumerate(str(entry["text"]) for entry in sprint["comments"]):
            occurrence = occurrences.get(body, 0)
            occurrences[body] = occurrence + 1
            intended.append(
                RestoreCommentOccurrence(
                    reference,
                    task_id,
                    body,
                    occurrence,
                    f"{prefix}sprint-comment:{reference}:{index}",
                    entity="sprint",
                )
            )
    restore_comments_batched(writer, intended)


SPRINT_PARITY_FIELDS = (
    "reference",
    "goal",
    "definition_of_done",
    "repositories",
    "product",
    "issues",
    "reservations",
    "status",
    "budget",
    "current_task",
    "resume",
    "audit",
    "observer",
)


def _check_restored_observers(sprints: list[dict[str, Any]], instance: Path | None) -> None:
    """Validate the whole exported observer set before the first backend write of any set.

    A row without a readable value is either a corrupt export or one taken before the observer
    migration, and restoring it either way would publish a row the reader this installation comes
    back with immediately calls corrupt. Nothing is written here: it is the preflight.
    """
    profiles: set[str] = set()
    if any(str(sprint.get("status") or "") == "open" for sprint in sprints):
        # Only open rows need a registered head; closed archives remain restorable.
        try:
            profiles = installed_observer_profiles(instance)
        except ObserverMetadataError as exc:
            raise RestoreError(f"sprint observer metadata cannot be validated: {exc.message}") from None
    problems: list[str] = []
    for sprint in sprints:
        reference = str(sprint.get("reference") or "?")
        status = str(sprint.get("status") or "")
        if "observer" not in sprint:
            problems.append(
                f"{reference}: the row carries no observer field. The export is either corrupt or "
                "was taken before the observer migration; both are refused. Add the value to that "
                'row in the export\'s state/board/sprints.json ("observer": {"kind": "head", '
                '"profile": "<profile>"} for a closed row\'s head, or {"kind": "none"}) and run '
                "the restore again"
            )
            continue
        value = parse_observer(sprint.get("observer"))
        if value is None:
            problems.append(f"{reference}: observer value is not one of the tagged forms")
            continue
        if status == "open" and not is_executable(value):
            problems.append(
                f"{reference}: an open sprint may not carry migration provenance ({value.get('source')})"
            )
            continue
        if status == "open":
            # An open row's declared head must still exist.
            try:
                check_observer_profile(value, profiles, subject=reference)
            except ObserverMetadataError as exc:
                problems.append(exc.message)
    if problems:
        raise RestoreError("sprint observer metadata is invalid: " + "; ".join(problems))


def _check_restored_admission(sprints: list[dict[str, Any]], instance: Path | None) -> None:
    """Refuse an export whose open sprints this installation would never have admitted.

    `restore_create` is deliberately not an admission decision, so the set as a whole is asked once,
    here, before the first backend write: an archive carrying two open sprints that share a product,
    a reservation, a repository tree or an observer head would otherwise be a way to arrive at
    exactly the pair admission exists to refuse. The limit is the target installation's.
    """
    from secretary.sprints import (
        instance_open_sprint_limit,
        open_sprint_admission_error,
    )

    rows = [
        {
            "ref": str(sprint.get("reference") or ""),
            "product": str(sprint.get("product") or ""),
            "reservations": list(sprint.get("reservations") or []),
            "repositories": list(sprint.get("repositories") or []),
            "observer": parse_observer(sprint.get("observer")),
        }
        for sprint in sprints
        if str(sprint.get("status") or "") == "open"
    ]
    problem = open_sprint_admission_error(rows, limit=instance_open_sprint_limit(instance))
    if problem is not None:
        raise RestoreError(f"restored open sprints are not admissible on this installation: {problem}")


# Preserve absent ownership keys; empty replacement is lossy.
_ABSENT = object()


def _sprint_core(sprint: dict[str, Any]) -> dict[str, Any]:
    """The exported sprint contract, without what a rewrite cannot reproduce."""
    core: dict[str, Any] = {field: sprint.get(field, _ABSENT) for field in SPRINT_PARITY_FIELDS}
    core["comments"] = [
        str(comment.get("text") or "") for comment in sprint.get("comments", []) if isinstance(comment, dict)
    ]
    return core


def _restore_sprint_metadata(sprint: dict[str, Any]) -> dict[str, str]:
    resume = sprint.get("resume")
    ownership = {
        key: value
        for key, value in (
            ("sprint_product", str(sprint.get("product") or "")),
            ("sprint_issues", json.dumps(list(sprint.get("issues") or []), separators=(",", ":"))),
            (
                "sprint_reservations",
                json.dumps(list(sprint.get("reservations") or []), separators=(",", ":")),
            ),
        )
        # Preserve absent ownership fields rather than invent empty values.
        if value not in {"", "[]"}
    }
    return ownership | {
        "sprint_goal": str(sprint["goal"]),
        "sprint_definition_of_done": str(sprint["definition_of_done"]),
        "sprint_repositories": json.dumps(list(sprint["repositories"]), separators=(",", ":")),
        "sprint_status": str(sprint["status"]),
        "sprint_budget": json.dumps(
            {"by_type": sprint["budget"]["by_type"]}, sort_keys=True, separators=(",", ":")
        ),
        **(
            {
                "sprint_budget_uncharged": json.dumps(
                    sprint["budget"]["uncharged"], sort_keys=True, separators=(",", ":")
                )
            }
            if sprint["budget"].get("uncharged")
            else {}
        ),
        "sprint_current_task": str(sprint["current_task"]),
        "sprint_resume": (json.dumps(resume, sort_keys=True, separators=(",", ":")) if resume else ""),
        "sprint_source_audit": json.dumps(sprint["audit"], sort_keys=True, separators=(",", ":")),
        **({"sprint_observer": encode_observer(sprint["observer"])} if "observer" in sprint else {}),
    }


DEFAULT_MEMORY_MODEL = "intfloat/multilingual-e5-large"
DEFAULT_MEMORY_DIM = 1024


def rebuild_memory_index(
    data_dir: Path,
    instance_dir: Path | None,
    *,
    python: Path | None = None,
    script: Path | None = None,
    model: str | None = None,
    dim: int | None = None,
    threads: int | None = None,
    runner=None,
) -> int:
    """Replace the derived index from restored canon."""
    data_dir = data_dir.expanduser().resolve()
    memory_dir = data_dir / "memory"
    facts_dir = _memory_canon_dir(data_dir, instance_dir)
    try:
        if runner is not None:
            result = runner(facts_dir, memory_dir / "export.ndjson", memory_dir / "index.sqlite")
            count = int(result["parity"]["indexed"])
        elif python is not None or script is not None:
            if (
                python is None
                or script is None
                or not isinstance(model, str)
                or not model
                or not isinstance(dim, int)
            ):
                raise RuntimeError("external memory rebuild contract is not configured")
            python = python.expanduser().absolute()
            script = script.expanduser().resolve()
            if not python.is_file() or not os.access(python, os.X_OK) or not script.is_file():
                raise RuntimeError("external memory rebuild argv contract is unavailable")
            completed = _proc.run(
                [
                    str(python),
                    str(script),
                    "--canon",
                    str(facts_dir),
                    "--export",
                    str(memory_dir / "export.ndjson"),
                    "--target-db",
                    str(memory_dir / "index.sqlite"),
                    "--model",
                    model,
                    "--dim",
                    str(dim),
                ],
                timeout=MEMORY_REINDEX_TIMEOUT_SECONDS,
                env={
                    **os.environ,
                    "MEMORY_CACHE_DIR": str(memory_dir / "fastembed-cache"),
                    "MEMORY_THREADS": str(threads or 1),
                },
            )
            if completed.returncode:
                raise RuntimeError("memory reindex command failed: " + _reindex_error_detail(completed))
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
        # Older recovery state without sprints has no sprint step to diagnose.
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


def _normalized_cards(
    data_dir: Path, *, registered_project_ids: set[str] | None = None
) -> list[dict[str, Any]]:
    try:
        # Released materializers before the paired export left only cards.json in the local
        # restore directory. Preserve that demonstrated input; every current producer requires
        # the pair, and a present NDJSON file must still match exactly.
        cards = validated_normalized_cards(
            data_dir / "board",
            registered_project_ids=registered_project_ids,
            require_ndjson=False,
        )
    except NormalizedBoardError as exc:
        raise RestoreError(str(exc)) from None
    for card in cards:
        if not isinstance(card.get("column"), str) or _state_for_column(card["column"]) is None:
            raise RestoreError("normalized board export has an invalid column")
        if not isinstance(card.get("fields"), dict) or not isinstance(card.get("metadata"), dict):
            raise RestoreError("normalized board export has invalid task data")
        if card["metadata"].get("record_type") not in _RECORD_TYPES:
            raise RestoreError(f"normalized board export card {card['reference']} has no record type")
        if not isinstance(card.get("title"), str) or not isinstance(card.get("description"), str):
            raise RestoreError("normalized board export has invalid task text")
        if "closed" in card and not isinstance(card["closed"], bool):
            raise RestoreError("normalized board export has an invalid closed state")
        if not isinstance(card.get("comments", []), list) or any(
            not isinstance(comment, dict) or not isinstance(comment.get("text"), str)
            for comment in card.get("comments", [])
        ):
            raise RestoreError("normalized board export has invalid comments")
    return sorted(cards, key=lambda card: str(card["reference"]))


def _normalized_sprints(data_dir: Path) -> list[dict[str, Any]]:
    """Read the exported sprint entities."""
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
    if any(not isinstance(ref, str) or not ref.startswith(SPRINT_REFERENCE_PREFIX) for ref in refs) or len(
        set(refs)
    ) != len(refs):
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
        # Pre-ownership exports retain absent ownership.
        if not isinstance(sprint.get("product", ""), str):
            raise RestoreError("normalized sprint export has an invalid product")
        for field in ("issues", "reservations"):
            value = sprint.get(field, [])
            if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                raise RestoreError(f"normalized sprint export has invalid {field}")
        budget = sprint.get("budget")
        if (
            not isinstance(budget, dict)
            or not isinstance(budget.get("by_type"), dict)
            or any(not isinstance(count, int) for count in budget["by_type"].values())
        ):
            raise RestoreError("normalized sprint export has an invalid budget")
        # Missing legacy uncharged counts restore as zero.
        uncharged = budget.get("uncharged", {})
        if not isinstance(uncharged, dict) or any(
            not isinstance(count, int) or isinstance(count, bool) or count < 0 for count in uncharged.values()
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

    Restore events are durable, so a second recovery from a recovered checkpoint meets its own
    request ids again; reusing them would short-circuit every write as already committed against a
    backend that holds nothing. A namespace whose committed events name entities the target does not
    have was written against an earlier backend, so this recovery takes a fresh one and
    `restore-state.json` keeps it.
    """
    state = restore_state(data_dir)
    token = state.get("restore_namespace")
    if not isinstance(token, str) or not token or not _namespace_is_local(audit, token, live_refs):
        token = uuid.uuid4().hex
        # Missing `sprints` means this recovery never tracked that step.
        _update_restore_state(data_dir, restore_namespace=token, sprints=state.get("sprints", "pending"))
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
    issue_column = _state_for_column(str(card.get("column") or "")) == "issues"
    metadata = card.get("metadata")
    record_type = metadata.get("record_type") if isinstance(metadata, dict) else None
    if issue_column or record_type in {"issue", "product"}:
        _create_restored_non_task(writer, card)
        return
    writer.create(
        role="steward",
        actor="restore",
        project=fields["project"] or "product-backlog",
        task_type=fields["task_type"] or "research",
        target="ready",
        restoring=True,
        title=card["title"],
        description=card["description"],
        reference=card["reference"],
        blocked_by=fields["blocked_by"],
        head=fields["head"],
        review_head=fields["review_head"],
        slug=fields["slug"],
        base_branch=fields["base_branch"],
        seed_ref=fields["seed_ref"],
        supersedes=fields["supersedes"],
        complexity=fields["complexity"],
        family_preference=fields["family_preference"],
        codex_launch_mode=fields["codex_launch_mode"],
        request_id=f"{prefix}create:{card['reference']}",
    )


def _create_restored_non_task(writer: TaskWriter, card: dict[str, Any]) -> None:
    """Recreate a Product or an Issue in its own column, without classifying it."""
    board_id, columns, swimlanes = writer.reader._board()
    column = str(card.get("column") or "")
    column_id = next((identifier for identifier, title in columns.items() if title == column), None)
    if column_id is None:
        raise RestoreError(f"restored card has an unknown column: {column}")
    payload: dict[str, Any] = {
        "project_id": board_id,
        "title": card["title"],
        "description": card["description"],
        "column_id": column_id,
    }
    # Restore the exported lane; create named lanes and omit Kanboard-invalid id 0.
    exported_lane = str(card.get("swimlane") or "")
    swimlane_id = _matching_swimlane(swimlanes, exported_lane)
    if swimlane_id is None and exported_lane.strip():
        swimlane_id = ensure_swimlane(writer.client, board_id, exported_lane)
    if swimlane_id is not None:
        payload["swimlane_id"] = swimlane_id
    # Reject bool: it is an int subclass but not a valid Kanboard id.
    task_id = _positive_int(writer.client.call("createTask", **payload))
    if task_id is None:
        raise RestoreError("could not create restored Product or Issue record")
    if writer.client.call("updateTask", id=task_id, reference=card["reference"]) is not True:
        raise RestoreError("could not set restored Product or Issue reference")


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


def _restored_order_mismatch(cards: list[dict[str, Any]], actual: dict[str, dict[str, Any]]) -> bool:
    """Сверяет порядок открытых карточек внутри (колонка, свимлейн).

    Абсолютные номера позиций сравнивать нельзя: Kanboard держит позиции плотными среди активных
    задач, а закрытая задача сохраняет устаревшее значение и перестаёт занимать слот, поэтому
    экспорт живой доски содержит и дыры, и повторы. Восстановимо здесь только относительное
    расположение; у закрытых карточек позиции нет вовсе.
    """
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for card in cards:
        if card.get("closed"):
            continue
        groups.setdefault((str(card["column"]), str(card.get("swimlane") or "")), []).append(card)
    for group in groups.values():
        # Match the creation ordering and tie-breaker.
        expected = [card["reference"] for card in sorted(group, key=_restore_card_order)]
        live = sorted(
            group,
            key=lambda card: (
                _positive_int(actual[card["reference"]].get("position")) or 0,
                str(card["reference"]),
            ),
        )
        if expected != [card["reference"] for card in live]:
            return True
    return False


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
        "project": value("project"),
        "task_type": value("task_type"),
        "blocked_by": value("blocked_by"),
        "head": value("head"),
        "review_head": value("review_head"),
        "slug": value("slug"),
        "base_branch": value("base_branch"),
        "seed_ref": value("seed_ref"),
        "supersedes": value("supersedes"),
        "complexity": _enum_or_default(
            value("complexity"), {"cheap", "standard", "hard", "frontier"}, "standard"
        ),
        "family_preference": _enum_or_default(
            value("family_preference"), {"auto", "claude", "codex"}, "auto"
        ),
        # Legacy `exec` reads as no mode; live modes round-trip unchanged.
        "codex_launch_mode": _enum_or_default(value("codex_launch_mode"), {"", *CODEX_LAUNCH_MODES}, ""),
    }


def _enum_or_default(value: str, allowed: set[str], default: str) -> str:
    return value if value in allowed else default


def _core_from_export(card: dict[str, Any]) -> dict[str, Any]:
    fields = _restore_fields(card)
    metadata = card["metadata"]
    return {
        "ref": card["reference"],
        "title": card["title"],
        "description": card["description"],
        "state": _state_for_column(card["column"]),
        "closed": bool(card.get("closed", False)),
        "project": fields["project"],
        "type": fields["task_type"],
        "blocked_by": fields["blocked_by"] or None,
        "claim": {"worker": metadata.get("claim") or None, "claimed_at": None},
        "routing": {
            "complexity": fields["complexity"],
            "family_preference": fields["family_preference"],
            "head": fields["head"] or None,
            "review_head": fields["review_head"] or None,
            "resolved_head": metadata.get("resolved_head") or None,
            "resolved_review_head": metadata.get("resolved_review_head") or None,
            "codex_launch_mode": fields["codex_launch_mode"] or None,
        },
        "workspace": {
            "slug": metadata.get("slug") or None,
            "base_branch": metadata.get("base_branch") or None,
            "seed_ref": metadata.get("seed_ref") or None,
            "supersedes": metadata.get("supersedes") or None,
        },
        # Absolute position is intentionally not compared; see _restored_order_mismatch.
        "swimlane": str(card.get("swimlane") or "") or None,
        "comments": [{"body": body} for body in _restore_comments(card)],
        "product_issue_metadata": _product_issue_metadata(card["metadata"]),
    }


def _core_from_live(card: dict[str, Any]) -> dict[str, Any]:
    extensions = card.get("extensions", {}).get("kanboard", {})
    return {
        "ref": card.get("ref"),
        "title": card.get("title"),
        "description": card.get("description"),
        "state": card.get("state"),
        "closed": bool(card.get("closed", False)),
        "project": card.get("project"),
        "type": card.get("type"),
        "blocked_by": card.get("blocked_by"),
        "claim": card.get("claim"),
        "routing": {
            "complexity": card["routing"].get("complexity"),
            "family_preference": card["routing"].get("family_preference"),
            "head": card["routing"].get("head_override"),
            "review_head": card["routing"].get("review_head_override"),
            "resolved_head": card["routing"].get("resolved_worker_head"),
            "resolved_review_head": card["routing"].get("resolved_review_head"),
            "codex_launch_mode": card["routing"].get("codex_launch_mode"),
        },
        "workspace": card.get("workspace"),
        "swimlane": extensions.get("swimlane"),
        "comments": [{"body": str(comment.get("body") or "")} for comment in card.get("comments", [])],
        "product_issue_metadata": _product_issue_metadata(extensions),
    }


def _product_issue_metadata(metadata: dict[str, Any]) -> dict[str, str]:
    record_type = metadata.get("record_type")
    if record_type not in {"issue", "product"}:
        return {}
    return {key: str(metadata[key]) for key in _PRODUCT_ISSUE_METADATA if key in metadata}


def _state_for_column(column: str) -> str | None:
    return _STATE_BY_COLUMN.get(column)


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
    report = validate_instance(instance_path)
    if report.errors:
        raise RestoreError("invalid target instance: " + "; ".join(map(str, report.errors)))
    try:
        target = instance_data_dir(report.instance_path)
    except DataDirError as exc:
        raise RestoreError(str(exc)) from None
    return report.instance_path, target, _identity(report.instance)


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
                # Archive audit and progress belong to this data directory's backend.
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
