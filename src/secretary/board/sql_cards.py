"""The card reader's and writer's second implementation: the PostgreSQL board store.

`docs/BOARD_STORE.md` §2.2 is the reason this module has the shape it has.  Almost every consumer
of the board reaches cards through `TaskReader` and `TaskWriter`, not through the JSON-RPC client,
so the cheapest honest place to put a second implementation is *underneath* those two classes and
nowhere else.  `SqlCardClient` therefore answers the same eleven-method board vocabulary
`tasks.py` and `board/kanboard.py` speak — `getAllTasks`, `getTaskMetadata`, `saveTaskMetadata`,
`createTask`, `updateTask`, `moveTaskPosition`, `closeTask`, `createComment`, `getAllComments`,
`getColumns`, `getActiveSwimlanes`, `getProjectByName`, `getTaskByReference`, `addSwimlane` — over
`tasks`, `task_comments` and their satellites (§3.5, §3.7).  The public behaviour of the two
classes above it does not change; where their data comes from and where it lands does.

Three mappings do the whole job, and each is the inverse of one the importer already proved on
live data (`board/import_board.py`):

* **state ↔ column.**  The store keeps `tasks.state`; the board keeps a column id.  §3.5's seven
  states and `_STATE_BY_COLUMN`'s seven column titles are the same seven, so the virtual board
  below numbers them once and both directions read that one table.
* **metadata bag ↔ columns.**  §8.1: the keys the model names are columns, and the keys it does
  not are `tasks.extensions.kanboard` (§8.2).  `saveTaskMetadata` writes columns for the former
  and the bag for the latter, so a key nobody modelled is still readable rather than dropped.
* **swimlane ↔ nothing.**  The store has no lane: a lane is a Kanboard presentation of the
  product a card belongs to.  It is kept exactly where the importer keeps it —
  `extensions.kanboard.swimlane` — and the lane *table* is virtual, derived from the lanes the
  rows themselves name plus the products the store holds.

What is deliberately **not** here: a numeric Kanboard card id.  §9 makes the reference the card's
stable identifier and the store has no column for Kanboard's integer, so the integer this client
answers with is `tasks.task_number` — the store's own number for the card, parsed from its
reference by the importer.  Two projects can spell the same number, and rather than pick one this
client refuses (`SqlCardError`), because silently serving the wrong card is the one failure a
board client must not have.
"""

from __future__ import annotations

import contextlib
import json
import re
import threading
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from secretary.board.backend import record_key_kind
from secretary.board.sql_product_issues import ProductIssueRecords
from secretary.board.sql_sprints import SqlSprintRecords
from secretary.board.store import BoardStoreCredentials
from secretary.tasks import TaskError

#: The seven columns of the Pipeline board, in board order.  Their ids are this module's, not
#: Kanboard's: nothing outside the client may depend on the number, only on the title.
BOARD_COLUMNS = (
    (1, "Issues"),
    (2, "Ready"),
    (3, "In progress"),
    (4, "Validate"),
    (5, "Assessment"),
    (6, "Blocked"),
    (7, "Done"),
)

#: `_STATE_BY_COLUMN` read the other way, so a store row can name its column.
_COLUMN_ID_BY_STATE = {
    "issues": 1,
    "ready": 2,
    "in_progress": 3,
    "validate": 4,
    "assessment": 5,
    "blocked": 6,
    "done": 7,
}
_STATE_BY_COLUMN_ID = {identifier: state for state, identifier in _COLUMN_ID_BY_STATE.items()}

#: The one virtual board this client serves.  A second board is a Kanboard concept the store does
#: not have; a name that is not this one is not found, exactly as Kanboard answers.
BOARD_NAME = "Pipeline"
BOARD_ID = 1
SPRINT_BOARD_NAME = "Secretary sprints"
SPRINT_BOARD_ID = 2

