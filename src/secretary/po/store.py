"""PO head sessions, turns and feed in the board store (revisions `0008_po_sessions`, `0009_po_turn_request_id`).

One short connection per operation: the runner's waiter threads settle turns concurrently, and a
connection shared between them would serialize exactly what must not be serialized. Every state
change of a turn is conditional on the turn still being `running`, so a stop, a recovery and a
process exit racing each other settle a turn once, by whichever came first.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

CLIS = ("claude", "codex")
SESSION_OPEN = "open"

RUNNING = "running"
COMPLETED = "completed"
FAILED = "failed"
INTERRUPTED = "interrupted"

OWNER = "owner"
AGENT = "agent"

# The partial unique index that holds "at most one running turn per session".
ONE_RUNNING_INDEX = "po_turns_one_running_per_session"


class PoStoreError(RuntimeError):
    """The PO session store refused or could not answer."""


class SessionNotFound(PoStoreError):
    pass


class TurnInProgress(PoStoreError):
    """A turn is already running in this session; nothing was written."""


@dataclass(frozen=True)
class Session:
    session_id: str
    cli: str
    model: str
    cwd: str
    created_at: datetime
    state: str
    cli_session_id: str | None


@dataclass(frozen=True)
class Turn:
    session_id: str
    seq: int
    started_at: datetime
    finished_at: datetime | None
    state: str
    stdout_path: str
    pid: int | None
    process_identity: str | None
    reason: str | None
    # The form request id the turn was started under (revision 0009), or None.
    request_id: str | None = None


@dataclass(frozen=True)
class FeedEntry:
    entry_id: int
    session_id: str
    turn_seq: int
    role: str
    text: str
    created_at: datetime


_SESSION_COLUMNS = "session_id, cli, model, cwd, created_at, state, cli_session_id"
_TURN_COLUMNS = (
    "session_id, seq, started_at, finished_at, state, stdout_path, pid, process_identity, reason, request_id"
)
_FEED_COLUMNS = "entry_id, session_id, turn_seq, role, text, created_at"


class PoStore:
    def __init__(self, credentials: Any) -> None:
        self.credentials = credentials

    @classmethod
    def for_instance(cls, instance_dir: Path | str, *, role: str = "app") -> PoStore:
        from secretary.board.store import resolve_role

        return cls(resolve_role(instance_dir, role))

    @contextlib.contextmanager
    def _transaction(self) -> Iterator[Any]:
        """One connection and one transaction: committed on success, rolled back on any error."""
        import psycopg

        try:
            with psycopg.connect(self.credentials.conninfo()) as connection:
                yield connection
        except psycopg.errors.UniqueViolation as exc:
            if exc.diag.constraint_name == ONE_RUNNING_INDEX:
                raise TurnInProgress("a turn is already running in this session") from None
            raise PoStoreError(f"the board store refused a PO session write: {exc}") from exc
        except psycopg.Error as exc:
            raise PoStoreError(f"the board store did not answer a PO session operation: {exc}") from exc

    # --- sessions ---------------------------------------------------------------------------

    def create_session(
        self, *, session_id: str, cli: str, model: str, cwd: str, cli_session_id: str | None
    ) -> Session:
        with self._transaction() as connection:
            row = connection.execute(
                f"INSERT INTO po_sessions ({_SESSION_COLUMNS}) "
                f"VALUES (%s, %s, %s, %s, now(), %s, %s) RETURNING {_SESSION_COLUMNS}",
                (session_id, cli, model, cwd, SESSION_OPEN, cli_session_id),
            ).fetchone()
        return Session(*row)

    def session(self, session_id: str) -> Session:
        with self._transaction() as connection:
            row = connection.execute(
                f"SELECT {_SESSION_COLUMNS} FROM po_sessions WHERE session_id = %s", (session_id,)
            ).fetchone()
        if row is None:
            raise SessionNotFound(f"there is no PO session {session_id}")
        return Session(*row)

    def sessions(self) -> list[Session]:
        with self._transaction() as connection:
            rows = connection.execute(
                f"SELECT {_SESSION_COLUMNS} FROM po_sessions ORDER BY created_at, session_id"
            ).fetchall()
        return [Session(*row) for row in rows]

    def set_cli_session_id(self, session_id: str, cli_session_id: str) -> bool:
        """Record the CLI's own id once; an id already recorded is never replaced."""
        with self._transaction() as connection:
            cursor = connection.execute(
                "UPDATE po_sessions SET cli_session_id = %s WHERE session_id = %s AND cli_session_id IS NULL",
                (cli_session_id, session_id),
            )
            return cursor.rowcount == 1

    # --- turns ------------------------------------------------------------------------------

    def begin_turn(self, session_id: str, text: str, stdout_path: Callable[[int], Path]) -> Turn:
        """Atomically: the next turn as `running` and the owner's message in the feed."""
        return self.claim_turn(session_id, text, stdout_path)[0]

    def claim_turn(
        self,
        session_id: str,
        text: str,
        stdout_path: Callable[[int], Path],
        *,
        request_id: str | None = None,
    ) -> tuple[Turn, bool]:
        """`begin_turn`, or the turn `request_id` already started in this session.

        The flag is True when this call created the turn. A request id that already names a turn is
        answered with that turn in whatever state it is — running, completed, failed or interrupted —
        and nothing is written, so a repeated form never becomes a second turn. The lookup runs under
        the same session row lock as the insert, and a unique index on (session_id, request_id)
        backs it.
        """
        with self._transaction() as connection:
            found = connection.execute(
                "SELECT 1 FROM po_sessions WHERE session_id = %s FOR UPDATE", (session_id,)
            ).fetchone()
            if found is None:
                raise SessionNotFound(f"there is no PO session {session_id}")
            if request_id is not None:
                existing = connection.execute(
                    f"SELECT {_TURN_COLUMNS} FROM po_turns WHERE session_id = %s AND request_id = %s",
                    (session_id, request_id),
                ).fetchone()
                if existing is not None:
                    return Turn(*existing), False
            busy = connection.execute(
                "SELECT seq FROM po_turns WHERE session_id = %s AND state = %s",
                (session_id, RUNNING),
            ).fetchone()
            if busy is not None:
                raise TurnInProgress(
                    f"turn {busy[0]} is still running in PO session {session_id}; "
                    "wait for its answer or stop it first"
                )
            seq = connection.execute(
                "SELECT coalesce(max(seq), 0) + 1 FROM po_turns WHERE session_id = %s", (session_id,)
            ).fetchone()[0]
            row = connection.execute(
                f"INSERT INTO po_turns (session_id, seq, started_at, state, stdout_path, request_id) "
                f"VALUES (%s, %s, now(), %s, %s, %s) RETURNING {_TURN_COLUMNS}",
                (session_id, seq, RUNNING, str(stdout_path(seq)), request_id),
            ).fetchone()
            connection.execute(
                "INSERT INTO po_feed (session_id, turn_seq, role, text, created_at) "
                "VALUES (%s, %s, %s, %s, now())",
                (session_id, seq, OWNER, text),
            )
        return Turn(*row), True

    def record_process(self, session_id: str, seq: int, pid: int, identity: str | None) -> bool:
        with self._transaction() as connection:
            cursor = connection.execute(
                "UPDATE po_turns SET pid = %s, process_identity = %s "
                "WHERE session_id = %s AND seq = %s AND state = %s",
                (pid, identity, session_id, seq, RUNNING),
            )
            return cursor.rowcount == 1

    def complete_turn(self, session_id: str, seq: int, answer: str) -> bool:
        """The agent's final answer into the feed and the turn `completed`, or nothing at all."""
        with self._transaction() as connection:
            cursor = connection.execute(
                "UPDATE po_turns SET state = %s, finished_at = now() "
                "WHERE session_id = %s AND seq = %s AND state = %s",
                (COMPLETED, session_id, seq, RUNNING),
            )
            if cursor.rowcount != 1:
                return False
            connection.execute(
                "INSERT INTO po_feed (session_id, turn_seq, role, text, created_at) "
                "VALUES (%s, %s, %s, %s, now())",
                (session_id, seq, AGENT, answer),
            )
            return True

    def finish_turn(self, session_id: str, seq: int, state: str, reason: str) -> bool:
        if state not in (FAILED, INTERRUPTED):
            raise ValueError(f"a turn is finished as failed or interrupted, not {state}")
        with self._transaction() as connection:
            cursor = connection.execute(
                "UPDATE po_turns SET state = %s, finished_at = now(), reason = %s "
                "WHERE session_id = %s AND seq = %s AND state = %s",
                (state, reason, session_id, seq, RUNNING),
            )
            return cursor.rowcount == 1

    def turn(self, session_id: str, seq: int) -> Turn:
        with self._transaction() as connection:
            row = connection.execute(
                f"SELECT {_TURN_COLUMNS} FROM po_turns WHERE session_id = %s AND seq = %s",
                (session_id, seq),
            ).fetchone()
        if row is None:
            raise PoStoreError(f"there is no turn {seq} in PO session {session_id}")
        return Turn(*row)

    def turns(self, session_id: str) -> list[Turn]:
        with self._transaction() as connection:
            rows = connection.execute(
                f"SELECT {_TURN_COLUMNS} FROM po_turns WHERE session_id = %s ORDER BY seq",
                (session_id,),
            ).fetchall()
        return [Turn(*row) for row in rows]

    def running_turns(self, session_id: str | None = None) -> list[Turn]:
        query = f"SELECT {_TURN_COLUMNS} FROM po_turns WHERE state = %s"
        parameters: tuple[Any, ...] = (RUNNING,)
        if session_id is not None:
            query += " AND session_id = %s"
            parameters += (session_id,)
        with self._transaction() as connection:
            rows = connection.execute(query + " ORDER BY started_at, session_id", parameters).fetchall()
        return [Turn(*row) for row in rows]

    # --- feed -------------------------------------------------------------------------------

    def feed(self, session_id: str) -> list[FeedEntry]:
        with self._transaction() as connection:
            rows = connection.execute(
                f"SELECT {_FEED_COLUMNS} FROM po_feed WHERE session_id = %s ORDER BY entry_id",
                (session_id,),
            ).fetchall()
        return [FeedEntry(*row) for row in rows]


__all__ = [
    "AGENT",
    "CLIS",
    "COMPLETED",
    "FAILED",
    "INTERRUPTED",
    "OWNER",
    "RUNNING",
    "FeedEntry",
    "PoStore",
    "PoStoreError",
    "Session",
    "SessionNotFound",
    "Turn",
    "TurnInProgress",
]
