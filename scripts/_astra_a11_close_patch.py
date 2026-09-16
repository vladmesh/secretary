from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPRINTS = ROOT / "src/secretary/sprints.py"
SPRINT_CLOSE = ROOT / "src/secretary/sprint_close.py"
TYPED_CLOSE = ROOT / "src/secretary/board/sprint_close.py"
PYPROJECT = ROOT / "pyproject.toml"
SHARDS = ROOT / "tests/ci-shards.txt"
TEST = ROOT / "tests/test_sprint_close_model.py"


def replace_once(text: str, old: str, new: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"expected one occurrence, found {count}: {old[:120]!r}")
    return text.replace(old, new, 1)


def replace_region(text: str, start: str, end: str, replacement: str) -> str:
    first = text.find(start)
    if first < 0:
        raise RuntimeError(f"start marker not found: {start!r}")
    second = text.find(end, first + len(start))
    if second < 0:
        raise RuntimeError(f"end marker not found: {end!r}")
    return text[:first] + replacement + text[second:]


TYPED_CLOSE.write_text(
    '''"""Typed values for the Sprint close domain at the durable JSON boundary.

The close transaction and board/event stores deliberately retain their released dictionary/JSON
shape.  Close planning should not reason in those bags, though: this module parses the closed parts
of a close once into immutable values and renders them only when persistence or public output needs
the historical document.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal

from secretary.board.roles import Role

CloseSection = Literal["issues", "cards"]


def _string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValueError("expected a sequence of strings")
    if not all(isinstance(item, str) for item in value):
        raise ValueError("expected a sequence of strings")
    return tuple(value)


@dataclass(frozen=True, slots=True)
class SprintCloseIntent:
    """Replay identity of one Sprint close."""

    role: Role
    actor: str
    reference: str

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> SprintCloseIntent:
        return cls(
            role=Role(str(document.get("role") or "")),
            actor=str(document.get("actor") or ""),
            reference=str(document.get("reference") or ""),
        )

    def to_document(self) -> dict[str, str]:
        return {"role": self.role.value, "actor": self.actor, "reference": self.reference}


@dataclass(frozen=True, slots=True)
class SprintCloseDecision:
    """One explicit issue verdict or remaining-card disposition."""

    ref: str
    verdict: str
    reason: str
    actual: str | None = None

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> SprintCloseDecision:
        ref = document.get("ref")
        verdict = document.get("verdict")
        reason = document.get("reason")
        actual = document.get("actual")
        if not isinstance(ref, str) or not isinstance(verdict, str) or not isinstance(reason, str):
            raise ValueError("invalid Sprint close decision")
        if actual is not None and not isinstance(actual, str):
            raise ValueError("invalid Sprint close decision confirmation")
        return cls(ref=ref, verdict=verdict, reason=reason, actual=actual)

    def to_document(self) -> dict[str, str]:
        document = {"ref": self.ref, "verdict": self.verdict, "reason": self.reason}
        if self.actual is not None:
            document["actual"] = self.actual
        return document


@dataclass(frozen=True, slots=True)
class SprintCloseDecisions:
    """The complete explicit decisions carried by one close."""

    issues: tuple[SprintCloseDecision, ...] = ()
    cards: tuple[SprintCloseDecision, ...] = ()

    @classmethod
    def from_document(cls, value: Any) -> SprintCloseDecisions:
        if isinstance(value, cls):
            return value
        if value is None:
            return cls()
        if not isinstance(value, Mapping):
            raise ValueError("invalid Sprint close decisions")
        return cls(
            issues=cls._entries(value.get("issues")),
            cards=cls._entries(value.get("cards")),
        )

    @staticmethod
    def _entries(value: Any) -> tuple[SprintCloseDecision, ...]:
        if value is None:
            return ()
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
            raise ValueError("invalid Sprint close decision list")
        entries: list[SprintCloseDecision] = []
        for item in value:
            if not isinstance(item, Mapping):
                raise ValueError("invalid Sprint close decision list")
            entries.append(SprintCloseDecision.from_document(item))
        return tuple(entries)

    def to_document(self) -> dict[str, list[dict[str, str]]]:
        return {
            "issues": [entry.to_document() for entry in self.issues],
            "cards": [entry.to_document() for entry in self.cards],
        }


@dataclass(frozen=True, slots=True)
class SprintCloseSnapshot:
    """Sprint fields close planning consumes before the transaction is staged."""

    ref: str
    goal: str
    issues: tuple[str, ...]
    has_reservations: bool

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> SprintCloseSnapshot:
        issues = document.get("issues") or []
        if not isinstance(issues, Sequence) or isinstance(issues, (str, bytes, bytearray)):
            raise ValueError("invalid Sprint issue list")
        return cls(
            ref=str(document.get("ref") or ""),
            goal=str(document.get("goal") or ""),
            issues=tuple(str(issue) for issue in issues),
            has_reservations="reservations" in document,
        )


@dataclass(frozen=True, slots=True)
class SprintCloseTargets:
    """The task set frozen before a close performs any archival write."""

    archive: tuple[str, ...] = ()
    remaining: tuple[str, ...] = ()
    remaining_states: tuple[tuple[str, str], ...] = ()

    @classmethod
    def from_document(cls, value: Any) -> SprintCloseTargets:
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise ValueError("invalid Sprint close targets")
        states = value.get("remaining_states")
        if not isinstance(states, Mapping):
            raise ValueError("invalid Sprint close remaining states")
        return cls(
            archive=_string_tuple(value.get("archive") or ()),
            remaining=_string_tuple(value.get("remaining") or ()),
            remaining_states=tuple(sorted((str(key), str(item)) for key, item in states.items())),
        )

    @classmethod
    def from_cards(cls, cards: Sequence[Mapping[str, Any]]) -> SprintCloseTargets:
        tasks = [card for card in cards if card.get("record_type") not in {"product", "issue"}]
        archive = sorted(str(card["ref"]) for card in tasks if card.get("state") == "done")
        remaining = sorted(str(card["ref"]) for card in tasks if card.get("state") != "done")
        states = sorted(
            (str(card["ref"]), str(card.get("state") or "unknown"))
            for card in tasks
            if card.get("state") != "done"
        )
        return cls(tuple(archive), tuple(remaining), tuple(states))

    @property
    def remaining_state_map(self) -> dict[str, str]:
        return dict(self.remaining_states)

    def to_document(self) -> dict[str, Any]:
        return {
            "archive": list(self.archive),
            "remaining": list(self.remaining),
            "remaining_states": self.remaining_state_map,
        }


@dataclass(frozen=True, slots=True)
class SprintCloseConflict:
    """A recoverable collision with somebody else's close-time write."""

    section: CloseSection
    ref: str
    verdict: str
    actual: str

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> SprintCloseConflict:
        section = str(document.get("section") or "")
        if section not in {"issues", "cards"}:
            raise ValueError("invalid Sprint close conflict section")
        return cls(
            section=section,  # type: ignore[arg-type]
            ref=str(document.get("ref") or ""),
            verdict=str(document.get("verdict") or ""),
            actual=str(document.get("actual") or ""),
        )

    def to_document(self) -> dict[str, str]:
        return {
            "section": self.section,
            "ref": self.ref,
            "verdict": self.verdict,
            "actual": self.actual,
        }


@dataclass(frozen=True, slots=True)
class SprintCloseoutPlan:
    """The knowledge closeout frozen when the close transaction opens."""

    document: str
    text: str
    body_sha256: str
    written: bool = False
    commit: str = ""

    @classmethod
    def from_document(cls, value: Any) -> SprintCloseoutPlan | None:
        if value is None:
            return None
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping) or not value.get("document"):
            return None
        return cls(
            document=str(value.get("document") or ""),
            text=str(value.get("text") or ""),
            body_sha256=str(value.get("body_sha256") or ""),
            written=bool(value.get("written")),
            commit=str(value.get("commit") or ""),
        )

    def mark_written(self, commit: str) -> SprintCloseoutPlan:
        return replace(self, written=True, commit=commit)

    def to_document(self) -> dict[str, Any]:
        return {
            "document": self.document,
            "text": self.text,
            "body_sha256": self.body_sha256,
            "written": self.written,
            "commit": self.commit,
        }

    def to_result(self) -> dict[str, Any]:
        return {"document": self.document, "commit": self.commit, "written": self.written}


__all__ = [
    "CloseSection",
    "SprintCloseConflict",
    "SprintCloseDecision",
    "SprintCloseDecisions",
    "SprintCloseIntent",
    "SprintCloseSnapshot",
    "SprintCloseTargets",
    "SprintCloseoutPlan",
]
''',
    encoding="utf-8",
)


