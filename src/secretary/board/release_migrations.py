"""The release's schema boundary: the target commit's board migrations, applied before its code runs.

When the dispatcher releases a Secretary card it is about to fast-forward the production checkout it
is itself running from. The board store must already be at the schema of the commit it moves to:
readers of the new code refuse an older one (`board.schema_gate`), and nothing runs `secretary
upgrade` in between. :func:`prepare` is that step, asked by `dispatch.production_checkout` between
pinning the target commit and moving the checkout; the checkout moves only when it returns.

**The target's bundle, not the runner's.** The running dispatcher is still the old build, so its
own `board/migrations` cannot know a revision the target adds. The target commit is pinned by its
full object id, and its `src/secretary/board/migrations` tree is read out of the production
checkout's own object store (`git archive <sha>`, extracted with `tarfile` into a temporary
directory): no worktree, no moving ref, nothing written to the checkout. `migrate.apply` then runs
that directory as Alembic's script location, on the owner connection of `board-store.env`, under
the one advisory lock every runner shares. The bundle's revisions are loaded by the running build,
so a revision may import from the product only what the previous release already ships (the
revisions' existing rule: "spelled here, not imported").

**What runs unattended.** Only a revision that declares itself additive at module level::

    release_safety = "additive"

Additive means the previous release keeps working against the migrated store: it adds tables,
nullable or defaulted columns, indexes, grants and widened CHECK vocabularies, and drops, renames
or narrows nothing the previous release reads or writes. `release_safety = "destructive"` (it
does), and a revision with no declaration or any other value (unclassified), are refused
before any owed revision runs, and the checkout does not move: that release is a person's upgrade
(`secretary upgrade`), which applies every revision as it always has. Nothing is guessed from a
revision's name or number. Revisions `0001`-`0024` shipped before this boundary and declare
nothing; a store that still owes one of them is refused here too.

**Bounded.** The connection has a connect timeout and a session `lock_timeout`
(`SECRETARY_RELEASE_MIGRATION_LOCK_TIMEOUT_SECONDS`, default 30), which bounds both the wait for the
advisory lock and every DDL lock a revision waits for, so a release cannot hold the dispatcher's
tick indefinitely. A timeout is a refusal like any other.

**Idempotent.** The owed revisions are read under the lock, so a replayed release, or one racing an
upgrade, applies nothing already applied; a store with nothing owed takes one lock, one read and
answers unchanged. After applying, the version table is read again and must hold exactly the
target's head before the caller may move the checkout.

Every refusal is one `ReleaseSchemaRefused`, code `release_schema_refused`, with a `reason`, the
target commit, and what the store owed, applied and failed on.
"""

from __future__ import annotations

import io
import re
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from secretary.board import migrate
from secretary.board import store as board_store
from secretary.board.store import BoardStoreError
from secretary.infra.env import positive_int

#: The migration bundle's path inside a Secretary commit.
MIGRATIONS_TREE = "src/secretary/board/migrations"
#: The module attribute a revision declares its release safety with, and its two values.
RELEASE_SAFETY = "release_safety"
ADDITIVE = "additive"
DESTRUCTIVE = "destructive"

#: The code every refusal carries.
RELEASE_SCHEMA_REFUSED = "release_schema_refused"
# Its reasons.
BUNDLE_UNREADABLE = "bundle_unreadable"
STORE_UNAVAILABLE = "store_unavailable"
LOCK_TIMEOUT = "lock_timeout"
DESTRUCTIVE_PENDING = "destructive"
UNCLASSIFIED_PENDING = "unclassified"
MIGRATION_FAILED = "migration_failed"
UNKNOWN_REVISION = "unknown_revision"
NOT_VERIFIED = "not_verified"
REASONS = (
    BUNDLE_UNREADABLE,
    STORE_UNAVAILABLE,
    LOCK_TIMEOUT,
    DESTRUCTIVE_PENDING,
    UNCLASSIFIED_PENDING,
    MIGRATION_FAILED,
    UNKNOWN_REVISION,
    NOT_VERIFIED,
)

LOCK_TIMEOUT_ENV = "SECRETARY_RELEASE_MIGRATION_LOCK_TIMEOUT_SECONDS"
DEFAULT_LOCK_TIMEOUT_SECONDS = 30
CONNECT_TIMEOUT_SECONDS = 10
_GIT_TIMEOUT_SECONDS = 120
_OBJECT_ID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
# SQLSTATE 55P03, lock_not_available: what `lock_timeout` raises.
_LOCK_NOT_AVAILABLE = "55P03"


