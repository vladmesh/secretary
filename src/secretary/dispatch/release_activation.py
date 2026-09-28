"""A release whose production activation was refused: one reason, one operation, the card Blocked.

`complete_green` raises `ProductionActivationRefused` when the Secretary production checkout could not
be advanced because the target's board schema was refused (`dispatch.production_checkout`). By then the
release has delivered its commit to the remote (pushed, or the pull request merged), so the card is
neither undelivered nor Done: its code is on the base and not running. :func:`block_refused_activation`
makes that visible on the board, in this order, each step under a request id derived from the attempt
and the card so a replayed tick repeats none of them:

1. one `operation` card for the PO (`production_rights.ACTIVATION_OPERATION_REQUEST_PREFIX`, the one
   operation the dispatcher may create), in the card's sprint, or under the card's PO origin when it has
   no sprint, touching the card's own project's production. It names the old and target code revisions,
   the owed and failing migration, what was applied, and the supported recovery and verification;
2. one dispatcher comment on the card carrying the typed reason (`release_schema_refused`, its reason,
   the failing revision, the target, the remote merge kept apart from the production checkout) as JSON;
3. the card Blocked (`block_merge_path`), its reason naming the same.

The operation is created first so an interrupted tick cannot lose it: until the Block commits, the card
is still in its release and the next tick replays it, which finds the operation already created. A
create that is refused (a closed sprint, no registry) is named in the comment and the reason instead of
stopping the Block.
"""

from __future__ import annotations

import json
from typing import Any

from secretary.board import po_origin as origin_field
from secretary.board.production_rights import ACTIVATION_OPERATION_REQUEST_PREFIX
from secretary.board.release_migrations import (
    DESTRUCTIVE_PENDING,
    LOCK_TIMEOUT,
    MIGRATION_FAILED,
    UNCLASSIFIED_PENDING,
)
from secretary.dispatch.production_checkout import ProductionActivationRefused
from secretary.dispatch.state import DispatcherRecord, request_token
from secretary.dispatch.state import attempt_request_id as _attempt_request_id
from secretary.tasks import TaskError

ACTION = "production-activation-blocked"
COMMENT_ACTION = "production-activation-refused"
HEADING = "## Production activation refused"


def operation_request_id(reference: str, attempt_id: str) -> str:
    return f"{ACTIVATION_OPERATION_REQUEST_PREFIX}{request_token(reference)}-{request_token(attempt_id)}"


def block_refused_activation(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
    refused: ProductionActivationRefused,
    *,
    step: str,
) -> dict[str, Any]:
    from secretary.dispatch.release_lifecycle import block_merge_path

    ref = str(task["ref"])
    attempt = record.attempt_id or attempt_id
    facts = refused.facts()
    operation, problem = _operation(runtime, task, facts, operation_request_id(ref, attempt))
    facts["operation"] = operation or None
    if problem:
        facts["operation_problem"] = problem
    _once(
        runtime,
        _attempt_request_id(attempt, COMMENT_ACTION, ref),
        lambda request_id: runtime.writer.comment(
            role="dispatcher",
            actor=runtime.owner,
            reference=ref,
            body=_comment(facts),
            request_id=request_id,
        ),
    )
    outcome = block_merge_path(
        runtime,
        task,
        record,
        records,
        payload,
        attempt_id,
        action=ACTION,
        reason=_reason(facts),
        step=step,
        outcome="production activation refused",
    )
    outcome["activation_refused"] = {
        key: facts[key] for key in ("code", "reason", "revision", "target", "old", "operation")
    }
    return outcome


def _once(runtime: Any, request_id: str, write: Any) -> dict[str, Any] | None:
    """Write under `request_id` unless a write under it already committed; the committed event then."""
    committed = runtime.audit.committed_event(request_id)
    if committed is not None:
        return committed
    return write(request_id)


def _operation(runtime: Any, task: dict[str, Any], facts: dict[str, Any], request_id: str) -> tuple[str, str]:
    """The operation card's ref (created now or by an interrupted earlier tick), and why there is none."""
    committed = runtime.audit.committed_event(request_id)
    if committed is not None and committed.get("ref"):
        return str(committed["ref"]), ""
    project = str(task.get("project") or "")
    sprint = str(task.get("sprint") or "")
    origin = None if sprint else origin_field.po_origin(task)
    if not sprint and origin is None:
        return (
            "",
            f"{task['ref']} belongs to no sprint and carries no PO origin: no PO session executes an operation",
        )
    try:
        created = runtime.writer.create(
            role="dispatcher",
            actor=runtime.owner,
            project=project,
            task_type="operation",
            title=f"Recover the production activation of {task['ref']} ({facts['reason']})",
            description=_description(task, facts),
            target="ready",
            sprint=sprint,
            touches_production=project,
            origin=origin,
            request_id=request_id,
        )
    except TaskError as exc:
        return "", f"the operation card was refused: {exc.code}: {exc}"
    return str(created["task"]["ref"]), ""


