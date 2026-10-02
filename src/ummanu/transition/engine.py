"""Run the steps: `--plan` prints them, `--apply` resumes at the first unfinished one, `--rollback`
undoes them. Only `--apply` and `--rollback` take the lock and write the journal."""

from __future__ import annotations

from typing import Any

from . import preconditions, rollback, steps
from .context import Context, TransitionError, apply_lock
from .names import NEW, OLD

#: The package this code was imported as: the old name before the rename card lands, the new after.
RUNNING_PACKAGE = (__package__ or "").split(".")[0]


def running_renamed() -> bool:
    return RUNNING_PACKAGE == NEW.package


def plan(ctx: Context) -> int:
    """Print every step with what it would touch and every precondition's result. Writes nothing."""
    layout = ctx.layout
    if not ctx.sprint:
        ctx.sprint = str(ctx.journal.fact("sprint") or "")
    print(f"Transition {OLD.package} -> {NEW.package}: plan only, nothing is written")
    print(f"  home {layout.home}, instance {layout.instance}, running package {RUNNING_PACKAGE}")
    print(f"  journal {layout.journal_path} ({'present' if ctx.journal.exists else 'absent'}), "
          f"rollback copies {layout.state_dir}")
    unmet: list[str] = []
    for step in steps.STEPS:
        state = "done" if ctx.journal.done(step.name) else "pending"
        print(f"\nStep {step.number} [{step.name}, {state}] {step.title}")
        if step.name == "preconditions" and not ctx.journal.done(step.name):
            checks, facts = preconditions.run(ctx, ctx.board.reads(ctx), fetch=False)
            print(f"  $ git -C {layout.code_root()} fetch origin   (--apply only; the plan reads the refs as they are)")
            for check in checks:
                print(check.line())
            unmet = [check.name for check in checks if not check.ok]
            ctx.sprint = ctx.sprint or str(facts.get("sprint") or "")
            continue
        try:
            lines = step.plan(ctx)
        except (TransitionError, OSError) as exc:
            lines = [f"  (cannot describe: {exc})"]
        for line in lines:
            print(line)
    print()
    if unmet:
        print(f"Preconditions unmet: {', '.join(unmet)}. --apply would refuse at step 1.")
        return 1
    print("Preconditions hold." if not ctx.journal.done("preconditions") else "Step 1 already passed.")
    return 0


def apply(ctx: Context, *, through: str = "") -> int:
    stop = steps.step_named(through).number if through else 0
    with apply_lock(ctx.layout.lock_path):
        if not ctx.sprint:
            ctx.sprint = str(ctx.journal.fact("sprint") or "")
        for step in steps.STEPS:
            if ctx.journal.done(step.name):
                continue
            if step.phase != "old" and ctx.require_renamed and not running_renamed():
                print(f"Steps 1-4 are done; step {step.number} runs from the renamed checkout. Continue with "
                      f"scripts/transition-from-{OLD.package}.sh (it performs step 5 and execs "
                      f"{ctx.layout.cli(NEW)}).")
                return 0
            ctx.say(f"Step {step.number} [{step.name}] {step.title}")
            facts: dict[str, Any] = step.apply(ctx)
            ctx.journal.finish(step.name, **facts)
            ctx.say(f"Step {step.number} [{step.name}] done")
            if stop and step.number >= stop:
                return 0
    ctx.say(f"Transition finished; report at {ctx.layout.state_dir / 'report.md'}")
    return 0


def run_rollback(ctx: Context) -> int:
    with apply_lock(ctx.layout.lock_path):
        outcome = rollback.rollback(ctx)
    for key, value in outcome.items():
        print(f"  {key}: {value}")
    print(f"Rolled back. The pipeline is still frozen: `{OLD.package} resume` once the old installation checks out.")
    return 0


__all__ = ["RUNNING_PACKAGE", "apply", "plan", "run_rollback", "running_renamed"]
