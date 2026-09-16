"""Typed values for the Sprint close domain at the durable JSON boundary.

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


class SprintCloseDocumentError(ValueError):
    """A durable Sprint-close document does not match its closed domain shape."""


def _string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise SprintCloseDocumentError("expected a sequence of strings")
    if not all(isinstance(item, str) for item in value):
        raise SprintCloseDocumentError("expected a sequence of strings")
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
            raise SprintCloseDocumentError("invalid Sprint close decision")
        if actual is not None and not isinstance(actual, str):
            raise SprintCloseDocumentError("invalid Sprint close decision confirmation")
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
            raise SprintCloseDocumentError("invalid Sprint close decisions")
        return cls(
            issues=cls._entries(value.get("issues")),
            cards=cls._entries(value.get("cards")),
        )

    @staticmethod
    def _entries(value: Any) -> tuple[SprintCloseDecision, ...]:
        if value is None:
            return ()
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
            raise SprintCloseDocumentError("invalid Sprint close decision list")
        entries: list[SprintCloseDecision] = []
        for item in value:
            if not isinstance(item, Mapping):
                raise SprintCloseDocumentError("invalid Sprint close decision list")
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
            raise SprintCloseDocumentError("invalid Sprint issue list")
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
            raise SprintCloseDocumentError("invalid Sprint close targets")
        states = value.get("remaining_states")
        if not isinstance(states, Mapping):
            raise SprintCloseDocumentError("invalid Sprint close remaining states")
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
            raise SprintCloseDocumentError("invalid Sprint close conflict section")
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