SPRINT_CLOSE.write_text(
    '''"""The decisions a sprint close carries, and the one file they arrive in."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import yaml

from secretary.board.sprint_close import SprintCloseDecision, SprintCloseDecisions
from secretary.product_issues import ISSUE_CLOSE_REASONS
from secretary.tasks import TaskError

KEEP_OPEN = "open"
ALREADY_CLOSED = "already_closed"
ALREADY_MOVED = "already_moved"
ISSUE_VERDICTS = tuple(sorted(ISSUE_CLOSE_REASONS)) + (KEEP_OPEN, ALREADY_CLOSED)
CARD_DISPOSITIONS = ("done", "drop", ALREADY_MOVED)
DISPOSITION_TARGETS = {"done": "done", "drop": "ready"}
CONFIRMABLE_CARD_STATES = tuple(sorted(set(DISPOSITION_TARGETS.values())))
CONFIRMATIONS = {
    "issue": (ALREADY_CLOSED, tuple(sorted(ISSUE_CLOSE_REASONS))),
    "card": (ALREADY_MOVED, CONFIRMABLE_CARD_STATES),
}

_SECTIONS = ("issues", "cards")
_ENTRY_FIELDS = {"ref", "verdict", "reason", "actual"}
_SHAPE = (
    "sprint close decisions file must be a mapping with the optional keys 'issues' and 'cards', "
    "each a list of {ref, verdict, reason} entries"
)


def parse_close_decisions(text: str) -> SprintCloseDecisions:
    """Read the decisions file into its normalized typed shape, or refuse it."""
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError:
        raise TaskError("validation", "sprint close decisions file is not valid YAML", 2) from None
    if document is None:
        document = {}
    if not isinstance(document, dict):
        raise TaskError("validation", _SHAPE, 2)
    _check_names(document, "sprint close decisions file")
    unknown = sorted(key for key in document if key not in _SECTIONS)
    if unknown:
        raise TaskError(
            "validation",
            "sprint close decisions file has unknown section(s): " + ", ".join(map(str, unknown)),
            2,
        )
    return SprintCloseDecisions(
        issues=_entries(document.get("issues"), "issue", ISSUE_VERDICTS),
        cards=_entries(document.get("cards"), "card", CARD_DISPOSITIONS),
    )


def _check_names(mapping: Mapping[Any, Any], what: str) -> None:
    unnamed = [key for key in mapping if not isinstance(key, str)]
    if unnamed:
        raise TaskError(
            "validation",
            f"{what} has non-string key(s): " + ", ".join(sorted(repr(key) for key in unnamed)),
            2,
        )


def _entries(raw: Any, kind: str, verdicts: tuple[str, ...]) -> tuple[SprintCloseDecision, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise TaskError("validation", _SHAPE, 2)
    seen: set[str] = set()
    entries: list[SprintCloseDecision] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise TaskError("validation", _SHAPE, 2)
        _check_names(entry, f"{kind} decision")
        extra = sorted(key for key in entry if key not in _ENTRY_FIELDS)
        if extra:
            raise TaskError(
                "validation",
                f"{kind} decision has unknown field(s): " + ", ".join(map(str, extra)),
                2,
            )
        reference = entry.get("ref")
        if not isinstance(reference, str) or not reference.strip():
            raise TaskError("validation", f"every {kind} decision needs a ref", 2)
        reference = reference.strip()
        if reference in seen:
            raise TaskError("validation", f"{kind} {reference} has more than one decision", 2)
        seen.add(reference)
        verdict = entry.get("verdict")
        if not isinstance(verdict, str) or verdict not in verdicts:
            raise TaskError(
                "validation",
                f"{kind} decision for {reference} needs a verdict, one of: " + ", ".join(verdicts),
                2,
            )
        reason = entry.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise TaskError(
                "validation",
                f"{kind} decision for {reference} requires a non-empty reason",
                2,
            )
        confirmation, facts = CONFIRMATIONS[kind]
        actual = entry.get("actual")
        if verdict == confirmation:
            if not isinstance(actual, str) or actual not in facts:
                raise TaskError(
                    "validation",
                    f"{kind} decision for {reference} confirms what somebody else did, so it must "
                    f"name it in 'actual', one of: " + ", ".join(facts),
                    2,
                )
        elif actual is not None:
            raise TaskError(
                "validation",
                f"{kind} decision for {reference} states 'actual', which only a {confirmation} "
                "decision carries",
                2,
            )
        entries.append(
            SprintCloseDecision(
                ref=reference,
                verdict=verdict,
                reason=reason.strip(),
                actual=str(actual) if verdict == confirmation else None,
            )
        )
    return tuple(entries)


def plan_close_decisions(
    decisions: SprintCloseDecisions | Mapping[str, Any] | None,
    *,
    declared_issues: Sequence[str],
    remaining: Sequence[str],
    states: Mapping[str, str],
    issue_states: Mapping[str, Mapping[str, Any]] | None = None,
) -> SprintCloseDecisions:
    """Match typed decisions against what this sprint actually declared and holds."""
    try:
        parsed = SprintCloseDecisions.from_document(decisions)
    except ValueError as exc:
        raise TaskError("validation", str(exc), 2) from None
    issues = list(parsed.issues)
    cards = list(parsed.cards)
    declared = list(declared_issues)
    unknown_issues = sorted({entry.ref for entry in issues} - set(declared))
    if unknown_issues:
        raise TaskError(
            "validation",
            "sprint close was given a decision for issue(s) the sprint did not declare: "
            + ", ".join(unknown_issues),
            2,
        )
    missing_issues = [reference for reference in declared if reference not in {entry.ref for entry in issues}]
    if missing_issues:
        raise TaskError(
            "validation",
            "sprint close needs an explicit decision for every declared issue; none was given for: "
            + ", ".join(missing_issues),
            2,
        )
    unknown_cards = sorted({entry.ref for entry in cards} - set(remaining))
    if unknown_cards:
        raise TaskError(
            "validation",
            "sprint close was given a disposition for card(s) that are not open work of this sprint: "
            + ", ".join(unknown_cards),
            2,
        )
    undisposed = [reference for reference in remaining if reference not in {entry.ref for entry in cards}]
    if undisposed:
        raise TaskError(
            "validation",
            "sprint close refuses to leave cards on a closed contract; dispose of each of them: "
            + ", ".join(f"{reference} ({states.get(reference, 'unknown')})" for reference in undisposed),
            2,
        )
    _check_issue_decisions_match_reality(issues, issue_states or {})
    _check_card_confirmations_match_reality(cards, states)
    return SprintCloseDecisions(
        issues=tuple(sorted(issues, key=lambda entry: entry.ref)),
        cards=tuple(sorted(cards, key=lambda entry: entry.ref)),
    )


def _check_issue_decisions_match_reality(
    issues: Sequence[SprintCloseDecision],
    issue_states: Mapping[str, Mapping[str, Any]],
) -> None:
    conflicting: list[str] = []
    for entry in issues:
        state = issue_states.get(entry.ref)
        if not isinstance(state, Mapping):
            continue
        closed = bool(state.get("closed"))
        carried = str(state.get("close_reason") or "")
        if entry.verdict == ALREADY_CLOSED:
            if not closed:
                raise TaskError(
                    "validation",
                    f"issue {entry.ref} is open, so there is nothing to confirm; decide it "
                    "with a closing verdict or leave it open",
                    2,
                )
            if entry.actual != carried:
                raise TaskError(
                    "validation",
                    f"issue {entry.ref} is closed as {carried or 'unknown'}, not as {entry.actual}",
                    2,
                )
        elif closed:
            conflicting.append(f"{entry.ref} ({carried or 'unknown'})")
    if conflicting:
        raise TaskError(
            "validation",
            "sprint close cannot decide issue(s) somebody else has already closed; confirm each "
            "with already_closed naming the reason it carries: " + ", ".join(sorted(conflicting)),
            2,
        )


def _check_card_confirmations_match_reality(
    cards: Sequence[SprintCloseDecision],
    states: Mapping[str, str],
) -> None:
    for entry in cards:
        if entry.verdict != ALREADY_MOVED:
            continue
        carried = states.get(entry.ref, "unknown")
        if entry.actual != carried:
            raise TaskError(
                "validation",
                f"card {entry.ref} is in {carried}, not in {entry.actual}",
                2,
            )


CLOSE_NOT_DONE = (
    "Closing a sprint states what became of its work. It is not a statement that the sprint's "
    "Definition of Done was reached: a closed sprint is not a satisfied contract, and what was and "
    "was not achieved is what the decisions and the closeout below say."
)
CLOSEOUT_DIRECTORY = "closeouts"


def closeout_path(reference: str, *, day: str) -> str:
    slug = "".join(character if character.isalnum() else "-" for character in reference).strip("-")
    return f"{CLOSEOUT_DIRECTORY}/{day}-{slug}.md"


def closeout_document(
    *,
    reference: str,
    goal: str,
    actor: str,
    reason: str,
    body: str,
    decisions: SprintCloseDecisions | Mapping[str, Any] | None,
) -> str:
    plan = SprintCloseDecisions.from_document(decisions)
    lines = [
        f"# Sprint closeout: {reference}",
        "",
        CLOSE_NOT_DONE,
        "",
        f"- Sprint: {reference}",
        f"- Goal: {goal or 'not recorded on the sprint'}",
        f"- Closed by: {actor}",
        f"- Reason for closing: {reason or 'not stated'}",
        "",
        "## What became of the work",
        "",
        body.strip(),
        "",
        "## Declared issues",
        "",
    ]
    lines.extend(_closeout_entries(plan.issues, "This sprint declared no issue."))
    lines.extend(["", "## Cards that were not done", ""])
    lines.extend(_closeout_entries(plan.cards, "No card was left in a working state at the close."))
    return "\n".join(lines).rstrip() + "\n"


def _closeout_entries(entries: Sequence[SprintCloseDecision], empty: str) -> list[str]:
    if not entries:
        return [empty]
    return [
        f"- {entry.ref} — {entry.verdict}"
        + (f" ({entry.actual})" if entry.actual else "")
        + f": {entry.reason}"
        for entry in entries
    ]
''',
    encoding="utf-8",
)