#: §8.1's metadata keys that are `tasks` columns, and the column each one is.  Everything else a
#: caller writes lands in `extensions.kanboard` (§8.2).
_METADATA_COLUMNS = {
    "project": "project_id",
    "task_type": "task_type",
    "claim": "claim_worker",
    "slug": "slug",
    "base_branch": "base_branch",
    "seed_ref": "seed_ref",
    "complexity": "complexity",
    "family_preference": "family_preference",
    "head": "head_override",
    "review_head": "review_head_override",
    "resolved_head": "resolved_worker_head",
    "resolved_review_head": "resolved_review_head",
    "routing_reason": "routing_reason",
    "codex_launch_mode": "codex_launch_mode",
    "sprint_ref": "sprint_ref",
}

#: The two counters, which are integers in the store and decimal strings on the board.
_METADATA_COUNTERS = {"retry_same": "retry_same", "retry_switch": "retry_switch"}

#: The columns whose closed vocabulary has a default the board spells as absence (§3.12).
_ENUM_DEFAULTS = {"complexity": "standard", "family_preference": "auto"}

#: `quota_snapshot_at` is a `timestamptz` column and an RFC3339 string on the board.
_METADATA_TIMESTAMP = ("quota_snapshot_at", "quota_snapshot_at")

#: Metadata keys with satellite tables rather than columns (§3.5).
_METADATA_LINKS = ("retry_heads", "blocked_by", "supersedes", "issues")


class SqlCardError(TaskError):
    """The store cannot answer this board question without guessing.

    A `TaskError`, not a bare `RuntimeError`: every command above this client renders that one
    vocabulary as a named refusal with an exit status (`task_commands.run_task_command`), and a
    `RuntimeError` reaching a CLI handler is a traceback with a connection string somewhere up
    the stack.  The code is the one Kanboard's own malformed-reply refusals already use.
    """

    def __init__(self, message: str) -> None:
        super().__init__("backend_error", message, 1)


def _driver_error(action: str, exc: BaseException) -> TaskError:
    """One driver failure, in the refusal vocabulary the CLI already prints.

    Three classes and each keeps its own name.  A driver that is not installed at all, and a
    server that will not accept or keep a connection, are `backend_unavailable` — the code the
    Kanboard transport already uses for exactly that, and the one `board/kanboard.py` treats as
    "the effect may or may not have landed".  Everything else psycopg raises — a constraint, a
    type, a statement the schema refuses — is `backend_error`.  What PostgreSQL said is carried
    through, and only that: psycopg's diagnostics do not contain the connection string, so an
    operator gets the reason without the credentials.
    """
    if isinstance(exc, ModuleNotFoundError):
        return TaskError("backend_unavailable", f"the board store driver is not installed: {exc}", 1)
    import psycopg

    if isinstance(exc, psycopg.OperationalError):
        return TaskError(
            "backend_unavailable", f"the board store is unreachable while it must {action}: {exc}", 1
        )
    return TaskError("backend_error", f"the board store refused to {action}: {exc}", 1)


@contextlib.contextmanager
def _translated(action: str) -> Iterator[None]:
    """`_driver_error`, applied to everything the driver raises inside the block."""
    try:
        import psycopg
    except ModuleNotFoundError as exc:
        raise _driver_error(action, exc) from None
    try:
        yield
    except psycopg.Error as exc:
        raise _driver_error(action, exc) from None


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _epoch(value: datetime | None) -> str:
    if value is None:
        return ""
    return str(int(value.timestamp()))


def _now() -> datetime:
    return datetime.now(UTC)


