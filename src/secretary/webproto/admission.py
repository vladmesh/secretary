"""The one place a product run is allowed to exist for a card, and the order it decides in.

Criterion 4 of secretary-1562 asks for a single owner of a card and names the failure it is
guarding against: a product run must not become a second owner beside the production dispatcher,
and it must not step into work an open sprint's observer is running. Two owners of one card is not
a race to be locked out — it is two agents editing one workspace and two writers reporting on one
attempt, and no lock repairs that after the fact.

So there is one gate, it is this function, and **every** path that starts a head goes through it:
`run_start` for the worker and `run_review` for the reviewer both call :func:`admit` before they
build a workspace or spawn anything, and neither has a branch that skips it. It answers with a
value the caller then uses (the card and its binding), so a caller cannot proceed without having
asked — there is no "check" it could forget beside a "read" it could not.

It decides in this order, and the order matters because each question is only meaningful once the
one before it has been answered:

1. **the card exists.** Everything below is about a card, so a reference the board does not hold is
   `not_found` before anything else is asked;
2. **its project is registered and enabled here.** A run provisions a workspace out of the
   project's repository, and an unregistered project has no repository this installation owns;
3. **no open sprint reserves that project.** This is the existing rule, read from the existing
   index (`secretary.sprints.active_sprint_projects`) that `TaskWriter._guard_sprint_write`
   already authorises board writes against. A reserved project is a sprint's, and its observer
   decides what runs in it — so the product run refuses rather than negotiating;
4. **the card is not in the production dispatcher's lane.** The dispatcher claims `ready` cards and
   holds `in_progress`, `validate`, `assessment` and `blocked` ones; a card in any of those is an
   attempt somebody is running. What is left — the backlog — is where a product run may take a
   card, and that is a fact about the card's state rather than a new register of ownership;
5. **the dispatcher holds no durable record for it.** The state file is the dispatcher's own answer
   to "am I running this card", read (never written) exactly as the read layer reads it. A card
   whose column moved while an attempt is still recorded is still that attempt's;
6. **this layer holds no run for it that is not over.** One product run per card at a time. A run
   that is over is history and does not block the next one, which is what lets a review follow its
   worker.

   The fact this reads is `ProductRun.ended`, and it is the **only** fact about a run any branch of
   this gate consults: whether the run is over, never how it ended. The two were one value until
   secretary-1563 -- a card was freed by the run carrying a value in `(finished, process_failed)`,
   so a run that was genuinely over but whose ending could only be named `source_unavailable` had
   to be given a false one before its card could be freed. Told apart, this condition asks the
   question it actually means, and no ending has to be invented to answer it.

   This is also the whole of how an *unresolved* run fences a card, and deliberately so. A run
   whose head could not be confirmed stopped is not over (:mod:`secretary.webproto.lifecycle`),
   so this condition already refuses the next run over it -- with no second register of ownership
   and no new rule here. The failure that made this necessary got past this gate only because the
   code that could not confirm a cleanup settled the run anyway; the repair is that it no longer
   may, not that this gate learned a new question.

Nothing here is a scheduler, a store or an audit of its own. Every fact it consults already has an
owner elsewhere, and it consults them in a fixed order rather than re-deriving any of them.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from secretary.config import InstanceReport
from secretary.tasks import ACTIVE_STATES, TaskError, TaskReader
from secretary.webproto.errors import OwnerConflict, TaskNotFound, ValidationRefused
from secretary.webproto.runs import ProductRun, RunStore

#: The states in which a card belongs to the production pipeline. `ready` is what the dispatcher's
#: claim pass takes; `ACTIVE_STATES` are the ones in which a card holds a workspace, a suspended
#: worker or a running head; `blocked` is an attempt waiting on an observer decision. A product run
#: takes a card from none of them.
DISPATCHER_LANE = frozenset({"ready", "blocked"}) | ACTIVE_STATES

#: The states a product run may take a card from: the backlog, and only it.
ADMISSIBLE_STATES = frozenset({"issues"})


@dataclass(frozen=True)
class Admission:
    """A card this layer is allowed to run, with what the caller needs so it need not re-read."""

    ref: str
    project: str
    card: dict[str, Any]
    binding: dict[str, Any]
    #: The runs this card already has, oldest first, all of them over. `run_review` finds its
    #: worker here.
    runs: tuple[ProductRun, ...] = ()

    @property
    def repo(self) -> str:
        return str(self.binding.get("repo") or "")

    @property
    def default_branch(self) -> str:
        return str(self.binding.get("default_branch") or "main")


def admit(
    ref: str,
    *,
    report: InstanceReport,
    data_dir: Path,
    board: Any,
    store: RunStore,
    production_state: Path,
) -> Admission:
    """Whether this card may carry a product run, decided once, in the order documented above."""
    reference = str(ref or "")
    if not reference:
        raise ValidationRefused("a product run names the card it runs")

    card = _card(reference, board)
    project = str(card.get("project") or "")
    binding = _binding(report, project)
    _refuse_reserved_project(project, data_dir)
    _refuse_dispatcher_lane(reference, str(card.get("state") or ""))
    _refuse_dispatcher_record(reference, production_state)
    runs = tuple(store.for_ref(reference))
    _refuse_open_run(reference, runs)
    return Admission(ref=reference, project=project, card=card, binding=binding, runs=runs)


def _card(ref: str, board: Any) -> dict[str, Any]:
    try:
        return TaskReader(board).show(ref)
    except TaskError as exc:
        if exc.code == "not_found":
            raise TaskNotFound(f"the board holds no card {ref!r}") from None
        raise OwnerConflict(
            f"the board could not say who owns {ref!r}, so no run is started over it: {exc.message}"
        ) from None


def _binding(report: InstanceReport, project: str) -> dict[str, Any]:
    if not project:
        raise ValidationRefused("this card names no project, so there is no repository to run in")
    for binding in report.bindings:
        if isinstance(binding, dict) and str(binding.get("id") or "") == project:
            if not bool(binding.get("enabled", True)):
                raise ValidationRefused(f"project {project!r} is registered here but disabled")
            if not str(binding.get("repo") or ""):
                raise ValidationRefused(f"project {project!r} declares no repository to run in")
            return binding
    raise ValidationRefused(f"project {project!r} is not registered on this installation")


def _refuse_reserved_project(project: str, data_dir: Path) -> None:
    """An open sprint's project belongs to that sprint, and its observer decides what runs in it."""
    from secretary.sprints import active_sprint_projects

    holders = active_sprint_projects(data_dir).get(project) or []
    if holders:
        raise OwnerConflict(
            f"project {project!r} is reserved by open sprint {', '.join(sorted(holders))}: its "
            "observer owns what runs there, so this product run is refused rather than started "
            "beside it"
        )


