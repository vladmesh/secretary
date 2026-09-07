"""The three read operations every transport of this installation answers from.

`system_snapshot`, `task_snapshot` and `task_events` are the whole surface. A dashboard is one
system snapshot; a card page is one task snapshot plus a cursor it polls; a Telegram head asking
"what is running" is the same system snapshot rendered as a message. There is no fourth operation
here for a future need, and no operation that writes anything at all.

Everything below is assembled from sources that already exist and already own their meaning --
`collect_status` for installation health, the validated project bindings for the registry,
`TaskReader` for cards, the board audit journal for history, the dispatcher's durable production
state plus the launch heartbeats for agents. Nothing here is a second collector of a fact somebody
already collects, and nothing here writes: no dispatcher tick, no repair, no board mutation, not
even a cache file.

The sources fail independently, so each section of a snapshot carries its own availability record
(:mod:`secretary.webproto.sources`) instead of the whole read failing. A dead Kanboard must not
blank out the agent list, and an unreadable dispatcher state must not hide the cards.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from secretary.board.backend import CARD, board_client
from secretary.config import InstanceReport, validate_instance
from secretary.dispatcher_state import DispatcherRecord
from secretary.dispatcher_types import HostError
from secretary.status import collect_status
from secretary.tasks import TaskError, TaskReader
from secretary.webproto import agents as agent_reads
from secretary.webproto import sources
from secretary.webproto.boundary import ProtocolBoundary
from secretary.webproto.cursor import Cursor, decode
from secretary.webproto.errors import InstallationUnavailable, TaskNotFound
from secretary.webproto.journal import DEFAULT_LIMIT, EventJournal, EventPage

SCHEMA_VERSION = 1

#: The states a card is "current" in: everything the pipeline is carrying right now. `issues` is
#: the backlog and `done` is history, and a dashboard that mixed either into "current tasks" would
#: report a thousand-card board as a thousand cards in flight.
CURRENT_TASK_STATES = ("ready", "in_progress", "validate", "assessment", "blocked")

#: How many of a card's most recent events its task snapshot opens with.
TASK_SNAPSHOT_EVENTS = 20

#: Failures a source read may answer with instead of a value. They are caught per section, never
#: around the whole snapshot: a section that fails records why, and the rest of the read continues.
_SOURCE_FAILURES = (TaskError, HostError, OSError, ValueError, KeyError, TypeError, AssertionError)


class ReadLayer(ProtocolBoundary):
    """One installation, read three ways, with no knowledge of who is asking.

    Construction is cheap and does no I/O: every operation reads what it needs when it is called,
    so a long-lived transport holding one of these never serves a value it cached at start-up.

    ``board_client`` and ``status_reader`` exist so a test -- or a transport with its own
    connection policy -- can supply those two sources directly. Neither is a mode: the same code
    path runs with the live client as with a fake one.
    """

    def __init__(
        self,
        instance: str | Path,
        *,
        data_dir: str | Path | None = None,
        board_client: Any | None = None,
        status_reader: Callable[[], dict[str, Any]] | None = None,
        offline: bool = False,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.instance = Path(instance)
        self._data_dir = Path(data_dir) if data_dir is not None else None
        self._board_client = board_client
        self._status_reader = status_reader
        self.offline = offline
        self._clock = clock

    # -- shared plumbing -------------------------------------------------------------------

    def report(self) -> InstanceReport:
        """The validated instance, or a refusal naming what does not validate.

        An instance whose config is invalid is not a source outage that a snapshot can report
        around: without it there is no data directory to read anything else from, and a snapshot
        built on a guess would be a fabrication.
        """
        report = validate_instance(self.instance)
        if not report.ok or report.data_dir is None:
            raise InstallationUnavailable(
                "this instance config does not validate: "
                + "; ".join(str(error) for error in report.errors[:5])
            )
        return report

    def data_dir(self, report: InstanceReport | None = None) -> Path:
        """The data plane this installation reads from, overridden explicitly or taken from config.

        Every operation resolves the config exactly once and threads the result through its
        sections, so one snapshot cannot be assembled half from a config read before an edit and
        half from one read after it.
        """
        if self._data_dir is not None:
            return self._data_dir
        report = report if report is not None else self.report()
        assert report.data_dir is not None
        return report.data_dir

    def _client(self) -> Any:
        """The board client of this installation: an injected one, or the switch's (§2.2)."""
        return self._board_client or board_client(
            self.instance.parent if self.instance.is_file() else self.instance, serves=(CARD,)
        )

    def _production_path(self, data_dir: Path) -> Path:
        return data_dir / "dispatcher" / "production-state.json"

    # -- operations ------------------------------------------------------------------------

    def system_snapshot(self) -> dict[str, Any]:
        """Installation health, registered projects, current cards and the agents running now."""
        now = self._clock()
        report = self.report()
        data_dir = self.data_dir(report)
        health = self._health(report, data_dir, now=now)
        projects = self._projects(report, now=now)
        tasks = self._tasks(data_dir, now=now)
        agents = self._agents(data_dir, now=now, projects_by_ref=_projects_by_ref(tasks["items"]))
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": "system",
            "observed_at": sources.isoformat(now),
            "installation": {
                "instance": str(report.instance_path),
                "name": report.name or None,
                "data_dir": str(data_dir),
                "health": health,
            },
            "projects": projects,
            "tasks": tasks,
            "agents": agents,
        }

    def task_snapshot(self, ref: str, *, events: int = TASK_SNAPSHOT_EVENTS) -> dict[str, Any]:
        """One card: its state, its project, its recent history, its heads and its result."""
        now = self._clock()
        reference = str(ref or "")
        if not reference:
            raise TaskNotFound("a task reference is required")
        report = self.report()
        data_dir = self.data_dir(report)
        card, card_source = self._card(reference, data_dir, now=now)
        page = EventJournal(data_dir).tail(reference, limit=events, now=now)
        attempt, record, attempt_source = self._attempt(reference, data_dir, now=now)
        rows = agent_reads.agent_rows(record, reference) if record is not None else []
        project = _text(card.get("project")) if card else None
        for row in rows:
            row["project"] = project
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": "task",
            "observed_at": sources.isoformat(now),
            "ref": reference,
            "card": {"source": card_source.to_json(), "value": _card_value(card)},
            "project": self._project_of(report, project),
            "attempt": {"source": attempt_source.to_json(), "value": attempt},
            "agents": {"source": attempt_source.to_json(), "items": rows},
            "work": _work(card),
            "events": {
                "source": page.source.to_json(),
                "items": list(page.items),
                "next_cursor": page.next_cursor.encode(),
            },
        }

    def task_events(
        self, ref: str, cursor: str | None = None, *, limit: int = DEFAULT_LIMIT
    ) -> dict[str, Any]:
        """One page of a card's history and the cursor that continues it.

        The cursor is the whole contract: read with the ``next_cursor`` of a page and you get what
        was appended after it, exactly once; read the same cursor twice and you get the same page.
        Both hold because the position is a place in an append-only journal rather than a time or a
        recomputed index -- see :mod:`secretary.webproto.cursor`.
        """
        now = self._clock()
        reference = str(ref or "")
        if not reference:
            raise TaskNotFound("a task reference is required")
        position: Cursor | None = None if cursor in (None, "") else decode(str(cursor), ref=reference)
        page = EventJournal(self.data_dir()).page(reference, cursor=position, limit=limit, now=now)
        return _events_document(reference, page, now=now, cursor=cursor)

    # -- sections --------------------------------------------------------------------------

    def _health(self, report: InstanceReport, data_dir: Path, *, now: float) -> dict[str, Any]:
        """Installation health, straight from `secretary status`'s own collector.

        Deliberately not a second health model. `collect_status` already owns what "healthy" means
        for units, heads, sprints, checkpoint lag and host resources, and a dashboard that answered
        that question differently from `secretary status --json` would make an operator debug the
        difference between two answers instead of the installation.
        """
        try:
            status = self._read_status(report)
        except _SOURCE_FAILURES as exc:
            return {
                "source": sources.unavailable(
                    f"installation health could not be collected: {exc}",
                    now=now,
                    evidence=self._production_path(data_dir),
                ).to_json(),
                "status": None,
            }
        return {"source": sources.available(now).to_json(), "status": status}

    def _read_status(self, report: InstanceReport) -> dict[str, Any]:
        if self._status_reader is not None:
            return self._status_reader()
        return collect_status(report, offline=self.offline, sprint_client=self._board_client)

    def _projects(self, report: InstanceReport, *, now: float) -> dict[str, Any]:
        """The registered projects, from the bindings the instance already validates."""
        items = [
            {
                "id": _text(binding.get("id")),
                "repo": _text(binding.get("repo")) or None,
                "remote": _text(binding.get("remote")) or None,
                "adapter": _text(binding.get("adapter")) or None,
                "default_branch": _text(binding.get("default_branch")) or None,
                "plane": _text(binding.get("plane")) or None,
                "enabled": bool(binding.get("enabled", True)),
            }
            for binding in report.bindings
            if isinstance(binding, dict) and _text(binding.get("id"))
        ]
        return {
            "source": sources.available(now).to_json(),
            "items": sorted(items, key=lambda project: project["id"]),
        }

    def _project_of(self, report: InstanceReport, project: str | None) -> dict[str, Any]:
        """A card's project, and whether this installation has it registered."""
        if not project:
            return {"id": None, "registered": False, "binding": None}
        for binding in report.bindings:
            if isinstance(binding, dict) and _text(binding.get("id")) == project:
                return {
                    "id": project,
                    "registered": True,
                    "binding": {
                        "repo": _text(binding.get("repo")) or None,
                        "adapter": _text(binding.get("adapter")) or None,
                        "default_branch": _text(binding.get("default_branch")) or None,
                        "enabled": bool(binding.get("enabled", True)),
                    },
                }
        return {"id": project, "registered": False, "binding": None}

    def _tasks(self, data_dir: Path, *, now: float) -> dict[str, Any]:
        """The cards the pipeline is carrying, as `secretary task list` reads them."""
        try:
            rows = TaskReader(self._client()).list(states=set(CURRENT_TASK_STATES))
        except _SOURCE_FAILURES as exc:
            return {
                "source": sources.unavailable(
                    f"the board could not be read: {_reason(exc)}",
                    now=now,
                    evidence=data_dir / "board" / "cards.ndjson",
                ).to_json(),
                "items": [],
            }
        return {"source": sources.available(now).to_json(), "items": [_task_row(row) for row in rows]}

    def _records(self, data_dir: Path) -> dict[str, DispatcherRecord]:
        payload = json.loads(self._production_path(data_dir).read_text(encoding="utf-8"))
        records = payload.get("records") if isinstance(payload, dict) else None
        if not isinstance(records, dict):
            # Reported as an unavailable source, like every other unreadable input here: a state
            # file whose shape this reader does not recognise proves nothing about the agents.
            raise TypeError("the dispatcher production state carries no records object")
        return {
            reference: DispatcherRecord.from_json(record)
            for reference, record in sorted(records.items())
            if isinstance(reference, str) and isinstance(record, dict)
        }

    def _agents(self, data_dir: Path, *, now: float, projects_by_ref: dict[str, str]) -> dict[str, Any]:
        """Every head the dispatcher holds, with its liveness taken from process state.

        An unreadable or absent production state is reported as an unavailable source rather than
        as an empty list: "the dispatcher is running nothing" and "nobody could tell me what the
        dispatcher is running" are opposite answers for an operator.
        """
        try:
            records = self._records(data_dir)
        except _SOURCE_FAILURES as exc:
            return {
                "source": sources.unavailable(
                    f"the dispatcher production state could not be read: {exc}",
                    now=now,
                    evidence=self._production_path(data_dir),
                ).to_json(),
                "items": [],
            }
        items: list[dict[str, Any]] = []
        for reference, record in records.items():
            for row in agent_reads.agent_rows(record, reference):
                row["project"] = projects_by_ref.get(reference)
                items.append(row)
        return {"source": sources.available(now).to_json(), "items": items}

    def _card(self, ref: str, data_dir: Path, *, now: float) -> tuple[dict[str, Any] | None, sources.Source]:
        try:
            return TaskReader(self._client()).show(ref), sources.available(now)
        except TaskError as exc:
            if exc.code == "not_found":
                raise TaskNotFound(f"the board holds no card {ref!r}") from None
            return None, sources.unavailable(
                f"the board could not be read: {exc.message}",
                now=now,
                evidence=data_dir / "board" / "cards.ndjson",
            )
        except _SOURCE_FAILURES as exc:
            return None, sources.unavailable(
                f"the board could not be read: {_reason(exc)}",
                now=now,
                evidence=data_dir / "board" / "cards.ndjson",
            )

    def _attempt(
        self, ref: str, data_dir: Path, *, now: float
    ) -> tuple[dict[str, Any] | None, DispatcherRecord | None, sources.Source]:
        """What the dispatcher durably holds for this card, if it holds anything."""
        try:
            record = self._records(data_dir).get(ref)
        except _SOURCE_FAILURES as exc:
            return (
                None,
                None,
                sources.unavailable(
                    f"the dispatcher production state could not be read: {exc}",
                    now=now,
                    evidence=self._production_path(data_dir),
                ),
            )
        if record is None:
            return None, None, sources.available(now)
        return (
            {
                "state": record.state or None,
                "attempt_id": record.attempt_id or None,
                "attempt_round": record.attempt_round,
                "report_generation": record.report_generation,
                "gate_state": record.gate_state or None,
                "workspace": record.workspace or None,
                "head": record.head or None,
                "review_head": record.review_head or None,
                "paused": {
                    "worker": record.paused_worker_at > 0,
                    "reviewer": record.paused_reviewer_at > 0,
                },
            },
            record,
            sources.available(now),
        )


