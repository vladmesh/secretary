"""Executable boundaries for the incremental source-layout migration."""

from __future__ import annotations

import ast
import inspect
import re
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]

# Existing flat modules may leave this set one feature at a time. New modules belong in one of the
# feature packages documented in ARCHITECTURE.md instead of making the root wider again.
LEGACY_FLAT_MODULES = frozenset(
    """
    __init__.py __main__.py _fsutil.py _proc.py automations.py backup.py
    backup_policy.py backup_retention.py backup_verify.py bootstrap.py
    broad_check.py candidate_history.py check_commands.py checkpoint.py cli.py cli_output.py
    codex_provider_events.py config.py data.py
    gate.py
    head_health.py head_registry.py host.py host_apply.py host_commands.py installation.py
    knowledge_write.py memory_errors.py memory_journal.py memory_reindex.py memory_service.py
    memory_write.py observer_root.py onboarding.py product_issue_commands.py product_issues.py
    product_lanes.py provision.py restore.py restore_commands.py role_skills.py
    routing_journal.py runtime_env.py secret_commands.py secret_recover.py secret_store.py
    secret_words.py session.py sprint_close.py sprint_commands.py sprint_observer.py sprints.py
    state_repo.py status.py task_commands.py task_restore.py tasks.py upgrade.py
    """.split()
)

# These are the only approved product edges.  Production telemetry reads the installation config;
# curator discovery reads the canonical project registry and SprintReader rather than copying either
# protocol into the triggered-agent package. Holding the exact set prevents another back edge.
# `secretary.runtime` is the other admitted direction: it is where the head-runtime utilities move
# out of `triggered_agents`, so the legacy package depends on it and never the reverse.
LEGACY_TRIGGERED_AGENTS_IMPORTS = frozenset(
    {
        ("runtime/production_telemetry.py", "secretary.config"),
        ("agents/curator/discover.py", "secretary.config"),
        ("agents/curator/discover.py", "secretary.sprints"),
    }
)


# The dispatcher state machine lives in `secretary.dispatch.runtime`. The retired flat root module
# must not come back, and nothing may import it under its old name.
RETIRED_DISPATCHER_MODULE = ("secretary", "dispatcher")


