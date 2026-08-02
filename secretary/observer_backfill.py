"""The one-time cutover that gives every sprint row explicit observer metadata.

Before this migration a sprint declared nothing and the dispatcher picked an observer head from
`role_defaults.observer`.  After it a sprint carries exactly one value and the reader is strict.
Those two facts cannot be true at different times on a running installation: a strict reader let
loose on a row that predates the field would call the live sprint corrupt and fence its projects.

So the whole thing is one ordered sequence, and the order is the contract:

  freeze without exclusions
    -> confirm every worker, reviewer and observer head is stopped
    -> checkpoint, pushed to the remote, while the tolerant reader is still what runs
    -> persist the immutable inventory and journal
    -> per-ref writes, idempotent and refusing to overwrite a different value
    -> strict rescan
    -> the durable completion event
    -> checkpoint, pushed again, now carrying that event
    -> latch the strict-reader marker
    -> resume

Every step that can fail leaves the freeze in force, because the alternative is a dispatcher
running against a half-migrated board.  Every step that has already run recognises its own work
and does nothing, because a cutover is retried from the beginning rather than resumed from a
step an operator has to identify.

Provenance is selected once and written down before the first backend write.  The journal is what
a retry reads: recomputing it would re-derive history from an audit log that the migration's own
events have grown, and two runs would then disagree about what a closed sprint's observer was.

What makes the installation strict is the completion event in the audit log, not the marker file
beside it.  The log is checkpoint canon and comes back with a recovered host; a local file does
not, and a replacement host rebuilt from a post-migration checkpoint would otherwise be strict
before the disaster and tolerant after it.  Both checkpoints are pushed rather than committed
locally, because a recovery point that never left the machine is not one.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

from secretary.sprint_observer import (
    CUTOVER_DIR,
    CUTOVER_INVENTORY,
    CUTOVER_JOURNAL,
    MIGRATION_COMPLETED_KIND,
    ObserverMetadataError,
    activate_strict_reader,
    check_observer_profile,
    installed_observer_profiles,
    encode_observer,
    head_choice,
    historical_recovered,
    historical_unknown,
    is_executable,
    parse_observer,
    strict_marker_present,
    strict_reader_active,
)
from secretary.tasks import TaskError

INVENTORY_VERSION = 1
# Named in `sprint_observer`, because the reader keys "a cutover is in flight on this host" on the
# presence of the inventory these paths point at.
JOURNAL_DIR = CUTOVER_DIR
INVENTORY_FILE = CUTOVER_INVENTORY
JOURNAL_FILE = CUTOVER_JOURNAL

# The lifecycle kinds that prove a head actually came up for a sprint.  A deferral is not one: it
# records the launch that did not happen.
LAUNCH_KINDS = ("observer_launched", "observer_relaunched")


class BackfillError(Exception):
    """A cutover step that refused. The freeze stays in force and nothing has been half-written."""


def journal_dir(data_dir: str | Path) -> Path:
    return Path(data_dir) / JOURNAL_DIR


def recover_observer(reference: str, events: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The head a closed sprint actually ran, from its durable lifecycle events.

    The latest successful launch wins, and it wins on purpose: `sprint:818` was launched on
    `codex-observer` and relaunched on `claude-observer`, and the sprint's observer is the one it
    finished under.  Ties on the timestamp fall back to the order the log was written in, so two
    runs over the same log select the same event.

    Returns None when no successful launch exists.  That is not a gap to guess at: those sprints
    ran without an observer ever coming up, and the honest record says so.
    """
    launched = [
        (str(event.get("occurred_at") or ""), index, event)
        for index, event in enumerate(events)
        if str(event.get("ref") or "") == reference
        and str(event.get("kind") or "") in LAUNCH_KINDS
        and str(event.get("outcome") or "") == "success"
        and str((event.get("payload") or {}).get("head") or "")
    ]
    if not launched:
        return None
    _, _, winner = max(launched, key=lambda row: (row[0], row[1]))
    return historical_recovered(
        str((winner.get("payload") or {}).get("head") or ""),
        str(winner.get("event_id") or ""),
    )


def running_observer_heads(payload: dict[str, Any]) -> dict[str, str]:
    """The head each tracked observer record names, read from the production state.

    This is what proves the open sprint's value.  The lifecycle log says which head was launched;
    the record says which one the dispatcher still believes is running, and for an open sprint
    those must agree before anything is written.
    """
    from secretary.dispatcher_observer import load_observers

    return {
        reference: record.head
        for reference, record in load_observers(payload).items()
        if record.head
    }