text = SPRINTS.read_text(encoding="utf-8")
text = replace_once(text, "from collections.abc import Callable\n", "from collections.abc import Callable, Mapping\n")
text = replace_once(
    text,
    "from secretary.board.sprint_admission import SprintAdmission, SprintReservationIndex\n",
    "from secretary.board.sprint_admission import SprintAdmission, SprintReservationIndex\n"
    "from secretary.board.sprint_close import (\n"
    "    SprintCloseConflict,\n"
    "    SprintCloseDecisions,\n"
    "    SprintCloseIntent,\n"
    "    SprintCloseSnapshot,\n"
    "    SprintCloseTargets,\n"
    "    SprintCloseoutPlan,\n"
    ")\n",
)

text = replace_region(
    text,
    "    @_sql_atomic\n    def close(\n",
    "\n    def _issue_store(self) -> Any:\n",
    '''    @_sql_atomic
    def close(
        self,
        *,
        role: str,
        actor: str,
        reference: str,
        decisions: SprintCloseDecisions | Mapping[str, Any] | None = None,
        request_id: str | None = None,
        reason: str = "",
        closeout: str = "",
    ) -> dict[str, Any]:
        """Close a sprint on explicit typed decisions while preserving the durable JSON contract."""
        self._role(role, {"po"})
        request_id = request_id or str(uuid.uuid4())
        self.audit.require_pending_layout()
        try:
            offered = SprintCloseDecisions.from_document(decisions) if decisions is not None else None
        except ValueError as exc:
            raise TaskError("validation", str(exc), 2) from None
        intent = SprintCloseIntent(Role(role), actor, reference)
        intent_document = intent.to_document()
        with sprint_admission_lock(self.data_dir), self.transactions.reference_lock(reference) as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                document, committed = self.transactions.existing(
                    request_id,
                    kind=SPRINT_CLOSED,
                    intent=intent_document,
                )
                if committed is not None:
                    self._check_completed_close(committed, offered, reason=reason, closeout=closeout)
                    return self._close_result(committed)
                if document is not None:
                    self._check_staged_decisions(document, offered)
                    self._check_staged_closeout(document, reason=reason, closeout=closeout)
                if document is None:
                    from secretary.sprint_close import plan_close_decisions

                    sprint_document = self.reader.show(reference, include_cards=False)
                    sprint = SprintCloseSnapshot.from_document(sprint_document)
                    targets = self._close_targets(sprint)
                    plan = plan_close_decisions(
                        offered,
                        declared_issues=sprint.issues,
                        remaining=targets.remaining,
                        states=targets.remaining_state_map,
                        issue_states=self._declared_issue_states(list(sprint.issues)),
                    )
                    self._check_close_decisions_are_writable(plan)
                    closeout_plan = self._plan_closeout(
                        sprint, plan, actor=actor, reason=reason, closeout=closeout
                    )
                    event = self._event(
                        SPRINT_CLOSED,
                        role,
                        actor,
                        reference,
                        request_id,
                        {
                            "intent": intent_document,
                            "reason": str(reason or ""),
                            "closeout": closeout_plan.to_document() if closeout_plan else None,
                            "targets": targets.to_document(),
                            "archived_tasks": [],
                            "remaining_tasks": list(targets.remaining),
                            "decisions": plan.to_document(),
                            "closed_issues": [],
                            "moved_tasks": [],
                            "disposed_tasks": [],
                            "conflicts": [],
                        },
                        sprint_document,
                    )
                    document, committed = self.transactions.begin(
                        request_id,
                        kind=SPRINT_CLOSED,
                        intent=intent_document,
                        event=event,
                    )
                    if committed is not None:
                        return self._close_result(committed)
                    if document is None:
                        raise TaskError("audit_pending", "sprint close transaction claim is unavailable", 4)
                if getattr(self.client, "backend_kind", "kanboard") == "postgres":
                    close_payload = document["event"].get("payload", {})
                    close_plan = SprintCloseDecisions.from_document(close_payload.get("decisions"))
                    closeout_plan = SprintCloseoutPlan.from_document(close_payload.get("closeout"))
                    self.client.sprints.save_close(
                        reference,
                        request_id,
                        close_plan.to_document(),
                        reason=str(close_payload.get("reason") or ""),
                        closeout_document=closeout_plan.document if closeout_plan else None,
                    )
                return self._run_close(document)
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
''',
)

