"""Non-secret, local PostgreSQL connection configuration for the board store.

Installation configuration, not a secret (§5.4 of ``docs/BOARD_STORE.md``): a ``KEY=VALUE``
file at ``<instance>/board-store.env``, mode 0600, never exported (the snapshot export allowlist
does not match it, `infra.export_allowlist.is_exported`), refused rather than repaired when it is
partial.  It is not a secret-store value: a database password is regenerable by
recreating the role, is meaningless without the volume it guards, and is needed by
``docker compose up`` before the instance repository is necessarily in a state where the store
can be opened — exactly the argument that kept the old board API token out of the store.

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

from ummanu._fsutil import stage_text
from ummanu.infra.export_allowlist import is_exported
from ummanu.runtime.paths import instance_dir as normalize_instance_dir

STORE_FILE = "board-store.env"

#: §5.4's nine keys, in the order the file lists them.  All nine are required: the parse is
#: all-or-nothing, exactly as the transport's three-key tuple is.
STORE_ENV = (
    "UMMANU_DB_HOST",
    "UMMANU_DB_PORT",
    "UMMANU_DB_NAME",
    "UMMANU_DB_OWNER_USER",
    "UMMANU_DB_OWNER_PASSWORD",
    "UMMANU_DB_APP_USER",
    "UMMANU_DB_APP_PASSWORD",
    "UMMANU_DB_READ_USER",
    "UMMANU_DB_READ_PASSWORD",
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
        dbname="ummanu",
        owner_user="ummanu_owner",
        owner_password=passwords[0],
        app_user="ummanu_app",
        app_password=passwords[1],
        read_user="ummanu_read",
        read_password=passwords[2],
    )


def materialize_fresh(instance_dir: Path | str) -> BoardStoreConfig:
    """Atomically create the complete private file, never replacing credentials.

    The exclusion is checked before credentials exist: a file the snapshot export would copy is
    refused before a password is generated.  The temporary is mode 0600 before its
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
    port = fields["UMMANU_DB_PORT"]
    if not port.isdigit() or not 1 <= int(port) <= 65535:
        raise BoardStoreError(f"board store port is not a TCP port: {port}")
    return BoardStoreConfig(
        host=fields["UMMANU_DB_HOST"],
        port=int(port),
        dbname=fields["UMMANU_DB_NAME"],
        owner_user=fields["UMMANU_DB_OWNER_USER"],
        owner_password=fields["UMMANU_DB_OWNER_PASSWORD"],
        app_user=fields["UMMANU_DB_APP_USER"],
        app_password=fields["UMMANU_DB_APP_PASSWORD"],
        read_user=fields["UMMANU_DB_READ_USER"],
        read_password=fields["UMMANU_DB_READ_PASSWORD"],
    )


def resolve(instance_dir: Path | str) -> BoardStoreConfig:
    """The whole configuration of one installation, behind the exclusion enforcement.

    Every path to a *configured* store goes through here — `resolve_role`, the migration runner,
    `env.py`, and every consumer a later card adds — which is why the enforcement lives here and
    not in the upgrade step alone.  A `board-store.env` at a path the snapshot export copies
    refuses with that reason (`enforce_exclusion`), because these are database credentials and
    migrating on top of an exported credential file would publish it with every checkpoint.
    """
    return resolve_with_lifecycle(instance_dir)[0]


def resolve_with_lifecycle(instance_dir: Path | str) -> tuple[BoardStoreConfig, StoreOutcome]:
    """`resolve`, with the exclusion action it took, so a caller can report it.

    `upgrade.py`'s step renders this outcome; nothing else has to, and nothing may skip it -- except
    a process that has already held the exclusion once (`hold_exclusion`), which answers from that.
    """
    held = _HELD.get(_held_key(instance_dir))
    if isinstance(held, BoardStoreError):
        raise BoardStoreError(str(held))
    path = store_path(instance_dir)
    if not path.exists() and not path.is_symlink():
        # Nothing to exclude, so nothing is written: a read of an installation with no store --
        # `status` and `doctor` among them -- must leave its repository as it found it.
        raise BoardStoreError(f"board store configuration is missing: {path}")
    outcome = enforce_exclusion(instance_dir) if held is None else StoreOutcome()
    return parse(store_path(instance_dir)), outcome