def _is_runtime_package(module: str) -> bool:
    return module == "secretary.runtime" or module.startswith("secretary.runtime.")


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

    def test_retired_dispatcher_root_module_stays_retired(self) -> None:
        """The retired flat dispatcher root module is gone and nothing imports it by its old name."""
        self.assertFalse((ROOT / "src" / "secretary" / "dispatcher.py").exists())
        retired = ".".join(RETIRED_DISPATCHER_MODULE)
        offenders: list[str] = []
        for tree_root in ("src", "tests", "scripts"):
            for path in (ROOT / tree_root).rglob("*.py"):
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                for node in ast.walk(tree):
                    modules: list[str] = []
                    if isinstance(node, ast.Import):
                        modules = [alias.name for alias in node.names]
                    elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                        modules = [node.module]
                        modules += [f"{node.module}.{alias.name}" for alias in node.names]
                    for module in modules:
                        if module == retired or module.startswith(f"{retired}."):
                            offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}: {module}")
        self.assertEqual(offenders, [])

    def test_dispatcher_claim_flow_is_package_owned(self) -> None:
        dispatcher_source = (ROOT / "src" / "secretary" / "dispatch" / "runtime.py").read_text(
            encoding="utf-8"
        )
        production_source = (
            ROOT / "src" / "secretary" / "dispatch" / "production.py"
        ).read_text(encoding="utf-8")
        for helper in (
            "_failover_collapse",
            "_broad_check_contract_verdict",
            "_sprint_admission_refusal",
            "_project_git_access",
            "_write_claim_preflight_block",
        ):
            self.assertNotIn(f"\n    def {helper}(", dispatcher_source)
        self.assertNotIn("runtime._claim(", production_source)

    def test_dispatcher_worker_launch_flow_is_package_owned(self) -> None:
        dispatcher_source = (ROOT / "src" / "secretary" / "dispatch" / "runtime.py").read_text(
            encoding="utf-8"
        )
        claim_source = (
            ROOT / "src" / "secretary" / "dispatch" / "claim.py"
        ).read_text(encoding="utf-8")
        worker_launch_source = (
            ROOT / "src" / "secretary" / "dispatch" / "worker_launch.py"
        ).read_text(encoding="utf-8")
        for helper in (
            "_launch_worker_after_claim",
            "_worker_launch_failure",
            "_bring_up_worker_head",
            "_worker_relaunch_intent",
            "_resolve_headless_worker",
            "_relaunch_headless_worker",
            "_refuse_headless_worker",
        ):
            self.assertNotIn(f"\n    def {helper}(", dispatcher_source)
        self.assertNotIn("runtime._launch_worker_after_claim(", claim_source)
        self.assertIn("def launch_worker_after_claim(", worker_launch_source)
        self.assertIn("def resolve_headless_worker(", worker_launch_source)

    def test_dispatcher_worker_report_flow_is_package_owned(self) -> None:
        dispatcher_source = (ROOT / "src" / "secretary" / "dispatch" / "runtime.py").read_text(encoding="utf-8")
        report_source = (ROOT / "src" / "secretary" / "dispatch" / "worker_report.py").read_text(encoding="utf-8")
        for helper in (
            "_record_infra_completion", "_accept_stale_infrastructure_done",
            "_block_repeated_infrastructure_done", "_reject_stale_done", "_prompt_worker_report",
        ):
            self.assertNotIn(f"\n    def {helper}(", dispatcher_source)
            self.assertNotIn(f"self.{helper}(", dispatcher_source)
            self.assertNotIn(f"runtime.{helper}(", report_source)
        runtime_tree = ast.parse(dispatcher_source)
        advance = next(node for node in ast.walk(runtime_tree) if isinstance(node, ast.FunctionDef) and node.name == "_advance_worker")
        advance_source = ast.get_source_segment(dispatcher_source, advance)
        self.assertIn("_worker_report_marker(", advance_source)
        self.assertIn("_handle_worker_report(", advance_source)
        self.assertNotIn("verify_worker_result(", advance_source)
        self.assertLess(
            advance_source.index("_worker_report_marker("),
            advance_source.index("_recover_worker_continuation("),
        )
        self.assertLess(
            advance_source.index("_recover_worker_continuation("),
            advance_source.index("_handle_worker_report("),
        )
        self.assertLess(
            advance_source.index("_handle_worker_report("),
            advance_source.index("_wait_watchdog(self, "),
        )
        for entry in ("worker_report_marker", "handle_worker_report", "prompt_worker_report"):
            self.assertIn(f"def {entry}(", report_source)
        self.assertNotIn("from secretary.dispatch.runtime import", report_source)

    def test_dispatcher_gate_lifecycle_is_package_owned(self) -> None:
        dispatcher_source = (ROOT / "src" / "secretary" / "dispatch" / "runtime.py").read_text(encoding="utf-8")
        gate_source = (
            ROOT / "src" / "secretary" / "dispatch" / "gate_lifecycle.py"
        ).read_text(encoding="utf-8")
        gate_domain_source = (
            ROOT / "src" / "secretary" / "dispatch" / "gate.py"
        ).read_text(encoding="utf-8")
        helpers = (
            "_run_gate",
            "_accept_green_gate",
            "_block_missing_gate_receipt",
            "_gate_red_to_worker",
            "_retry_infrastructure_gate",
            "_reset_infrastructure_reruns",
            "_block_infrastructure_reruns_exhausted",
            "_block_infrastructure_rerun_unavailable",
            "_gate_answered",
            "_gate_transport_retry",
            "_gate_rerun_transport_retry",
            "_block_gate_transport",
            "_gate_pending",
            "_worker_vitality_for_gate",
        )
        for helper in helpers:
            self.assertNotIn(f"\n    def {helper}(", dispatcher_source)
            self.assertNotIn(f"runtime.{helper}(", gate_source)
        for entry in (
            "run_gate",
            "accept_green_gate",
            "gate_red_to_worker",
            "gate_transport_retry",
            "block_gate_transport",
            "gate_answered",
            "gate_pending",
        ):
            self.assertIn(f"def {entry}(", gate_source)
        self.assertIn("def reset_infrastructure_reruns(", gate_domain_source)
        self.assertNotIn("from secretary.dispatch.runtime import", gate_source)

    def test_dispatcher_review_verdict_parking_is_package_owned(self) -> None:
        dispatcher_source = (ROOT / "src" / "secretary" / "dispatch" / "runtime.py").read_text(encoding="utf-8")
        verdict_source = (
            ROOT / "src" / "secretary" / "dispatch" / "review_verdict.py"
        ).read_text(encoding="utf-8")
        helpers = (
            "_parks_for_decision",
            "_park_green_verdict",
            "_merge_ready_for_park",
            "_begin_park",
            "_complete_park",
            "_block_red_review_ceiling",
        )
        for helper in helpers:
            self.assertNotIn(f"\n    def {helper}(", dispatcher_source)
            self.assertNotIn(f"self.{helper}(", dispatcher_source)
            self.assertNotIn(f"runtime.{helper}(", verdict_source)
        for entry in (
            "advance_review_verdict",
            "park_green_verdict",
            "merge_ready_for_park",
            "begin_park",
            "complete_park",
        ):
            self.assertIn(f"def {entry}(", verdict_source)
        self.assertIn("_advance_review_verdict(self, task, record, records, payload, attempt_id)", dispatcher_source)
        self.assertNotIn("\n    def _advance_assessment(", dispatcher_source)
        self.assertIn("_advance_assessment(self, task, records, payload, attempt_id)", dispatcher_source)
        self.assertNotIn("\n    def _release_parked(", dispatcher_source)
        self.assertNotIn("from secretary.dispatch.runtime import", verdict_source)

    def test_dispatcher_assessment_decision_flow_is_package_owned(self) -> None:
        dispatcher_source = (ROOT / "src" / "secretary" / "dispatch" / "runtime.py").read_text(encoding="utf-8")
        decision_source = (
            ROOT / "src" / "secretary" / "dispatch" / "assessment_decision.py"
        ).read_text(encoding="utf-8")
        for helper in (
            "_advance_assessment",
            "_recorded_decision",
            "_rework_parked",
            "_reslice_parked",
        ):
            self.assertNotIn(f"\n    def {helper}(", dispatcher_source)
        for entry in (
            "advance_assessment",
            "recorded_decision",
            "rework_parked",
            "reslice_parked",
        ):
            self.assertIn(f"def {entry}(", decision_source)
        self.assertIn("_advance_assessment(self, task, records, payload, attempt_id)", dispatcher_source)
        self.assertIn("release_lifecycle.release_parked(", decision_source)
        self.assertNotIn("runtime._release_parked(", decision_source)
        self.assertNotIn("\n    def _release_parked(", dispatcher_source)
        self.assertNotIn("from secretary.dispatch.runtime import", decision_source)

    def test_dispatcher_release_completion_flow_is_package_owned(self) -> None:
        dispatcher_source = (ROOT / "src" / "secretary" / "dispatch" / "runtime.py").read_text(encoding="utf-8")
        release_source = (
            ROOT / "src" / "secretary" / "dispatch" / "release_lifecycle.py"
        ).read_text(encoding="utf-8")
        gate_source = (
            ROOT / "src" / "secretary" / "dispatch" / "gate_lifecycle.py"
        ).read_text(encoding="utf-8")
        verdict_source = (
            ROOT / "src" / "secretary" / "dispatch" / "review_verdict.py"
        ).read_text(encoding="utf-8")
        decision_source = (
            ROOT / "src" / "secretary" / "dispatch" / "assessment_decision.py"
        ).read_text(encoding="utf-8")

        for helper in (
            "_block_merge_path",
            "_release_parked",
            "_release_effect",
            "_require_completion_evidence",
            "_transfer_research_report",
        ):
            self.assertNotIn(f"\n    def {helper}(", dispatcher_source)
        for helper in ("_released_verdict", "_merge_terminal_reason"):
            self.assertNotIn(f"\ndef {helper}(", dispatcher_source)
        for entry in (
            "block_merge_path",
            "release_parked",
            "release_effect",
            "require_completion_evidence",
            "transfer_research_report",
            "review_drift",
            "merge_readiness",
        ):
            self.assertIn(f"def {entry}(", release_source)

        self.assertNotIn("def review_drift(", verdict_source)
        self.assertNotIn("def merge_readiness(", verdict_source)
        self.assertIn("release_lifecycle.merge_readiness(runtime,", verdict_source)
        self.assertIn("release_lifecycle.release_effect(runtime,", verdict_source)
        self.assertIn("release_lifecycle.release_parked(", decision_source)
        self.assertIn("release_lifecycle.block_merge_path(runtime,", gate_source)
        self.assertNotIn("runtime._block_merge_path(", gate_source)
        self.assertNotIn("runtime._block_merge_path(", verdict_source)
        self.assertNotIn("runtime._release_effect(", verdict_source)
        self.assertNotIn("runtime._release_parked(", decision_source)
        self.assertNotIn("from secretary.dispatch.runtime import", release_source)

    def test_dispatcher_attempt_accounting_is_package_owned(self) -> None:
        dispatcher_source = (ROOT / "src" / "secretary" / "dispatch" / "runtime.py").read_text(encoding="utf-8")
        accounting_source = (
            ROOT / "src" / "secretary" / "dispatch" / "attempt_accounting.py"
        ).read_text(encoding="utf-8")
        for helper in (
            "pending_attempt_usage",
            "_attempt_outcome_obligation",
            "_outcome_lineage_sources",
            "_outcome_round_context_request_id",
            "_persist_outcome_round_context",
            "_capture_outcome_source",
            "_outcome_round_context",
            "_outcome_usage_source",
            "_finish_attempt_outcome",
            "terminal_effect",
            "publish_pending_attempt_outcomes",
            "publish_pending_attempt_usage",
            "record_attempt_usage",
            "_write_attempt_usage",
        ):
            self.assertNotIn(f"\n    def {helper}(", dispatcher_source)
        for entry in (
            "persist_outcome_round_context",
            "capture_outcome_source",
            "terminal_effect",
            "publish_pending_attempt_outcomes",
            "publish_pending_attempt_usage",
            "record_attempt_usage",
        ):
            self.assertIn(f"def {entry}(", accounting_source)
        for path in (ROOT / "src" / "secretary" / "dispatch").glob("*.py"):
            if path.name == "attempt_accounting.py":
                continue
            source = path.read_text(encoding="utf-8")
            for legacy_call in (
                "runtime.terminal_effect(",
                "runtime._persist_outcome_round_context(",
                "runtime._capture_outcome_source(",
                "runtime.record_attempt_usage(",
                "runtime.publish_pending_attempt_outcomes(",
                "runtime.publish_pending_attempt_usage(",
            ):
                self.assertNotIn(legacy_call, source, path.name)
        self.assertNotIn("from secretary.dispatch.runtime import", accounting_source)

    def test_dispatcher_wait_vitality_flow_is_package_owned(self) -> None:
        dispatcher_source = (ROOT / "src" / "secretary" / "dispatch" / "runtime.py").read_text(encoding="utf-8")
        wait_source = (
            ROOT / "src" / "secretary" / "dispatch" / "wait_vitality.py"
        ).read_text(encoding="utf-8")
        helpers = (
            "_decide_wait_by_verdict",
            "_escalate_unobservable_wait",
            "_recovery_policy_decision",
            "_execute_recovery_intent",
            "_sigcont_head",
            "_guard_or_wait",
            "_trigger_wait_watchdog",
            "_respawn_wait",
            "_escalate_wait",
        )
        for helper in helpers:
            self.assertNotIn(f"\n    def {helper}(", dispatcher_source)
            self.assertNotIn(f"runtime.{helper}(", wait_source)
        for entry in (
            "wait_watchdog",
            "execute_recovery_intent",
            "recovery_policy_outcome",
            "reduce_and_store_vitality_episode",
        ):
            self.assertIn(f"def {entry}(", wait_source)
        self.assertNotIn("from secretary.dispatch.runtime import", wait_source)

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
        imports = {edge for edge in imports if not _is_runtime_package(edge[1])}
        self.assertEqual(imports, LEGACY_TRIGGERED_AGENTS_IMPORTS)

    def test_secretary_runtime_never_imports_triggered_agents(self) -> None:
        """The landing zone for the head-runtime core must not depend back on the legacy package."""
        package = ROOT / "src" / "secretary" / "runtime"
        offenders: list[str] = []
        for path in sorted(package.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                    names = [node.module]
                offenders.extend(
                    f"{path.relative_to(ROOT)}:{node.lineno}: {name}"
                    for name in names
                    if name == "triggered_agents" or name.startswith("triggered_agents.")
                )
        self.assertTrue((package / "__init__.py").is_file())
        self.assertEqual(offenders, [])


# Every place in `secretary` that builds the *file* audit (`TaskAudit` over a data dir) rather than
# asking `secretary.tasks.task_audit_for` for the audit owner of a card client, and the reason each
# one may. `requests`/`board_events` is the card audit (`docs/BOARD_STORE.md` §7.3), so a live
# reader built from the file journal of a data directory alone answers from a store the
# installation does not write: on 2026-09-10 that made a committed
# `report:done` invisible to the dispatcher (sprint:1437, secretary-1614), and it is the same shape
# as an empty command history or a false `not_found`. A new entry here is a new reader that decided
# its audit by default instead of by its client, so it is added deliberately with its reason or it
# is a defect.
#
# Since secretary-1673 the file audit class is gone, so the allowance is empty: the typed canon takes
# its audit owner as a required argument, and the fake host hands in an in-memory one.
FILE_AUDIT_CONSTRUCTIONS: dict[str, str] = {}

#: Where a live audit reader asks for its owner. Cards, Sprints and Products/Issues have one
#: implementation, PostgreSQL, and so one audit owner (`task_audit_for`).
LIVE_AUDIT_SELECTORS = {
    "checkpoint.py": "task_audit_for(",
    "task_commands.py": "task_audit_for(",
    "webproto/command_reads.py": "task_audit_for(",
    "webproto/ops.py": "task_audit_for(",
    "webproto/reads.py": "task_audit_for(",
    "webproto/sprint_reads.py": "task_audit_for(",
    "board/sql_host.py": "task_audit_for(",
    "sprints.py": "task_audit_for(",
    "data.py": "task_audit_for(",
    "dispatch/bootstrap.py": "task_audit_for(",
    "product_issues.py": "task_audit_for(",
}


def _source_modules() -> list[Path]:
    """Every Python module under `src/`, both packages."""
    return sorted((ROOT / "src").rglob("*.py"))


class FileAuditOwnershipTests(unittest.TestCase):
    """A live audit reader follows its card client, and the exceptions are named with their reasons."""

    def _constructions(self) -> dict[str, list[int]]:
        """Every call of a `TaskAudit` name in `src/secretary`, by module and line."""
        found: dict[str, list[int]] = {}
        for path in sorted((ROOT / "src" / "secretary").rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                target = node.func
                if isinstance(target, ast.Name) and target.id == "TaskAudit":
                    key = str(path.relative_to(ROOT / "src" / "secretary"))
                    found.setdefault(key, []).append(node.lineno)
        return found

    def test_only_the_named_modules_build_the_file_audit_directly(self) -> None:
        """Anything else reads a journal its installation's backend may never write."""
        offenders = sorted(set(self._constructions()) - set(FILE_AUDIT_CONSTRUCTIONS))
        self.assertEqual(
            offenders,
            [],
            "these modules build the file journal's TaskAudit from a data directory instead of "
            "asking secretary.tasks.task_audit_for for the audit owner of their card client",
        )

    def test_every_named_module_still_builds_one(self) -> None:
        """The allowance is a statement about live code, not a list that outlives its reasons."""
        self.assertEqual(sorted(self._constructions()), sorted(FILE_AUDIT_CONSTRUCTIONS))

    def test_the_file_audit_is_gone_and_the_canon_names_its_owner(self) -> None:
        """No `TaskAudit` to build, and no canon that falls back to one (secretary-1673)."""
        from secretary import tasks
        from secretary.board.events import BoardEventCanon

        self.assertFalse(hasattr(tasks, "TaskAudit"))
        parameters = inspect.signature(BoardEventCanon.__init__).parameters
        self.assertEqual([name for name in parameters if name != "self"], ["audit"])
        self.assertIs(parameters["audit"].default, inspect.Parameter.empty)
        source = inspect.getsource(BoardEventCanon)
        for forbidden in ("data_dir", "events.ndjson", "TaskAudit"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)

    def test_every_live_reader_construction_site_goes_through_the_selector(self) -> None:
        """The readers this card enumerated, each holding the selector call in its own source.

        Named rather than derived: these are the sites that had a data-dir-only audit and are the
        ones a regression would arrive at again. A module that stops calling `task_audit_for` is
        either a deliberate removal of a reader or the bypass coming back, and both belong in a diff
        that has to change this list.
        """
        for module, selector in LIVE_AUDIT_SELECTORS.items():
            source = (ROOT / "src" / "secretary" / module).read_text(encoding="utf-8")
            with self.subTest(module=module):
                self.assertIn(selector, source, module)

    def test_the_card_audit_has_one_owner(self) -> None:
        """`task_audit_for` returns the SQL audit whatever it is handed; no card writer builds a file one."""
        from secretary.board.sql_audit import SqlTaskAudit
        from secretary.tasks import task_audit_for

        self.assertIsInstance(task_audit_for(mock.sentinel.client, "/nonexistent"), SqlTaskAudit)
        source = inspect.getsource(task_audit_for)
        self.assertIsNone(re.search(r"(?<!Sql)TaskAudit\(", source))

    def test_no_source_module_has_a_backend_branch(self) -> None:
        """Cards, Sprints and Products/Issues have one implementation, so nothing asks which one it holds."""
        for path in _source_modules():
            module = str(path.relative_to(ROOT / "src"))
            tree = ast.parse(path.read_text(encoding="utf-8"))
            reads = [
                node.lineno
                for node in ast.walk(tree)
                if (isinstance(node, ast.Attribute) and node.attr == "backend_kind")
                or (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "getattr"
                    and any(
                        isinstance(argument, ast.Constant) and argument.value == "backend_kind"
                        for argument in node.args
                    )
                )
            ]
            with self.subTest(module=module):
                self.assertEqual(reads, [], f"{module} reads a client's backend_kind")

    def test_no_source_module_writes_a_retired_backend_identity(self) -> None:
        """A new write names the PostgreSQL store; an earlier store's identities are only read.

        The store word can hide in data rather than in a branch: an identity minted with another
        store word, or a `"kind"` literal written into a fresh audit event's `backend`, is not a
        `backend_kind` read, and secretary-1669's first submission restored cards under the retired
        identity that way. So `entity_id` takes no store word at all, and every `backend` document a
        module writes names its kind through a name (`BOARD_STORE_KIND`), never a store literal. The
        only literal kind is the dispatcher's own, which names no store.
        """
        from secretary.board.backend import entity_id

        self.assertEqual(list(inspect.signature(entity_id).parameters), ["kind", "number"])
        non_store_kinds = {"dispatcher"}
        for path in _source_modules():
            module = str(path.relative_to(ROOT / "src")).removeprefix("secretary/")
            tree = ast.parse(path.read_text(encoding="utf-8"))
            backends: list[ast.AST] = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Dict):
                    backends.extend(
                        value
                        for key, value in zip(node.keys, node.values, strict=True)
                        if isinstance(key, ast.Constant) and key.value == "backend"
                    )
                if isinstance(node, ast.Assign):
                    backends.extend(
                        node.value
                        for target in node.targets
                        if isinstance(target, ast.Subscript)
                        and isinstance(target.slice, ast.Constant)
                        and target.slice.value == "backend"
                    )
            found = [
                value.lineno
                for backend in backends
                if isinstance(backend, ast.Dict)
                for key, value in zip(backend.keys, backend.values, strict=True)
                if isinstance(key, ast.Constant)
                and key.value == "kind"
                and isinstance(value, ast.Constant)
                and value.value not in non_store_kinds
            ]
            with self.subTest(module=module):
                self.assertEqual(sorted(set(found)), [], f"{module} writes a store literal as a backend kind")

    def test_the_product_issue_store_keeps_no_file_journal_guard(self) -> None:
        """The pre-cutover file-claim guard protected nothing after the importer copied every id.

        On 2026-09-22 the live `board/events.ndjson` (last written 2026-09-10) held 27,966 request
        ids, all but three already in SQL `requests`, and those three were dispatcher records, not
        Product/Issue ones; `board/pending-audit` was empty (secretary-1670).
        """
        from secretary.board.sql_audit import SqlTaskAudit
        from secretary.product_issues import ProductIssueStore

        self.assertFalse(hasattr(ProductIssueStore, "_require_sql_legacy_namespace_free"))
        self.assertNotIn("legacy_audit", inspect.getsource(ProductIssueStore))
        self.assertNotIn("legacy_audit", inspect.getsource(SqlTaskAudit))
        self.assertFalse(hasattr(SqlTaskAudit, "require_pending_layout"))

    def test_the_sprint_traversal_cannot_be_built_from_a_data_directory(self) -> None:
        """`_AuditOnce` takes records or an audit owner, and has no directory to fall back to."""
        from secretary.sprints import _AuditOnce

        parameters = inspect.signature(_AuditOnce.__init__).parameters
        self.assertEqual([name for name in parameters if name != "self"], ["events", "audit"])
        self.assertEqual(_AuditOnce().events(), [])


class IndirectFileAuditReaderTests(unittest.TestCase):
    """No reader opens the file journal itself: the card history is the card audit's traversal.

    `secretary-1622`'s first submission is why this class exists: the product-run events had moved
    to `requests` while `ReadLayer.task_snapshot` and `task_events` still built
    `EventJournal(data_dir)`, so a migrated installation answered a card's history from a projection
    its writers never touch. The file reader is gone with the second card backend.
    """

    def test_the_file_event_reader_is_gone(self) -> None:
        from secretary.webproto import journal

        self.assertFalse(hasattr(journal, "EventJournal"))

    def test_the_read_layer_selects_its_event_reader_from_its_client(self) -> None:
        """Both card-event operations read the owner the client names, and neither a file by default."""
        source = (ROOT / "src" / "secretary" / "webproto" / "reads.py").read_text(encoding="utf-8")
        self.assertIn("task_audit_for(", source)
        self.assertIn("CommittedAudit(", source)
        for operation in ("def task_snapshot", "def task_events"):
            with self.subTest(operation=operation):
                body = source[source.index(operation) :]
                body = body[: body.index("\n    def ", 1)]
                self.assertNotIn("EventJournal(", body)

    def test_the_sql_reader_never_touches_a_path(self) -> None:
        """`CommittedAudit` has no data directory to read: it pages what its audit owner traverses."""
        from secretary.webproto.journal import CommittedAudit

        parameters = inspect.signature(CommittedAudit.__init__).parameters
        self.assertEqual([name for name in parameters if name != "self"], ["audit", "backend"])
        source = inspect.getsource(CommittedAudit)
        for forbidden in ("open(", "Path(", "events.ndjson"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)


class OneBoardClientTests(unittest.TestCase):
    """There is one board client, built in one place, and nothing in the environment selects it."""

    def test_the_client_is_built_from_the_instance_and_never_from_the_environment(self) -> None:
        from secretary.board import backend

        parameters = inspect.signature(backend.board_client).parameters
        self.assertEqual([name for name in parameters], ["instance_dir", "serves", "role"])
        tree = ast.parse((ROOT / "src" / "secretary" / "board" / "backend.py").read_text(encoding="utf-8"))
        environment = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and node.attr in {"environ", "getenv"}
        ]
        self.assertEqual(environment, [], "board/backend.py reads the process environment")

    def test_the_legacy_host_module_is_gone(self) -> None:
        """The host `SqlCardClient` runs on has a neutral name, and no other module is its alias.

        An alias is a board module that only imports from `sql_host`: the shape the old host module
        would keep if it stayed behind as a compatibility name.
        """
        board = ROOT / "src" / "secretary" / "board"
        self.assertTrue((board / "sql_host.py").exists())
        aliases: list[str] = []
        for path in sorted(board.glob("*.py")):
            if path.name in {"sql_host.py", "__init__.py"}:
                continue
            body = ast.parse(path.read_text(encoding="utf-8")).body
            imports_host = any(
                isinstance(node, ast.ImportFrom) and node.module == "secretary.board.sql_host" for node in body
            )
            only_imports = all(
                isinstance(node, (ast.Import, ast.ImportFrom))
                or (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant))
                or (
                    isinstance(node, ast.Assign)
                    and [getattr(target, "id", "") for target in node.targets] == ["__all__"]
                )
                for node in body
            )
            if imports_host and only_imports:
                aliases.append(path.name)
        self.assertEqual(aliases, [])