text = replace_region(
    text,
    "    def _close_conflict(\n",
    "\n    def _close_targets(\n",
    '''    def _close_conflict(
        self,
        document: dict[str, Any],
        payload: dict[str, Any],
        *,
        section: str,
        reference: str,
        verdict: str,
        actual: str,
        message: str,
    ) -> None:
        """Stop on somebody else's change, recording a typed recoverable conflict."""
        try:
            conflict = SprintCloseConflict.from_document(
                {"section": section, "ref": reference, "verdict": verdict, "actual": actual}
            )
        except ValueError as exc:
            raise TaskError("audit_pending", str(exc), 4) from None
        conflicts = payload.setdefault("conflicts", [])
        if isinstance(conflicts, list):
            existing = []
            for item in conflicts:
                if isinstance(item, Mapping):
                    try:
                        existing.append(SprintCloseConflict.from_document(item))
                    except ValueError:
                        raise TaskError(
                            "audit_pending", "sprint close transaction has invalid conflicts", 4
                        ) from None
            if not any(item.ref == reference for item in existing):
                conflicts.append(conflict.to_document())
                self.transactions.save(document)
        raise TaskError("close_conflict", message, 3)
''',
)

text = replace_region(
    text,
    "    def _close_targets(\n",
    "\n    def _check_staged_decisions(\n",
    '''    def _close_targets(self, sprint: SprintCloseSnapshot) -> SprintCloseTargets:
        """Freeze this close's task set before any archival write."""
        if not sprint.has_reservations:
            return SprintCloseTargets()
        cards = TaskReader(self.client).list(sprint=sprint.ref)
        return SprintCloseTargets.from_cards(cards)
''',
)

