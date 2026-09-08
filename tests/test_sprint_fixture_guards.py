from __future__ import annotations

import ast
import inspect
import textwrap
import types
import unittest
from pathlib import Path

from secretary.tasks import TaskError
from tests import (
    test_sprint_executors,
    test_sprint_listing_budget,
    test_sprint_restore,
    test_sprints,
)
from tests.fakes.sprints import SprintBackendFixture, SprintFixture
from tests.sprint_contract import KANBOARD_ONLY

SUITES = (
    test_sprints,
    test_sprint_executors,
    test_sprint_restore,
    test_sprint_listing_budget,
)
EXPECTED_METHODS = {
    "tests.test_sprints": 150,
    "tests.test_sprint_executors": 24,
    "tests.test_sprint_restore": 26,
    "tests.test_sprint_listing_budget": 4,
}
BEFORE_REACH_INS = {
    "client.calls": 34,
    "client.tasks": 28,
    "_sprint_rows": 18,
    "client.metadata": 18,
    "_transactions": 12,
    "client.comments": 6,
}
AFTER_REACH_INS = {
    "client.calls": 22,
    "client.tasks": 13,
    "_sprint_rows": 12,
    "client.metadata": 8,
    "_transactions": 9,
    "client.comments": 2,
}
EXPECTED_CLASSES = {
    "tests.test_sprints.SprintOwnershipTests": 33,
    "tests.test_sprints.TwoOpenSprintAdmissionTests": 18,
    "tests.test_sprints.TwoOpenSprintIsolationTests": 9,
    "tests.test_sprints.SprintTests": 35,
    "tests.test_sprints.SprintStatusHeadlessCommandTests": 3,
    "tests.test_sprints.SprintAuditTraversalTests": 9,
    "tests.test_sprints.SprintSingleWriterGuardTests": 15,
    "tests.test_sprints.SprintReservedProjectGuardTests": 6,
    "tests.test_sprints.SprintCloseDecisionTests": 19,
    "tests.test_sprints.CloseDecisionFileTests": 3,
    "tests.test_sprint_executors.ExecutorValueTests": 2,
    "tests.test_sprint_executors.SprintExecutorPinTests": 7,
    "tests.test_sprint_executors.ObserverPromptExecutorTests": 3,
    "tests.test_sprint_executors.SprintCardExecutorTests": 5,
    "tests.test_sprint_executors.SprintExecutorRecoveryTests": 4,
    "tests.test_sprint_executors.CardEditExecutorTests": 3,
    "tests.test_sprint_restore.SprintRestoreTests": 26,
    "tests.test_sprint_listing_budget.SprintListingBudgetTests": 4,
}

# These are the complete permitted locations.  Adding a storage reach-in anywhere else fails the
# guard even if a broad aggregate count happens to stay unchanged.
ALLOWED_KANBOARD_ONLY_LOCATIONS = frozenset(KANBOARD_ONLY)


def _methods(module: object) -> dict[str, object]:
    found: dict[str, object] = {}
    for _name, owner in inspect.getmembers(module, inspect.isclass):
        if owner.__module__ != module.__name__:
            continue
        for name, value in owner.__dict__.items():
            if name.startswith("test_") and callable(value):
                found[f"{module.__name__}.{owner.__name__}.{name}"] = value
    return found


def _portable_sql_shadows(module: object) -> list[str]:
    violations: list[str] = []
    for owner in vars(module).values():
        if not inspect.isclass(owner) or owner.__module__ != module.__name__:
            continue
        inherited = owner.__mro__[1:]
        for name, replacement in owner.__dict__.items():
            if not name.startswith("test_"):
                continue
            original = next(
                (
                    base.__dict__[name]
                    for base in inherited
                    if name in base.__dict__ and callable(base.__dict__[name])
                ),
                None,
            )
            if original is None:
                continue
            qualified = f"{original.__module__}.{original.__qualname__}"
            if qualified not in KANBOARD_ONLY:
                action = "overrides" if callable(replacement) else "shadows"
                violations.append(f"{owner.__name__}.{name} {action} portable {qualified}")
    return violations


FORBIDDEN_NAMES = {"SprintKanboard", "ProductSprintKanboard", "ensure_sprint_board"}
FORBIDDEN_RPC = {
    "getProjectByName",
    "getColumns",
    "getAllTasks",
    "getTaskByReference",
    "getTaskMetadata",
    "getAllComments",
    "createProject",
    "createTask",
    "updateTask",
    "saveTaskMetadata",
    "createComment",
    "closeTask",
    "removeTask",
    "moveTaskPosition",
}