@dataclass(frozen=True)
class ReleaseSchema:
    """The store is at the target's head: what it was at before, and what this run applied."""

    target: str
    head: str
    before: str | None
    applied: tuple[str, ...]


class ReleaseSchemaRefused(Exception):
    """The target's schema could not be established; the caller must not move the checkout."""

    code = RELEASE_SCHEMA_REFUSED

    def __init__(
        self,
        reason: str,
        message: str,
        *,
        target: str,
        head: str = "",
        before: str | None = None,
        pending: tuple[str, ...] = (),
        applied: tuple[str, ...] = (),
        revision: str = "",
        cause: str = "",
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.target = target
        self.head = head
        self.before = before
        self.pending = tuple(pending)
        self.applied = tuple(applied)
        self.revision = revision
        self.cause = cause

    def facts(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "reason": self.reason,
            "target": self.target,
            "target_head": self.head or None,
            "store_revision_before": self.before,
            "pending": list(self.pending),
            "applied": list(self.applied),
            "revision": self.revision or None,
            "cause": self.cause or None,
            "message": str(self),
        }


def lock_timeout_seconds() -> int:
    return positive_int(LOCK_TIMEOUT_ENV, DEFAULT_LOCK_TIMEOUT_SECONDS)


def materialize(repo: Path | str, target: str, destination: Path) -> Path:
    """Extract the migration bundle of commit `target` from `repo`'s objects into `destination`.

    Returns the bundle's directory (Alembic's script location). Nothing in `repo` is written.
    """
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), "archive", "--format=tar", target, "--", MIGRATIONS_TREE],
            capture_output=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise _unreadable(target, f"git archive could not run: {exc}") from None
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", "replace").strip() or f"exit {completed.returncode}"
        raise _unreadable(target, f"git archive of {MIGRATIONS_TREE} failed: {detail}")
    try:
        with tarfile.open(fileobj=io.BytesIO(completed.stdout), mode="r:") as archive:
            archive.extractall(destination, filter="data")
    except (tarfile.TarError, OSError) as exc:
        raise _unreadable(target, f"the archived bundle could not be extracted: {exc}") from None
    bundle = destination / MIGRATIONS_TREE
    if not (bundle / "env.py").is_file() or not (bundle / "versions").is_dir():
        raise _unreadable(target, f"{MIGRATIONS_TREE} holds no Alembic environment and versions")
    return bundle


def target_graph(bundle: Path, target: str) -> tuple[Any, str]:
    """The bundle's script directory, every revision loaded, and its one head."""
    try:
        script = migrate.script_directory(bundle)
        # Load every revision now, so an unimportable one refuses before a connection is opened.
        list(script.walk_revisions())
        heads = tuple(script.get_heads())
    except Exception as exc:  # noqa: BLE001 - any failure to read the graph is the same refusal
        raise _unreadable(target, f"the migration graph could not be read: {_first_line(exc)}") from None
    if len(heads) != 1:
        raise _unreadable(target, f"the migration graph has {len(heads)} heads, not one: {', '.join(heads)}")
    return script, heads[0]


def admit_additive(script: Any, owed: tuple[str, ...]) -> None:
    """`migrate.apply`'s admission: refuse the run unless every owed revision declares itself additive."""
    for revision in owed:
        declared = getattr(script.get_revision(revision).module, RELEASE_SAFETY, None)
        if declared == ADDITIVE:
            continue
        if declared == DESTRUCTIVE:
            reason, what = DESTRUCTIVE_PENDING, f'declares {RELEASE_SAFETY} = "{DESTRUCTIVE}"'
        else:
            reason, what = (
                UNCLASSIFIED_PENDING,
                (
                    f"declares no {RELEASE_SAFETY}"
                    if declared is None
                    else f"declares {RELEASE_SAFETY} = {declared!r}"
                ),
            )
        raise migrate.MigrationRefused(
            revision,
            reason,
            f"owed revision {revision} {what}; a release applies only revisions declaring "
            f'{RELEASE_SAFETY} = "{ADDITIVE}", so nothing was applied. Owed, in order: {", ".join(owed)}',
            owed,
        )


def prepare(
    repo: Path | str,
    target: str,
    instance_dir: Path | str,
    *,
    lock_timeout: int | None = None,
) -> ReleaseSchema:
    """Bring the installation's board store to the schema of commit `target` of `repo`, or refuse.

    Returns only when the version table holds exactly the target's head; raises
    `ReleaseSchemaRefused` otherwise, having moved nothing but the revisions it reports applied.
    """
    if not _OBJECT_ID.match(target):
        raise _unreadable(target, "the release target is not a full commit object id")
    with tempfile.TemporaryDirectory(prefix="secretary-release-schema-") as scratch:
        bundle = materialize(repo, target, Path(scratch))
        _script, head = target_graph(bundle, target)
        return _apply(bundle, head, target, instance_dir, lock_timeout or lock_timeout_seconds())