text = replace_region(
    text,
    "    def _check_staged_decisions(\n",
    "\n    def _check_completed_close(\n",
    '''    def _check_staged_decisions(
        self,
        document: dict[str, Any],
        decisions: SprintCloseDecisions | None,
    ) -> None:
        """A retry may repeat the staged plan, or amend exactly a recorded conflict."""
        if decisions is None:
            return
        payload = (document.get("event") or {}).get("payload") or {}
        try:
            staged = SprintCloseDecisions.from_document(payload.get("decisions"))
        except ValueError:
            return
        if decisions == staged:
            return
        from secretary.sprint_close import ALREADY_CLOSED, ALREADY_MOVED

        confirmation = {"issues": ALREADY_CLOSED, "cards": ALREADY_MOVED}
        conflicts: dict[str, SprintCloseConflict] = {}
        for item in payload.get("conflicts") or []:
            if not isinstance(item, Mapping):
                continue
            try:
                conflict = SprintCloseConflict.from_document(item)
            except ValueError:
                continue
            conflicts[conflict.ref] = conflict
        amended: list[SprintCloseConflict] = []
        for section, current, offered in (
            ("issues", staged.issues, decisions.issues),
            ("cards", staged.cards, decisions.cards),
        ):
            if len(offered) != len(current):
                self._refuse_restated_decisions()
            for was, now in zip(current, offered):
                if was == now:
                    continue
                conflict = conflicts.get(now.ref)
                if (
                    conflict is None
                    or was.ref != now.ref
                    or conflict.section != section
                    or now.verdict != confirmation[section]
                    or now.actual != conflict.actual
                ):
                    self._refuse_restated_decisions()
                amended.append(conflict)
        if not amended:
            self._refuse_restated_decisions()
        payload["decisions"] = decisions.to_document()
        payload["conflicts"] = [
            item
            for item in (payload.get("conflicts") or [])
            if not (
                isinstance(item, Mapping)
                and any(
                    item.get("ref") == conflict.ref and item.get("section") == conflict.section
                    for conflict in amended
                )
            )
        ]
        self.transactions.save(document)
''',
)

text = replace_region(
    text,
    "    def _check_completed_close(\n",
    "\n    def _refuse_restated_decisions(\n",
    '''    def _check_completed_close(
        self,
        committed: dict[str, Any],
        decisions: SprintCloseDecisions | None,
        *,
        reason: str,
        closeout: str,
    ) -> None:
        """A repeat of a finished close carries the same typed plan, or is refused."""
        payload = committed.get("payload") if isinstance(committed.get("payload"), dict) else {}
        try:
            staged = SprintCloseDecisions.from_document(payload.get("decisions"))
        except ValueError as exc:
            raise TaskError("audit_pending", "committed sprint close has invalid decisions", 4) from exc
        if decisions is not None and decisions != staged:
            self._refuse_restated_decisions()
        self._check_staged_closeout({"event": committed}, reason=reason, closeout=closeout)
''',
)

text = replace_region(
    text,
    "    def _plan_closeout(\n",
    "\n    def _check_closeout_is_writable(\n",
    '''    def _plan_closeout(
        self,
        sprint: SprintCloseSnapshot,
        plan: SprintCloseDecisions,
        *,
        actor: str,
        reason: str,
        closeout: str,
    ) -> SprintCloseoutPlan | None:
        """Freeze the closeout this close will write, before the transaction opens."""
        if not str(closeout or "").strip():
            return None
        from secretary.sprint_close import closeout_document, closeout_path

        document = closeout_path(sprint.ref, day=_now()[:10])
        text = closeout_document(
            reference=sprint.ref,
            goal=sprint.goal,
            actor=actor,
            reason=str(reason or ""),
            body=str(closeout),
            decisions=plan,
        )
        self._check_closeout_is_writable(document, text, actor=actor)
        return SprintCloseoutPlan(
            document=document,
            text=text,
            body_sha256=_digest(str(closeout)),
        )
''',
)

text = replace_region(
    text,
    "    def _check_staged_closeout(\n",
    "\n    def _refuse_restated_closeout(\n",
    '''    def _check_staged_closeout(self, document: dict[str, Any], *, reason: str, closeout: str) -> None:
        """A retry of a staged close carries the same closeout body and reason."""
        payload = (document.get("event") or {}).get("payload") or {}
        if "closeout" not in payload and "reason" not in payload:
            return
        staged = SprintCloseoutPlan.from_document(payload.get("closeout"))
        body = str(closeout or "").strip()
        if str(reason or "") and str(reason) != str(payload.get("reason") or ""):
            self._refuse_restated_closeout()
        if not body:
            return
        if staged is None or staged.body_sha256 != _digest(str(closeout)):
            self._refuse_restated_closeout()
''',
)

