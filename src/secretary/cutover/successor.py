"""Preserve an imported cutover target and prepare one empty successor target.

This is deliberately a database lifecycle, not an importer retry.  The database OID is
the authority across every restart because the configured name changes halfway through
the operation.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any

from secretary._fsutil import sha256_file, stage_text
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
    "release_receipt",
)

UPGRADE_PREREQUISITE = (
    "preserved PostgreSQL target is at 0006_sprint_transport_key; owner/operator must install "
    "the expected revision, run and verify the external 0007_card_transport_key upgrade, then "
    "rerun prepare-successor"
)
RECEIPT_KIND = "postgres-successor-release"


def confirmation(plan_id: str, database_oid: int, database_name: str) -> str:
    material = f"{plan_id}\0{database_oid}\0{database_name}".encode()
    return "PREPARE-SUCCESSOR-" + hashlib.sha256(material).hexdigest()[:16]


def archive_name(plan_id: str, database_oid: int) -> str:
    # PostgreSQL identifiers are at most 63 bytes.  This is 56 bytes at the largest OID.
    return f"secretary_archive_{plan_id[:20]}_{database_oid}"


def release_receipt_path(paths: Any, plan_id: str) -> Path:
    return paths.history / f"successor-release-{plan_id}.json"


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
    elif not isinstance(state.get("recovery"), dict) or state["recovery"].get("branch") != "kanboard-before-first-write":
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
        else:
            parity_evidence = parity.get("evidence")
            imported_evidence = imported.get("evidence")
            parity_result = parity_evidence.get("parity") if isinstance(parity_evidence, dict) else None
            import_result = imported_evidence.get("import") if isinstance(imported_evidence, dict) else None
            import_parity = import_result.get("parity") if isinstance(import_result, dict) else None
            if not isinstance(parity_result, dict) or parity_result.get("ok") is not True:
                reason = "full-parity-not-clean"
            elif "selector_activation" in phases and not _activation_proved_writeless(phases):
                # `recover` chose kanboard-before-first-write only after reading the SQL audit
                # count back at the activation baseline, and the eligibility above already
                # requires that branch with no recorded first write. An activation that never
                # completed or recorded no baseline leaves that proof without a reference point.
                reason = "selector-activation-uncertain"
            elif not isinstance(import_parity, dict) or import_parity.get("ok") is not True:
                reason = "import-parity-not-clean"
    if reason == "eligible-post-import-kanboard-recovery" and artifacts is not None:
        try:
            _report(state, artifacts)
        except (KeyError, RuntimeError, TypeError, ValueError):
            reason = "import-report-unreadable-or-mismatched"
    return {"eligible": reason == "eligible-post-import-kanboard-recovery", "reason": reason}


def _activation_proved_writeless(phases: dict[str, Any]) -> bool:
    activation = phases.get("selector_activation")
    if not isinstance(activation, dict) or activation.get("status") != "complete":
        return False
    evidence = activation.get("evidence")
    baseline = evidence.get("sql_audit_baseline") if isinstance(evidence, dict) else None
    return isinstance(baseline, dict) and isinstance(baseline.get("committed_events"), int)


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


def inspect_schema_revision(config: BoardStoreConfig) -> str:
    try:
        with _connect(config) as connection:
            row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    except Exception as exc:  # noqa: BLE001 - normalize driver/schema failures at the boundary
        raise RuntimeError(f"could not inspect PostgreSQL schema revision: {exc}") from None
    if row is None or not isinstance(row[0], str):
        raise RuntimeError("PostgreSQL database has no readable Alembic revision")
    return row[0]


def _report(
    state: dict[str, Any], artifacts: Path | None = None
) -> tuple[Path, dict[str, Any], str]:
    phases = state.get("phases")
    imported_phase = phases.get("final_fenced_import") if isinstance(phases, dict) else None
    imported = imported_phase.get("evidence") if isinstance(imported_phase, dict) else None
    if not isinstance(imported, dict):
        raise TypeError("canonical imported evidence is malformed")
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
        client.close()
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
    preparation = state.get("successor_preparation")
    phases = preparation.get("phases", {}) if isinstance(preparation, dict) else {}
    result: dict[str, Any] = {"eligibility": eligible, "phases": phases}
    if not eligible["eligible"]:
        return result
    try:
        from secretary.head_registry import read_source

        config = parse(store_path(instance))
        pin = read_source(instance)
        revision = str(pin.get("revision") or "") if isinstance(pin, dict) else ""
        if len(revision) != 40:
            raise RuntimeError("installed head registry has no exact product revision")
        saved = preparation.get("database", {}) if isinstance(preparation, dict) else {}
        if saved.get("original_oid") is not None:
            identity = {
                "oid": int(saved["original_oid"]),
                "name": str(saved["original_name"]),
                "owner": str(saved["owner"]),
            }
        else:
            identity = inspect_database(config)
        token = confirmation(state["plan_id"], identity["oid"], config.dbname)
        prepare_command = (
            f"secretary cutover prepare-successor --instance {instance} "
            f"--expected-revision {revision} --actor <actor> --reason <reason> "
            f"--confirm {token}"
        )
        result.update({"database": identity, "expected_revision": revision, "confirmation": token})
        # From connection_fence onward the configured name is fenced, absent or unmigrated, so
        # the schema probe cannot connect; the retry path skips it for the same reason, and the
        # operator's next command is always the identical prepare-successor retry.
        if "connection_fence" in phases:
            result.update({"schema_revision": None, "next_command": prepare_command})
            return result
        try:
            schema_revision = inspect_schema_revision(config)
        except Exception as exc:  # noqa: BLE001 - the schema branch is advisory, the command is not
            result.update(
                {
                    "schema_revision": None,
                    "schema_probe_error": str(exc),
                    "next_command": prepare_command,
                }
            )
            return result
        schema_action: dict[str, Any]
        if schema_revision == "0006_sprint_transport_key":
            schema_action = {
                "prerequisite": UPGRADE_PREREQUISITE,
                "next_command": f"secretary upgrade --no-pull --instance {instance}",
            }
        elif schema_revision == migrate.head_revision():
            schema_action = {"next_command": prepare_command}
        else:
            schema_action = {
                "prerequisite": (
                    "preserved PostgreSQL target has an unsupported schema; owner/operator "
                    "must verify 0007_card_transport_key before prepare-successor"
                ),
                "next_command": None,
            }
        result.update({"schema_revision": schema_revision, **schema_action})
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
        return {
            **proof,
            "schema_before": proof["schema_head"],
            "forward_migrations": [],
        }

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
        from secretary.cutover import _publish_recovered_archive

        evidence = {
            "archive": str(self.paths.history / f"postgres-v1-{self.state['plan_id']}.json"),
            "immutable": True,
            "directory_fsync": True,
        }
        # Publish the verified completion document, while the canonical path still
        # durably carries intent.  A crash after link/fsync therefore resumes from
        # the linked document without claiming release of the canonical slot.
        published = deepcopy(self.state)
        prep = published["successor_preparation"]
        started_at = prep["phases"]["history_publication"]["started_at"]
        prep["phases"]["history_publication"] = {
            "status": "complete",
            "started_at": started_at,
            "completed_at": started_at,
            "evidence": evidence,
        }
        prep["phases"].setdefault(
            "canonical_release",
            {"status": "intent", "started_at": started_at},
        )
        prep["phases"].setdefault(
            "release_receipt",
            {"status": "intent", "started_at": started_at},
        )
        prep["status"] = "preparing"
        published["updated_at"] = started_at
        archive = _publish_recovered_archive(self.paths, published)
        if not archive.is_file():
            raise RuntimeError("recovered history link was not published")
        published_prep = published.pop("successor_preparation")
        self.state.clear()
        self.state.update(published)
        self.prep.clear()
        self.prep.update(published_prep)
        self.state["successor_preparation"] = self.prep
        return evidence

    def canonical_release(self) -> dict[str, Any]:
        from secretary.cutover import _read_state

        current = _read_state(self.paths)
        if current != self.state:
            raise RuntimeError("canonical cutover state changed before successor release")
        try:
            self.paths.state.unlink()
            descriptor = os.open(self.paths.state.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError as exc:
            raise RuntimeError(f"could not release recovered canonical cutover state: {exc}") from None
        if self.paths.state.exists():
            raise RuntimeError("canonical cutover state remains after successor release")
        return {
            "ordering": "history-fsync-before-canonical-unlink",
            "released": True,
            "directory_fsync": True,
        }

    def release_receipt(self) -> dict[str, Any]:
        _verify_completed_targets(self.paths, self.state)
        return publish_release_receipt(self.paths, self.state)


def _receipt_document(item: dict[str, Any]) -> dict[str, Any]:
    from secretary.cutover import CutoverError

    state = item.get("state")
    prep = state.get("successor_preparation") if isinstance(state, dict) else None
    phases = prep.get("phases") if isinstance(prep, dict) else None
    history = phases.get("history_publication") if isinstance(phases, dict) else None
    release = phases.get("canonical_release") if isinstance(phases, dict) else None
    receipt_phase = phases.get("release_receipt") if isinstance(phases, dict) else None
    history_evidence = history.get("evidence") if isinstance(history, dict) else None
    database = prep.get("database") if isinstance(prep, dict) else None
    dump = prep.get("dump") if isinstance(prep, dict) else None
    completed_evidence = (
        isinstance(phases, dict)
        and all(
            isinstance(phases.get(name), dict)
            and phases[name].get("status") == "complete"
            and isinstance(phases[name].get("evidence"), dict)
            for name in PHASES[: PHASES.index("history_publication")]
        )
    )
    required = (
        isinstance(prep, dict)
        and prep.get("version") == 1
        and prep.get("status") in {"preparing", "complete"}
        and all(
            isinstance(prep.get(name), str) and bool(prep[name])
            for name in (
                "instance",
                "actor",
                "reason",
                "expected_revision",
                "confirmation",
                "started_at",
            )
        )
        and completed_evidence
        and isinstance(history, dict)
        and history.get("status") == "complete"
        and isinstance(history_evidence, dict)
        and history_evidence.get("immutable") is True
        and history_evidence.get("archive") == item.get("path")
        and isinstance(release, dict)
        and release.get("status") in {"intent", "complete"}
        and isinstance(release.get("started_at"), str)
        and isinstance(receipt_phase, dict)
        and receipt_phase.get("status") in {"intent", "complete"}
        and isinstance(receipt_phase.get("started_at"), str)
        and isinstance(database, dict)
        and isinstance(database.get("original_oid"), int)
        and isinstance(database.get("successor_oid"), int)
        and isinstance(database.get("original_name"), str)
        and isinstance(database.get("archive_name"), str)
        and isinstance(database.get("owner"), str)
        and isinstance(dump, dict)
        and isinstance(dump.get("path"), str)
        and isinstance(dump.get("sha256"), str)
        and len(dump["sha256"]) == 64
        and isinstance(item.get("archive_sha256"), str)
        and len(item["archive_sha256"]) == 64
    )
    if not required:
        raise CutoverError("successor recovered history has malformed release evidence")
    return {
        "version": 1,
        "kind": RECEIPT_KIND,
        "plan_id": state["plan_id"],
        "released_at": release.get("completed_at", release["started_at"]),
        "history": {"path": item["path"], "sha256": item["archive_sha256"]},
        "archived_database": {
            "name": database["archive_name"],
            "oid": database["original_oid"],
        },
        "successor_database": {
            "name": database["original_name"],
            "oid": database["successor_oid"],
        },
        "dump": {"path": dump["path"], "sha256": dump["sha256"]},
    }


def _terminal_state(
    state: dict[str, Any], receipt: dict[str, Any], receipt_path: Path, receipt_sha: str
) -> dict[str, Any]:
    completed = deepcopy(state)
    prep = completed["successor_preparation"]
    phases = prep["phases"]
    started_at = phases["canonical_release"]["started_at"]
    phases["canonical_release"] = {
        "status": "complete",
        "started_at": started_at,
        "completed_at": receipt["released_at"],
        "evidence": {
            "ordering": "history-fsync-before-canonical-unlink",
            "released": True,
            "directory_fsync": True,
        },
    }
    phases["release_receipt"] = {
        "status": "complete",
        "started_at": phases["release_receipt"].get("started_at"),
        "completed_at": receipt["released_at"],
        "evidence": {
            "path": str(receipt_path),
            "sha256": receipt_sha,
            "immutable": True,
        },
    }
    prep["status"] = "complete"
    prep["completed_at"] = receipt["released_at"]
    completed["successor_eligibility"] = {
        "eligible": True,
        "reason": "successor-target-prepared",
    }
    completed["successor"] = {
        "archive": receipt["history"]["path"],
        "prepared_at": receipt["released_at"],
        "canonical_slot": "released-after-receipt",
        "release_receipt": phases["release_receipt"]["evidence"],
    }
    return completed


def resolve_history(paths: Any, history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Validate successor history/receipt pairs and project terminal state once."""
    from secretary.cutover import CutoverError, _safe_regular

    resolved: list[dict[str, Any]] = []
    expected_receipts: set[Path] = set()
    for source in history:
        item = dict(source)
        state = item.get("state")
        if not isinstance(state, dict) or "successor_preparation" not in state:
            resolved.append(item)
            continue
        expected = _receipt_document(item)
        receipt_path = release_receipt_path(paths, state["plan_id"])
        expected_receipts.add(receipt_path)
        try:
            receipt_path.lstat()
        except FileNotFoundError:
            item["successor_release"] = {
                "status": "pending",
                "receipt": str(receipt_path),
                "reason": "release-receipt-missing",
            }
        except OSError as exc:
            raise CutoverError(f"cannot inspect successor release receipt: {exc}") from None
        else:
            _safe_regular(receipt_path, "successor release receipt")
            try:
                raw = receipt_path.read_bytes()
                document = json.loads(raw)
            except (OSError, UnicodeError, ValueError) as exc:
                raise CutoverError(
                    f"successor release receipt is unreadable: {type(exc).__name__}"
                ) from None
            if not isinstance(document, dict) or document != expected:
                raise CutoverError("successor release receipt does not match recovered history")
            receipt_sha = hashlib.sha256(raw).hexdigest()
            item["state"] = _terminal_state(state, document, receipt_path, receipt_sha)
            item["successor_release"] = {
                "status": "complete",
                "receipt": str(receipt_path),
                "sha256": receipt_sha,
            }
        resolved.append(item)
    if paths.history.exists():
        for receipt in paths.history.glob("successor-release-*.json"):
            if receipt not in expected_receipts:
                raise CutoverError("successor release receipt has no matching recovered history")
    return resolved


