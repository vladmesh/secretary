"""Provision and reconcile the containerized PostgreSQL board-store boundary."""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from secretary import _proc
from secretary._fsutil import write_text_atomic
from secretary.board import store
from secretary.board.store import BoardStoreConfig, BoardStoreError

IMAGE = "postgres:16"
PROJECT = "secretary-board-store"
VOLUME = "board-db"
DEFAULT_COMPOSE_PATH = Path("/opt/secretary/postgres-compose.yml")
COMPOSE_TEXT = f"""services:
  postgres:
    image: {IMAGE}
    restart: unless-stopped
    ports:
      - 127.0.0.1:5432:5432
    environment:
      POSTGRES_DB: ${{SECRETARY_DB_NAME}}
      POSTGRES_USER: ${{SECRETARY_DB_OWNER_USER}}
      POSTGRES_PASSWORD: ${{SECRETARY_DB_OWNER_PASSWORD}}
    volumes:
      - {VOLUME}:/var/lib/postgresql/data
volumes:
  {VOLUME}:
"""


@dataclass(frozen=True)
class ProvisionOutcome:
    actions: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        return bool(self.actions)

    def render(self, *, dry_run: bool = False) -> str:
        if not self.actions:
            return "PostgreSQL container and volume match the supported definition"
        prefix = "would " if dry_run else ""
        return "; ".join(prefix + action for action in self.actions)


def _run(arguments: list[str], *, timeout: int = 180, input_text: str | None = None) -> str:
    try:
        result = _proc.run(arguments, timeout=timeout, input=input_text)
    except FileNotFoundError:
        raise BoardStoreError("docker is required to provision the board store") from None
    except subprocess.TimeoutExpired:
        raise BoardStoreError("Docker timed out while provisioning the board store") from None
    except OSError as exc:
        raise BoardStoreError(f"Docker could not run while provisioning the board store: {exc}") from None
    if result.returncode:
        reason = (result.stderr or result.stdout).strip().splitlines()
        raise BoardStoreError(
            "Docker refused board store provisioning: " + (reason[0] if reason else "command failed")
        )
    return (result.stdout or "").strip()


def _compose_argv(compose_path: Path, project: str, config_path: Path, *arguments: str) -> list[str]:
    return [
        "docker",
        "compose",
        "--project-name",
        project,
        "--env-file",
        str(config_path),
        "--file",
        str(compose_path),
        *arguments,
    ]


def _volume_name(project: str) -> str:
    return f"{project}_{VOLUME}"


def _exists(kind: str, name: str) -> bool:
    try:
        result = _proc.run(["docker", kind, "inspect", name], timeout=30)
    except FileNotFoundError:
        raise BoardStoreError("docker is required to provision the board store") from None
    except (OSError, subprocess.TimeoutExpired):
        raise BoardStoreError(f"Docker could not inspect board store {kind} {name}") from None
    if result.returncode == 0:
        return True
    reason = (result.stderr or result.stdout).strip()
    normalized = reason.casefold()
    if "no such volume" in normalized or "no such object" in normalized:
        return False
    raise BoardStoreError(
        f"Docker could not determine whether board store {kind} {name} exists: "
        + (reason.splitlines()[0] if reason else "inspection failed")
    )


def _write_compose(path: Path, *, dry_run: bool) -> bool:
    try:
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise BoardStoreError("board store compose definition must be a regular file")
        if path.exists():
            if path.read_text(encoding="utf-8") != COMPOSE_TEXT:
                raise BoardStoreError(
                    f"board store compose definition drift at {path}; refusing to replace it"
                )
            if path.stat().st_mode & 0o077:
                raise BoardStoreError(f"board store compose definition permissions are too broad: {path}")
            return False
        if not dry_run:
            write_text_atomic(path, COMPOSE_TEXT)
            path.chmod(0o600)
        return True
    except BoardStoreError:
        raise
    except OSError as exc:
        raise BoardStoreError(f"could not reconcile board store compose definition: {exc}") from None


