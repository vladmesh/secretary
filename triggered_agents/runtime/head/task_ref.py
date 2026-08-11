"""`TaskRef`: the durable document a head was pointed at, whatever kind of work that document is.

A head is always started *at* something, and until now that something was a Pipeline card: the
dispatcher's whole bring-up path threads a `task` dict with a `ref` and a project through it, and
the operations that own a head's life would have inherited that assumption by taking the same dict.
They must not. The observer head of a sprint is pointed at a sprint entity, a steward or a curator
runs off a standing instruction for its role, and neither has a card — nor should one be invented so
that a signature can keep asking for one.

So what an operation takes is a pointer with a kind on it. Three kinds exist because three kinds of
durable document exist in this product today, and a fourth is a value added here rather than a new
parameter everywhere:

  * `card` — one Pipeline card, named by its reference (`secretary-1412`);
  * `sprint` — a sprint entity on the board, which outlives every head that ever worked in it;
  * `standing` — a role's standing instruction, the document a head that is not serving one unit of
    work at all is nonetheless carrying out.

The pointer is a pointer and not the document: `document` is where the text lives on disk when the
caller has already written it (that is what `nudge` names in a pane), and it is legitimately empty
for a head whose task document is the board entity itself. Nothing here reads the document — that
is `prompt_document`'s job — and nothing here decides what a head is told; this only fixes what a
run is a run *of*, so that the record of a stopped head still says what it was doing.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

# The three kinds of durable task document a head can be pointed at.
TASK_CARD = "card"
TASK_SPRINT = "sprint"
TASK_STANDING = "standing"
TASK_KINDS = (TASK_CARD, TASK_SPRINT, TASK_STANDING)


class TaskRefError(ValueError):
    """A pointer that names no task, or names one of a kind this product does not have."""


@dataclass(frozen=True)
class TaskRef:
    """What a head run is a run of: a kind, the identifier of that kind, and where it is written.

    Frozen and carried by value, like `HeadSpec`: the pointer a run was started on is part of that
    run's identity, and a stop that reports a different task than the spawn is a record nobody can
    read backwards.
    """

    kind: str
    ref: str
    document: str = ""

    def __post_init__(self) -> None:
        if self.kind not in TASK_KINDS:
            raise TaskRefError(
                f"a task pointer is one of {', '.join(TASK_KINDS)}, not {self.kind!r}"
            )
        if not self.ref:
            raise TaskRefError(f"a {self.kind} task pointer names its task, and this one is empty")
        if self.document and not os.path.isabs(self.document):
            # Same rule as the nudge that will name it: a head's own working directory is not
            # something the pointer's writer knows, so a relative path is a different file in
            # every pane it could be delivered to.
            raise TaskRefError(
                f"a task document is named by absolute path, and {self.document!r} is not one"
            )

    @classmethod
    def card(cls, ref: str, *, document: str = "") -> "TaskRef":
        """One Pipeline card, the kind of work the production dispatcher runs."""
        return cls(kind=TASK_CARD, ref=ref, document=document)

    @classmethod
    def sprint(cls, ref: str, *, document: str = "") -> "TaskRef":
        """A sprint entity: what an observer head is pointed at, and no card at all."""
        return cls(kind=TASK_SPRINT, ref=ref, document=document)

    @classmethod
    def standing(cls, role: str, *, document: str = "") -> "TaskRef":
        """A role's standing instruction, for a head whose task is its role rather than a unit."""
        return cls(kind=TASK_STANDING, ref=role, document=document)

    def to_json(self) -> dict[str, Any]:
        return {"kind": self.kind, "ref": self.ref, "document": self.document}

    @classmethod
    def from_json(cls, payload: Any) -> "TaskRef":
        if not isinstance(payload, dict):
            raise TaskRefError("a task pointer is read from an object, and this is not one")
        return cls(
            kind=str(payload.get("kind") or ""),
            ref=str(payload.get("ref") or ""),
            document=str(payload.get("document") or ""),
        )
