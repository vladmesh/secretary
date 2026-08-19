"""Executable boundaries for the incremental source-layout migration."""

from __future__ import annotations

import ast
import os
import unittest
from pathlib import Path
from unittest import mock

from secretary import _env
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

# This historical telemetry reader is the one known back edge. Holding the exact edge here prevents
# another one while a later migration removes it.
LEGACY_TRIGGERED_AGENTS_IMPORTS = frozenset(
    {("runtime/production_telemetry.py", "secretary.config")}
)


class SourceLayoutTests(unittest.TestCase):
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
                elif isinstance(node, ast.ImportFrom) and node.module and (
                    node.module == "secretary" or node.module.startswith("secretary.")
                ):
                    imports.add((path.relative_to(package).as_posix(), node.module))
        self.assertEqual(imports, LEGACY_TRIGGERED_AGENTS_IMPORTS)

    def test_old_environment_import_is_the_same_implementation(self) -> None:
        self.assertIs(_env.positive_int, env.positive_int)
        with mock.patch.dict(os.environ, {"COUNT": "7"}):
            self.assertEqual(env.positive_int("COUNT", 3), 7)


if __name__ == "__main__":
    unittest.main()
