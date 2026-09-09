"""Version-matched PostgreSQL dump and local restore primitives.

Passwords travel only through a mode-0600 pgpass file mounted into the pinned
client container.  They are never command arguments, output, or archive data.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from secretary import _proc
from secretary._fsutil import sha256_file, write_text_atomic
from secretary.board import migrate
from secretary.board.provision import IMAGE, POSTGRES_MAJOR, verify_roles
from secretary.board.store import BoardStoreConfig, BoardStoreError, resolve

ENGINE = "postgresql"
DUMP_FORMAT = "custom"
DUMP_PURPOSE = "data-only local recovery into the shipped schema and role boundary"
_VERSION_RE = re.compile(r"\(PostgreSQL\)\s+(\d+)(?:\.(\d+))?")


class PostgresRecoveryError(RuntimeError):
    pass


def endpoint_identity(config: BoardStoreConfig) -> str:
    value = f"{config.host}\0{config.port}\0{config.dbname}".encode()
    return hashlib.sha256(value).hexdigest()


def inspect_source(instance_dir: Path) -> tuple[BoardStoreConfig, dict[str, Any]]:
    try:
        config = resolve(instance_dir)
        import psycopg
    except (BoardStoreError, ImportError) as exc:
        raise PostgresRecoveryError(f"PostgreSQL board store is not usable: {exc}") from None
    try:
        with psycopg.connect(config.for_role("owner").conninfo(), connect_timeout=5) as connection:
            server_num = int(connection.execute("SHOW server_version_num").fetchone()[0])
            revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
            counts = _table_counts(connection)
    except (psycopg.Error, TypeError, ValueError) as exc:
        raise PostgresRecoveryError(
            "PostgreSQL board store preflight failed: " + str(exc).strip().splitlines()[0]
        ) from None
    server_major = server_num // 10000
    if server_major != POSTGRES_MAJOR:
        raise PostgresRecoveryError(
            f"PostgreSQL server major {server_major} does not match the shipped client major "
            f"{POSTGRES_MAJOR}"
        )
    head = migrate.head_revision()
    current = str(revision[0]) if revision else ""
    if current != head:
        raise PostgresRecoveryError(
            f"PostgreSQL board store is at schema revision {current or '(none)'}; expected {head}"
        )
    return config, {
        "engine": ENGINE,
        "format": DUMP_FORMAT,
        "dump_version": 1,
        "image": IMAGE,
        "server_major": server_major,
        "source_schema": current,
        "alembic_head": head,
        "restore_purpose": DUMP_PURPOSE,
        "source_endpoint_id": endpoint_identity(config),
        "table_counts": counts,
    }


def create_dump(config: BoardStoreConfig, destination: Path, metadata: dict[str, Any]) -> dict[str, Any]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.parent.chmod(0o700)
    pgpass = destination.parent / ".pgpass"
    try:
        write_text_atomic(pgpass, _pgpass(config))
        pgpass.chmod(0o600)
        args = _docker_client_args(destination.parent, pgpass.name) + [
            "pg_dump",
            "--host", config.host,
            "--port", str(config.port),
            "--username", config.owner_user,
            "--dbname", config.dbname,
            "--format=custom",
            "--data-only",
            "--no-owner",
            "--no-privileges",
            "--exclude-table=alembic_version",
            f"--file=/backup/{destination.name}",
        ]
        _run_client(args, "pg_dump")
        destination.chmod(0o600)
        _run_client(
            _docker_client_args(destination.parent, pgpass.name)
            + ["pg_restore", "--list", f"/backup/{destination.name}"],
            "pg_restore validation",
        )
        version = _tool_version("pg_dump")
        return {**metadata, "tool_version": version, "bytes": destination.stat().st_size}
    finally:
        with contextlib.suppress(FileNotFoundError):
            pgpass.unlink()


def restore_dump(
    dump: Path,
    instance_dir: Path,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    try:
        import psycopg

        config = resolve(instance_dir)
        if endpoint_identity(config) == metadata.get("source_endpoint_id"):
            raise PostgresRecoveryError("refusing to restore into the source PostgreSQL database")
        migrate.migrate_instance(instance_dir)
        verify_roles(instance_dir)
        with psycopg.connect(config.for_role("owner").conninfo(), connect_timeout=5) as connection:
            revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
            current = str(revision[0]) if revision else None
            counts = _table_counts(connection)
    except PostgresRecoveryError:
        raise
    except (BoardStoreError, ImportError) as exc:
        raise PostgresRecoveryError(f"PostgreSQL restore target is not usable: {exc}") from None
    except psycopg.Error as exc:
        raise PostgresRecoveryError(
            "PostgreSQL restore target preflight failed: " + str(exc).strip().splitlines()[0]
        ) from None
    expected_head = metadata.get("alembic_head")
    if current != expected_head or current != migrate.head_revision():
        raise PostgresRecoveryError(
            f"PostgreSQL restore target schema {current or '(none)'} does not match dump head {expected_head}"
        )
    nonempty = {name: count for name, count in counts.items() if count}
    if nonempty:
        raise PostgresRecoveryError(
            "PostgreSQL restore target is not empty: " + sorted(nonempty)[0]
        )
    with tempfile.TemporaryDirectory(prefix=".secretary-pg-restore-") as temporary:
        stage = Path(temporary)
        stage.chmod(0o700)
        staged_dump = stage / "postgres.dump"
        # copyfileobj keeps the archive-sized object out of Python memory.
        with dump.open("rb") as source, staged_dump.open("wb") as output:
            while chunk := source.read(1024 * 1024):
                output.write(chunk)
        staged_dump.chmod(0o600)
        pgpass = stage / ".pgpass"
        try:
            write_text_atomic(pgpass, _pgpass(config))
            pgpass.chmod(0o600)
            args = _docker_client_args(stage, pgpass.name) + [
                "pg_restore",
                "--host", config.host,
                "--port", str(config.port),
                "--username", config.owner_user,
                "--dbname", config.dbname,
                "--data-only",
                "--no-owner",
                "--no-privileges",
                "--single-transaction",
                "--exit-on-error",
                "/backup/postgres.dump",
            ]
            _run_client(args, "pg_restore")
        finally:
            with contextlib.suppress(FileNotFoundError):
                pgpass.unlink()
    actual = target_counts(config)
    expected = metadata.get("table_counts")
    if not isinstance(expected, dict) or actual != expected:
        raise PostgresRecoveryError("restored PostgreSQL table counts do not match the source dump")
    return {"migration_head": current, "table_counts": actual, "tool_version": _tool_version("pg_restore")}


def target_counts(config: BoardStoreConfig) -> dict[str, int]:
    import psycopg
    try:
        with psycopg.connect(config.for_role("read").conninfo(), connect_timeout=5) as connection:
            return _table_counts(connection)
    except psycopg.Error as exc:
        raise PostgresRecoveryError(
            "restored PostgreSQL verification failed: " + str(exc).strip().splitlines()[0]
        ) from None


def _table_counts(connection: Any) -> dict[str, int]:
    rows = connection.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
        "AND tablename <> 'alembic_version' ORDER BY tablename"
    ).fetchall()
    result: dict[str, int] = {}
    for (name,) in rows:
        # Names come only from pg_catalog and are identifier-quoted by psycopg.
        from psycopg import sql
        result[str(name)] = int(
            connection.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(name))).fetchone()[0]
        )
    return result


def _docker_client_args(directory: Path, pgpass_name: str) -> list[str]:
    return [
        "docker", "run", "--rm", "--network", "host",
        "--user", f"{os.getuid()}:{os.getgid()}",
        "--volume", f"{directory}:/backup",
        "--env", f"PGPASSFILE=/backup/{pgpass_name}",
        IMAGE,
    ]


def _run_client(args: list[str], action: str) -> str:
    try:
        result = _proc.run(args, timeout=300)
    except FileNotFoundError:
        raise PostgresRecoveryError("docker is required for PostgreSQL recovery") from None
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PostgresRecoveryError(f"{action} could not run: {exc}") from None
    if result.returncode:
        reason = (result.stderr or result.stdout or "command failed").strip().splitlines()
        raise PostgresRecoveryError(f"{action} failed: {reason[-1] if reason else 'command failed'}")
    return (result.stdout or "").strip()


def _tool_version(tool: str) -> str:
    output = _run_client(
        ["docker", "run", "--rm", IMAGE, tool, "--version"],
        f"{tool} version check",
    )
    match = _VERSION_RE.search(output)
    if match is None or int(match.group(1)) != POSTGRES_MAJOR:
        raise PostgresRecoveryError(
            f"{tool} is not the expected PostgreSQL {POSTGRES_MAJOR} client"
        )
    return match.group(0).removeprefix("pg_dump ").removeprefix("pg_restore ")


def _pgpass(config: BoardStoreConfig) -> str:
    def escape(value: object) -> str:
        return str(value).replace("\\", "\\\\").replace(":", "\\:")
    return ":".join(
        escape(value)
        for value in (config.host, config.port, config.dbname, config.owner_user, config.owner_password)
    ) + "\n"


def write_restore_marker(data_dir: Path, archive: Path, result: dict[str, Any]) -> None:
    payload = {
        "version": 1,
        "archive_sha256": sha256_file(archive),
        "migration_head": result["migration_head"],
        "table_counts": result["table_counts"],
        "processes_started": False,
    }
    write_text_atomic(
        data_dir / "postgres-restore.json",
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
    )
