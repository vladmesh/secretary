from __future__ import annotations

import ast
import inspect
import textwrap
import unittest

from tests import test_product_issues as product_issue_tests

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
        forbidden = {"tasks", "metadata", "comments", "swimlanes", "calls", "call"}
        portable = {
            product_issue_tests.ProductIssueStoreTests: product_issue_tests.ProductIssueStoreTests.PORTABLE_CONTRACT,
            product_issue_tests.ProductIssueSwimlaneTests: product_issue_tests.ProductIssueSwimlaneTests.PORTABLE_CONTRACT,
        }
        violations: list[str] = []
        for owner, names in portable.items():
            for name in sorted(names):
                tree = ast.parse(textwrap.dedent(inspect.getsource(getattr(owner, name))))
                attrs = sorted(
                    {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)} & forbidden
                )
                if attrs:
                    violations.append(f"{owner.__name__}.{name}: {', '.join(attrs)}")
        self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()