def _events_document(ref: str, page: EventPage, *, now: float, cursor: str | None) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "task_events",
        "observed_at": sources.isoformat(now),
        "ref": ref,
        "source": page.source.to_json(),
        "cursor": cursor or None,
        "next_cursor": page.next_cursor.encode(),
        "has_more": page.has_more,
        "items": list(page.items),
    }


def _projects_by_ref(rows: Iterable[dict[str, Any]]) -> dict[str, str]:
    return {row["ref"]: row["project"] for row in rows if row.get("ref") and row.get("project")}


def _task_row(row: dict[str, Any]) -> dict[str, Any]:
    claim = row.get("claim") if isinstance(row.get("claim"), dict) else {}
    audit = row.get("audit") if isinstance(row.get("audit"), dict) else {}
    return {
        "ref": _text(row.get("ref")),
        "title": _text(row.get("title")),
        "state": _text(row.get("state")),
        "project": _text(row.get("project")) or None,
        "sprint": row.get("sprint"),
        "type": _text(row.get("type")) or None,
        "blocked_by": row.get("blocked_by"),
        "claimed_by": claim.get("worker"),
        "created_at": audit.get("created_at"),
        "updated_at": audit.get("updated_at"),
    }


def _card_value(card: dict[str, Any] | None) -> dict[str, Any] | None:
    if card is None:
        return None
    value = _task_row(card)
    value["description"] = _text(card.get("description"))
    value["closed"] = bool(card.get("closed"))
    value["workspace"] = card.get("workspace")
    value["routing"] = card.get("routing")
    return value