if __name__ == "__main__":
    unittest.main()


# One home for each of these. A second copy is how the role environment ended up with a façade and
# two entry points (issue:a45731709558936b7b6a, secretary-1683): each lived where its first caller
# was, and the next caller copied rather than imported.
ROLE_ENV_HOME = "secretary/runtime/role_env.py"
SINGLE_HOME_ASSIGNMENTS = {
    "ROLE_ALLOWLIST": ROLE_ENV_HOME,
    "SENSITIVE_ENV_NAME_RE": ROLE_ENV_HOME,
    "CODEX_EFFORTS": "secretary/runtime/head/command.py",
}
SINGLE_HOME_FUNCTIONS = {
    "is_sensitive_env_name": ROLE_ENV_HOME,
    # The board's batched transport; the JSON-RPC Kanboard client that had its own is gone.
    "call_batch": "secretary/board/sql_cards.py",
}


def _is_sensitive_name_pattern(text: str) -> bool:
    """The name classifier's shape: credential words anchored between `_` or the string's ends."""
    return "(^|_)" in text and "(_|$)" in text and "TOKEN" in text.upper()


def _second_copies(sources: dict[str, str]) -> list[str]:
    """Every definition in `sources` (path under `src/` -> text) that is not in its one home."""
    offenders: list[str] = []
    for path, text in sorted(sources.items()):
        if Path(path).name == "role_env.py" and path != ROLE_ENV_HOME:
            offenders.append(f"{path}: role_env module")
        tree = ast.parse(text, filename=path)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    home = SINGLE_HOME_ASSIGNMENTS.get(getattr(target, "id", ""))
                    if home is not None and path != home:
                        offenders.append(f"{path}:{node.lineno}: {target.id}")  # type: ignore[attr-defined]
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                home = SINGLE_HOME_FUNCTIONS.get(node.name)
                if home is not None and path != home:
                    offenders.append(f"{path}:{node.lineno}: def {node.name}")
            elif (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and _is_sensitive_name_pattern(node.value)
                and path != ROLE_ENV_HOME
            ):
                offenders.append(f"{path}:{node.lineno}: sensitive-name pattern")
    return offenders