#: The exclusions this process has held, by instance: established (`StoreOutcome`) or refused
#: (the refusal, answered again on every later read). Filled only by `hold_exclusion`.
_HELD: dict[Path, StoreOutcome | BoardStoreError] = {}


def _held_key(instance_dir: Path | str) -> Path:
    return normalize_instance_dir(instance_dir).expanduser().resolve()


def hold_exclusion(instance_dir: Path | str) -> StoreOutcome:
    """Run the exclusion guard once for this process, so its reads do not run it again.

    A long-lived reader -- `web-serve` -- calls this at start-up, before it serves anything. What it
    found holds for the life of the process. A refusal holds too: an exported or missing file keeps
    refusing with the same reason rather than being retried by a read. A store that appears or is
    repaired later is picked up by restarting the process.
    """
    key = _held_key(instance_dir)
    path = store_path(instance_dir)
    try:
        if not path.exists() and not path.is_symlink():
            raise BoardStoreError(f"board store configuration is missing: {path}")
        outcome = enforce_exclusion(instance_dir)
    except BoardStoreError as exc:
        _HELD[key] = exc
        raise
    _HELD[key] = outcome
    return outcome


def resolve_role(instance_dir: Path | str, role: str) -> BoardStoreCredentials:
    """§5.4's role-scoped shape: one installation, one role, one credential."""
    return resolve(instance_dir).for_role(role)


@dataclass(frozen=True)
class StoreOutcome:
    """Independent lifecycle actions taken for one store-configuration reconciliation.

    The exclusion itself takes no action any more: it is a property of the export allowlist, not an
    entry this product writes, so only a mode repair could be reported here.
    """

    mode_repaired: bool = False

    @property
    def changed(self) -> bool:
        return self.mode_repaired

    def render(self, *, dry_run: bool = False) -> str:
        if self.mode_repaired:
            return "would secure board store mode" if dry_run else "secured board store mode"
        return "unchanged"


def _exported_refusal() -> str:
    return (
        f"board store configuration {STORE_FILE} is a live-root path the snapshot export copies; "
        "it must stay out of the export allowlist before it can hold credentials"
    )


def enforce_exclusion(instance_dir: Path | str, *, dry_run: bool = False) -> StoreOutcome:
    """The exclusion half of `ensure_ignored`, and the gate every read of a configured store passes.

    A `board-store.env` the snapshot export would copy (`infra.export_allowlist.is_exported`) raises,
    naming why; otherwise nothing happens. It writes nothing and starts no process, so `dry_run`
    changes nothing either. In particular there is no mode repair here: a credential file that was
    world-readable has already been exposed, so `parse` refuses it rather than quietly chmodding it
    mid-read.
    """
    if is_exported(store_path(instance_dir).name):
        raise BoardStoreError(_exported_refusal())
    return StoreOutcome()


def ensure_ignored(instance_dir: Path | str, *, dry_run: bool = False) -> StoreOutcome:
    """The exclusion of this file from everything that leaves the host.

    It refuses a file at a path the snapshot export allowlist matches; a symlink or permissive mode
    also refuses. Credentials that may already have been exposed are never made healthy by a silent
    chmod. Nothing is written: exclusion is a property of the allowlist, not a `.gitignore` entry.

    It **never creates the file**. The passwords are generated once, by the bootstrap or reconcile
    path that materializes `board-store.env`, and that owner calls this before it writes — which
    is the order that keeps a generated credential from ever being an exported one.
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
    return enforce_exclusion(instance_dir, dry_run=dry_run)


def findings(instance_dir: Path | str) -> list[str]:
    """Public, non-secret store health evidence for status and doctor.

    Read-only by construction: it reports an exported or unreadable file and never creates the
    file. An installation with no file is a pre-store installation, not an unhealthy one, so it
    reports nothing.
    """
    path = store_path(instance_dir)
    if is_exported(path.name):
        return [_exported_refusal()]
    if not path.exists() and not path.is_symlink():
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
    "hold_exclusion",
    "materialize_fresh",
    "parse",
    "resolve",
    "resolve_role",
    "resolve_with_lifecycle",
    "store_path",
]
