"""A code card's e2e record: every workflow run the dispatcher dispatched for it (secretary-1795).

A project adapter may declare an e2e check (`validation.e2e`, `dispatch/e2e.py`). The dispatcher then
dispatches that GitHub Actions workflow on a `code` card's candidate and waits for the run through a
`wait` card. What it knows about each run lives on the code card itself, in one typed field of its
extension bag (`extensions.extra`, docs/BOARD_STORE.md §8.2), JSON text, no column:

- `e2e`: `{"runs": [...]}`, one record per dispatch, oldest first. Only the dispatcher writes it
  (`TaskWriter.record_e2e_state`), and it rewrites it only when what it knows changed.

A run record is written as an **intent** (card, SHA, dispatch id) before the workflow is dispatched,
so a dispatcher that dies between the intent and the call looks the run up instead and never
dispatches a second one. It then gains the run (from GitHub's dispatch answer, or that lookup), the SHA
the run was checked to run on, the wait card that waits for it, the
wait's frozen result, and, when the result Blocked the card, the request id of that Blocked move
(`closing`): once the move is committed that run's pass is over, and a card brought back to the same
SHA after an unblock may spend a new run on it.

The count of runs dispatched for a card is the number of records whose dispatch was not refused. It
is durable because the records are. A card of a sprint spends the sprint's e2e run budget, charged at
each intent (`board/e2e_budget.py`, secretary-1796); a card outside every sprint is bounded by its own
cap, :data:`E2E_RUN_CAP` plus every raise the owner authorized.

A card that reached the stage with the budget spent carries `budget_wait` (`{decision, generation,
scope, since}`): the decision card it waits on, and the budget (or cap) the runs were spent against.
`task show` says `e2e: budget spent, waiting on <decision>`.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

from secretary.board import e2e_budget
from secretary.board.extension_bag import EXTENSION_BAG

E2E_FIELD = "e2e"

#: The bound on runs one card outside every sprint may dispatch across all its SHAs, before a raise.
E2E_RUN_CAP = e2e_budget.CARD_E2E_CAP

#: A run record's dispatch status: the intent is on the card and the call was not confirmed (it may
#: or may not have reached GitHub), GitHub accepted it, or GitHub refused it.
INTENT = "intent"
SENT = "sent"
REFUSED = "refused"
_DISPATCH_STATES = (INTENT, SENT, REFUSED)

#: The one run conclusion that lets the card proceed, and the one that returns it to rework.
SUCCESS = "success"
FAILURE = "failure"


@dataclass
class E2eRun:
    """One dispatch of the declared workflow for one candidate SHA."""

    dispatch_id: str
    sha: str
    repo: str
    branch: str
    workflow: str
    intent_at: str
    # The adapter's deadline as it read at the intent: the wait card's create repeats it unchanged.
    deadline: str = ""
    dispatch: str = INTENT
    dispatch_detail: str = ""
    run_id: int = 0
    run_url: str = ""
    # The SHA GitHub says the run ran on, once checked against `sha`; empty until then.
    head_sha: str = ""
    # How the run was named: `answer` (GitHub's dispatch answer) or `recovery` (the lookup after the
    # answer was lost), and for `recovery` the rule it matched.
    identified_by: str = ""
    recovery_rule: str = ""
    wait_ref: str = ""
    # {outcome, conclusion, summary, evidence, key}: the wait card's frozen result, copied once.
    result: dict[str, str] | None = None
    # The request id of the Blocked move this run's result (or its dispatch) ended in, and its reason,
    # written before the move so a repeat after a crash moves with the same id and the same words.
    closing: str = ""
    closing_reason: str = ""
    # The result was acted on (the card proceeded, went to rework or was Blocked on it).
    acted: bool = False
    # Base-only moves this green run was carried across (`reconcile_reviewed_base_move`'s record).
    reconciled: list[dict[str, Any]] = field(default_factory=list)

    @property
    def conclusion(self) -> str:
        return str((self.result or {}).get("conclusion") or "")

    @property
    def green(self) -> bool:
        return (
            self.result is not None
            and self.result.get("outcome") == "target_reached"
            and (self.conclusion == SUCCESS)
        )

    def status(self) -> str:
        """One word for `task show`."""
        if self.dispatch == REFUSED:
            return "dispatch_refused"
        if self.result is not None:
            outcome = str(self.result.get("outcome") or "")
            return (self.conclusion or "no_conclusion") if outcome == "target_reached" else outcome
        if not self.run_id or not self.head_sha:
            return "identifying" if self.dispatch == SENT or self.run_id else "dispatching"
        return "waiting" if self.wait_ref else "wait_card_pending"

    @classmethod
    def from_json(cls, payload: Any) -> E2eRun | None:
        if not isinstance(payload, Mapping):
            return None
        texts = {
            name: str(payload.get(name) or "")
            for name in (
                "dispatch_id",
                "sha",
                "repo",
                "branch",
                "workflow",
                "intent_at",
                "deadline",
                "dispatch",
                "dispatch_detail",
                "run_url",
                "head_sha",
                "identified_by",
                "recovery_rule",
                "wait_ref",
                "closing",
                "closing_reason",
            )
        }
        if not (texts["dispatch_id"] and texts["sha"]) or texts["dispatch"] not in _DISPATCH_STATES:
            return None
        run_id = payload.get("run_id")
        result = payload.get("result")
        reconciled = payload.get("reconciled")
        return cls(
            **texts,
            acted=payload.get("acted") is True,
            reconciled=[dict(item) for item in reconciled if isinstance(item, Mapping)]
            if isinstance(reconciled, list)
            else [],
            run_id=run_id if isinstance(run_id, int) and not isinstance(run_id, bool) and run_id > 0 else 0,
            result=(
                {str(key): str(value) for key, value in result.items()}
                if isinstance(result, Mapping) and result.get("outcome")
                else None
            ),
        )


@dataclass
class BudgetWait:
    """A card waiting on the decision its spent e2e budget needs."""

    decision: str
    # The budget (a sprint's) or the cap (a card's) the runs were spent against: its decision's key.
    generation: int
    # `sprint` or `card`: whose budget is spent.
    scope: str
    since: str

    @property
    def mark(self) -> str:
        return f"e2e: budget spent, waiting on {self.decision}"

    @classmethod
    def from_json(cls, payload: Any) -> BudgetWait | None:
        if not isinstance(payload, Mapping) or not payload.get("decision"):
            return None
        generation = payload.get("generation")
        if isinstance(generation, bool) or not isinstance(generation, int):
            return None
        return cls(
            decision=str(payload["decision"]),
            generation=generation,
            scope=str(payload.get("scope") or ""),
            since=str(payload.get("since") or ""),
        )


@dataclass
class E2eState:
    """Every run record of one card, as its `e2e` field holds them."""

    runs: list[E2eRun] = field(default_factory=list)
    budget_wait: BudgetWait | None = None

    @property
    def dispatched(self) -> int:
        """Runs dispatched for this card, across all its SHAs: every intent GitHub did not refuse."""
        return sum(1 for run in self.runs if run.dispatch != REFUSED)

    def latest(self, sha: str) -> E2eRun | None:
        """The newest record for this SHA, or None."""
        return next((run for run in reversed(self.runs) if run.sha == sha), None)

    def green(self, sha: str) -> E2eRun | None:
        """A run that concluded `success` on exactly this SHA, or None."""
        return next((run for run in reversed(self.runs) if run.sha == sha and run.green), None)

    def last_green(self) -> E2eRun | None:
        """The newest run that concluded `success`, on whatever SHA, or None."""
        return next((run for run in reversed(self.runs) if run.green), None)

    def to_json(self) -> dict[str, Any]:
        document: dict[str, Any] = {"runs": [asdict(run) for run in self.runs]}
        if self.budget_wait is not None:
            document["budget_wait"] = asdict(self.budget_wait)
        return document

    def text(self) -> str:
        return json.dumps(self.to_json(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, payload: Any) -> E2eState:
        runs = payload.get("runs") if isinstance(payload, Mapping) else None
        parsed = [E2eRun.from_json(run) for run in runs] if isinstance(runs, list) else []
        waiting = BudgetWait.from_json(payload.get("budget_wait")) if isinstance(payload, Mapping) else None
        return cls([run for run in parsed if run is not None], waiting)


def _json_field(task: Mapping[str, Any]) -> Any:
    extensions = task.get("extensions")
    bag = extensions.get(EXTENSION_BAG) if isinstance(extensions, Mapping) else None
    raw = bag.get(E2E_FIELD) if isinstance(bag, Mapping) else None
    if isinstance(raw, Mapping):
        return raw
    try:
        return json.loads(str(raw or ""))
    except ValueError:
        return None


def e2e_state(task: Mapping[str, Any]) -> E2eState:
    """The card's run records; a field that does not parse reads as none."""
    return E2eState.from_json(_json_field(task))