def _inspect_container(container: str, *, volume_name: str) -> None:
    try:
        payload = json.loads(_run(["docker", "inspect", container], timeout=30))[0]
    except (json.JSONDecodeError, IndexError, TypeError):
        raise BoardStoreError("Docker returned invalid board store container inspection data") from None
    image = payload.get("Config", {}).get("Image")
    restart = payload.get("HostConfig", {}).get("RestartPolicy", {}).get("Name")
    bindings = payload.get("HostConfig", {}).get("PortBindings", {}).get("5432/tcp") or []
    ports = {(item.get("HostIp"), item.get("HostPort")) for item in bindings}
    mounts = {
        (item.get("Type"), item.get("Name"), item.get("Destination")) for item in payload.get("Mounts", [])
    }
    problems = []
    if image != IMAGE:
        problems.append(f"image is {image!r}, expected {IMAGE}")
    if restart != "unless-stopped":
        problems.append(f"restart policy is {restart!r}, expected unless-stopped")
    if ports != {("127.0.0.1", "5432")}:
        problems.append("published port is not exactly 127.0.0.1:5432:5432")
    if ("volume", volume_name, "/var/lib/postgresql/data") not in mounts:
        problems.append(f"persistent volume is not {volume_name}")
    if problems:
        raise BoardStoreError("board store container drift: " + "; ".join(problems))


def _wait_ready(config: BoardStoreConfig, *, timeout: int = 90) -> None:
    try:
        import psycopg
    except ImportError as exc:
        raise BoardStoreError("psycopg is required to verify board store readiness") from exc
    deadline = time.monotonic() + timeout
    last = "connection failed"
    while time.monotonic() < deadline:
        try:
            with psycopg.connect(config.for_role("owner").conninfo(), connect_timeout=3):
                return
        except psycopg.Error as exc:
            last = str(exc).strip().splitlines()[0]
            time.sleep(0.5)
    raise BoardStoreError(f"board store container did not become ready: {last}")


def provision(
    instance_dir: Path | str,
    *,
    allow_create: bool = False,
    dry_run: bool = False,
    compose_path: Path = DEFAULT_COMPOSE_PATH,
    project: str = PROJECT,
) -> ProvisionOutcome | None:
    """Create a fresh store or reconcile a configured one without rotating credentials.

    An absent configuration is an ordinary no-op unless bootstrap explicitly sets
    ``allow_create``.  A pre-existing volume without configuration is refused because the
    entrypoint environment is not an authority for the password already stored in that volume.
    """
    instance = Path(instance_dir)
    config_path = store.store_path(instance)
    volume_name = _volume_name(project)
    actions: list[str] = []
    present = config_path.exists() or config_path.is_symlink()
    if not present and not allow_create:
        return None
    if not present and _exists("volume", volume_name):
        raise BoardStoreError(
            f"board store volume {volume_name} exists without board-store.env; refusing new credentials"
        )
    config = store.resolve(instance) if present else None
    if _write_compose(compose_path, dry_run=dry_run):
        actions.append("materialize PostgreSQL compose definition")
    if not present:
        actions.append("materialize board-store.env")
        if dry_run:
            return ProvisionOutcome(tuple(actions + ["create PostgreSQL container and volume"]))
        config = store.materialize_fresh(instance)
    if dry_run:
        return ProvisionOutcome(tuple(actions))
    assert config is not None
    container = _run(_compose_argv(compose_path, project, config_path, "ps", "--all", "--quiet", "postgres"))
    if container:
        _inspect_container(container, volume_name=volume_name)
    else:
        actions.append("create PostgreSQL container and volume")
    _run(_compose_argv(compose_path, project, config_path, "up", "--detach", "postgres"))
    container = _run(_compose_argv(compose_path, project, config_path, "ps", "--all", "--quiet", "postgres"))
    if not container:
        raise BoardStoreError("Docker Compose did not create the board store container")
    _inspect_container(container, volume_name=volume_name)
    _wait_ready(config)
    return ProvisionOutcome(tuple(actions))