text = replace_region(
    text,
    "    def _check_close_decisions_are_writable(\n",
    "\n    def _run_close(\n",
    '''    def _check_close_decisions_are_writable(self, plan: SprintCloseDecisions) -> None:
        """Refuse a plan this installation cannot perform, before the transaction opens."""
        from secretary.sprint_close import ALREADY_CLOSED, KEEP_OPEN

        closing = [
            entry for entry in plan.issues if entry.verdict not in {KEEP_OPEN, ALREADY_CLOSED}
        ]
        if closing and self.instance is None:
            raise TaskError(
                "validation",
                "closing an issue with the sprint needs the instance directory; pass --instance",
                2,
            )
        if not plan.cards:
            return
        writer = TaskWriter(self.client, data_dir=self.data_dir)
        live = []
        for entry in plan.cards:
            try:
                writer._check_dispatcher_archivable(entry.ref)
            except TaskError as exc:
                if exc.code != "live_work":
                    raise
                live.append(entry.ref)
        if live:
            raise TaskError(
                "live_work",
                "sprint close cannot dispose of card(s) whose dispatcher work is still live; "
                "settle them first: " + ", ".join(live),
                3,
            )
''',
)

text = replace_region(
    text,
    "    def _run_close(\n",
    "\n    def _close_declared_issues(\n",
    '''    def _run_close(self, document: dict[str, Any]) -> dict[str, Any]:
        event = document.get("event")
        if not isinstance(event, dict):
            raise TaskError("audit_pending", "sprint close transaction has no audit event", 4)
        payload = event.get("payload")
        if not isinstance(payload, dict):
            raise TaskError("audit_pending", "sprint close transaction has no payload", 4)
        try:
            targets = SprintCloseTargets.from_document(payload.get("targets"))
            decisions = SprintCloseDecisions.from_document(payload.get("decisions"))
        except ValueError as exc:
            raise TaskError("audit_pending", f"sprint close transaction is invalid: {exc}", 4) from None
        archive = list(targets.archive)
        archived = payload.setdefault("archived_tasks", [])
        if not isinstance(archived, list) or not all(isinstance(ref, str) for ref in archived):
            raise TaskError("audit_pending", "sprint close transaction has invalid archival progress", 4)
        try:
            self._close_declared_issues(document, event, payload, decisions)
            writer = TaskWriter(self.client, data_dir=self.data_dir) if archive else None
            for task_ref in archive:
                step_request_id = _close_archive_request_id(str(document["request_id"]), task_ref)
                if self._close_step_status(step_request_id) != "done":
                    assert writer is not None
                    writer.archive(
                        role="po",
                        actor=str(document["intent"]["actor"]),
                        reference=task_ref,
                        reason=f"archived when sprint {event['ref']} closed",
                        request_id=step_request_id,
                    )
                    self._require_close_step_settled(step_request_id)
                if task_ref not in archived:
                    archived.append(task_ref)
                    self.transactions.save(document)
            self._dispose_remaining_cards(document, event, payload, decisions, targets)
            self._write_closeout(document, event, payload)
            sprint = self.reader.show(str(event["ref"]), include_cards=False)
            typed_request_id = str(document["request_id"]) + ":typed-close"
            typed_pending = self.audit.pending_event(typed_request_id)
            if sprint["status"] != "closed" or (
                typed_pending is not None and typed_pending.get("record_type") == "board.protocol_event"
            ):
                document.setdefault("progress", {})["status_started"] = True
                self.transactions.save(document)
                self._transition_host(
                    role=str(document["intent"]["role"]),
                    actor=str(document["intent"]["actor"]),
                    reference=str(event["ref"]),
                    target="closed",
                    reason="Sprint closed",
                    request_id=typed_request_id,
                )
            document.setdefault("progress", {})["status_done"] = True
            self.transactions.save(document)
            self.transactions.complete(document)
        except TaskError as exc:
            if getattr(self.client, "backend_kind", "kanboard") == "postgres":
                raise
            if exc.code == "close_conflict":
                raise
            if exc.code in {
                "validation",
                "closed",
                "not_found",
                "transition_forbidden",
                "live_work",
                "role_forbidden",
            } and not _close_progressed(document, payload):
                self.transactions.discard(document)
                raise
            raise TaskError(
                "audit_pending", "sprint close is pending repair; retry with the same request id", 4
            ) from None
        except (OSError, KeyError, TypeError, ValueError):
            raise TaskError(
                "audit_pending", "sprint close is pending repair; retry with the same request id", 4
            ) from None
        update_active_sprint_projects(self.data_dir, self.reader.show(str(event["ref"]), include_cards=False))
        return self._close_result(event)
''',
)