def _history_item(paths: Any, state: dict[str, Any]) -> dict[str, Any]:
    from secretary.cutover import CutoverError, _read_recovered_history

    matches = [item for item in _read_recovered_history(paths) if item["state"].get("plan_id") == state.get("plan_id")]
    if len(matches) != 1:
        raise CutoverError("successor recovered history identity is missing or ambiguous")
    return matches[0]


def publish_release_receipt(paths: Any, state: dict[str, Any]) -> dict[str, Any]:
    from secretary.cutover import CutoverError, _safe_directory, _safe_regular

    item = _history_item(paths, state)
    expected = _receipt_document(item)
    target = release_receipt_path(paths, state["plan_id"])
    body = json.dumps(expected, indent=2, sort_keys=True) + "\n"
    try:
        target.lstat()
    except FileNotFoundError:
        staged = stage_text(target, body)
        try:
            staged.chmod(0o444)
            os.link(staged, target)
            descriptor = os.open(paths.history, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError as exc:
            raise CutoverError(f"could not publish successor release receipt: {exc}") from None
        finally:
            staged.unlink(missing_ok=True)
    except OSError as exc:
        raise CutoverError(f"could not inspect successor release receipt: {exc}") from None
    else:
        _safe_directory(paths.history, "cutover history directory")
        _safe_regular(target, "successor release receipt")
        try:
            existing = target.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise CutoverError(f"could not inspect successor release receipt: {exc}") from None
        if existing != body:
            raise CutoverError("successor release receipt conflicts with recovered history")
        try:
            descriptor = os.open(paths.history, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError as exc:
            raise CutoverError(f"could not sync successor release receipt: {exc}") from None
    return {
        "path": str(target),
        "sha256": sha256_file(target),
        "immutable": True,
    }


def history_status(item: dict[str, Any]) -> dict[str, Any]:
    state = item["state"]
    prep = state["successor_preparation"]
    command = (
        "secretary cutover prepare-successor "
        f"--instance {shlex.quote(prep['instance'])} "
        f"--expected-revision {prep['expected_revision']} "
        f"--actor {shlex.quote(prep['actor'])} --reason {shlex.quote(prep['reason'])} "
        f"--confirm {prep['confirmation']}"
    )
    return {
        "status": item["successor_release"]["status"],
        "phases": prep["phases"],
        "next_command": command if item["successor_release"]["status"] == "pending" else None,
        "release_receipt": item["successor_release"],
    }


def _verify_completed_targets(paths: Any, state: dict[str, Any]) -> None:
    from secretary.cutover import CutoverError

    prep = state.get("successor_preparation")
    database = prep.get("database") if isinstance(prep, dict) else None
    dump_evidence = prep.get("dump") if isinstance(prep, dict) else None
    if not isinstance(database, dict) or not isinstance(dump_evidence, dict):
        raise CutoverError("successor history has malformed database or dump evidence")
    try:
        config = resolve(paths.instance)
        archived = inspect_database(config, name=database["archive_name"])
        configured = inspect_database(config)
        dump = Path(dump_evidence["path"])
        valid = (
            archived["oid"] == database["original_oid"]
            and not archived["allow_connections"]
            and configured["oid"] == database["successor_oid"]
            and dump.is_file()
            and sha256_file(dump) == dump_evidence["sha256"]
        )
    except (KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
        raise CutoverError(f"completed successor evidence cannot be verified: {exc}") from None
    if not valid:
        raise CutoverError("completed successor database or dump identity no longer matches history")


def prepare(args: Any, paths: Any) -> dict[str, Any]:
    from secretary.cutover import (
        CutoverError,
        CutoverLock,
        _backend,
        _now,
        _provenance,
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
        history = _read_recovered_history(paths)

        def matching(item: dict[str, Any]) -> bool:
            archived_state = item.get("state")
            archived_prep = (
                archived_state.get("successor_preparation")
                if isinstance(archived_state, dict)
                else None
            )
            return bool(
                isinstance(archived_prep, dict)
                and archived_prep.get("confirmation") == args.confirm
                and archived_prep.get("expected_revision") == args.expected_revision
                and archived_prep.get("actor") == actor
                and archived_prep.get("reason") == reason
            )

        matches = [item for item in history if matching(item)]
        completed_matches = [
            item
            for item in matches
            if item.get("successor_release", {}).get("status") == "complete"
        ]
        if len(completed_matches) == 1:
            completed = completed_matches[0]["state"]
            _verify_completed_targets(paths, completed)
            return {
                **completed,
                "archived_state": completed_matches[0]["path"],
                "successor_ready": True,
                "idempotent_replay": True,
            }
        if len(completed_matches) > 1:
            raise CutoverError("completed successor history identity is ambiguous")
        pending_matches = [
            item
            for item in matches
            if item.get("successor_release", {}).get("status") == "pending"
        ]
        if state is None:
            if len(pending_matches) != 1:
                raise CutoverError("there is no canonical or matching successor release intent")
            pending = pending_matches[0]
            _verify_completed_targets(paths, pending["state"])
            try:
                SuccessorOperations(paths, pending["state"]).release_receipt()
            except Exception as exc:  # noqa: BLE001 - pending receipt must remain a bounded refusal
                reason_text = (
                    str(exc)
                    if isinstance(exc, (CutoverError, RuntimeError, BoardStoreError))
                    else f"{type(exc).__name__}: {exc}"
                )
                raise CutoverError(
                    f"successor release_receipt remains pending; rerun the identical command: {reason_text}"
                ) from None
            completed = [item for item in _read_recovered_history(paths) if matching(item)]
            if len(completed) != 1 or completed[0].get("successor_release", {}).get("status") != "complete":
                raise CutoverError("successor release receipt did not complete the recovered identity")
            return {
                **completed[0]["state"],
                "archived_state": completed[0]["path"],
                "successor_ready": True,
                "idempotent_replay": True,
                "resumed_release_receipt": True,
            }
        if pending_matches and any(
            item["state"].get("plan_id") != state.get("plan_id") for item in pending_matches
        ):
            raise CutoverError("successor release receipt is pending beside a canonical identity")
        if state.get("expected_revision") == args.expected_revision:
            raise CutoverError("prepare-successor requires an installed revision distinct from the recovered attempt")
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
            schema_revision = inspect_schema_revision(config)
            if schema_revision == "0006_sprint_transport_key":
                raise CutoverError(UPGRADE_PREREQUISITE)
            if schema_revision != migrate.head_revision():
                raise CutoverError(
                    "preserved PostgreSQL target must be at 0007_card_transport_key before prepare-successor"
                )
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
            if "connection_fence" not in prep.get("phases", {}):
                schema_revision = inspect_schema_revision(resolve(paths.instance))
                if schema_revision == "0006_sprint_transport_key":
                    raise CutoverError(UPGRADE_PREREQUISITE)
                if schema_revision != migrate.head_revision():
                    raise CutoverError(
                        "preserved PostgreSQL target must be at 0007_card_transport_key before prepare-successor"
                    )
        operations = SuccessorOperations(paths, state)
        for name in PHASES:
            phase = prep["phases"].get(name, {})
            if phase.get("status") == "complete":
                continue
            prep["phases"][name] = {"status": "intent", "started_at": phase.get("started_at", _now())}
            state["updated_at"] = _now()
            if name != "release_receipt":
                _write_state(paths, state)
            try:
                evidence = getattr(operations, name)()
            except Exception as exc:  # noqa: BLE001 - every failed effect must become durable evidence
                reason_text = str(exc) if isinstance(exc, (RuntimeError, BoardStoreError)) else f"{type(exc).__name__}: {exc}"
                if not paths.state.exists():
                    raise CutoverError(
                        "successor canonical release completed but release_receipt is pending; "
                        f"rerun the identical command: {reason_text}"
                    ) from None
                prep["phases"][name].update({"status": "failed", "failed_at": _now(), "reason": reason_text[:1000]})
                prep["status"] = "failed"
                _write_state(paths, state)
                raise CutoverError(f"successor phase {name} failed: {reason_text}") from None
            # history_publication constructs and publishes its exact completion
            # document itself, so do not replace its timestamps after the link.
            if prep["phases"][name].get("status") != "complete":
                prep["phases"][name].update({"status": "complete", "completed_at": _now(), "evidence": evidence})
            if name == "dump_publication":
                prep["dump"].update({"sha256": evidence["sha256"], "bytes": evidence["bytes"], "tool_version": evidence["tool_version"]})
            if name == "database_create":
                prep["database"]["successor_oid"] = evidence["oid"]
            if name == "canonical_release":
                continue
            if name == "release_receipt":
                resolved = [item for item in _read_recovered_history(paths) if matching(item)]
                if len(resolved) != 1 or resolved[0].get("successor_release", {}).get("status") != "complete":
                    raise CutoverError("successor release receipt did not resolve terminal completion")
                state = resolved[0]["state"]
                prep = state["successor_preparation"]
                break
            prep["status"] = "preparing"
            _write_state(paths, state)
        archive = paths.history / f"postgres-v1-{state['plan_id']}.json"
        return {**state, "archived_state": str(archive), "successor_ready": True}
