from __future__ import annotations

import ast
import inspect
import textwrap
import unittest

from secretary.tasks import TaskError
from tests import test_product_issues as product_issue_tests
from tests.product_issue_fixtures import ProductIssueFixture

SWIMLANE_METHODS = {
    name
    for name, value in product_issue_tests.ProductIssueSwimlaneTests.__dict__.items()
    if name.startswith("test_") and callable(value)
}
STORE_METHODS = {
    name
    for name, value in product_issue_tests.ProductIssueStoreTests.__dict__.items()
    if name.startswith("test_") and callable(value)
}


class ProductIssueFixtureGuards(unittest.TestCase):
    def test_all_original_methods_are_classified_once(self) -> None:
        self.assertEqual(len(SWIMLANE_METHODS), 16)
        self.assertEqual(len(STORE_METHODS), 30)
        self.assertEqual(
            set(product_issue_tests.ProductIssueSwimlaneTests.KANBOARD_ONLY)
            | product_issue_tests.ProductIssueSwimlaneTests.PORTABLE_CONTRACT,
            SWIMLANE_METHODS,
        )
        self.assertFalse(
            set(product_issue_tests.ProductIssueSwimlaneTests.KANBOARD_ONLY)
            & product_issue_tests.ProductIssueSwimlaneTests.PORTABLE_CONTRACT
        )
        self.assertEqual(
            set(product_issue_tests.ProductIssueStoreTests.KANBOARD_ONLY)
            | product_issue_tests.ProductIssueStoreTests.PORTABLE_CONTRACT,
            STORE_METHODS,
        )
        self.assertFalse(
            set(product_issue_tests.ProductIssueStoreTests.KANBOARD_ONLY)
            & product_issue_tests.ProductIssueStoreTests.PORTABLE_CONTRACT
        )
        portable = product_issue_tests.ProductIssueStoreTests.PORTABLE_CONTRACT
        self.assertEqual(len(portable), 14)
        self.assertEqual(len(product_issue_tests.ProductIssueSwimlaneTests.PORTABLE_CONTRACT), 5)
        self.assertEqual(len(SWIMLANE_METHODS | STORE_METHODS), 46)

    def test_portable_bodies_do_not_reach_into_the_kanboard_fake(self) -> None:
        forbidden_attrs = {
            "tasks",
            "metadata",
            "comments",
            "swimlanes",
            "calls",
            "call",
            "lane_store",
            "lane_binding_for",
            "_store",
            "_created",
            "_lane_names",
        }
        forbidden_names = {"ProductBoard", "LiveSwimlaneBoard", "NoSwimlaneBoard", "client"}
        forbidden_rpc = {
            "getTaskByReference",
            "getActiveSwimlanes",
            "getAllTasks",
            "createTask",
            "updateTask",
            "saveTaskMetadata",
            "createComment",
            "closeTask",
            "addSwimlane",
        }
        portable = {
            product_issue_tests.ProductIssueStoreTests: product_issue_tests.ProductIssueStoreTests.PORTABLE_CONTRACT,
            product_issue_tests.ProductIssueSwimlaneTests: product_issue_tests.ProductIssueSwimlaneTests.PORTABLE_CONTRACT,
        }
        violations: list[str] = []
        for owner, names in portable.items():
            for name in sorted(names):
                tree = ast.parse(textwrap.dedent(inspect.getsource(getattr(owner, name))))
                attrs = {
                    node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
                } & forbidden_attrs
                names_used = {
                    node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
                } & forbidden_names
                rpc_used = {
                    node.value for node in ast.walk(tree) if isinstance(node, ast.Constant)
                } & forbidden_rpc
                found = sorted(attrs | names_used | rpc_used)
                if found:
                    violations.append(f"{owner.__name__}.{name}: {', '.join(found)}")
        self.assertEqual(violations, [])


class ProductIssueFixtureBehaviorTests(ProductIssueFixture, unittest.TestCase):
    def test_absence_rejects_a_persisted_record_without_product_metadata(self) -> None:
        self.client.call(
            "createTask",
            project_id=1,
            title="Partial product",
            description="",
            column_id=1,
            swimlane_id=4,
            reference="product:secretary",
        )

        self.assertEqual(self.record_count("product:secretary"), 1)
        with self.assertRaises(TaskError) as malformed:
            self.product("secretary")
        self.assertEqual(malformed.exception.code, "validation")
        with self.assertRaises(AssertionError):
            self.assert_product_absent("secretary")

    def test_named_failure_injects_one_supported_boundary_failure(self) -> None:
        with self.named_failure("record_create"), self.assertRaises(TaskError) as refused:
            self.create_product(
                product_id="secretary",
                projects=["secretary"],
                title="Secretary",
                description="",
                actor="po",
                request_id="fixture-failure",
            )

        self.assertEqual(refused.exception.code, "backend_rejected")
        self.assertEqual(self.record_count("product:secretary"), 0)


if __name__ == "__main__":
    unittest.main()