def _landing_line(facts: dict[str, Any]) -> str:
    merge = facts.get("remote_merge") or {}
    if not merge.get("sha") and not merge.get("branch"):
        return "the release recorded no remote merge"
    via = "pull request merge" if merge.get("path") == "github-pr" else merge.get("path") or "merge"
    commit = f"`{merge['sha']}`" if merge.get("sha") else f"the merge of `{merge.get('branch')}`"
    return f"{commit} landed on `{merge.get('base')}` ({via}) and stays there"


def _reason(facts: dict[str, Any]) -> str:
    revision = f" at revision {facts['revision']}" if facts.get("revision") else ""
    operation = facts.get("operation") or f"none ({facts.get('operation_problem')})"
    return (
        f"production activation refused ({facts['code']}: {facts['reason']}{revision}): {facts['message']}. "
        f"Delivered to the remote: {_landing_line(facts)}; the production checkout {facts['checkout']} stays at "
        f"{str(facts['old'])[:12]}. Operation card: {operation}."
    )


def _comment(facts: dict[str, Any]) -> str:
    return "\n".join(
        [
            HEADING,
            "",
            (
                f"Delivered to the remote: {_landing_line(facts)}. Not activated: the production checkout "
                f"`{facts['checkout']}` stays at `{facts['old']}`, because the board schema of `{facts['target']}` "
                f"was refused ({facts['reason']})."
            ),
            "",
            f"Operation card: {facts.get('operation') or 'none — ' + str(facts.get('operation_problem'))}",
            "",
            "```json",
            json.dumps(facts, indent=2, sort_keys=True),
            "```",
        ]
    )


def _what_to_fix(facts: dict[str, Any]) -> str:
    reason, revision = facts["reason"], facts.get("revision") or "(none)"
    if reason == MIGRATION_FAILED:
        return (
            f"Revision `{revision}` failed on the production board: `{facts.get('cause')}`. It is a defect of "
            "the merged code: fix the revision on `main` through a code card; never repair the production "
            "database by hand."
        )
    if reason == LOCK_TIMEOUT:
        return (
            f"The migration waited longer than its bound for a lock (revision `{revision}`): another session "
            "held the migration advisory lock or a table the revision alters. Find it (`pg_locks`, "
            "`pg_stat_activity`), let it finish, then upgrade."
        )
    if reason in (DESTRUCTIVE_PENDING, UNCLASSIFIED_PENDING):
        return (
            f'Revision `{revision}` is not declared `release_safety = "additive"`, so the release applied '
            "nothing. Decide whether the running code survives it; applying it is a deliberate upgrade "
            "(`docs/BOARD_STORE.md` §7.4)."
        )
    return f"The release could not read or reach what it needed: {facts['message']}"


def _description(task: dict[str, Any], facts: dict[str, Any]) -> str:
    applied = ", ".join(facts.get("applied") or []) or "none"
    pending = ", ".join(facts.get("pending") or []) or "none recorded"
    return "\n".join(
        [
            (
                f"The dispatcher released {task['ref']} but did not activate it on production: the board "
                "schema of the target commit was refused, so the production checkout was not moved."
            ),
            "",
            "## State",
            "",
            f"- Delivered to the remote: {_landing_line(facts)}.",
            f"- Production checkout `{facts['checkout']}`: still at `{facts['old']}` (old code).",
            f"- Target code revision: `{facts['target']}`, migration head `{facts.get('target_head')}`.",
            f"- Board schema before the release: `{facts.get('store_revision_before')}`.",
            f"- Owed migrations, in order: {pending}.",
            f"- Applied by this release (committed, additive): {applied}.",
            (
                f"- Refused or failing migration: `{facts.get('revision') or 'none'}` — `{facts['code']}` / "
                f"`{facts['reason']}`: {facts['message']}"
            ),
            "",
            "## What to fix",
            "",
            _what_to_fix(facts),
            "",
            "## Supported recovery",
            "",
            (
                "1. Once the cause is fixed on `main`, run `secretary upgrade` as the installation's runtime user: it "
                f"fast-forwards `{facts['checkout']}` and applies every owed revision under the same migration "
                "advisory lock."
            ),
            (
                "2. Verify: `git -C "
                f"{facts['checkout']} rev-parse HEAD` is `{facts['target']}` or a descendant of it, and "
                "`secretary doctor --json` reports the board schema `current` with no pending migration and no "
                "finding beyond the sprint's baseline."
            ),
            "3. Complete this card with what was done and how it was verified.",
        ]
    )


__all__ = ["ACTION", "block_refused_activation", "operation_request_id"]