text = replace_region(
    text,
    "    def _close_declared_issues(\n",
    "\n    def _dispose_remaining_cards(\n",
    '''    def _close_declared_issues(
        self,
        document: dict[str, Any],
        event: dict[str, Any],
        payload: dict[str, Any],
        decisions: SprintCloseDecisions,
    ) -> None:
        """Perform the typed closing verdicts, one issue at a time."""
        from secretary.sprint_close import ALREADY_CLOSED, KEEP_OPEN

        closed = payload.setdefault("closed_issues", [])
        if not isinstance(closed, list):
            raise TaskError("audit_pending", "sprint close transaction has invalid issue progress", 4)
        pending = [
            entry
            for entry in decisions.issues
            if entry.verdict not in {KEEP_OPEN, ALREADY_CLOSED} and entry.ref not in closed
        ]
        if not pending:
            return
        if self.instance is None:
            raise TaskError(
                "validation",
                "closing an issue with the sprint needs the instance directory; pass --instance",
                2,
            )
        store = self._issue_store()
        document.setdefault("progress", {})["issues_started"] = True
        self.transactions.save(document)
        for entry in pending:
            reference = entry.ref
            step_request_id = _close_step_request_id(str(document["request_id"]), "issue", reference)
            status = self._close_step_status(step_request_id)
            if status != "done":
                current = store.show_issue(reference)
                if status == "todo" and current.get("closed"):
                    carried = str(current.get("close_reason") or "unknown")
                    self._close_conflict(
                        document,
                        payload,
                        section="issues",
                        reference=reference,
                        verdict=entry.verdict,
                        actual=carried,
                        message=(
                            f"issue {reference} was closed as {carried} by somebody else, and this "
                            f"close states {entry.verdict}; retry with that decision amended to "
                            f"already_closed naming {carried}"
                        ),
                    )
                store.close_issue(
                    reference=reference,
                    reason=entry.verdict,
                    actor=str(document["intent"]["actor"]),
                    request_id=step_request_id,
                )
                self._require_close_step_settled(step_request_id)
            closed.append(reference)
            self.transactions.save(document)
''',
)

text = replace_region(
    text,
    "    def _dispose_remaining_cards(\n",
    "\n    def _write_closeout(\n",
    '''    def _dispose_remaining_cards(
        self,
        document: dict[str, Any],
        event: dict[str, Any],
        payload: dict[str, Any],
        decisions: SprintCloseDecisions,
        targets: SprintCloseTargets,
    ) -> None:
        """Take every remaining card into the recorded end its typed disposition names."""
        from secretary.sprint_close import ALREADY_MOVED, DISPOSITION_TARGETS

        if not decisions.cards:
            return
        moved = payload.setdefault("moved_tasks", [])
        disposed = payload.setdefault("disposed_tasks", [])
        if not isinstance(moved, list) or not isinstance(disposed, list):
            raise TaskError("audit_pending", "sprint close transaction has invalid disposition progress", 4)
        planned = targets.remaining_state_map
        writer = TaskWriter(self.client, data_dir=self.data_dir)
        reader = TaskReader(self.client)
        actor = str(document["intent"]["actor"])
        for entry in decisions.cards:
            reference = entry.ref
            verdict = entry.verdict
            reason = entry.reason
            target = "" if verdict == ALREADY_MOVED else DISPOSITION_TARGETS[verdict]
            if target and str(planned.get(reference) or "") != target:
                move_request_id = _close_step_request_id(
                    str(document["request_id"]), "dispose-move", reference
                )
                status = self._close_step_status(move_request_id)
                if status != "done":
                    if status == "todo" and reader.show(reference)["state"] == target:
                        self._close_conflict(
                            document,
                            payload,
                            section="cards",
                            reference=reference,
                            verdict=verdict,
                            actual=target,
                            message=(
                                f"card {reference} was moved to {target} by somebody else, not by "
                                f"this close; retry with that disposition amended to already_moved "
                                f"naming {target}"
                            ),
                        )
                    writer.move(
                        role="po",
                        actor=actor,
                        reference=reference,
                        target=target,
                        reason=f"{verdict} when sprint {event['ref']} closed: {reason}",
                        sprint_override=True,
                        sprint_override_reason=f"disposed by the close of {event['ref']}: {reason}",
                        request_id=move_request_id,
                    )
                    self._require_close_step_settled(move_request_id)
            if reference not in moved:
                moved.append(reference)
                self.transactions.save(document)
            archive_request_id = _close_step_request_id(
                str(document["request_id"]), "dispose-archive", reference
            )
            if self._close_step_status(archive_request_id) != "done":
                writer.archive(
                    role="po",
                    actor=actor,
                    reference=reference,
                    reason=f"archived when sprint {event['ref']} closed: {reason}",
                    request_id=archive_request_id,
                )
                self._require_close_step_settled(archive_request_id)
            if reference not in disposed:
                disposed.append(reference)
                self.transactions.save(document)
''',
)

text = replace_region(
    text,
    "    def _write_closeout(\n",
    "\n    def _close_result(\n",
    '''    def _write_closeout(
        self, document: dict[str, Any], event: dict[str, Any], payload: dict[str, Any]
    ) -> None:
        """Write this close's typed knowledge-closeout plan exactly once."""
        plan = SprintCloseoutPlan.from_document(payload.get("closeout"))
        if plan is None:
            return
        reference = str(event["ref"])
        step_request_id = _close_step_request_id(str(document["request_id"]), "closeout", reference)
        if self._close_step_status(step_request_id) == "done":
            return
        from secretary.knowledge_write import KnowledgeError, write_knowledge_document

        actor = str(document["intent"]["actor"])
        self._check_closeout_is_writable(plan.document, plan.text, actor=actor)
        document.setdefault("progress", {})["closeout_started"] = True
        self.transactions.save(document)
        step = self._event(
            SPRINT_CLOSEOUT,
            str(document["intent"]["role"]),
            actor,
            reference,
            step_request_id,
            {"close_request_id": str(document["request_id"]), "document": plan.document, "commit": ""},
            self.reader.show(reference, include_cards=False),
        )
        self.audit.stage(step_request_id, step)
        try:
            written = write_knowledge_document(
                self._instance_dir(), document=plan.document, actor=actor, text=plan.text
            )
        except KnowledgeError as exc:
            raise TaskError("backend_error", f"sprint close could not write its closeout: {exc}", 1) from None
        step["payload"]["commit"] = written.commit
        step["payload"]["changed"] = bool(written.changed)
        self.audit.stage(step_request_id, step)
        self.audit.append(step_request_id, step)
        self._require_close_step_settled(step_request_id)
        payload["closeout"] = plan.mark_written(written.commit).to_document()
        self.transactions.save(document)
''',
)

