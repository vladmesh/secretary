"""`transition from-<old> --repair-products [--apply]`: archive the dead cutover canary Product.

The transition stopped registering the old project id (`projects/<old>.yaml` became the new one),
and the checkpoint refuses an open Product linked to an unregistered project. The archived old
Product keeps its links as history, which the validator admits for a closed Product (ummanu-5).
The one open Product still linked to the old project is the canary of the cutover controller
deleted in deacdedb: no open issue, no open sprint. This repair archives exactly that Product,
the way §T2.6 archived the old one, and changes nothing else: not its projects, issues, sprints
or comments, and not `updated_at`.

The Product store has no archive command and no audited Product state change (its journal knows
product creation and Issue writes only; `closeTask` on the SQL backend is the same bare UPDATE),
so this is the §T2.6 statement in one owner transaction, after a locked re-read.
"""

from __future__ import annotations

from typing import Any

from . import board
from .context import Layout, TransitionError
from .names import NEW, OLD

#: The canary Product the cutover acceptance created (sprint:1436); the only one this repair names.
CANARY_PRODUCT = "cutover-d9821c3499daaf9d"


def _describe(state: dict[str, Any]) -> str:
    issues = ", ".join(f"{count} {name}" for name, count in state["issues"].items()) or "none"
    return (
        f"state={state['state']} title={state['title']!r} projects={state['projects']} "
        f"issues: {issues}; sprints: {', '.join(state['sprints']) or 'none'}"
    )


def _check(state: dict[str, Any] | None) -> bool:
    """True when the archive is still to be written; False for the finished state; refuse otherwise."""
    if state is None:
        raise TransitionError(f"product {CANARY_PRODUCT} is not on the board; nothing written")
    if state["projects"] != [OLD.project_id]:
        raise TransitionError(
            f"product {CANARY_PRODUCT} links {state['projects']}, not [{OLD.project_id!r}]; nothing written"
        )
    if state["state"] == "archived":
        return False
    if state["state"] != "active":
        raise TransitionError(f"product {CANARY_PRODUCT} is {state['state']!r}, not active; nothing written")
    return True


def repair_products(layout: Layout, *, apply: bool) -> int:
    """Print the canary's state and the change; with `apply`, archive it. Refusals raise."""
    config = board.read_store_env(layout.instance / board.STORE_FILE, NEW)
    mode = "apply" if apply else "plan only, nothing is written"
    print(f"Product {CANARY_PRODUCT} on {config.dbname}@{config.host}:{config.port}: {mode}")
    before, after = board.archive_product(config, CANARY_PRODUCT, _check, apply=apply)
    assert before is not None
    print(f"  current: {_describe(before)}")
    if before["state"] == "archived":
        print("  already archived: nothing to do")
    elif after is None:
        print("  would set state active -> archived; projects, issues, sprints and comments stay as they are")
    else:
        print(f"  archived: state {before['state']} -> {after['state']}")
        print(f"  now:     {_describe(after)}")
    return 0


__all__ = ["CANARY_PRODUCT", "repair_products"]