def _method_findings(method: object, *, include_rpc: bool = True) -> set[str]:
    body = textwrap.dedent(inspect.getsource(method))
    tree = ast.parse(body)
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)} & FORBIDDEN_NAMES
    rpc = (
        {node.value for node in ast.walk(tree) if isinstance(node, ast.Constant)} & FORBIDDEN_RPC
        if include_rpc
        else set()
    )
    reach_ins = {
        token
        for token in (
            "self.client.calls",
            "self.client.tasks",
            "self.client.metadata",
            "self.client.comments",
            "self._sprint_rows()",
            "self._transactions()",
        )
        if token in body
    }
    storage_attrs = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute) or node.attr not in {
            "calls",
            "tasks",
            "metadata",
            "comments",
        }:
            continue
        owner = node.value
        if (isinstance(owner, ast.Name) and owner.id in {"client", "board", "source", "target"}) or (
            isinstance(owner, ast.Attribute)
            and owner.attr in {"client", "board", "source", "target"}
        ):
            storage_attrs.add(node.attr)
    return names | rpc | reach_ins | storage_attrs


def _owner_of(method: object) -> type:
    module = inspect.getmodule(method)
    if module is None:
        raise AssertionError(f"no module for {method!r}")
    owner_name = method.__qualname__.split(".", 1)[0]  # type: ignore[attr-defined]
    return getattr(module, owner_name)


def _reachable_helpers(owner: type, root: object) -> list[tuple[tuple[str, ...], object]]:
    pending = [((root.__name__,), root)]  # type: ignore[attr-defined]
    suite_module = root.__module__  # type: ignore[attr-defined]
    reached: list[tuple[tuple[str, ...], object]] = []
    seen: set[object] = set()
    while pending:
        path, method = pending.pop()
        if method in seen:
            continue
        seen.add(method)
        reached.append((path, method))
        tree = ast.parse(textwrap.dedent(inspect.getsource(method)))
        helper_names = {
            node.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
        }
        for name in sorted(helper_names):
            helper = getattr(owner, name, None)
            if inspect.isfunction(helper) and helper.__module__ == suite_module:
                pending.append((path + (name,), helper))
    return reached


def _portable_storage_violations(
    methods: dict[str, object], allowed: frozenset[str]
) -> list[str]:
    violations: list[str] = []
    for qualified, method in methods.items():
        if qualified in allowed:
            continue
        owner = _owner_of(method)
        for path, reached in _reachable_helpers(owner, method):
            found = sorted(_method_findings(reached, include_rpc=len(path) == 1))
            if found:
                violations.append(f"{qualified} via {' -> '.join(path)}: {', '.join(found)}")
    return violations


class _HelperBypass:
    def test_portable(self) -> None:
        self._writes()

    def _writes(self) -> object:
        return self.client.calls  # type: ignore[attr-defined]