def select_observer(
    sprint: dict[str, Any], events: list[dict[str, Any]], running: dict[str, str],
) -> dict[str, Any]:
    """The one value this migration writes for one row, or `BackfillError` if there is none.

    An open sprint gets an executable head, because the strict reader is about to require one and
    an operator would otherwise have to repair the live sprint by hand between the write and the
    activation.  Both sources of that head have to agree: the record the dispatcher is holding and
    the last successful launch in the log are the same fact seen twice, and a disagreement means
    the migration does not know what is running.

    A closed row gets provenance, never a declaration: it is a record of what ran.
    """
    reference = str(sprint.get("ref") or "")
    recovered = recover_observer(reference, events)
    if str(sprint.get("status") or "") != "open":
        return recovered or historical_unknown()
    live = running.get(reference, "")
    from_log = str(recovered.get("profile") or "") if recovered else ""
    if not live and not from_log:
        raise BackfillError(
            f"open sprint {reference} has neither a tracked observer record nor a successful "
            "launch event: declare its observer by hand before migrating"
        )
    if live and from_log and live != from_log:
        raise BackfillError(
            f"open sprint {reference} has a tracked observer head {live!r} and a last launch of "
            f"{from_log!r}: resolve which head is running before migrating"
        )
    return head_choice(live or from_log)


def build_inventory(
    sprints: list[dict[str, Any]], events: list[dict[str, Any]], running: dict[str, str],
) -> dict[str, Any]:
    """The immutable, versioned record of what this cutover intends to write.

    Every row of the board is listed, including the ones that already carry a value, so the
    inventory is the whole installation rather than the part that happened to need work.  Its
    digest identifies the cutover: the journal, the deterministic request ids and the strict-reader
    marker all name it, so a second cutover over a changed board cannot reuse the first one's work.
    """
    rows = []
    for sprint in sorted(sprints, key=lambda item: str(item.get("ref") or "")):
        reference = str(sprint.get("ref") or "")
        if not reference:
            continue
        rows.append({
            "ref": reference,
            "status": str(sprint.get("status") or ""),
            "observer": select_observer(sprint, events, running),
        })
    body = {"version": INVENTORY_VERSION, "rows": rows}
    return {**body, "digest": _digest(body)}


def _digest(body: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def persist_inventory(data_dir: str | Path, inventory: dict[str, Any]) -> dict[str, Any]:
    """Write the inventory once and never over a different one.

    A retry reads back what the first attempt selected instead of selecting again.  By then the
    audit log has this migration's own events in it and the board has some rows already written,
    so a second selection would be derived from a different world than the first.
    """
    path = journal_dir(data_dir) / INVENTORY_FILE
    existing = read_inventory(data_dir)
    if existing is not None:
        if existing.get("digest") != inventory.get("digest"):
            raise BackfillError(
                "a different observer migration inventory is already on disk "
                f"({existing.get('digest')}); the board changed since it was taken, so remove it "
                "deliberately before starting a new cutover"
            )
        return existing
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(inventory, sort_keys=True, separators=(",", ":")), encoding="utf-8"
    )
    temporary.replace(path)
    return inventory


def read_inventory(data_dir: str | Path) -> dict[str, Any] | None:
    try:
        raw = json.loads((journal_dir(data_dir) / INVENTORY_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return None
    return raw if isinstance(raw, dict) and raw.get("version") == INVENTORY_VERSION else None


def append_journal(data_dir: str | Path, entry: dict[str, Any]) -> None:
    path = journal_dir(data_dir) / JOURNAL_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, sort_keys=True, separators=(",", ":")) + "\n")
        handle.flush()


