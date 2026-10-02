"""The board store's schema gate: operational code refuses a schema older than itself (§7.4).

Every operational connection to the board store — `SqlCardClient`'s pool (cards, sprints,
products/issues, the SQL audit, `SqlBoardHost`), `PoStore` and `OwnerEventStore` — reads Alembic's
version table once when it opens, through :func:`require`, before any statement that depends on
the schema. Doctor reads the same answer through :func:`assess`. Three outcomes:

* **current.** The version table holds exactly `migrate.EXPECTED_SCHEMA_REVISION`. One primary-key
  read, and nothing imports Alembic or SQLAlchemy: this is the path every healthy read takes.
* **owed.** The store is at a revision of this build's own lineage short of its head, or has no
  version table at all (never migrated). The connection is refused with code `schema_owed`, naming
  the actual and expected revisions and every owed migration in application order; only this path
  consults the Alembic graph. Nothing is cached: the next connection reads again, so once the
  upgrade applies the migrations the same client reads.
* **ahead.** The store is at a revision this build's lineage does not contain — a later build
  migrated it. It is *not* refused. A release applies its additive migrations while the dispatcher
  of the previous build finishes its work; refusing that dispatcher every read would turn every
  release into an outage. Only additive migrations are supported this way: a later build that
  drops or renames what this one reads is not made safe by this gate, and no such migration ships.

Bootstrap, provisioning, migration and restore inspect and apply an owed schema over their own
connections (`migrate`, `provision`, `postgres_recovery`) and never pass through this gate: a gate
there would refuse exactly the run that repairs the refusal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ummanu.board.migrate import EXPECTED_SCHEMA_REVISION

#: The refusal code, whatever family of error carries it.
SCHEMA_OWED = "schema_owed"

CURRENT = "current"
OWED = "owed"
AHEAD = "ahead"
#: Doctor's two further answers (:func:`inspect_instance`): nothing to read, or no reading.
NOT_CONFIGURED = "not_configured"
UNAVAILABLE = "unavailable"

VERSION_QUERY = "SELECT version_num FROM alembic_version"


@dataclass(frozen=True)
class SchemaAssessment:
    """What one read of the version table says about the store against this build."""

    state: str
    #: The revisions the version table holds, sorted; empty when there is no schema at all.
    actual: tuple[str, ...]
    expected: str
    #: The migrations this store owes, in application order; empty unless `state` is owed.
    pending: tuple[str, ...] = field(default=())

    @property
    def owed(self) -> bool:
        return self.state == OWED

    @property
    def actual_text(self) -> str | None:
        return ", ".join(self.actual) or None

    def describe(self) -> str:
        if self.state == CURRENT:
            return f"the board store schema is current at {self.expected}"
        if self.state == AHEAD:
            return (
                f"the board store is at schema revision {self.actual_text}, which this build's "
                f"migrations (head {self.expected}) do not contain: a later build migrated it, and "
                "this build keeps reading it as additive"
            )
        where = (
            f"is at schema revision {self.actual_text}"
            if self.actual
            else "has no schema at all (no Alembic version table)"
        )
        return (
            f"the board store {where} and this build expects {self.expected}; it owes "
            f"{len(self.pending)} migration(s), in order: {', '.join(self.pending)}. "
            "Refusing to read or write through a schema older than this build: the upgrade "
            "applies them"
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "actual": self.actual_text,
            "expected": self.expected,
            "pending": list(self.pending),
        }


class SchemaOwed(Exception):
    """The mixin every family's schema refusal carries: code `schema_owed` and the assessment.

    A caller keeps catching its own family (`TaskError`, `PoStoreError`, `OwnerEventsUnavailable`);
    the concrete classes subclass both this and that family, so an existing ``except`` still
    answers the refusal and a caller that wants the details reads them here.
    """

    code = SCHEMA_OWED
    assessment: SchemaAssessment

    @property
    def actual(self) -> str | None:
        return self.assessment.actual_text

    @property
    def expected(self) -> str:
        return self.assessment.expected

    @property
    def pending(self) -> tuple[str, ...]:
        return self.assessment.pending


def classify(actual: tuple[str, ...], expected: str = EXPECTED_SCHEMA_REVISION) -> SchemaAssessment:
    """The assessment of a version table holding `actual`, against `expected`.

    Only a store that is not current consults the Alembic graph, for its lineage.
    """
    actual = tuple(sorted(actual))
    if actual == (expected,):
        return SchemaAssessment(CURRENT, actual, expected)
    from ummanu.board.migrate import lineage

    walked = lineage(expected)
    if not actual:
        return SchemaAssessment(OWED, actual, expected, walked)
    if any(revision not in walked for revision in actual):
        return SchemaAssessment(AHEAD, actual, expected)
    furthest = max(walked.index(revision) for revision in actual)
    return SchemaAssessment(OWED, actual, expected, walked[furthest + 1 :])


def assess(connection: Any, expected: str = EXPECTED_SCHEMA_REVISION) -> SchemaAssessment:
    """Read the version table on a psycopg connection and classify it.

    A missing version table is a store never migrated, not an error. Any other driver failure
    (an unreachable server, a refused grant) is raised as psycopg raised it, for the caller to
    translate into its own family. The read leaves a transaction open — aborted, when the table was
    missing — and ending it is the caller's: every caller here either closes the connection or ends
    the transaction it already owns.
    """
    import psycopg

    try:
        rows = connection.execute(VERSION_QUERY).fetchall()
    except psycopg.errors.UndefinedTable:
        rows = []
    return classify(tuple(str(row[0]) for row in rows), expected)


def inspect_instance(instance_dir: Any, *, role: str = "read") -> dict[str, Any]:
    """Doctor's reading of one installation's store: :func:`assess` as a JSON-ready dict.

    Read only: one statement on a connection of the `read` role, rolled back and closed, and the
    configuration is read without the git exclusion `board_store.resolve` would add. Besides the
    three states of :func:`classify`, `not_configured` (no `board-store.env`, so no store to read)
    and `unavailable` (configured, and the version table could not be read) carry a `reason`.
    """
    from ummanu.board.store import BoardStoreError, enforce_exclusion, parse, store_path

    base: dict[str, Any] = {"actual": None, "expected": EXPECTED_SCHEMA_REVISION, "pending": []}
    path = store_path(instance_dir)
    if not path.exists() and not path.is_symlink():
        return {**base, "state": NOT_CONFIGURED, "reason": "no board-store.env; the board store is not configured"}
    try:
        # `resolve`'s refusal of a tracked file, without the exclusion write it would make.
        enforce_exclusion(instance_dir, dry_run=True)
        credentials = parse(path).for_role(role)
        import psycopg
    except (BoardStoreError, ImportError) as exc:
        return {**base, "state": UNAVAILABLE, "reason": f"the board store is not usable: {exc}"}
    try:
        with psycopg.connect(credentials.conninfo(), connect_timeout=5) as connection:
            try:
                assessment = assess(connection)
            finally:
                connection.rollback()
    except psycopg.Error as exc:
        first = (str(exc).strip().splitlines() or [type(exc).__name__])[0]
        return {**base, "state": UNAVAILABLE, "reason": f"the board store schema could not be read: {first}"}
    except Exception as exc:  # noqa: BLE001 - an unreadable migration graph is an unavailable answer too
        return {**base, "state": UNAVAILABLE, "reason": f"the board store schema could not be assessed: {exc}"}
    return {**assessment.to_json(), "message": assessment.describe()}


def require(connection: Any, refusal: type[SchemaOwed]) -> SchemaAssessment:
    """:func:`assess`, raising `refusal(assessment)` when the store owes migrations."""
    assessment = assess(connection)
    if assessment.owed:
        raise refusal(assessment)
    return assessment


__all__ = [
    "AHEAD",
    "CURRENT",
    "NOT_CONFIGURED",
    "OWED",
    "SCHEMA_OWED",
    "UNAVAILABLE",
    "VERSION_QUERY",
    "SchemaAssessment",
    "SchemaOwed",
    "assess",
    "classify",
    "inspect_instance",
    "require",
]
