"""Preserve an imported cutover target and prepare one empty successor target.

This is deliberately a database lifecycle, not an importer retry.  The database OID is
the authority across every restart because the configured name changes halfway through
the operation.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any

from secretary._fsutil import sha256_file
from secretary.board import migrate, postgres_recovery
from secretary.board.provision import verify_roles
from secretary.board.store import BoardStoreConfig, BoardStoreError, parse, resolve, store_path

PHASES = (
    "eligibility",
    "occupied_verification",
    "dump_publication",
    "connection_fence",
    "database_rename",
    "database_create",
    "migration",
    "role_verification",
    "empty_verification",
    "history_publication",
    "canonical_release",
)


def confirmation(plan_id: str, database_oid: int, database_name: str) -> str:
    material = f"{plan_id}\0{database_oid}\0{database_name}".encode()
    return "PREPARE-SUCCESSOR-" + hashlib.sha256(material).hexdigest()[:16]


def archive_name(plan_id: str, database_oid: int) -> str:
    # PostgreSQL identifiers are at most 63 bytes.  This is 56 bytes at the largest OID.
    return f"secretary_archive_{plan_id[:20]}_{database_oid}"


def exact_eligibility(
    state: dict[str, Any] | None,
    backend: str,
    *,
    artifacts: Path | None = None,
) -> dict[str, Any]:
    """Central supported-shape gate for the sole post-import recovery route."""
    reason = "eligible-post-import-kanboard-recovery"
    if not isinstance(state, dict) or state.get("version") != 1:
        reason = "canonical-state-missing-or-invalid"
    elif state.get("status") != "recovered-frozen":
        reason = "canonical-state-not-recovered-frozen"
    elif state.get("recovery", {}).get("branch") != "kanboard-before-first-write":
        reason = "recovery-branch-not-kanboard-before-first-write"
    elif state.get("first_sql_write") is not None:
        reason = "sql-write-or-audit-uncertainty"
    elif backend != "kanboard":
        reason = "current-backend-not-kanboard"
    else:
        phases = state.get("phases")
        imported = phases.get("final_fenced_import") if isinstance(phases, dict) else None
        parity = phases.get("full_parity") if isinstance(phases, dict) else None
        if not isinstance(imported, dict) or imported.get("status") != "complete":
            reason = "final-import-not-complete"
        elif not isinstance(parity, dict) or parity.get("status") != "complete":
            reason = "full-parity-not-complete"
        elif not parity.get("evidence", {}).get("parity", {}).get("ok"):
            reason = "full-parity-not-clean"
        elif "selector_activation" in phases:
            reason = "selector-activation-entered"
        elif imported.get("evidence", {}).get("import", {}).get("parity", {}).get("ok") is not True:
            reason = "import-parity-not-clean"
    if reason == "eligible-post-import-kanboard-recovery" and artifacts is not None:
        try:
            _report(state, artifacts)
        except RuntimeError:
            reason = "import-report-unreadable-or-mismatched"
    return {"eligible": reason == "eligible-post-import-kanboard-recovery", "reason": reason}


def _config_for_database(config: BoardStoreConfig, name: str) -> BoardStoreConfig:
    return replace(config, dbname=name)


def _connect(config: BoardStoreConfig, database: str | None = None, *, autocommit: bool = False):
    import psycopg

    selected = _config_for_database(config, database) if database else config
    return psycopg.connect(
        selected.for_role("owner").conninfo(), connect_timeout=5, autocommit=autocommit
    )


def inspect_database(config: BoardStoreConfig, *, name: str | None = None) -> dict[str, Any]:
    import psycopg

    target = name or config.dbname
    try:
        with _connect(config, "postgres") as connection:
            row = connection.execute(
                "SELECT d.oid, d.datname, r.rolname, d.datallowconn "
                "FROM pg_database d JOIN pg_roles r ON r.oid=d.datdba WHERE d.datname=%s",
                (target,),
            ).fetchone()
    except (psycopg.Error, OSError, TypeError, ValueError) as exc:
        raise RuntimeError(f"could not inspect PostgreSQL database identity: {exc}") from None
    if row is None:
        raise RuntimeError(f"PostgreSQL database is missing: {target}")
    return {"oid": int(row[0]), "name": str(row[1]), "owner": str(row[2]), "allow_connections": bool(row[3])}


def _report(
    state: dict[str, Any], artifacts: Path | None = None
) -> tuple[Path, dict[str, Any], str]:
    imported = state["phases"]["final_fenced_import"]["evidence"]
    path = Path(str(imported.get("report") or ""))
    if not path.is_absolute() or not path.is_file() or path.is_symlink():
        raise RuntimeError("matching import report is not a readable regular file")
    if artifacts is not None and path != artifacts / f"import-{state['plan_id']}.json":
        raise RuntimeError("import report path does not match the canonical plan identity")
    try:
        raw = path.read_bytes()
        document = json.loads(raw)
    except (OSError, UnicodeError, ValueError) as exc:
        raise RuntimeError(f"matching import report is unreadable: {type(exc).__name__}") from None
    expected = imported.get("import")
    if not isinstance(document, dict) or document != expected:
        raise RuntimeError("import report does not match canonical imported evidence")
    return path, document, hashlib.sha256(raw).hexdigest()


def _occupied_proof(
    config: BoardStoreConfig, state: dict[str, Any], artifacts: Path
) -> dict[str, Any]:
    identity = inspect_database(config)
    if identity["owner"] != config.owner_user or not identity["allow_connections"]:
        raise RuntimeError("configured imported database owner or connection policy is unexpected")
    report_path, report, report_sha = _report(state, artifacts)
    source_config, metadata = postgres_recovery.inspect_source(Path(state["successor_preparation"]["instance"]))
    expected_counts = state["phases"]["full_parity"]["evidence"].get("counts")
    if metadata["table_counts"] != expected_counts or report.get("counts") != expected_counts:
        raise RuntimeError("occupied PostgreSQL table counts do not match canonical import evidence")
    if metadata["source_schema"] != migrate.head_revision():
        raise RuntimeError("occupied PostgreSQL database is not at the current migration head")
    if metadata["table_counts"].get("requests") != expected_counts.get("requests") or metadata["table_counts"].get("board_events") != expected_counts.get("board_events"):
        raise RuntimeError("occupied PostgreSQL application audit differs from the import boundary")
    # A public TaskReader export exercises the collision-safe card transport read path.
    from secretary.board.sql_cards import SqlCardClient
    from secretary.tasks import TaskReader

    client = SqlCardClient(source_config.for_role("read"), Path(state["successor_preparation"]["instance"]))
    try:
        cards = TaskReader(client).export()
    finally:
        if client._connection is not None:
            client._connection.close()
    by_ref = {str(card.get("reference")): card for card in cards}
    collision = {ref: by_ref.get(ref) for ref in ("butler-1", "codegen-product-kit-1")}
    if any(value is None for value in collision.values()):
        raise RuntimeError("SQL export/readback did not preserve the cross-project collision")
    with _connect(config, "postgres") as connection:
        foreign = int(connection.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE datid=%s AND pid<>pg_backend_pid()",
            (identity["oid"],),
        ).fetchone()[0])
    if foreign:
        raise RuntimeError(f"foreign PostgreSQL connections prevent target rotation: {foreign}")
    return {
        "database": identity,
        "schema_head": metadata["source_schema"],
        "counts": metadata["table_counts"],
        "audit_boundary": {"requests": expected_counts["requests"], "board_events": expected_counts["board_events"]},
        "import_report": {"path": str(report_path), "sha256": report_sha},
        "collision_readback": {
            ref: {
                "reference": collision[ref]["reference"],
                "transport_id": collision[ref].get("id"),
                "archived": bool(collision[ref].get("archived")),
                "comment_count": len(collision[ref].get("comments") or []),
                "metadata_sha256": hashlib.sha256(
                    json.dumps(
                        collision[ref].get("metadata") or {},
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest(),
            }
            for ref in collision
        },
        "dump_metadata": metadata,
    }


def status_probe(
    instance: Path, state: dict[str, Any], backend: str, *, artifacts: Path | None = None
) -> dict[str, Any]:
    eligible = exact_eligibility(state, backend, artifacts=artifacts)
    result: dict[str, Any] = {"eligibility": eligible, "phases": state.get("successor_preparation", {}).get("phases", {})}
    if not eligible["eligible"]:
        return result
    try:
        from secretary.head_registry import read_source

        config = parse(store_path(instance))
        pin = read_source(instance)
        revision = str(pin.get("revision") or "") if isinstance(pin, dict) else ""
        if len(revision) != 40:
            raise RuntimeError("installed head registry has no exact product revision")
        saved = state.get("successor_preparation", {}).get("database", {})
        if saved.get("original_oid") is not None:
            identity = {
                "oid": int(saved["original_oid"]),
                "name": str(saved["original_name"]),
                "owner": str(saved["owner"]),
            }
        else:
            identity = inspect_database(config)
        token = confirmation(state["plan_id"], identity["oid"], config.dbname)
        result.update({
            "database": identity,
            "expected_revision": revision,
            "confirmation": token,
            "next_command": (
                f"secretary cutover prepare-successor --instance {instance} "
                f"--expected-revision {revision} --actor <actor> --reason <reason> "
                f"--confirm {token}"
            ),
        })
    except Exception as exc:  # noqa: BLE001 - status must render an unavailable read-only probe
        result["probe_error"] = str(exc)
    return result


class SuccessorOperations:
    def __init__(self, paths: Any, state: dict[str, Any]) -> None:
        self.paths, self.state = paths, state
        self.prep = state["successor_preparation"]
        self.config = resolve(paths.instance)

    def eligibility(self) -> dict[str, Any]:
        from secretary.cutover import _backend
        proof = exact_eligibility(
            self.state, _backend(self.paths), artifacts=self.paths.artifacts
        )
        if not proof["eligible"]:
            raise RuntimeError(f"cutover identity is not eligible: {proof['reason']}")
        return proof

    def occupied_verification(self) -> dict[str, Any]:
        try:
            conflict = inspect_database(
                self.config, name=self.prep["database"]["archive_name"]
            )
        except RuntimeError as exc:
            if "is missing" not in str(exc):
                raise
        else:
            raise RuntimeError(
                "archival database name is already occupied by OID " + str(conflict["oid"])
            )
        proof = _occupied_proof(self.config, self.state, self.paths.artifacts)
        intended = self.prep["database"]
        if proof["database"]["oid"] != intended["original_oid"]:
            raise RuntimeError("configured database OID changed after successor confirmation")
        return proof

    def dump_publication(self) -> dict[str, Any]:
        occupied = self.prep["phases"]["occupied_verification"]["evidence"]
        destination = Path(self.prep["dump"]["path"])
        if destination.exists() and self.prep["dump"].get("sha256"):
            checksum = sha256_file(destination)
            saved = self.prep["dump"].get("sha256")
            if saved and checksum != saved:
                raise RuntimeError("successor dump conflicts with durable checksum evidence")
            return {**self.prep["dump"], "sha256": checksum, "reused": True}
        metadata = postgres_recovery.create_dump(self.config, destination, occupied["dump_metadata"])
        checksum = sha256_file(destination)
        return {**metadata, "path": str(destination), "sha256": checksum, "reused": False}

    def connection_fence(self) -> dict[str, Any]:
        db = self.prep["database"]
        current = inspect_database(self.config, name=db["original_name"])
        if current["oid"] != db["original_oid"]:
            raise RuntimeError("database name no longer resolves to the imported database OID")
        import psycopg.sql
        with _connect(self.config, "postgres", autocommit=True) as connection:
            if current["allow_connections"]:
                connection.execute(psycopg.sql.SQL("ALTER DATABASE {} ALLOW_CONNECTIONS false").format(psycopg.sql.Identifier(db["original_name"])))
            remaining = connection.execute("SELECT pid FROM pg_stat_activity WHERE datid=%s AND pid<>pg_backend_pid()", (db["original_oid"],)).fetchall()
            if remaining:
                raise RuntimeError(f"foreign PostgreSQL connections remain after fence: {len(remaining)}")
        fenced = inspect_database(self.config, name=db["original_name"])
        if fenced["allow_connections"]:
            raise RuntimeError("imported PostgreSQL database connection fence did not persist")
        return fenced

    def database_rename(self) -> dict[str, Any]:
        db = self.prep["database"]
        try:
            archived = inspect_database(self.config, name=db["archive_name"])
        except RuntimeError as exc:
            if "is missing" not in str(exc):
                raise
        else:
            if archived["oid"] != db["original_oid"]:
                raise RuntimeError("archival database name is occupied by an unexpected OID")
            return archived
        original = inspect_database(self.config, name=db["original_name"])
        if original["oid"] != db["original_oid"] or original["allow_connections"]:
            raise RuntimeError("only the fenced imported database may be renamed")
        import psycopg.sql
        with _connect(self.config, "postgres", autocommit=True) as connection:
            connection.execute(psycopg.sql.SQL("ALTER DATABASE {} RENAME TO {}").format(psycopg.sql.Identifier(db["original_name"]), psycopg.sql.Identifier(db["archive_name"])))
        archived = inspect_database(self.config, name=db["archive_name"])
        if archived["oid"] != db["original_oid"] or archived["allow_connections"]:
            raise RuntimeError("archived database identity or connection fence changed during rename")
        return archived

    def database_create(self) -> dict[str, Any]:
        db = self.prep["database"]
        try:
            current = inspect_database(self.config, name=db["original_name"])
        except RuntimeError as exc:
            if "is missing" not in str(exc):
                raise
        else:
            if current["oid"] == db["original_oid"]:
                raise RuntimeError("configured name still identifies the imported database")
            saved = db.get("successor_oid")
            if saved is not None and current["oid"] != saved:
                raise RuntimeError("configured successor database has an unexpected OID")
            return current
        import psycopg.sql
        with _connect(self.config, "postgres", autocommit=True) as connection:
            connection.execute(psycopg.sql.SQL("CREATE DATABASE {} OWNER {} TEMPLATE template0").format(psycopg.sql.Identifier(db["original_name"]), psycopg.sql.Identifier(db["owner"])))
        return inspect_database(self.config, name=db["original_name"])

    def migration(self) -> dict[str, Any]:
        applied = migrate.migrate_instance(self.paths.instance, reuse_existing_roles=True)
        _, metadata = postgres_recovery.inspect_source(self.paths.instance)
        return {"applied": list(applied), "schema_head": metadata["source_schema"]}

    def role_verification(self) -> dict[str, Any]:
        verify_roles(self.paths.instance)
        return {"owner": self.config.owner_user, "app": self.config.app_user, "read": self.config.read_user, "logins_and_default_privileges": "verified"}

    def empty_verification(self) -> dict[str, Any]:
        _, metadata = postgres_recovery.inspect_source(self.paths.instance)
        nonempty = {name: count for name, count in metadata["table_counts"].items() if count}
        if nonempty:
            raise RuntimeError("new configured PostgreSQL database is not empty: " + sorted(nonempty)[0])
        successor = inspect_database(self.config)
        if successor["oid"] == self.prep["database"]["original_oid"]:
            raise RuntimeError("new configured database did not receive a distinct OID")
        archived = inspect_database(self.config, name=self.prep["database"]["archive_name"])
        if archived["oid"] != self.prep["database"]["original_oid"] or archived["allow_connections"]:
            raise RuntimeError("archived imported database is no longer preserved and fenced")
        return {"database": successor, "schema_head": metadata["source_schema"], "table_counts": metadata["table_counts"], "archived_database": archived}

    def history_publication(self) -> dict[str, Any]:
        # The driver performs publication after it has made this terminal phase durable.
        return {"archive": str(self.paths.history / f"postgres-v1-{self.state['plan_id']}.json"), "immutable": True}

    def canonical_release(self) -> dict[str, Any]:
        return {"ordering": "history-fsync-before-canonical-unlink", "released": True}


def prepare(args: Any, paths: Any) -> dict[str, Any]:
    from secretary.cutover import (
        CutoverError,
        CutoverLock,
        _backend,
        _now,
        _provenance,
        _publish_recovered_archive,
        _read_recovered_history,
        _read_state,
        _write_state,
    )

    actor, reason = args.actor.strip(), args.reason.strip()
    if not actor or not reason or not args.confirm.strip():
        raise CutoverError("prepare-successor requires non-empty --actor, --reason and --confirm")
    with CutoverLock(paths):
        _provenance(paths, args.expected_revision)
        state = _read_state(paths)
        if state is None:
            matches = []
            for item in _read_recovered_history(paths):
                archived_state = item["state"]
                archived_prep = archived_state.get("successor_preparation", {})
                if (
                    archived_prep.get("status") == "complete"
                    and archived_prep.get("confirmation") == args.confirm
                    and archived_prep.get("expected_revision") == args.expected_revision
                    and archived_prep.get("actor") == actor
                    and archived_prep.get("reason") == reason
                ):
                    matches.append(item)
            if len(matches) != 1:
                raise CutoverError("there is no canonical or matching completed successor identity")
            completed = matches[0]["state"]
            saved = completed["successor_preparation"]
            config = resolve(paths.instance)
            archived = inspect_database(config, name=saved["database"]["archive_name"])
            configured = inspect_database(config)
            dump = Path(saved["dump"]["path"])
            if (
                archived["oid"] != saved["database"]["original_oid"]
                or archived["allow_connections"]
                or configured["oid"] != saved["database"]["successor_oid"]
                or not dump.is_file()
                or sha256_file(dump) != saved["dump"]["sha256"]
            ):
                raise CutoverError("completed successor database or dump identity no longer matches history")
            return {
                **completed,
                "archived_state": matches[0]["path"],
                "successor_ready": True,
                "idempotent_replay": True,
            }
        if state.get("expected_revision") == args.expected_revision:
            raise CutoverError("prepare-successor requires a later installed revision than the recovered attempt")
        eligible = exact_eligibility(
            state, _backend(paths), artifacts=paths.artifacts
        )
        if not eligible["eligible"]:
            raise CutoverError(f"cutover identity is not eligible for target preparation: {eligible['reason']}")
        prep = state.get("successor_preparation")
        if prep is None:
            config = resolve(paths.instance)
            identity = inspect_database(config)
            expected = confirmation(state["plan_id"], identity["oid"], config.dbname)
            if args.confirm != expected:
                raise CutoverError("confirmation token does not match the plan and current database identity")
            name = archive_name(state["plan_id"], identity["oid"])
            prep = state["successor_preparation"] = {
                "version": 1, "status": "preparing", "instance": str(paths.instance),
                "actor": actor, "reason": reason, "expected_revision": args.expected_revision,
                "confirmation": expected, "started_at": _now(), "phases": {},
                "database": {"original_oid": identity["oid"], "original_name": config.dbname,
                             "archive_name": name, "owner": identity["owner"]},
                "dump": {"path": str(paths.artifacts / f"successor-{state['plan_id']}-{identity['oid']}.dump")},
            }
            _write_state(paths, state)
        else:
            if prep.get("actor") != actor or prep.get("reason") != reason or prep.get("expected_revision") != args.expected_revision or prep.get("confirmation") != args.confirm:
                raise CutoverError("retry must use the successor preparation identity's actor, reason, revision and confirmation")
        operations = SuccessorOperations(paths, state)
        for name in PHASES:
            phase = prep["phases"].get(name, {})
            if phase.get("status") == "complete":
                continue
            prep["phases"][name] = {"status": "intent", "started_at": phase.get("started_at", _now())}
            state["updated_at"] = _now()
            _write_state(paths, state)
            try:
                evidence = getattr(operations, name)()
            except Exception as exc:  # noqa: BLE001 - every failed effect must become durable evidence
                reason_text = str(exc) if isinstance(exc, (RuntimeError, BoardStoreError)) else f"{type(exc).__name__}: {exc}"
                prep["phases"][name].update({"status": "failed", "failed_at": _now(), "reason": reason_text[:1000]})
                prep["status"] = "failed"
                _write_state(paths, state)
                raise CutoverError(f"successor phase {name} failed: {reason_text}") from None
            prep["phases"][name].update({"status": "complete", "completed_at": _now(), "evidence": evidence})
            if name == "dump_publication":
                prep["dump"].update({"sha256": evidence["sha256"], "bytes": evidence["bytes"], "tool_version": evidence["tool_version"]})
            if name == "database_create":
                prep["database"]["successor_oid"] = evidence["oid"]
            prep["status"] = "preparing"
            _write_state(paths, state)
        prep["status"] = "complete"
        prep.setdefault("completed_at", _now())
        state["successor_eligibility"] = {"eligible": True, "reason": "successor-target-prepared"}
        state["successor"] = {"archive": str(paths.history / f"postgres-v1-{state['plan_id']}.json"), "prepared_at": prep["completed_at"], "canonical_slot": "released-after-archive"}
        _write_state(paths, state)
        archive = _publish_recovered_archive(paths, state)
        current = _read_state(paths)
        if current != state:
            raise CutoverError("canonical cutover state changed before successor release")
        try:
            paths.state.unlink()
            descriptor = os.open(paths.state.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError as exc:
            raise CutoverError(f"could not release recovered canonical cutover state: {exc}") from None
        return {**state, "archived_state": str(archive), "successor_ready": True}