text = replace_region(
    text,
    "    def _close_result(\n",
    "\n    @_sql_atomic\n    def reopen(\n",
    '''    def _close_result(self, event: dict[str, Any]) -> dict[str, Any]:
        from secretary.sprint_close import CLOSE_NOT_DONE

        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        try:
            decisions = SprintCloseDecisions.from_document(payload.get("decisions"))
        except ValueError as exc:
            raise TaskError("audit_pending", "sprint close has invalid decisions", 4) from exc
        closeout_plan = SprintCloseoutPlan.from_document(payload.get("closeout"))
        return {
            "action": SPRINT_CLOSED,
            "sprint": self.reader.show(str(event["ref"])),
            "event_id": str(event["event_id"]),
            "archived_tasks": list(payload.get("archived_tasks") or []),
            "remaining_tasks": list(payload.get("remaining_tasks") or []),
            "issue_decisions": [entry.to_document() for entry in decisions.issues],
            "closed_issues": list(payload.get("closed_issues") or []),
            "card_dispositions": [entry.to_document() for entry in decisions.cards],
            "disposed_tasks": list(payload.get("disposed_tasks") or []),
            "reason": str(payload.get("reason") or ""),
            "closeout": closeout_plan.to_result() if closeout_plan else None,
            "definition_of_done": {"satisfied": False, "reason": CLOSE_NOT_DONE},
        }
''',
)

text = replace_region(
    text,
    "def _closeout_result(plan: Any) -> dict[str, Any] | None:\n",
    "\n\ndef _close_step_request_id(",
    '''def _closeout_result(plan: Any) -> dict[str, Any] | None:
    """Released compatibility helper backed by the typed closeout boundary."""
    typed = SprintCloseoutPlan.from_document(plan)
    return typed.to_result() if typed is not None else None
''',
)

SPRINTS.write_text(text, encoding="utf-8")


pyproject = PYPROJECT.read_text(encoding="utf-8")
pyproject = replace_once(
    pyproject,
    '    "src/secretary/board/sprint_admission.py",\n    "src/secretary/board/sprint_write.py",\n',
    '    "src/secretary/board/sprint_admission.py",\n'
    '    "src/secretary/board/sprint_write.py",\n'
    '    "src/secretary/board/sprint_close.py",\n',
)
PYPROJECT.write_text(pyproject, encoding="utf-8")


TEST.write_text(
    '''from __future__ import annotations

import unittest

from secretary.board.roles import Role
from secretary.board.sprint_close import (
    SprintCloseConflict,
    SprintCloseDecision,
    SprintCloseDecisions,
    SprintCloseIntent,
    SprintCloseSnapshot,
    SprintCloseTargets,
    SprintCloseoutPlan,
)
from secretary.sprint_close import parse_close_decisions, plan_close_decisions


class SprintCloseModelTests(unittest.TestCase):
    def test_decisions_round_trip_released_document(self) -> None:
        document = {
            "issues": [{"ref": "issue:7", "verdict": "open", "reason": "carry it"}],
            "cards": [
                {"ref": "task:9", "verdict": "already_moved", "reason": "raced", "actual": "ready"}
            ],
        }
        typed = SprintCloseDecisions.from_document(document)
        self.assertEqual(typed.to_document(), document)
        self.assertEqual(typed.issues[0].ref, "issue:7")
        self.assertEqual(typed.cards[0].actual, "ready")

    def test_intent_targets_conflict_and_closeout_round_trip(self) -> None:
        intent = SprintCloseIntent(Role.PO, "owner", "sprint:5")
        self.assertEqual(
            SprintCloseIntent.from_document(intent.to_document()),
            intent,
        )
        targets = SprintCloseTargets(
            archive=("task:1",),
            remaining=("task:2",),
            remaining_states=(("task:2", "doing"),),
        )
        self.assertEqual(SprintCloseTargets.from_document(targets.to_document()), targets)
        conflict = SprintCloseConflict("cards", "task:2", "drop", "ready")
        self.assertEqual(SprintCloseConflict.from_document(conflict.to_document()), conflict)
        closeout = SprintCloseoutPlan("closeouts/x.md", "body", "sha")
        self.assertEqual(
            SprintCloseoutPlan.from_document(closeout.to_document()),
            closeout,
        )
        self.assertEqual(closeout.mark_written("abc").to_result()["commit"], "abc")

    def test_snapshot_preserves_legacy_absence_of_reservations(self) -> None:
        old = SprintCloseSnapshot.from_document({"ref": "sprint:1", "goal": "g", "issues": []})
        current = SprintCloseSnapshot.from_document(
            {"ref": "sprint:2", "goal": "g", "issues": [], "reservations": []}
        )
        self.assertFalse(old.has_reservations)
        self.assertTrue(current.has_reservations)

    def test_parser_and_planner_return_typed_values(self) -> None:
        parsed = parse_close_decisions(
            "issues:\n  - ref: issue:1\n    verdict: open\n    reason: later\n"
            "cards:\n  - ref: task:2\n    verdict: drop\n    reason: descoped\n"
        )
        self.assertIsInstance(parsed, SprintCloseDecisions)
        planned = plan_close_decisions(
            parsed,
            declared_issues=["issue:1"],
            remaining=["task:2"],
            states={"task:2": "doing"},
        )
        self.assertIsInstance(planned, SprintCloseDecisions)
        self.assertEqual(planned.cards, (SprintCloseDecision("task:2", "drop", "descoped"),))


if __name__ == "__main__":
    unittest.main()
''',
    encoding="utf-8",
)

shards = SHARDS.read_text(encoding="utf-8")
needle = "unit tests/test_sprint_admission.py\n"
if "tests/test_sprint_close_model.py" not in shards:
    shards = replace_once(shards, needle, needle + "unit tests/test_sprint_close_model.py\n")
SHARDS.write_text(shards, encoding="utf-8")
