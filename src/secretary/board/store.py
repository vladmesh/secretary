"""Non-secret, local PostgreSQL connection configuration for the board store.

The same category as ``board_transport.py``, and deliberately the same mechanism (§5.4 of
``docs/BOARD_STORE.md``): a ``KEY=VALUE`` file at ``<instance>/board-store.env``, mode 0600,
git-ignored, refused rather than repaired when it is partial.  It is not a secret-store value:
a database password is regenerable by recreating the role, is meaningless without the volume it
guards, and is needed by ``docker compose up`` before the instance repository is necessarily in
a state where the store can be opened — exactly the argument that kept Kanboard's API token out
of the store.

It carries one credential **per role**, not one credential, because §5.5's three-role boundary is
unreachable otherwise: a single user would make every consumer connect as the owner.  The caller
does not pick a role freely either; the construction path binds it, which is what turns the
boundary from advisory into reachable.

The one write path is `materialize_fresh`: bootstrap calls it once after proving no persistent
volume already exists. It publishes a complete private file and refuses to replace it. Rotation
remains an explicit operation rather than an ordinary reconcile side effect.
"""

from __future__ import annotations

import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path

from secretary import state_repo
from secretary._fsutil import stage_text
from triggered_agents.runtime.paths import instance_dir as normalize_instance_dir

STORE_FILE = "board-store.env"

#: §5.4's nine keys, in the order the file lists them.  All nine are required: the parse is
#: all-or-nothing, exactly as the transport's three-key tuple is.
STORE_ENV = (
    "SECRETARY_DB_HOST",
    "SECRETARY_DB_PORT",
    "SECRETARY_DB_NAME",
    "SECRETARY_DB_OWNER_USER",
    "SECRETARY_DB_OWNER_PASSWORD",
    "SECRETARY_DB_APP_USER",
    "SECRETARY_DB_APP_PASSWORD",
    "SECRETARY_DB_READ_USER",
    "SECRETARY_DB_READ_PASSWORD",
)

#: The roles of §5.5, and the key prefix each one's credential pair lives under.
ROLES = ("owner", "app", "read")


class BoardStoreError(RuntimeError):
    pass


@dataclass(frozen=True)
class BoardStoreCredentials:
    """One role's connection, resolved from the file and ready to hand to the driver."""

    role: str
    host: str
    port: int
    dbname: str
    user: str
    password: str

    def conninfo(self) -> str:
        """A libpq connection string.  Values are escaped, so a generated password containing a
        space or a backslash still produces one keyword, not a truncated connection."""
        fields = {
            "host": self.host,
            "port": str(self.port),
            "dbname": self.dbname,
            "user": self.user,
            "password": self.password,
        }
        return " ".join(f"{key}={_escape(value)}" for key, value in fields.items())


def _escape(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


@dataclass(frozen=True)
class BoardStoreConfig:
    """The whole file: the server, and one credential per role."""

    host: str
    port: int
    dbname: str
    owner_user: str
    owner_password: str
    app_user: str
    app_password: str
    read_user: str
    read_password: str

    def as_environ(self) -> dict[str, str]:
        return dict(
            zip(
                STORE_ENV,
                (
                    self.host,
                    str(self.port),
                    self.dbname,
                    self.owner_user,
                    self.owner_password,
                    self.app_user,
                    self.app_password,
                    self.read_user,
                    self.read_password,
                ),
                strict=True,
            )
        )

    def for_role(self, role: str) -> BoardStoreCredentials:
        if role not in ROLES:
            raise BoardStoreError(f"board store role must be one of {', '.join(ROLES)}, not {role!r}")
        user, password = {
            "owner": (self.owner_user, self.owner_password),
            "app": (self.app_user, self.app_password),
            "read": (self.read_user, self.read_password),
        }[role]
        return BoardStoreCredentials(role, self.host, self.port, self.dbname, user, password)


def store_path(instance_dir: Path | str) -> Path:
    return normalize_instance_dir(instance_dir) / STORE_FILE


def fresh_config() -> BoardStoreConfig:
    """Build one installation's fixed identities and independent random credentials."""
    passwords = [secrets.token_urlsafe(32) for _ in ROLES]
    if len(set(passwords)) != len(passwords):  # defensive even though collision is negligible
        raise BoardStoreError("could not generate independent board store credentials")
    return BoardStoreConfig(
        host="127.0.0.1",
        port=5432,
        dbname="secretary",
        owner_user="secretary_owner",
        owner_password=passwords[0],
        app_user="secretary_app",
        app_password=passwords[1],
        read_user="secretary_read",
        read_password=passwords[2],
    )


def materialize_fresh(instance_dir: Path | str) -> BoardStoreConfig:
    """Atomically create the complete private file, never replacing credentials.

    The ignore is installed before credentials exist.  The temporary is mode 0600 before its
    rename, so neither a watcher nor a failing process can observe a permissive or partial file.
    """
    directory = normalize_instance_dir(instance_dir)
    path = store_path(directory)
    if path.exists() or path.is_symlink():
        raise BoardStoreError("board store configuration already exists; explicit rotation is required")
    ensure_ignored(directory)
    config = fresh_config()
    body = "".join(f"{key}={value}\n" for key, value in config.as_environ().items())
    staged: Path | None = None
    try:
        staged = stage_text(path, body)
        staged.chmod(0o600)
        os.link(staged, path, follow_symlinks=False)
        staged.unlink()
        staged = None
    except BoardStoreError:
        raise
    except FileExistsError:
        raise BoardStoreError("board store configuration appeared during provisioning") from None
    except OSError as exc:
        raise BoardStoreError(f"could not materialize board store configuration: {exc}") from None
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)
    return config