def read_journal(data_dir: str | Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    try:
        text = (journal_dir(data_dir) / JOURNAL_FILE).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return entries
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def backfill_request_id(digest: str, reference: str) -> str:
    """The request id one row's write is claimed under, derived from the cutover and the row.

    Deterministic so a retry of the same cutover is the same delivery of the same write, and
    digest-scoped so a later cutover over a changed board is a different one.
    """
    return f"observer-backfill:{digest}:{reference}"


def apply_backfill(
    writer: Any, data_dir: str | Path, inventory: dict[str, Any],
) -> list[dict[str, Any]]:
    """Write every row of the inventory, in order, skipping the ones already written.

    A row that already holds exactly this value is reported `unchanged` without a backend write;
    a row holding something else stops the whole migration.  The journal entry is appended after
    the write, so a crash between them is repaired by the next run finding the value on the board.
    """
    digest = str(inventory.get("digest") or "")
    results: list[dict[str, Any]] = []
    for row in inventory.get("rows") or []:
        reference = str(row.get("ref") or "")
        value = parse_observer(row.get("observer"))
        if value is None:
            raise BackfillError(f"inventory row {reference} carries no readable observer value")
        request_id = backfill_request_id(digest, reference)
        try:
            outcome = writer.backfill_observer(
                reference=reference, value=value, request_id=request_id
            )
        except TaskError as exc:
            raise BackfillError(
                f"observer metadata for {reference} could not be written: {exc.message}"
            ) from None
        entry = {
            "ref": reference,
            "observer": value,
            "request_id": request_id,
            "event_id": str(outcome.get("event_id") or ""),
            "digest": digest,
        }
        append_journal(data_dir, entry)
        results.append(entry)
    return results


def scan_rows(sprints: list[dict[str, Any]], profiles: set[str] | None = None) -> list[str]:
    """Every reason the board is not ready for the strict reader, or an empty list.

    This is the strict reader's own judgement run once, deliberately, before it is switched on:
    what it would refuse at a tick is what it refuses here, while the pipeline is still frozen and
    an operator is watching.  That includes resolving an open row's declared head against the head
    registry — a profile removed between the freeze and the cutover would otherwise let the
    migration activate a reader that immediately fences the sprint it just migrated.
    """
    problems: list[str] = []
    for sprint in sorted(sprints, key=lambda item: str(item.get("ref") or "")):
        reference = str(sprint.get("ref") or "")
        if "observer" not in sprint:
            problems.append(f"{reference}: no observer metadata")
            continue
        value = parse_observer(sprint.get("observer"))
        if value is None:
            problems.append(f"{reference}: observer metadata is not one of the tagged forms")
            continue
        if str(sprint.get("status") or "") != "open":
            continue
        if not is_executable(value):
            problems.append(f"{reference}: open sprint carries non-executable {value.get('source')}")
            continue
        if profiles is not None:
            try:
                check_observer_profile(value, profiles, subject=reference)
            except ObserverMetadataError as exc:
                problems.append(exc.message)
    return problems


def running_heads(runtime: Any, payload: dict[str, Any]) -> list[str]:
    """The heads a freeze left running. Empty when the installation is genuinely quiet.

    A freeze stops heads and clears the handle of each one it confirmed gone, so a handle still on
    a record is a head the freeze could not stop.  That head keeps its board writes coming, and
    this migration rewrites the field the observer machinery reads.
    """
    from secretary.dispatcher_observer import load_observers

    running: list[str] = []
    for reference, record in sorted(runtime.production_state.records(payload).items()):
        for role, handle in (("worker", record.handle), ("reviewer", record.review_handle)):
            if handle:
                running.append(f"{role} head of {reference}")
    for reference, record in sorted(load_observers(payload).items()):
        if record.handle and not record.abandoned_handle:
            running.append(f"observer head of {reference}")
    return running


# A push outcome that means the commit is on the remote. `unchanged` counts: the remote already
# holds this HEAD. Everything else — `skipped` (no remote, no branch), `failed`, `diverged`, or a
# window that was simply not due — leaves the checkpoint local, which is not a recovery point.
PUSH_SUCCESS_STATUSES = ("pushed", "unchanged")


def push_now(runtime: Any, label: str) -> dict[str, Any]:
    """Push the checkpoint just written, and prove it landed. Raises otherwise.

    The ordinary pusher runs on a 30-minute window and answers a call inside that window by
    returning the previous state untouched. That is right for a tick and wrong here: the cutover's
    contract is that this particular commit is on the remote before the board is rewritten, so the
    window is bypassed rather than waited on, and any outcome short of the commit being on the
    remote leaves the freeze in force.
    """
    pusher = getattr(runtime, "checkpoint_push", None)
    if pusher is None:
        raise BackfillError(
            f"the {label} checkpoint cannot be pushed: this runtime has no checkpoint pusher, so "
            "the cutover has no recovery point"
        )
    try:
        # An empty state is a pusher that has never run, which is exactly how the window is
        # bypassed: `_due` admits it. Nothing else about the tick's push state is touched.
        result = pusher.push({})
    except Exception as exc:  # noqa: BLE001 - the reason travels into the operator's refusal
        raise BackfillError(
            f"the {label} checkpoint could not be pushed: {type(exc).__name__}: {exc}"
        ) from None
    status = str((result or {}).get("status") or "")
    if status not in PUSH_SUCCESS_STATUSES:
        raise BackfillError(
            f"the {label} checkpoint was not pushed (status {status or 'unknown'}: "
            f"{(result or {}).get('reason') or 'no reason given'}); the cutover needs a remote "
            "recovery point before it rewrites the board"
        )
    return dict(result or {})


def record_migration_completed(
    runtime: Any, inventory: dict[str, Any], *, rows: int, at: str,
) -> str:
    """Write the one durable event that makes this installation migrated.

    Staged then committed, like every other durable effect, and keyed on the inventory digest so a
    retry of the same cutover finds its own event rather than writing a second one.
    """
    audit = runtime.audit
    request_id = f"observer-migration-completed:{inventory.get('digest') or ''}"
    committed = audit.committed_event(request_id)
    if committed is not None:
        return str(committed.get("event_id") or "")
    event = {
        "event_id": "evt_" + uuid.uuid4().hex,
        "schema_version": 1,
        "occurred_at": str(at),
        "actor": {"role": "steward", "id": "observer-migration"},
        "kind": MIGRATION_COMPLETED_KIND,
        "outcome": "success",
        "task_id": "",
        "ref": "",
        "backend": {"kind": "dispatcher", "task_id": None, "revision": "n/a"},
        "request_id": request_id,
        "payload": {
            "inventory_digest": str(inventory.get("digest") or ""),
            "rows": int(rows),
        },
    }
    audit.stage(request_id, event)
    return str(audit.append(request_id, event))


def run_cutover(
    runtime: Any,
    *,
    sprint_writer: Any,
    data_dir: str | Path,
    instance: str | Path | None,
    now: str,
    resume_actor: str = "observer-migration",
    resume: bool = True,
) -> dict[str, Any]:
    """The whole ordered cutover. Raises `BackfillError` on the first step that refuses.

    Nothing here is optional and nothing here is reorderable.  The caller's only choice is whether
    the last step lifts the freeze; an operator who wants to look at the board first passes
    `resume=False` and lifts it with `secretary dispatcher resume`.
    """
    from secretary.dispatcher_pause import normalize_pause_mode
    from secretary.dispatcher_pause_ops import resume as lift_pause
    from secretary.dispatcher_production import _write_checkpoint

    data_dir = Path(data_dir)
    steps: list[dict[str, Any]] = []

    if strict_marker_present(data_dir):
        # The marker is written last, so its presence is the proof that every step before it ran.
        # The gate is deliberately the marker rather than `strict_reader_active`: a run that died
        # between the durable completion event and the marker has a checkpoint still to take, and
        # this has to resume it rather than report a finished cutover.
        return {
            "ok": True, "action": "sprint migrate-observer", "status": "already-migrated",
            "steps": steps,
        }

    pause_state = runtime.pause.load()
    if normalize_pause_mode(pause_state.get("mode")) != "freeze":
        raise BackfillError(
            "the pipeline is not frozen: run `secretary dispatcher pause --mode freeze` first, "
            "with no workspace exclusions"
        )
    # Resolved before anything is written, so a registry this cutover cannot read stops it while
    # the board is still untouched rather than at the scan after seventeen writes.
    try:
        profiles = installed_observer_profiles(instance)
    except ObserverMetadataError as exc:
        raise BackfillError(
            f"the observer migration cannot validate declared heads: {exc.message}"
        ) from None
    excluded = pause_state.get("excluded_worker")
    if isinstance(excluded, list) and excluded:
        raise BackfillError(
            "the freeze carries workspace exclusions (" + ", ".join(map(str, excluded)) + "): a "
            "head left running would keep writing to the board this migration is rewriting"
        )
    steps.append({"step": "freeze", "status": "ok"})

    payload = runtime.production_state.load()
    # A state nobody can decode attests to nothing. `pause freeze` says so itself: it sets the
    # flag and stops no head when the records are unreadable, so an empty decode here would be
    # read as "every head is stopped" precisely when the opposite may be true, and the migration
    # would rewrite the board under a live head.
    phase = str(payload.get("phase") or "")
    if phase == "unavailable":
        raise BackfillError(
            "the production state cannot be read, so no head can be confirmed stopped; repair "
            f"{runtime.production_state.path} before migrating"
        )
    still_running = running_heads(runtime, payload)
    if still_running:
        raise BackfillError(
            "heads are still running under the freeze: " + ", ".join(still_running)
        )
    steps.append({"step": "heads-stopped", "status": "ok", "phase": phase})

    # Taken while the tolerant reader is still the one in force, so the checkpoint this cutover
    # can be rolled back to describes the installation as it ran, not as it is being rewritten.
    checkpoint = _write_checkpoint(runtime)
    if checkpoint is None or checkpoint.get("status") == "blocked":
        raise BackfillError(f"the pre-migration checkpoint could not be written: {checkpoint}")
    push = push_now(runtime, "pre-migration")
    steps.append({
        "step": "pre-migration-checkpoint", "status": "ok",
        "checkpoint": checkpoint, "push": push,
    })

    stored = read_inventory(data_dir)
    if stored is None:
        sprints = runtime.sprints.export()
        inventory = build_inventory(sprints, runtime.audit.events(), running_observer_heads(payload))
        stored = persist_inventory(data_dir, inventory)
    steps.append({
        "step": "inventory", "status": "ok", "digest": stored.get("digest"),
        "rows": len(stored.get("rows") or []),
    })

    written = apply_backfill(sprint_writer, data_dir, stored)
    steps.append({"step": "backfill", "status": "ok", "rows": len(written)})

    problems = scan_rows(runtime.sprints.export(), profiles)
    if problems:
        raise BackfillError("the post-migration scan refused the board: " + "; ".join(problems))
    steps.append({"step": "strict-scan", "status": "ok"})

    # The completion fact, and the only thing that makes this installation strict. It is written
    # after the scan, so a half-written board never reads as migrated, and before the checkpoint,
    # so the snapshot a replacement host is rebuilt from carries it.
    completion = record_migration_completed(runtime, stored, rows=len(written), at=now)
    steps.append({"step": "migration-completed", "status": "ok", "event_id": completion})

    after = _write_checkpoint(runtime)
    if after is None or after.get("status") == "blocked":
        raise BackfillError(f"the post-migration checkpoint could not be written: {after}")
    after_push = push_now(runtime, "post-migration")
    steps.append({
        "step": "post-migration-checkpoint", "status": "ok",
        "checkpoint": after, "push": after_push,
    })

    marker = activate_strict_reader(
        data_dir,
        inventory_digest=str(stored.get("digest") or ""),
        rows=len(stored.get("rows") or []),
        activated_at=now,
    )
    steps.append({"step": "strict-reader", "status": "ok", "marker": marker})

    if resume:
        lift_pause(runtime, actor=resume_actor)
        steps.append({"step": "resume", "status": "ok"})
    else:
        steps.append({"step": "resume", "status": "skipped", "reason": "resume was not requested"})

    return {
        "ok": True,
        "action": "sprint migrate-observer",
        "status": "migrated",
        "digest": stored.get("digest"),
        "rows": [
            {"ref": entry["ref"], "observer": entry["observer"]} for entry in written
        ],
        "steps": steps,
    }


def plan_cutover(
    runtime: Any, *, data_dir: str | Path, instance: str | Path | None = None,
) -> dict[str, Any]:
    """What the cutover would write, without writing or freezing anything.

    The dry run is a read of the same three sources the real run selects from, so an operator can
    check the provenance of every closed row before the pipeline is stopped for it.  It also names
    what the real run would refuse, so a head that has left the registry is visible before the
    pipeline is stopped rather than after.
    """
    payload = runtime.production_state.load()
    inventory = build_inventory(
        runtime.sprints.export(), runtime.audit.events(), running_observer_heads(payload)
    )
    refusals: list[str] = []
    try:
        profiles = installed_observer_profiles(instance)
    except ObserverMetadataError as exc:
        refusals.append(exc.message)
        profiles = None
    if profiles is not None:
        for row in inventory["rows"]:
            if str(row.get("status") or "") != "open":
                continue
            try:
                check_observer_profile(row["observer"], profiles, subject=str(row["ref"]))
            except ObserverMetadataError as exc:
                refusals.append(exc.message)
    return {
        "ok": not refusals,
        "action": "sprint migrate-observer",
        "status": "planned",
        "strict_reader_active": strict_reader_active(data_dir),
        "digest": inventory["digest"],
        "rows": inventory["rows"],
        "refusals": refusals,
    }


def observer_text(value: dict[str, Any]) -> str:
    return encode_observer(value)