def _apply(bundle: Path, head: str, target: str, instance_dir: Path | str, timeout: int) -> ReleaseSchema:
    try:
        config = board_store.resolve(instance_dir)
        import sqlalchemy as sa
    except (BoardStoreError, ImportError) as exc:
        raise ReleaseSchemaRefused(
            STORE_UNAVAILABLE,
            f"the board store cannot be migrated for release {target[:12]}: {exc}",
            target=target,
            head=head,
        ) from exc
    engine = sa.create_engine(
        migrate.sqlalchemy_url(config.for_role("owner")),
        poolclass=sa.pool.NullPool,
        connect_args={"connect_timeout": CONNECT_TIMEOUT_SECONDS},
    )
    before: str | None = None
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql(f"SET lock_timeout = '{int(timeout) * 1000}ms'")
            connection.commit()
            before = migrate.current_revision(connection)
            connection.commit()
            try:
                applied = migrate.apply(
                    connection,
                    passwords=migrate.passwords_for(config),
                    script_location=bundle,
                    admit=admit_additive,
                )
            except migrate.MigrationRefused as exc:
                raise ReleaseSchemaRefused(
                    exc.reason,
                    str(exc),
                    target=target,
                    head=head,
                    before=before,
                    pending=exc.owed,
                    revision=exc.revision,
                ) from exc
            except migrate.MigrationFailed as exc:
                timed_out = _lock_timed_out(exc.__cause__)
                raise ReleaseSchemaRefused(
                    LOCK_TIMEOUT if timed_out else MIGRATION_FAILED,
                    (
                        f"revision {exc.revision} of release {target[:12]} "
                        + (f"waited longer than {timeout}s for a lock" if timed_out else "failed")
                        + f": {exc.cause}"
                    ),
                    target=target,
                    head=head,
                    before=before,
                    pending=exc.owed,
                    applied=exc.applied,
                    revision=exc.revision,
                    cause=exc.cause,
                ) from exc
            after = migrate.current_revision(connection)
            connection.commit()
            if after != head:
                raise ReleaseSchemaRefused(
                    NOT_VERIFIED,
                    f"after migrating, the board store is at {after} and not at release {target[:12]}'s "
                    f"head {head}",
                    target=target,
                    head=head,
                    before=before,
                    applied=applied,
                )
            return ReleaseSchema(target, head, before, applied)
    except ReleaseSchemaRefused:
        raise
    except sa.exc.SQLAlchemyError as exc:
        timed_out = _lock_timed_out(exc)
        cause = _first_line(exc)
        raise ReleaseSchemaRefused(
            LOCK_TIMEOUT if timed_out else STORE_UNAVAILABLE,
            (
                f"the migration lock was not granted within {timeout}s: {cause}"
                if timed_out
                else f"the board store did not accept the release migration run: {cause}"
            ),
            target=target,
            head=head,
            before=before,
            cause=cause,
        ) from exc
    except Exception as exc:
        cause = _first_line(exc)
        raise ReleaseSchemaRefused(
            UNKNOWN_REVISION,
            f"the board store is at {before}, which release {target[:12]}'s migration graph does not "
            f"contain: {cause}",
            target=target,
            head=head,
            before=before,
            cause=cause,
        ) from exc
    finally:
        engine.dispose()


def _lock_timed_out(exc: BaseException | None) -> bool:
    orig = getattr(exc, "orig", exc)
    return getattr(orig, "sqlstate", None) == _LOCK_NOT_AVAILABLE


def _first_line(exc: BaseException) -> str:
    text = str(getattr(exc, "orig", None) or exc).strip()
    return (text.splitlines() or [type(exc).__name__])[0]


def _unreadable(target: str, detail: str) -> ReleaseSchemaRefused:
    return ReleaseSchemaRefused(
        BUNDLE_UNREADABLE,
        f"the migration bundle of release {target[:12] or '(none)'} is unusable: {detail}",
        target=target,
    )


__all__ = [
    "ADDITIVE",
    "DESTRUCTIVE",
    "LOCK_TIMEOUT_ENV",
    "MIGRATIONS_TREE",
    "REASONS",
    "RELEASE_SAFETY",
    "RELEASE_SCHEMA_REFUSED",
    "ReleaseSchema",
    "ReleaseSchemaRefused",
    "admit_additive",
    "materialize",
    "prepare",
    "target_graph",
]