def e2e_view(task: Mapping[str, Any]) -> dict[str, Any] | None:
    """The `e2e` block `task show` carries, or None for a card that never reached the stage.

    A card of a sprint spends the sprint's budget (`budget: <sprint>`, no `run_cap`); a card outside
    every sprint has its own cap. A card waiting on a budget decision carries `mark`.
    """
    state = e2e_state(task)
    if not state.runs and state.budget_wait is None:
        return None
    sprint = str(task.get("sprint") or "")
    return {
        "runs_dispatched": state.dispatched,
        "run_cap": None if sprint else e2e_budget.card_cap(task),
        "budget": sprint or None,
        **(
            {"mark": state.budget_wait.mark, "waiting_on": state.budget_wait.decision}
            if state.budget_wait is not None
            else {}
        ),
        "runs": [
            {
                "sha": run.sha,
                "dispatch_id": run.dispatch_id,
                "workflow": run.workflow,
                "state": run.status(),
                "run": run.run_url or None,
                "identified_by": run.identified_by or None,
                **({"recovery_rule": run.recovery_rule} if run.recovery_rule else {}),
                **(
                    {"reconciled_to": [item.get("head_sha") for item in run.reconciled]}
                    if run.reconciled
                    else {}
                ),
                "wait_card": run.wait_ref or None,
                "dispatched_at": run.intent_at,
                "result": (
                    {key: run.result.get(key) for key in ("outcome", "conclusion", "summary")}
                    if run.result is not None
                    else None
                ),
                **({"detail": run.dispatch_detail} if run.dispatch_detail else {}),
            }
            for run in state.runs
        ],
    }


__all__ = [
    "E2E_FIELD",
    "E2E_RUN_CAP",
    "FAILURE",
    "INTENT",
    "REFUSED",
    "SENT",
    "SUCCESS",
    "BudgetWait",
    "E2eRun",
    "E2eState",
    "e2e_state",
    "e2e_view",
]