def _timestamp(value: Any) -> datetime | None:
    text = _text(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _rfc3339(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _task_number_of(ref: str) -> int:
    match = re.search(r"(\d+)$", ref)
    if match is None:
        raise SqlCardError(f"a card reference must end in a number: {ref!r}")
    return int(match.group(1))


class SqlCardClient:
    """The board vocabulary of §2.2, answered from PostgreSQL instead of JSON-RPC.

    One connection, opened lazily and kept: §5.6 sizes the store for ten of them, and a client
    that reconnected per call would spend the whole budget on handshakes.  Autocommit is off, so
    a mutation issued inside `transaction()` is one transaction (§7.1) and one issued outside it
    still commits on its own — which is what keeps the reads of a read-only consumer cheap.
    """

    #: Which of the two card backends this client is (board/backend.py).  The reader spells the
    #: card's identity and its `audit.backend` from this, so a normalized card always says which
    #: store it came out of.
    backend_kind = "postgres"

    def __init__(self, credentials: BoardStoreCredentials, instance_dir: Path | str) -> None:
        self.credentials = credentials
        self.instance_dir = Path(instance_dir)
        self._connection: Any = None
        self._depth = 0
        self._transaction_lock = threading.RLock()
        # The virtual lane table: names the rows themselves carry, plus what `addSwimlane` adds.
        self._lanes: list[str] | None = None
        # The Product/Issue half of the same vocabulary, over the same connection (§3.1, §3.2).
        self.records = ProductIssueRecords(self)
        self.sprints = SqlSprintRecords(self)

    # --- connection ------------------------------------------------------------------

    @property
    def connection(self) -> Any:
        if self._connection is None:
            with _translated("open a connection"):
                import psycopg

                self._connection = psycopg.connect(self.credentials.conninfo(), autocommit=False)
        return self._connection

    def close(self) -> None:
        if self._connection is not None:
            with _translated("close its connection"):
                self._connection.close()
            self._connection = None

    @contextlib.contextmanager
    def transaction(self) -> Iterator[None]:
        """Serialize use of this client's single connection, including cross-thread tests."""
        with self._transaction_lock, self._transaction():
            yield

    @contextlib.contextmanager
    def _transaction(self) -> Iterator[None]:
        """One transaction per protocol mutation (§7.1), re-entrant for nested effects.

        The writer opens this once around a whole protocol mutation — the request claim, the card
        effect and the event — and every inner call joins it.  A failure anywhere inside rolls the
        whole thing back, which is why the `BoardEventPending` class of half-applied write §7.3
        describes does not exist on this backend.
        """
        if self._depth:
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
            return
        self._depth = 1
        try:
            yield
        except BaseException as failure:
            # A rollback that itself fails must not hide what it was rolling back, so the
            # original failure stays the cause of the refusal the caller sees.
            try:
                self.connection.rollback()
            except Exception as exc:  # noqa: BLE001 - every driver failure becomes one refusal.
                raise _driver_error("roll back", exc) from failure
            finally:
                # Two pieces of state are derived from rows this transaction wrote and are wrong
                # the moment those rows are gone: the staged creates and the virtual lane table.
                self.records.staged.clear()
                self.sprints.staged.clear()
                self._lanes = None
            raise
        else:
            if self.records.staged or self.sprints.staged:
                pending = ", ".join(
                    sorted(
                        [row["reference"] for row in self.records.staged.values()]
                        + [row["reference"] for row in self.sprints.staged.values()]
                    )
                )
                self.records.staged.clear()
                self.sprints.staged.clear()
                self._lanes = None
                self.connection.rollback()
                raise SqlCardError(
                    f"a Product/Issue create was never finished and cannot commit: {pending}. "
                    "The row's own table needs the values `saveTaskMetadata` carries (§3.2)"
                )
            with _translated("commit"):
                self.connection.commit()
        finally:
            self._depth = 0

    def _commit_unless_nested(self) -> None:
        if not self._depth:
            with _translated("commit"):
                self.connection.commit()

    def _query(self, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        with _translated("answer a read"), self.connection.cursor() as cursor:
            cursor.execute(sql, params)
            return cursor.fetchall()

    def _execute(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        with _translated("apply a write"), self.connection.cursor() as cursor:
            cursor.execute(sql, params)
            return cursor.rowcount

    # --- the board vocabulary --------------------------------------------------------

    def call(self, method: str, **params: Any) -> Any:
        handler = getattr(self, f"_rpc_{method}", None)
        if handler is None:
            raise SqlCardError(f"the board store does not serve {method}")
        return handler(**params)

    def call_batch(self, calls: Iterable[tuple[str, dict[str, Any]]]) -> list[Any]:
        """The batched read, which is one round trip here because there is no round trip."""
        return [self.call(method, **arguments) for method, arguments in calls]

    def restore_card_rows(self) -> list[dict[str, Any]]:
        """Stored Card rows only; Product/Issue ownership is not restore emptiness."""
        return self._rows()

    # --- board shape -----------------------------------------------------------------

    def _rpc_getProjectByName(self, *, name: str) -> dict[str, Any] | None:
        if name == BOARD_NAME:
            return {"id": BOARD_ID, "name": BOARD_NAME}
        if name == SPRINT_BOARD_NAME:
            return {"id": SPRINT_BOARD_ID, "name": SPRINT_BOARD_NAME}
        return None

    def _rpc_createProject(self, *, name: str) -> Any:
        return SPRINT_BOARD_ID if name == SPRINT_BOARD_NAME else False

    def _rpc_getColumns(self, *, project_id: int) -> list[dict[str, Any]]:
        if int(project_id) == SPRINT_BOARD_ID:
            return [{"id": 1, "title": "Sprints"}]
        return [{"id": identifier, "title": title} for identifier, title in BOARD_COLUMNS]

    def _lane_names(self) -> list[str]:
        if self._lanes is None:
            named = {
                row[0]
                for row in self._query(
                    "SELECT DISTINCT extensions->'kanboard'->>'swimlane' FROM tasks "
                    "WHERE extensions->'kanboard'->>'swimlane' IS NOT NULL"
                )
            }
            named |= {row[0] for row in self._query("SELECT product_id FROM products")}
            self._lanes = sorted(named)
        return self._lanes

    def _rpc_getActiveSwimlanes(self, *, project_id: int) -> list[dict[str, Any]]:
        return [
            {"id": index, "name": name, "position": index}
            for index, name in enumerate(self._lane_names(), start=1)
        ]

    def _rpc_addSwimlane(self, *, project_id: int, name: str) -> Any:
        lanes = self._lane_names()
        if name in lanes:
            return False  # Kanboard answers a duplicate name with false, not with the existing id.
        lanes.append(name)
        lanes.sort()
        return lanes.index(name) + 1

    def _lane_added(self, name: str) -> None:
        """A product this transaction created is a lane from now on (§8.6)."""
        lanes = self._lane_names()
        if name not in lanes:
            lanes.append(name)
            lanes.sort()

    def _lane_id(self, name: str | None) -> int:
        if not name:
            return 0
        lanes = self._lane_names()
        return lanes.index(name) + 1 if name in lanes else 0

    def _lane_name(self, identifier: Any) -> str | None:
        lanes = self._lane_names()
        try:
            index = int(identifier)
        except (TypeError, ValueError):
            return None
        return lanes[index - 1] if 1 <= index <= len(lanes) else None

    # --- cards -----------------------------------------------------------------------

    _CARD_COLUMNS = (
        "task_ref, task_number, title, description, state, archived, position, "
        "extensions, created_at, updated_at, date_moved"
    )

    def _row(self, values: tuple[Any, ...]) -> dict[str, Any]:
        (
            ref,
            number,
            title,
            description,
            state,
            archived,
            position,
            extensions,
            created,
            updated,
            moved,
        ) = values
        bag = extensions if isinstance(extensions, dict) else json.loads(extensions or "{}")
        lane = (bag.get("kanboard") or {}).get("swimlane")
        return {
            "id": number,
            "reference": ref,
            "title": _text(title),
            "description": _text(description),
            "column_id": _COLUMN_ID_BY_STATE[state],
            "position": position,
            "swimlane_id": self._lane_id(lane),
            "date_creation": _epoch(created),
            "date_modification": _epoch(updated),
            # §3.5's `date_moved`, added by revision `0004`: when this card entered the column it
            # is in.  A row the store cannot date — every row written before that revision —
            # answers no value at all rather than a substitute, which is what keeps Done
            # retention refusing an episode nobody can name (§8.6).
            **({"date_moved": _epoch(moved)} if moved is not None else {}),
            "is_active": 0 if archived else 1,
        }

    def _rows(self, where: str = "", params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        clause = f" WHERE {where}" if where else ""
        rows = [
            self._row(values)
            for values in self._query(
                f"SELECT {self._CARD_COLUMNS} FROM tasks{clause} ORDER BY task_ref", params
            )
        ]
        seen: dict[int, str] = {}
        for row in rows:
            previous = seen.get(row["id"])
            if previous is not None:
                raise SqlCardError(
                    "two cards share one card number and the store keeps no separate card id: "
                    f"{previous} and {row['reference']}"
                )
            seen[row["id"]] = row["reference"]
        return rows

    def _rpc_getAllTasks(self, *, project_id: int, status_id: int = 1) -> list[dict[str, Any]]:
        """Every row of the board, which is three tables here and one on Kanboard (§8.1).

        `all_project_cards` is how `ProductIssueStore` sees the board at all, so a Product or an
        Issue that is not in this answer is a record the catalogue cannot report.  The status
        filter means the same thing for all three: an archived Product and a closed Issue are
        `is_active = 0`, exactly as an archived card is.
        """
        if int(project_id) == SPRINT_BOARD_ID:
            return self.sprints.rows() if status_id == 1 else []
        if status_id not in {0, 1}:
            return []
        rows = self._rows("archived = %s", (status_id == 0,))
        active = status_id == 1
        rows += [row for row in self.records.rows() if bool(row["is_active"]) is active]
        return rows

    def _rpc_getTaskByReference(self, *, project_id: int, reference: str) -> dict[str, Any] | None:
        if int(project_id) == SPRINT_BOARD_ID:
            return self.sprints.row_by_reference(reference)
        if self.records.kind_of_reference(reference) is not None:
            return self.records.row_by_reference(reference)
        rows = self._rows("task_ref = %s", (reference,))
        return rows[0] if rows else None

    def _ref_of(self, task_id: Any) -> str:
        rows = self._query("SELECT task_ref FROM tasks WHERE task_number = %s", (int(task_id),))
        if not rows:
            raise SqlCardError(f"no card carries the number {task_id}")
        if len(rows) > 1:
            raise SqlCardError(f"two cards carry the number {task_id}")
        return rows[0][0]

    def _rpc_createTask(
        self,
        *,
        project_id: int,
        title: str,
        description: str = "",
        column_id: int = 1,
        swimlane_id: int = 0,
        reference: str = "",
    ) -> Any:
        if int(project_id) == SPRINT_BOARD_ID:
            return self.sprints.create(
                title=title, description=description, reference=reference
            )
        if not reference:
            raise SqlCardError("the board store identifies a card by its reference (§9)")
        if self.records.kind_of_reference(reference) is not None:
            return self.records.create(
                title=title, description=description, reference=reference
            )
        number = _task_number_of(reference)
        lane = self._lane_name(swimlane_id)
        extensions: dict[str, Any] = {"kanboard": {"swimlane": lane}} if lane else {}
        now = _now()
        self._execute(
            "INSERT INTO tasks (task_ref, task_number, title, description, state, archived, "
            "position, extensions, created_at, updated_at, date_moved) "
            "VALUES (%s, %s, %s, %s, %s, false, %s, %s::jsonb, %s, %s, %s)",
            (
                reference,
                number,
                title,
                description or "",
                _STATE_BY_COLUMN_ID[int(column_id)],
                self._next_position(_STATE_BY_COLUMN_ID[int(column_id)]),
                json.dumps(extensions),
                now,
                now,
                now,
            ),
        )
        self._commit_unless_nested()
        return number

    def _next_position(self, state: str) -> int:
        rows = self._query("SELECT coalesce(max(position), 0) + 1 FROM tasks WHERE state = %s", (state,))
        return int(rows[0][0])

    def _rpc_updateTask(self, *, id: int, **fields: Any) -> bool:
        kind = record_key_kind(id)
        if kind == "sprint":
            result = self.sprints.update(int(id), fields)
            self._commit_unless_nested()
            return result
        if kind is not None:
            result = self.records.update(int(id), fields)
            self._commit_unless_nested()
            return result
        ref = self._ref_of(id)
        assignments = []
        params: list[Any] = []
        for name in ("reference", "title", "description"):
            if name in fields:
                assignments.append(f"{'task_ref' if name == 'reference' else name} = %s")
                params.append(fields[name])
        if not assignments:
            return True
        if "reference" in fields:
            assignments.append("task_number = %s")
            params.append(_task_number_of(str(fields["reference"])))
        assignments.append("updated_at = %s")
        params.append(_now())
        params.append(ref)
        self._execute(f"UPDATE tasks SET {', '.join(assignments)} WHERE task_ref = %s", tuple(params))
        self._commit_unless_nested()
        return True

    def _rpc_moveTaskPosition(
        self, *, project_id: int, task_id: int, column_id: int, position: int, swimlane_id: int = 0
    ) -> bool:
        if record_key_kind(task_id) is not None:
            raise SqlCardError(
                "a Sprint, Product or Issue has no Pipeline card column to move to"
            )
        ref = self._ref_of(task_id)
        state = _STATE_BY_COLUMN_ID[int(column_id)]
        lane = self._lane_name(swimlane_id)
        self._execute(
            "UPDATE tasks SET state = %s, position = %s, updated_at = %s, date_moved = %s, "
            "extensions = CASE WHEN %s::text IS NULL THEN extensions "
            "ELSE jsonb_set(coalesce(extensions, '{}'::jsonb), '{kanboard,swimlane}', "
            "to_jsonb(%s::text), true) END "
            "WHERE task_ref = %s",
            (state, max(1, int(position)), _now(), _now(), lane, lane, ref),
        )
        self._commit_unless_nested()
        return True

    def _rpc_closeTask(self, *, task_id: int) -> bool:
        kind = record_key_kind(task_id)
        if kind == "sprint":
            # Sprint terminal state is a typed column, not archive state.
            return True
        if kind is not None:
            result = self.records.close(int(task_id))
            self._commit_unless_nested()
            return result
        ref = self._ref_of(task_id)
        self._execute(
            "UPDATE tasks SET archived = true, updated_at = %s WHERE task_ref = %s", (_now(), ref)
        )
        self._commit_unless_nested()
        return True

    # --- metadata --------------------------------------------------------------------

    def _rpc_getTaskMetadata(self, *, task_id: int) -> dict[str, str]:
        kind = record_key_kind(task_id)
        if kind == "sprint":
            return self.sprints.metadata(int(task_id))
        if kind is not None:
            return self.records.metadata(int(task_id))
        ref = self._ref_of(task_id)
        rows = self._query(
            "SELECT project_id, task_type, claim_worker, slug, base_branch, seed_ref, complexity, "
            "family_preference, head_override, review_head_override, resolved_worker_head, "
            "resolved_review_head, routing_reason, codex_launch_mode, sprint_ref, retry_same, "
            "retry_switch, quota_snapshot_at, extensions FROM tasks WHERE task_ref = %s",
            (ref,),
        )
        values = rows[0]
        names = (
            "project",
            "task_type",
            "claim",
            "slug",
            "base_branch",
            "seed_ref",
            "complexity",
            "family_preference",
            "head",
            "review_head",
            "resolved_head",
            "resolved_review_head",
            "routing_reason",
            "codex_launch_mode",
            "sprint_ref",
        )
        meta: dict[str, str] = {}
        for name, value in zip(names, values[: len(names)], strict=True):
            if value is not None and _text(value):
                meta[name] = _text(value)
        for name, value in (("retry_same", values[15]), ("retry_switch", values[16])):
            if value:
                meta[name] = str(value)
        if values[17] is not None:
            meta["quota_snapshot_at"] = _rfc3339(values[17])
        heads = [row[0] for row in self._query(
            "SELECT head FROM task_retry_heads WHERE task_ref = %s ORDER BY ordinal", (ref,)
        )]
        if heads:
            meta["retry_heads"] = ",".join(heads)
        blocked = [row[0] for row in self._query(
            "SELECT depends_on FROM task_dependencies WHERE task_ref = %s ORDER BY depends_on", (ref,)
        )]
        if blocked:
            meta["blocked_by"] = ",".join(blocked)
        supersedes = self._query(
            "SELECT supersedes FROM task_supersessions WHERE task_ref = %s", (ref,)
        )
        if supersedes:
            meta["supersedes"] = supersedes[0][0]
        issues = self._query(
            "SELECT issue_id FROM task_issues WHERE task_ref = %s ORDER BY issue_id", (ref,)
        )
        if issues:
            meta["issues"] = ",".join(f"issue:{issue_id}" for (issue_id,) in issues)
        bag = values[18] if isinstance(values[18], dict) else json.loads(values[18] or "{}")
        for key, value in (bag.get("kanboard") or {}).items():
            if key != "swimlane":
                meta[key] = _text(value)
        return meta

    def _rpc_saveTaskMetadata(self, *, task_id: int, values: dict[str, Any]) -> bool:
        kind = record_key_kind(task_id)
        if kind == "sprint":
            result = self.sprints.save_metadata(int(task_id), values)
            self._commit_unless_nested()
            return result
        if kind is not None:
            result = self.records.save_metadata(int(task_id), values)
            self._commit_unless_nested()
            return result
        ref = self._ref_of(task_id)
        assignments: list[str] = []
        params: list[Any] = []
        bag_updates: dict[str, Any] = {}
        bag_removals: list[str] = []
        for key, raw in values.items():
            text = _text(raw)
            if key in _METADATA_COLUMNS:
                column = _METADATA_COLUMNS[key]
                assignments.append(f"{column} = %s")
                params.append(text or _ENUM_DEFAULTS.get(key))
                if text:
                    bag_removals.append(key)
                else:
                    bag_updates[key] = ""
            elif key in _METADATA_COUNTERS:
                assignments.append(f"{_METADATA_COUNTERS[key]} = %s")
                params.append(int(text) if text.isdigit() else 0)
            elif key == _METADATA_TIMESTAMP[0]:
                assignments.append(f"{_METADATA_TIMESTAMP[1]} = %s")
                params.append(_timestamp(text))
                if text:
                    bag_removals.append(key)
                else:
                    bag_updates[key] = ""
            elif key in _METADATA_LINKS:
                self._write_link(ref, key, text)
                if text:
                    bag_removals.append(key)
                else:
                    bag_updates[key] = ""
            elif text:
                bag_updates[key] = text
            else:
                bag_removals.append(key)
        if bag_updates:
            assignments.append(
                "extensions = jsonb_set(coalesce(extensions, '{}'::jsonb), '{kanboard}', "
                "coalesce(extensions->'kanboard', '{}'::jsonb) || %s::jsonb, true)"
            )
            params.append(json.dumps(bag_updates))
        if assignments:
            assignments.append("updated_at = %s")
            params.append(_now())
            params.append(ref)
            self._execute(f"UPDATE tasks SET {', '.join(assignments)} WHERE task_ref = %s", tuple(params))
        for key in bag_removals:
            self._execute(
                "UPDATE tasks SET extensions = jsonb_set(coalesce(extensions, '{}'::jsonb), "
                "'{kanboard}', coalesce(extensions->'kanboard', '{}'::jsonb) - %s, true) "
                "WHERE task_ref = %s",
                (key, ref),
            )
        self._commit_unless_nested()
        return True

    def _write_link(self, ref: str, key: str, text: str) -> None:
        """The three metadata keys that are satellite rows rather than a column (§3.5)."""
        if key == "retry_heads":
            self._execute("DELETE FROM task_retry_heads WHERE task_ref = %s", (ref,))
            for ordinal, head in enumerate(part for part in text.split(",") if part.strip()):
                self._execute(
                    "INSERT INTO task_retry_heads (task_ref, ordinal, head) VALUES (%s, %s, %s)",
                    (ref, ordinal, head.strip()),
                )
        elif key == "blocked_by":
            self._execute("DELETE FROM task_dependencies WHERE task_ref = %s", (ref,))
            for value in (part.strip() for part in text.split(",") if part.strip()):
                if value == ref:
                    continue
                self._execute(
                    "INSERT INTO task_dependencies (task_ref, depends_on, depends_on_task) "
                    "SELECT %s, %s, (SELECT task_ref FROM tasks WHERE task_ref = %s)",
                    (ref, value, value),
                )
        elif key == "supersedes":
            self._execute("DELETE FROM task_supersessions WHERE task_ref = %s", (ref,))
            if text and text != ref and self._query(
                "SELECT 1 FROM tasks WHERE task_ref = %s", (text,)
            ):
                self._execute(
                    "INSERT INTO task_supersessions (task_ref, supersedes, recorded_at) "
                    "VALUES (%s, %s, %s)",
                    (ref, text, _now()),
                )
        elif key == "issues":
            self._execute("DELETE FROM task_issues WHERE task_ref = %s", (ref,))
            for value in (part.strip() for part in text.split(",") if part.strip()):
                issue_id = value.removeprefix("issue:")
                self._execute(
                    "INSERT INTO task_issues (task_ref, issue_id) "
                    "SELECT %s, issue_id FROM issues WHERE issue_id = %s",
                    (ref, issue_id),
                )

    # --- comments --------------------------------------------------------------------

    def _rpc_getAllComments(self, *, task_id: int) -> list[dict[str, Any]]:
        kind = record_key_kind(task_id)
        if kind == "sprint":
            return self.sprints.comments(int(task_id))
        if kind is not None:
            return self.records.comments(int(task_id))
        ref = self._ref_of(task_id)
        return [
            {"id": identifier, "date_creation": _epoch(created), "comment": body}
            for identifier, body, created in self._query(
                "SELECT comment_id, body, created_at FROM task_comments WHERE task_ref = %s "
                "ORDER BY created_at, comment_id",
                (ref,),
            )
        ]

    def _rpc_createComment(
        self, *, task_id: int, content: str, user_id: int = 0, created_at: Any = None
    ) -> Any:
        kind = record_key_kind(task_id)
        if kind == "sprint":
            comment_id = self.sprints.create_comment(int(task_id), content, created_at=created_at)
            self._commit_unless_nested()
            return comment_id
        if kind is not None:
            comment_id = self.records.create_comment(int(task_id), content)
            self._commit_unless_nested()
            return comment_id
        ref = self._ref_of(task_id)
        first = content.splitlines()[0] if content else ""
        marker = first[1:-1] if first.startswith("[") and first.endswith("]") else None
        rows = self._query(
            "INSERT INTO task_comments (task_ref, marker, body, created_at) "
            "VALUES (%s, %s, %s, %s) RETURNING comment_id",
            (ref, marker, content, _timestamp(created_at) if created_at else _now()),
        )
        self._commit_unless_nested()
        return int(rows[0][0])


    def _rpc_removeTask(self, *, task_id: int) -> bool:
        result = self.sprints.remove(int(task_id))
        self._commit_unless_nested()
        return result


__all__ = ["BOARD_COLUMNS", "BOARD_ID", "BOARD_NAME", "SqlCardClient", "SqlCardError"]