def parse(path: Path, *, require_private: bool = True) -> BoardStoreConfig:
    """The transport's all-or-nothing parse, over §5.4's nine keys.

    A symlink, a broad mode, an unknown key, a repeated key, an empty value and a missing key
    each refuse the file with a reason of their own.  Nothing here falls back to a default:
    unlike the transport there is no deterministic fresh-install tuple to fall back *to*, since
    the three passwords are generated per installation.
    """
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        raise BoardStoreError(f"board store configuration is missing: {path}") from None
    except OSError as exc:
        raise BoardStoreError(f"board store configuration is unreadable: {path}") from exc
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise BoardStoreError("board store configuration must be a regular file, not a symlink")
    if require_private and mode & 0o077:
        raise BoardStoreError("board store configuration permissions are too broad; run chmod 0600")
    try:
        raw = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise BoardStoreError(f"board store configuration is unreadable: {path}") from exc
    fields: dict[str, str] = {}
    for number, line in enumerate(raw, 1):
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise BoardStoreError(f"board store configuration line {number} must use KEY=VALUE")
        key, value = line.split("=", 1)
        if key not in STORE_ENV or key in fields or not value:
            raise BoardStoreError(f"board store configuration line {number} is invalid")
        fields[key] = value
    missing = [name for name in STORE_ENV if not fields.get(name)]
    if missing:
        raise BoardStoreError("board store configuration is missing " + ", ".join(missing))
    port = fields["SECRETARY_DB_PORT"]
    if not port.isdigit() or not 1 <= int(port) <= 65535:
        raise BoardStoreError(f"board store port is not a TCP port: {port}")
    return BoardStoreConfig(
        host=fields["SECRETARY_DB_HOST"],
        port=int(port),
        dbname=fields["SECRETARY_DB_NAME"],
        owner_user=fields["SECRETARY_DB_OWNER_USER"],
        owner_password=fields["SECRETARY_DB_OWNER_PASSWORD"],
        app_user=fields["SECRETARY_DB_APP_USER"],
        app_password=fields["SECRETARY_DB_APP_PASSWORD"],
        read_user=fields["SECRETARY_DB_READ_USER"],
        read_password=fields["SECRETARY_DB_READ_PASSWORD"],
    )


def resolve(instance_dir: Path | str) -> BoardStoreConfig:
    """The whole configuration of one installation, behind the git-exclusion enforcement.

    Every path to a *configured* store goes through here — `resolve_role`, the migration runner,
    `env.py`, and every consumer a later card adds — which is why the enforcement lives here and
    not in the upgrade step alone.  A `board-store.env` that the instance repository **tracks**
    refuses with that reason (`enforce_exclusion`), because these are database credentials and
    migrating on top of a tracked credential file would make the tracking permanent.
    """
    return resolve_with_lifecycle(instance_dir)[0]


def resolve_with_lifecycle(instance_dir: Path | str) -> tuple[BoardStoreConfig, StoreOutcome]:
    """`resolve`, with the exclusion action it took, so a caller can report it.

    `upgrade.py`'s step renders this outcome; nothing else has to, and nothing may skip it.
    """
    outcome = enforce_exclusion(instance_dir)
    return parse(store_path(instance_dir)), outcome


def resolve_role(instance_dir: Path | str, role: str) -> BoardStoreCredentials:
    """§5.4's role-scoped shape: one installation, one role, one credential."""
    return resolve(instance_dir).for_role(role)