def verify_roles(instance_dir: Path | str) -> None:
    """Verify logins and the public-schema privilege contract after Alembic reaches head."""
    config = store.resolve(instance_dir)
    try:
        import psycopg
    except ImportError as exc:
        raise BoardStoreError("psycopg is required to verify board store roles") from exc
    expected = {
        config.owner_user: (True, True, True, True),
        config.app_user: (True, False, False, False),
        config.read_user: (True, False, False, False),
    }
    try:
        with psycopg.connect(config.for_role("owner").conninfo()) as connection:
            rows = connection.execute(
                "SELECT rolname, rolcanlogin, rolsuper, rolcreatedb, rolcreaterole "
                "FROM pg_roles WHERE rolname = ANY(%s)",
                (list(expected),),
            ).fetchall()
            actual = {
                name: (login, superuser, createdb, createrole)
                for name, login, superuser, createdb, createrole in rows
            }
            if actual != expected:
                raise BoardStoreError("board store role attributes drift from the supported contract")
            checks = connection.execute(
                "SELECT has_schema_privilege(%s, 'public', 'USAGE'), "
                "has_schema_privilege(%s, 'public', 'USAGE'), "
                "has_schema_privilege(%s, 'public', 'CREATE'), "
                "has_schema_privilege(%s, 'public', 'CREATE'), "
                "has_database_privilege(%s, current_database(), 'CREATE'), "
                "has_database_privilege(%s, current_database(), 'CREATE')",
                (
                    config.app_user,
                    config.read_user,
                    config.app_user,
                    config.read_user,
                    config.app_user,
                    config.read_user,
                ),
            ).fetchone()
            if checks != (True, True, False, False, False, False):
                raise BoardStoreError("board store app/read schema or DDL privileges drifted")
            tables = connection.execute(
                "SELECT quote_ident(schemaname) || '.' || quote_ident(tablename) "
                "FROM pg_tables WHERE schemaname = 'public'"
            ).fetchall()
            for (table,) in tables:
                app = connection.execute(
                    "SELECT has_table_privilege(%s, %s, 'SELECT') "
                    "AND has_table_privilege(%s, %s, 'INSERT') "
                    "AND has_table_privilege(%s, %s, 'UPDATE') "
                    "AND has_table_privilege(%s, %s, 'DELETE')",
                    (config.app_user, table) * 4,
                ).fetchone()[0]
                read_select = connection.execute(
                    "SELECT has_table_privilege(%s, %s, 'SELECT')",
                    (config.read_user, table),
                ).fetchone()[0]
                read_write = connection.execute(
                    "SELECT has_table_privilege(%s, %s, 'INSERT') "
                    "OR has_table_privilege(%s, %s, 'UPDATE') "
                    "OR has_table_privilege(%s, %s, 'DELETE')",
                    (config.read_user, table) * 3,
                ).fetchone()[0]
                if not app or not read_select or read_write:
                    raise BoardStoreError(f"board store table privilege drift on {table}")
            defaults = set(
                connection.execute(
                    "SELECT pg_get_userbyid(a.grantee), d.defaclobjtype, a.privilege_type "
                    "FROM pg_default_acl d CROSS JOIN LATERAL aclexplode(d.defaclacl) a "
                    "WHERE d.defaclrole = (SELECT oid FROM pg_roles WHERE rolname = %s) "
                    "AND d.defaclnamespace = 'public'::regnamespace",
                    (config.owner_user,),
                ).fetchall()
            )
            required_defaults = {
                *(
                    (config.app_user, "r", privilege)
                    for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE")
                ),
                (config.app_user, "S", "USAGE"),
                (config.read_user, "r", "SELECT"),
            }
            if not required_defaults.issubset(defaults):
                raise BoardStoreError("board store default privileges drifted")
        for role in ("app", "read"):
            with psycopg.connect(config.for_role(role).conninfo()) as connection:
                connection.execute("SELECT 1").fetchone()
    except BoardStoreError:
        raise
    except psycopg.Error as exc:
        raise BoardStoreError(
            "board store role or credential verification failed: " + str(exc).strip().splitlines()[0]
        ) from exc


__all__ = [
    "COMPOSE_TEXT",
    "DEFAULT_COMPOSE_PATH",
    "IMAGE",
    "PROJECT",
    "ProvisionOutcome",
    "provision",
    "verify_roles",
]
