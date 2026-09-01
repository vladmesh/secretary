"""Executable boundaries for the incremental source-layout migration."""

from __future__ import annotations

import ast
import os
import typing
import unittest
from pathlib import Path
from unittest import mock

from secretary import _env
from secretary.dispatch import verdict_effect
from secretary.dispatch.review_context import ValidatedReviewIdentity
from secretary.dispatcher_state import DispatcherRecord
from secretary.infra import env

ROOT = Path(__file__).resolve().parents[1]

# Existing flat modules may leave this set one feature at a time. New modules belong in one of the
# feature packages documented in ARCHITECTURE.md instead of making the root wider again.
LEGACY_FLAT_MODULES = frozenset(
    """
    __init__.py __main__.py _env.py _fsutil.py _proc.py automations.py backup.py
    backup_policy.py backup_retention.py backup_verify.py board_transport.py bootstrap.py
    broad_check.py candidate_history.py check_commands.py checkpoint.py cli.py cli_output.py
    codex_provider_events.py config.py data.py dispatcher.py dispatcher_commands.py
    dispatcher_gate.py dispatcher_gate_receipt.py dispatcher_heartbeat.py dispatcher_helpers.py
    dispatcher_launch.py dispatcher_launcher.py dispatcher_observer.py
    dispatcher_observer_fence.py dispatcher_pause.py dispatcher_pause_ops.py
    dispatcher_production.py dispatcher_review.py dispatcher_state.py dispatcher_tui.py
    dispatcher_types.py dispatcher_watchdog.py dispatcher_worker_lifecycle.py gate.py
    head_health.py head_registry.py host.py host_apply.py host_commands.py installation.py
    knowledge_write.py memory_errors.py memory_journal.py memory_reindex.py memory_service.py
    memory_write.py observer_root.py onboarding.py product_issue_commands.py product_issues.py
    product_lanes.py provision.py restore.py restore_commands.py role_env.py role_skills.py
    routing_journal.py runtime_env.py secret_commands.py secret_recover.py secret_store.py
    secret_words.py session.py sprint_close.py sprint_commands.py sprint_observer.py sprints.py
    state_repo.py status.py task_commands.py task_restore.py tasks.py upgrade.py
    """.split()
)

# These are the only approved product edges.  Production telemetry reads the installation config;
# curator discovery reads the canonical project registry and SprintReader rather than copying either
# protocol into the triggered-agent package. Holding the exact set prevents another back edge.
LEGACY_TRIGGERED_AGENTS_IMPORTS = frozenset(
    {
        ("runtime/production_telemetry.py", "secretary.config"),
        ("agents/curator/discover.py", "secretary.config"),
        ("agents/curator/discover.py", "secretary.sprints"),
        ("agents/curator/discover.py", "secretary.tasks"),
    }
)


# The two irreversible things a reviewer verdict can earn, spelled as they are called.
def _verdict_effect_call(node: ast.Call) -> str:
    """Name the verdict effect this call performs, or "" when it performs none."""
    function = node.func
    if isinstance(function, ast.Attribute) and function.attr == "complete_green":
        return "the merge"
    if isinstance(function, ast.Attribute) and function.attr == "move":
        for keyword in node.keywords:
            if keyword.arg != "target" or not isinstance(keyword.value, ast.Constant):
                continue
            if keyword.value.value == "assessment":
                return "the Assessment move"
            if keyword.value.value == "done":
                return "the Done publication move"
    return ""


def _defines_the_host_effect(path: Path, node: ast.Call) -> bool:
    """The host's own implementation of `complete_green` is the effect, not a caller of it."""
    return path.name == "host.py" and isinstance(node.func, ast.Attribute) and node.func.attr != "move"


class SourceLayoutTests(unittest.TestCase):
    def test_test_support_never_imports_a_test_module(self) -> None:
        """Shared fakes are a one-way dependency, not bridges between test modules."""
        offenders: list[str] = []
        for path in (ROOT / "tests").rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                module = node.module if isinstance(node, ast.ImportFrom) else None
                if module and (module == "tests.test" or module.startswith("tests.test_")):
                    offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}: {module}")
        self.assertEqual(offenders, [])

    def test_new_secretary_modules_do_not_widen_the_flat_root(self) -> None:
        current = {path.name for path in (ROOT / "src" / "secretary").glob("*.py")}
        self.assertEqual(current - LEGACY_FLAT_MODULES, set())

    def test_triggered_agents_adds_no_new_dependency_on_secretary(self) -> None:
        package = ROOT / "src" / "triggered_agents"
        imports: set[tuple[str, str]] = set()
        for path in package.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports.update(
                        (path.relative_to(package).as_posix(), alias.name)
                        for alias in node.names
                        if alias.name == "secretary" or alias.name.startswith("secretary.")
                    )
                elif (
                    isinstance(node, ast.ImportFrom)
                    and node.module
                    and (node.module == "secretary" or node.module.startswith("secretary."))
                ):
                    imports.add((path.relative_to(package).as_posix(), node.module))
        self.assertEqual(imports, LEGACY_TRIGGERED_AGENTS_IMPORTS)

    def test_the_verdict_effects_are_reachable_only_through_their_executor(self) -> None:
        """The board's Assessment move, the Done publication and the merge itself have one owner.

        The defect this boundary exists for was a second route to the merge: a recovery arm that
        reached it having re-established less than the tick that opened the intent. So the calls
        themselves are held to one module, statically, and every entry into that module goes
        through the executor rather than around it into an effect.
        """
        owner = "src/secretary/dispatch/verdict_effect.py"
        offenders: list[str] = []
        for path in sorted((ROOT / "src" / "secretary").rglob("*.py")):
            relative = path.relative_to(ROOT).as_posix()
            if relative == owner:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                effect = _verdict_effect_call(node)
                if effect and not _defines_the_host_effect(path, node):
                    offenders.append(f"{relative}:{node.lineno}: {effect}")
        self.assertEqual(offenders, [], "a verdict effect is invoked outside its executor")

    def test_no_effect_can_be_performed_without_the_executors_own_preconditions(self) -> None:
        """The other half: inside the executor, the effects take the sealed precondition value.

        The seal is what makes "this invocation re-established everything" a type rather than a
        convention, so an entry that skipped the chain cannot spell the value out longhand and
        carry on into the board move or the merge.
        """
        for name in ("_move_to_assessment", "_publish_merge"):
            with self.subTest(effect=name):
                hints = typing.get_type_hints(
                    getattr(verdict_effect, name),
                    localns={"DispatcherRecord": DispatcherRecord},
                )
                self.assertIs(
                    hints.get("preconditions"),
                    verdict_effect.EffectPreconditions,
                    f"{name} must require the established preconditions",
                )
        with self.assertRaises(ValueError):
            verdict_effect.EffectPreconditions(
                intent=verdict_effect.VerdictEffectIntent(
                    effect=verdict_effect.PARK_EFFECT_RELEASE,
                    report_baseline=0,
                    move_reason="",
                    verdict_outcome="green",
                ),
                identity=object(),  # type: ignore[arg-type]
                checkout_sha="c" * 40,
                base_sha="b" * 40,
                gate_stage="release",
                receipt=None,
            )
        self.assertIsNot(
            ValidatedReviewIdentity, verdict_effect.EffectPreconditions, "two distinct seals"
        )

    def test_old_environment_import_is_the_same_implementation(self) -> None:
        self.assertIs(_env.positive_int, env.positive_int)
        with mock.patch.dict(os.environ, {"COUNT": "7"}):
            self.assertEqual(env.positive_int("COUNT", 3), 7)


if __name__ == "__main__":
    unittest.main()
