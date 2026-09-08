"""A throwaway `postgres:16` and the seeding that makes it the same board a fake Kanboard is.

The card asks for the *existing* card tests to run on the PostgreSQL backend, not for a second set
of SQL-flavoured ones.  That is only possible if the two backends can be handed the same starting
board, so this module takes the fixture the Kanboard fakes carry — their `tasks`, `metadata` and
`comments` — and writes it into a migrated store through the same `SqlCardClient` the product
uses.  Nothing here reaches a live installation: the container publishes on loopback, holds no
volume and is removed with the class.

There is no skip.  `tests/test_board_store_schema.py` states the reason and it is the same one
here: a backend proof that quietly passes because it never reached a database is worth less than
no proof at all.  The suite is `integration-board`, where Docker and the core dependencies are
both present.
"""

from __future__ import annotations

import json
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from secretary.board import migrate, schema
from secretary.board.backend import record_key
from secretary.board.sql_cards import SqlCardClient, _task_number_of
from secretary.board.store import BoardStoreConfig

IMAGE = "postgres:16"
OWNER = "secretary_owner"
OWNER_PASSWORD = "throwaway-owner-password"
APP_PASSWORD = "throwaway@app/pass:word"
READ_PASSWORD = "throwaway-read-password"
READY_TIMEOUT_SECONDS = 90


def docker(*arguments: str) -> str:
    completed = subprocess.run(
        ["docker", *arguments], capture_output=True, text=True, timeout=180, check=False
    )
    if completed.returncode != 0:
        raise RuntimeError(f"docker {' '.join(arguments)} failed: {completed.stderr.strip()}")
    return completed.stdout.strip()