class SingleHomeTests(unittest.TestCase):
    """The role environment, the sensitive-name pattern, the Codex effort table and the board
    transport each have one definition under `src/`; a second one anywhere fails here."""

    def test_nothing_under_src_defines_a_second_copy(self) -> None:
        src = ROOT / "src"
        sources = {
            path.relative_to(src).as_posix(): path.read_text(encoding="utf-8") for path in _source_modules()
        }
        for home in {ROLE_ENV_HOME, *SINGLE_HOME_ASSIGNMENTS.values(), *SINGLE_HOME_FUNCTIONS.values()}:
            self.assertIn(home, sources)
        self.assertEqual(_second_copies(sources), [])

    def test_each_second_copy_is_caught(self) -> None:
        probes = {
            "role_env module": ("triggered_agents/runtime/role_env.py", "X = 1\n"),
            "ROLE_ALLOWLIST": ("secretary/session.py", "ROLE_ALLOWLIST = {}\n"),
            "SENSITIVE_ENV_NAME_RE": ("secretary/tasks.py", "SENSITIVE_ENV_NAME_RE = None\n"),
            "sensitive-name pattern": (
                "triggered_agents/runtime/scrub.py",
                'import re\nNAMES = re.compile(r"(^|_)(TOKEN|SECRET)(_|$)")\n',
            ),
            "def is_sensitive_env_name": (
                "secretary/checkpoint.py",
                "def is_sensitive_env_name(n):\n    return n\n",
            ),
            "CODEX_EFFORTS": ("secretary/dispatch/launcher.py", "CODEX_EFFORTS: dict = {}\n"),
            "def call_batch": (
                "secretary/board/kanboard.py",
                "class Client:\n    def call_batch(self, calls):\n        return []\n",
            ),
        }
        for label, (path, text) in probes.items():
            with self.subTest(label):
                offenders = _second_copies({path: text})
                self.assertEqual(len(offenders), 1, offenders)
                self.assertTrue(offenders[0].startswith(path) and offenders[0].endswith(label), offenders)
        # The homes themselves are not second copies.
        self.assertEqual(
            _second_copies(
                {
                    ROLE_ENV_HOME: 'ROLE_ALLOWLIST = {}\nSENSITIVE_ENV_NAME_RE = r"(^|_)(TOKEN)(_|$)"\n',
                    "secretary/board/sql_cards.py": "def call_batch(calls):\n    return []\n",
                }
            ),
            [],
        )