class SprintFixtureGuards(unittest.TestCase):
    def test_all_204_original_methods_are_classified_once(self) -> None:
        methods = {qualified: value for module in SUITES for qualified, value in _methods(module).items()}
        by_module = {module.__name__: len(_methods(module)) for module in SUITES}
        self.assertEqual(by_module, EXPECTED_METHODS)
        self.assertEqual(len(methods), 204)
        by_class: dict[str, int] = {}
        for qualified in methods:
            owner = qualified.rsplit(".", 1)[0]
            by_class[owner] = by_class.get(owner, 0) + 1
        self.assertEqual(by_class, EXPECTED_CLASSES)
        self.assertEqual(set(KANBOARD_ONLY) - set(methods), set())
        portable = set(methods) - set(KANBOARD_ONLY)
        self.assertFalse(portable & set(KANBOARD_ONLY))
        self.assertEqual(len(portable) + len(KANBOARD_ONLY), 204)
        self.assertEqual((len(portable), len(KANBOARD_ONLY)), (147, 57))
        self.assertTrue(all(reason.strip() for reason in KANBOARD_ONLY.values()))

    def test_saved_before_inventory_is_reproducible(self) -> None:
        roots = Path(__file__).parent
        source = "\n".join(
            (roots / name).read_text(encoding="utf-8")
            for name in (
                "test_sprints.py",
                "test_sprint_executors.py",
                "test_sprint_restore.py",
                "test_sprint_listing_budget.py",
            )
        )
        actual = {
            "client.calls": source.count("self.client.calls"),
            "client.tasks": source.count("self.client.tasks"),
            "_sprint_rows": source.count("self._sprint_rows()"),
            "client.metadata": source.count("self.client.metadata"),
            "_transactions": source.count("self._transactions()"),
            "client.comments": source.count("self.client.comments"),
        }
        # This assertion deliberately records the old report's inventory.  Once the bodies are
        # neutralized, the exact current count is reported by the next test and this historical
        # value remains reviewable here rather than being inferred from a weaker total.
        self.assertLessEqual(sum(actual.values()), sum(BEFORE_REACH_INS.values()))
        self.assertEqual(actual, AFTER_REACH_INS)
        # The saved report and TASK.md call this 118, but their six reproduced category counts
        # add up to 116.  Keep the primary counts, and make the arithmetic discrepancy explicit.
        self.assertEqual(sum(BEFORE_REACH_INS.values()), 116)

    def test_portable_bodies_do_not_reach_into_fake_or_storage_layout(self) -> None:
        methods = {qualified: value for module in SUITES for qualified, value in _methods(module).items()}
        self.assertEqual(_portable_storage_violations(methods, ALLOWED_KANBOARD_ONLY_LOCATIONS), [])

    def test_portable_helper_indirection_cannot_bypass_the_storage_guard(self) -> None:
        qualified = f"{_HelperBypass.__module__}.{_HelperBypass.__qualname__}.test_portable"
        violations = _portable_storage_violations(
            {qualified: _HelperBypass.test_portable}, frozenset()
        )
        self.assertEqual(len(violations), 1)
        self.assertIn("test_portable -> _writes", violations[0])
        self.assertIn("self.client.calls", violations[0])

    def test_kanboard_only_locations_are_exactly_the_named_inventory(self) -> None:
        self.assertEqual(ALLOWED_KANBOARD_ONLY_LOCATIONS, frozenset(KANBOARD_ONLY))

    def test_sql_subclasses_do_not_override_portable_test_bodies(self) -> None:
        from tests import test_sprints_sql_backend as sql

        self.assertEqual(_portable_sql_shadows(sql), [])

    def test_a_non_callable_attribute_cannot_hide_a_portable_sql_test(self) -> None:
        base = type("PortableBase", (), {"test_portable": lambda self: None})
        base.__module__ = "tests.synthetic_shared"
        shadow = type("SqlShadow", (base,), {"test_portable": None})
        shadow.__module__ = "tests.synthetic_sql"
        module = types.SimpleNamespace(__name__="tests.synthetic_sql", SqlShadow=shadow)

        violations = _portable_sql_shadows(module)

        self.assertEqual(len(violations), 1)
        self.assertIn("shadows portable", violations[0])

    def test_portable_fixture_helpers_do_not_reach_into_fake_storage(self) -> None:
        allowed = {"make_sprint_client", "_sprint_rows", "_transactions"}
        violations: list[str] = []
        for name, method in SprintFixture.__dict__.items():
            if name in allowed or not callable(method):
                continue
            body = textwrap.dedent(inspect.getsource(method))
            found = sorted(
                token
                for token in (
                    "self.client.calls",
                    "self.client.tasks",
                    "self.client.metadata",
                    "self.client.comments",
                    "self.client._record",
                )
                if token in body
            )
            if found:
                violations.append(f"{name}: {', '.join(found)}")
        self.assertEqual(violations, [])

    def test_every_backend_dependent_portable_suite_uses_the_one_factory_seam(self) -> None:
        owners = (
            test_sprints.SprintOwnershipTests,
            test_sprints.TwoOpenSprintAdmissionTests,
            test_sprints.TwoOpenSprintIsolationTests,
            test_sprints.SprintTests,
            test_sprints.SprintStatusHeadlessCommandTests,
            test_sprints.SprintAuditTraversalTests,
            test_sprints.SprintSingleWriterGuardTests,
            test_sprints.SprintReservedProjectGuardTests,
            test_sprints.SprintCloseDecisionTests,
            test_sprint_executors.SprintExecutorPinTests,
            test_sprint_executors.SprintCardExecutorTests,
            test_sprint_executors.SprintExecutorRecoveryTests,
            test_sprint_executors.CardEditExecutorTests,
            test_sprint_restore.SprintRestoreTests,
        )
        self.assertTrue(all(issubclass(owner, SprintBackendFixture) for owner in owners))


class SprintFixtureBehaviorTests(SprintFixture):
    def test_named_failure_injects_one_semantic_boundary_failure(self) -> None:
        with self.named_failure("record_create"), self.assertRaises(TaskError) as refused:
            self._create(goal="fixture failure", reference="sprint:fixture-failure")

        self.assertEqual(refused.exception.code, "audit_pending")
        self.assertEqual(self.sprint_record_count("sprint:fixture-failure"), 0)