class PostgresBoard:
    """One container for a whole test module, and one fresh database per test."""

    def __init__(self) -> None:
        self.container = docker(
            "run",
            "--rm",
            "-d",
            "-e",
            "POSTGRES_DB=postgres",
            "-e",
            f"POSTGRES_USER={OWNER}",
            "-e",
            f"POSTGRES_PASSWORD={OWNER_PASSWORD}",
            "--tmpfs",
            "/var/lib/postgresql/data:rw,size=1g",
            "-p",
            "127.0.0.1::5432",
            IMAGE,
        )
        published = json.loads(docker("inspect", "-f", "{{json .NetworkSettings.Ports}}", self.container))
        self.port = int(published["5432/tcp"][0]["HostPort"])
        self._serial = 0
        self._await_server()

    def stop(self) -> None:
        docker("rm", "-f", self.container)

    def _await_server(self) -> None:
        import psycopg

        deadline = time.monotonic() + READY_TIMEOUT_SECONDS
        last = ""
        while time.monotonic() < deadline:
            try:
                with psycopg.connect(self.config("postgres").for_role("owner").conninfo(), connect_timeout=3):
                    return
            except psycopg.Error as exc:
                last = str(exc).strip()
                time.sleep(0.5)
        raise RuntimeError(f"the throwaway {IMAGE} never accepted a connection: {last}")

    def config(self, dbname: str) -> BoardStoreConfig:
        return BoardStoreConfig(
            host="127.0.0.1",
            port=self.port,
            dbname=dbname,
            owner_user=OWNER,
            owner_password=OWNER_PASSWORD,
            app_user=schema.APP_ROLE,
            app_password=APP_PASSWORD,
            read_user=schema.READ_ROLE,
            read_password=READ_PASSWORD,
        )

    def fresh_database(self) -> BoardStoreConfig:
        """A migrated, empty store of its own, so no test can see another's rows."""
        import psycopg
        import sqlalchemy as sa

        self._serial += 1
        name = f"board_store_case_{self._serial}"
        with psycopg.connect(
            self.config("postgres").for_role("owner").conninfo(), autocommit=True
        ) as maintenance:
            maintenance.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
            maintenance.execute(f"CREATE DATABASE {name} OWNER {OWNER}")
        config = self.config(name)
        engine = sa.create_engine(migrate.sqlalchemy_url(config.for_role("owner")))
        try:
            with engine.connect() as connection:
                if self._serial == 1:
                    migrate.apply(connection, passwords=migrate.passwords_for(config))
                else:
                    # PostgreSQL roles are cluster-wide.  Later per-test databases reuse the
                    # roles proven by the first migration and materialize the same current
                    # metadata without rerunning 0001's intentionally one-time CREATE ROLE.
                    schema.metadata.create_all(connection)
                    connection.exec_driver_sql(f"GRANT USAGE ON SCHEMA public TO {schema.APP_ROLE}, {schema.READ_ROLE}")
                    connection.exec_driver_sql(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {schema.APP_ROLE}")
                    connection.exec_driver_sql(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {schema.APP_ROLE}")
                    connection.exec_driver_sql(f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO {schema.READ_ROLE}")
                    from alembic import command

                    command.stamp(
                        migrate.alembic_config(
                            connection=connection, passwords=migrate.passwords_for(config)
                        ),
                        "heads",
                    )
                    connection.commit()
        finally:
            engine.dispose()
        return config

    def drop_database(self, name: str) -> None:
        """Release one per-test database as soon as its last client is closed."""
        import psycopg

        with psycopg.connect(
            self.config("postgres").for_role("owner").conninfo(), autocommit=True
        ) as maintenance:
            maintenance.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


def _epoch(value: Any) -> datetime:
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        seconds = 0
    return datetime.fromtimestamp(max(seconds, 0), UTC)


def seed_client(config: BoardStoreConfig, fake: Any, instance_dir: Path | str) -> SqlCardClient:
    """Write a Kanboard fake's board into a migrated store and return a client over it.

    The mapping is the importer's (`board/import_board.py`), used the same way it was used on the
    live board: metadata keys the model names become columns, the rest become
    `tasks.extensions.kanboard`, and the swimlane stays in that same bag.  It is deliberately the
    product's own write path — `createTask`, `saveTaskMetadata`, `createComment`,
    `moveTaskPosition` — rather than direct SQL, so a seeding that disagrees with the client is a
    failure of the client rather than a divergence nobody sees.
    """
    client = SqlCardClient(config.for_role("owner"), instance_dir)
    columns = {int(column["id"]): str(column["title"]) for column in fake.call("getColumns", project_id=7)}
    lanes = {
        int(lane["id"]): str(lane["name"])
        for lane in fake.call("getActiveSwimlanes", project_id=7)
    }
    projects = {
        str(meta.get("project"))
        for meta in fake.metadata.values()
        if str(meta.get("project") or "")
    }
    with client.transaction():
        for project in sorted(projects):
            client._execute(
                "INSERT INTO projects (project_id, enabled, registry_present) VALUES (%s, true, true) "
                "ON CONFLICT (project_id) DO NOTHING",
                (project,),
            )
        for name in sorted(set(lanes.values())):
            client._lane_names().append(name) if name not in client._lane_names() else None
        client._lanes = sorted(set(client._lanes or []))
        numbers = {}
        for row in fake.tasks:
            reference = str(row["reference"])
            kind = reference.split(":", 1)[0] if ":" in reference else ""
            numbers[int(row["id"])] = (
                record_key(kind, reference.split(":", 1)[1])
                if kind in {"product", "issue"}
                else _task_number_of(reference)
            )
        for row in fake.tasks:
            number = numbers[int(row["id"])]
            reference = str(row["reference"])
            meta = dict(fake.metadata.get(int(row["id"]), {}))
            if meta.get("record_type") in {"product", "issue"}:
                client.call(
                    "createTask", project_id=1, title=str(row.get("title") or reference),
                    description=str(row.get("description") or ""), column_id=1,
                    swimlane_id=0, reference=reference,
                )
                client.call("saveTaskMetadata", task_id=number, values=meta)
                if int(row.get("is_active", 1) or 0) == 0:
                    client.call("closeTask", task_id=number)
                continue
            state_column = columns.get(int(row.get("column_id") or 0), "Issues")
            from secretary.tasks import _STATE_BY_COLUMN

            created = _epoch(row.get("date_creation"))
            updated = _epoch(row.get("date_modification") or row.get("date_creation"))
            lane = lanes.get(int(row.get("swimlane_id") or 0))
            extensions = {"kanboard": {"swimlane": lane}} if lane else {}
            position = row.get("position")
            try:
                position_value = max(int(position), 0)
            except (TypeError, ValueError):
                position_value = 0
            client._execute(
                "INSERT INTO tasks (task_ref, task_number, title, description, state, archived, "
                "position, project_id, created_at, updated_at, extensions) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)",
                (
                    reference,
                    number,
                    str(row.get("title") or reference),
                    str(row.get("description") or ""),
                    _STATE_BY_COLUMN[state_column],
                    int(row.get("is_active", row.get("status", 1)) or 0) == 0,
                    position_value,
                    meta.get("project") or None,
                    created,
                    updated,
                    json.dumps(extensions),
                ),
            )
        for identifier, meta in fake.metadata.items():
            if (
                meta
                and int(identifier) in numbers
                and meta.get("record_type") not in {"product", "issue"}
            ):
                client.call("saveTaskMetadata", task_id=numbers[int(identifier)], values=dict(meta))
        for identifier in sorted(numbers):
            for comment in fake.call("getAllComments", task_id=identifier) or []:
                # The board's own creation time, not the seeding's: a comment's timestamp is part
                # of what the reader returns, so a fixture that stamped `now()` would be asking
                # the two backends about two different comments.
                body = str(comment.get("comment") or "")
                first = body.splitlines()[0] if body else ""
                marker = first[1:-1] if first.startswith("[") and first.endswith("]") else None
                client._execute(
                    "INSERT INTO task_comments (task_ref, marker, body, created_at) "
                    "VALUES ((SELECT task_ref FROM tasks WHERE task_number = %s), %s, %s, %s)",
                    (numbers[int(identifier)], marker, body, _epoch(comment.get("date_creation"))),
                )
    return client


__all__ = ["IMAGE", "PostgresBoard", "docker", "seed_client"]