def _work(card: dict[str, Any] | None) -> dict[str, Any]:
    """The worker's report, the reviewer's verdict, the observer's decision, and the result.

    All four come from the card's own marker comments, which are the protocol's public record of
    those events (`secretary.board.events.render_marker_comment`) -- not from a transcript, a pane
    or a log file. A card with no marker of a kind has null there, which is different from an empty
    body: the round has not produced that answer yet.
    """
    empty = {"worker_report": None, "review_verdict": None, "decision": None, "outcome": None}
    if card is None:
        return empty
    comments = card.get("comments")
    if not isinstance(comments, list):
        return empty
    found: dict[str, dict[str, Any]] = {}
    latest: dict[str, Any] | None = None
    for comment in comments:
        if not isinstance(comment, dict):
            continue
        marker = comment.get("marker")
        if not isinstance(marker, str) or ":" not in marker:
            continue
        family, _, value = marker.partition(":")
        slot = {"report": "worker_report", "review": "review_verdict", "decision": "decision"}.get(family)
        if slot is None:
            continue
        entry = {
            "marker": marker,
            "value": value,
            "at": comment.get("created_at"),
            "body": _text(comment.get("body")),
            "classification": _classification(_text(comment.get("body"))),
        }
        found[slot] = entry
        latest = {"kind": family, "value": value, "at": entry["at"]}
    result = dict(empty)
    result.update(found)
    # The result of the work is the last answer the card has, whatever kind it is: a decision on an
    # assessed card, a verdict on one in review, a report on one still being validated. `terminal`
    # is the only thing that says the card is finished, and it comes from the card's state.
    if latest is not None:
        latest["terminal"] = _text(card.get("state")) == "done"
        result["outcome"] = latest
    return result


def _classification(body: str) -> str | None:
    """The blocked classification a report marker carries on its own line, when it carries one."""
    for line in body.splitlines():
        if line.startswith("classification:"):
            return line.partition(":")[2].strip() or None
    return None


def _reason(exc: Exception) -> str:
    return getattr(exc, "message", None) or str(exc) or type(exc).__name__


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""
