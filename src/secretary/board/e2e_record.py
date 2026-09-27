"""A code card's e2e record: every workflow run the dispatcher dispatched for it (secretary-1795).

A project adapter may declare an e2e check (`validation.e2e`, `dispatch/e2e.py`). The dispatcher then
dispatches that GitHub Actions workflow on a `code` card's candidate and waits for the run through a
`wait` card. What it knows about each run lives on the code card itself, in one typed field of its
extension bag (`extensions.extra`, docs/BOARD_STORE.md §8.2), JSON text, no column:

- `e2e`: `{"runs": [...]}`, one record per dispatch, oldest first. Only the dispatcher writes it
  (`TaskWriter.record_e2e_state`), and it rewrites it only when what it knows changed.

A run record is written as an **intent** (card, SHA, dispatch id) before the workflow is dispatched,
so a dispatcher that dies between the intent and the call finds the run by its dispatch id and never
dispatches a second one. It then gains the run it identified, the wait card that waits for it, the
wait's frozen result, and, when the result Blocked the card, the request id of that Blocked move
(`closing`): once the move is committed that run's pass is over, and a card brought back to the same
SHA after an unblock may spend a new run on it.

The count of runs dispatched for a card is the number of records whose dispatch was not refused. It
is durable because the records are, and it is bounded by :data:`E2E_RUN_CAP` until the sprint e2e
budget replaces that interim cap.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

from secretary.board.extension_bag import EXTENSION_BAG

E2E_FIELD = "e2e"

#: The interim bound on runs one card may dispatch across all its SHAs.
E2E_RUN_CAP = 3

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
    wait_ref: str = ""
    # {outcome, conclusion, summary, evidence, key}: the wait card's frozen result, copied once.
    result: dict[str, str] | None = None
    # The request id of the Blocked move this run's result (or its dispatch) ended in, and its reason,
    # written before the move so a repeat after a crash moves with the same id and the same words.
    closing: str = ""
    closing_reason: str = ""

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
        if not self.run_id:
            return "identifying" if self.dispatch == SENT else "dispatching"
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
                "wait_ref",
                "closing",
                "closing_reason",
            )
        }
        if not (texts["dispatch_id"] and texts["sha"]) or texts["dispatch"] not in _DISPATCH_STATES:
            return None
        run_id = payload.get("run_id")
        result = payload.get("result")
        return cls(
            **texts,
            run_id=run_id if isinstance(run_id, int) and not isinstance(run_id, bool) and run_id > 0 else 0,
            result=(
                {str(key): str(value) for key, value in result.items()}
                if isinstance(result, Mapping) and result.get("outcome")
                else None
            ),
        )


@dataclass
class E2eState:
    """Every run record of one card, as its `e2e` field holds them."""

    runs: list[E2eRun] = field(default_factory=list)

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

    def to_json(self) -> dict[str, Any]:
        return {"runs": [asdict(run) for run in self.runs]}

    def text(self) -> str:
        return json.dumps(self.to_json(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, payload: Any) -> E2eState:
        runs = payload.get("runs") if isinstance(payload, Mapping) else None
        parsed = [E2eRun.from_json(run) for run in runs] if isinstance(runs, list) else []
        return cls([run for run in parsed if run is not None])


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
    """The `e2e` block `task show` carries, or None for a card that never dispatched a run."""
    state = e2e_state(task)
    if not state.runs:
        return None
    return {
        "runs_dispatched": state.dispatched,
        "run_cap": E2E_RUN_CAP,
        "runs": [
            {
                "sha": run.sha,
                "dispatch_id": run.dispatch_id,
                "workflow": run.workflow,
                "state": run.status(),
                "run": run.run_url or None,
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
    "E2eRun",
    "E2eState",
    "e2e_state",
    "e2e_view",
]