@dataclass(frozen=True)
class StoreOutcome:
    """Independent lifecycle actions taken for one store-configuration reconciliation."""

    ignore_added: bool = False
    mode_repaired: bool = False

    @property
    def changed(self) -> bool:
        return self.ignore_added or self.mode_repaired

    def render(self, *, dry_run: bool = False) -> str:
        actions: list[str] = []
        if self.ignore_added:
            actions.append("would add board store ignore" if dry_run else "added board store ignore")
        if self.mode_repaired:
            actions.append("would secure board store mode" if dry_run else "secured board store mode")
        return "; ".join(actions) if actions else "unchanged"


def enforce_exclusion(instance_dir: Path | str, *, dry_run: bool = False) -> StoreOutcome:
    """The git half of `ensure_ignored`, and the gate every read of a configured store passes.

    Two things happen and one of them is a refusal: a `board-store.env` that is **tracked** in the
    instance repository raises, naming why, and an untracked one gets the durable
    `/board-store.env` exclusion.  Nothing else — in particular no mode repair, which is
    `ensure_ignored`'s and deliberately not on the read path: a credential file that was
    world-readable has already been exposed, so `parse` refuses it rather than quietly chmodding
    it mid-read.
    """
    path = store_path(instance_dir)
    if state_repo.is_tracked(path.parent, f"/{STORE_FILE}"):
        raise BoardStoreError(
            "board store configuration is tracked in the instance repository; "
            "remove it from tracked history before it can be excluded"
        )
    try:
        ignore_added = (
            state_repo.ensure_ignored(path.parent, f"/{STORE_FILE}", dry_run=dry_run)
            if (path.parent / ".git").exists()
            else False
        )
    except state_repo.StateRepoError as exc:
        raise BoardStoreError(f"board store ignore lifecycle failed: {exc}") from exc
    return StoreOutcome(ignore_added=ignore_added)


def ensure_ignored(instance_dir: Path | str, *, dry_run: bool = False) -> StoreOutcome:
    """The durable exclusion `board_transport.ensure` gives the transport, for this file.

    It adds the `/board-store.env` entry to the instance repository's exclusions. A file already
    in the index is not something an exclusion can fix, so it refuses rather than pretending; a
    symlink or permissive mode also refuses. Credentials that may already have been exposed are
    never made healthy by a silent chmod.

    It **never creates the file**. The passwords are generated once, by the bootstrap or reconcile
    path that materializes `board-store.env`, and that owner calls this before it writes — which
    is the order that keeps a generated credential from ever being a tracked one. This card ships
    the operation and does not run it against any live installation.
    """
    path = store_path(instance_dir)
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return enforce_exclusion(instance_dir, dry_run=dry_run)
    except OSError as exc:
        raise BoardStoreError(f"board store configuration is unreadable: {path}") from exc
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise BoardStoreError("board store configuration must be a regular file, not a symlink")
    if mode & 0o077:
        raise BoardStoreError("board store configuration permissions are too broad; run chmod 0600")
    ignore_added = enforce_exclusion(instance_dir, dry_run=dry_run).ignore_added
    return StoreOutcome(ignore_added=ignore_added)


def findings(instance_dir: Path | str) -> list[str]:
    """Public, non-secret store health evidence, in `board_transport.findings`'s shape.

    Read-only by construction: it reports a tracked or unreadable file, it never creates the
    ignore entry or the file itself.  A checkout with no lifecycle marker yet — no file and no
    ignore entry — is a pre-store installation, not an unhealthy one, so it reports nothing.
    """
    path = store_path(instance_dir)
    if state_repo.is_tracked(path.parent, f"/{STORE_FILE}"):
        return [
            (
                "board store configuration is tracked in the instance repository; "
                "remove it from tracked history and rerun upgrade"
            )
        ]
    if (
        not path.exists()
        and not path.is_symlink()
        and not state_repo.is_ignored(path.parent, f"/{STORE_FILE}")
    ):
        return []
    try:
        parse(path)
    except BoardStoreError as exc:
        return [f"board store configuration: {exc}"]
    return []


__all__ = [
    "ROLES",
    "STORE_ENV",
    "STORE_FILE",
    "BoardStoreConfig",
    "BoardStoreCredentials",
    "BoardStoreError",
    "StoreOutcome",
    "enforce_exclusion",
    "ensure_ignored",
    "findings",
    "fresh_config",
    "materialize_fresh",
    "parse",
    "resolve",
    "resolve_role",
    "resolve_with_lifecycle",
    "store_path",
]