def _refuse_dispatcher_lane(ref: str, state: str) -> None:
    if state in DISPATCHER_LANE:
        raise OwnerConflict(
            f"card {ref} is in {state!r}, which is the production dispatcher's lane: it claims "
            f"{', '.join(sorted(DISPATCHER_LANE))} cards itself, and a product run over one of "
            "them would be a second owner of the same attempt"
        )
    if state not in ADMISSIBLE_STATES:
        raise OwnerConflict(
            f"card {ref} is in {state!r}, and a product run takes a card only from "
            f"{', '.join(sorted(ADMISSIBLE_STATES))}"
        )


def _refuse_dispatcher_record(ref: str, production_state: Path) -> None:
    """The dispatcher's own durable answer to "am I running this card", read and never written.

    An unreadable state file is not "the dispatcher is running nothing": it is a source that could
    not say, and starting a second owner on a maybe is exactly what this gate exists to prevent. So
    it refuses, and names the file.
    """
    import json

    try:
        payload = json.loads(production_state.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return
    except (OSError, ValueError) as exc:
        raise OwnerConflict(
            f"the dispatcher production state at {production_state} could not be read ({exc}), so "
            "whether it is running this card could not be established and no run is started"
        ) from None
    records = payload.get("records") if isinstance(payload, dict) else None
    if not isinstance(records, dict):
        raise OwnerConflict(
            f"the dispatcher production state at {production_state} carries no records object, so "
            "whether it is running this card could not be established and no run is started"
        )
    if ref in records:
        raise OwnerConflict(
            f"the production dispatcher holds a durable record for {ref}: it owns this card's "
            "attempt, and a product run over it would be a second owner"
        )


def _refuse_open_run(ref: str, runs: tuple[ProductRun, ...]) -> None:
    """The fence, decided by one fact: is each run of this card over.

    `run.ended` and nothing else. Not the run's outcome value, not whether that value is one this
    reader would call a success or a failure, and not the phase spelled out again here: a run is
    over when the process it may have held is provably gone or was never spawned, and that is the
    only property of a run that can free a card. Whatever a run ended *as* is a matter for whoever
    reads it, and it has no vote here.
    """
    open_runs = [run for run in runs if not run.ended]
    if open_runs:
        names = ", ".join(f"{run.run_id} ({run.role})" for run in open_runs)
        raise OwnerConflict(
            f"card {ref} already carries an unsettled product run: {names}. One product run owns a "
            "card at a time; read its state, and start the next one when it has ended"
        )
