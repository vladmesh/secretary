"""How a card and a sprint get the number in their reference.

Two board families are numbered rather than hashed: Pipeline cards, `<project>-<n>`, and sprints,
`sprint:<n>`. Products and issues carry a hash and are not allocated here.

Both families follow one rule: a new reference is the first number above every number the family
has already used, counted over the board's open **and** archived rows. The archived half is what
made this a defect twice. A counter that forgets what it handed out re-issues it: on 2026-08-06 a
sprint created without an explicit reference took `sprint:804`, already the reference of a sprint
closed on 2026-07-27, and `sprint show` then resolved the new sprint's reference to the old row;
on 2026-08-18 `create` derived `codegen-orchestrator-1127` from a fresh Kanboard row id and
addressed a card archived long before it.

Allocation alone cannot make a reference unique, because the rule can only count the rows the
backend actually returned. So it is not the last word: every caller writes an allocated reference
only after asking the backend whether that exact reference is claimed, and refuses loudly when it
is. That check, not a guess about how complete an enumeration looked, is what keeps two rows from
sharing one reference.
"""
from __future__ import annotations

import contextlib
import fcntl
import os
import re
from collections.abc import Callable, Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any

# Where an installation keeps its data plane. The role environment propagates it to every agent
# process, which is what lets writers in different processes name one lock.
DATA_DIR_ENV = "SECRETARY_DATA_DIR"

class BoardRowsUnavailable(RuntimeError):
    """A row enumeration answered with something that is not a list of rows."""


def board_rows(call: Callable[..., Any], project_id: int) -> list[dict[str, Any]]:
    """Every row of one board, open and archived alike.

    Kanboard 1.2 splits rows into status 1 (open) and status 0 (closed, which is where an archived
    row lands) and has no complete-set status, so both sets are read and the first copy of each task
    id is kept in case a backend returns a row in both answers.
    """
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for status_id in (1, 0):
        answer = call("getAllTasks", project_id=project_id, status_id=status_id)
        if not isinstance(answer, list):
            raise BoardRowsUnavailable("Kanboard returned an invalid task list")
        for row in answer:
            if not isinstance(row, dict):
                continue
            identifier = row.get("id")
            if identifier is not None:
                key = str(identifier)
                if key in seen:
                    continue
                seen.add(key)
            rows.append(row)
    return rows


def next_reference(rows: Iterable[Mapping[str, Any]], prefix: str) -> str:
    """The first reference of this family above every number the given rows already use."""
    pattern = re.compile(rf"{re.escape(prefix)}(\d+)$")
    used = (
        pattern.fullmatch(str(row.get("reference") or ""))
        for row in rows
    )
    return f"{prefix}{max((int(match.group(1)) for match in used if match), default=0) + 1}"


@contextlib.contextmanager
def reference_allocation_lock(data_dir: Path | str | None = None) -> Iterator[None]:
    """Serialize allocate, claim and write across every local writer of one board.

    Kanboard accepts duplicate references and offers no compare-and-swap, so the three steps are
    only atomic if one boundary covers all of them. Every local writer of the same board takes this
    one file, whichever entry point it came through: two processes that each read the high-water
    mark, each find the reference unclaimed and each create it would otherwise manufacture exactly
    the duplicate the claim check exists to prevent.

    The lock lives in the installation's data plane, so callers that know their data directory pass
    it and the rest resolve the same one from the environment.
    """
    root = Path(data_dir) if data_dir else Path(
        os.environ.get(DATA_DIR_ENV) or Path.home() / "secretary-data"
    )
    board = root / "board"
    board.mkdir(parents=True, exist_ok=True)
    with (board / ".create.lock").open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
