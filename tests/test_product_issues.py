from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from secretary.cli import main
from secretary.product_issues import ProductIssueStore
from secretary.tasks import TaskError, TaskWriter
from tests.test_tasks import WriteKanboard


class ProductBoard(WriteKanboard):
    """Kanboard fixture with the Product/Issue layout and real status filtering."""

    def __init__(self) -> None:
        super().__init__()
        self.tasks[0]["id"] = 12

    def call(self, method: str, **params: object) -> object:
        if method == "getColumns":
            return [
                {"id": 1, "title": "Issues"}, {"id": 2, "title": "Ready"},
                {"id": 3, "title": "In progress"}, {"id": 4, "title": "Validate"},
                {"id": 5, "title": "Blocked"}, {"id": 6, "title": "Done"},
            ]
        if method == "getAllTasks":
            self.calls.append((method, params))
            status = params.get("status_id")
            if status == 1:
                return [task for task in self.tasks if int(task.get("is_active", 1) or 0) != 0]
            if status == 0:
                return [task for task in self.tasks if int(task.get("is_active", 1) or 0) == 0]
            if status == 2:
                return list(self.tasks)
        return super().call(method, **params)


class ProductIssueStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        (self.root / "projects").mkdir()
        (self.root / "projects" / "secretary.yaml").write_text("id: secretary\n", encoding="utf-8")
        self.client = ProductBoard()
        self.store = ProductIssueStore(self.client, data_dir=self.root / "data", instance=self.root)

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_product_and_issue_lists_use_complete_set_and_show_audit_history(self) -> None:
        product = self.store.create_product(
            product_id="secretary", projects=["secretary"], title="Secretary", description="",
            actor="po", request_id="product-create",
        )
        self.assertEqual(product["id"], "secretary")
        self.assertEqual([item["id"] for item in self.store.list_products()], ["secretary"])

        issue = self.store.create_issue(
            product="secretary", issue_kind="feature", priority="P2", title="Foundation",
            description="", actor="po", request_id="issue-create",
        )
        self.store.update_priority(
            reference=issue["ref"], priority="P1", reason="urgent", actor="po", request_id="priority",
        )
        self.store.close_issue(
            reference=issue["ref"], reason="resolved", actor="po", request_id="close",
        )

        self.assertEqual(self.store.list_issues(), [])
        self.assertEqual([item["ref"] for item in self.store.list_issues(include_closed=True)], [issue["ref"]])
        shown = self.store.show_issue(issue["ref"])
        self.assertTrue(shown["closed"])
        self.assertEqual(shown["close_reason"], "resolved")
        self.assertIn(
            "[issue:priority]\nurgent\n[product-issue-request:priority]",
            [entry["text"] for entry in shown["history"]["comments"]],
        )
        self.assertEqual(
            [entry["kind"] for entry in shown["history"]["audit"]],
            ["issue_created", "issue_priority_changed", "issue_closed"],
        )
        status_ids = [params.get("status_id") for method, params in self.client.calls if method == "getAllTasks"]
        self.assertIn(2, status_ids)
        self.assertNotIn(0, status_ids)

    def test_issue_needs_all_required_values_and_archive_cannot_bypass_close(self) -> None:
        with self.assertRaises(TaskError) as raised:
            self.store.create_issue(
                product="", issue_kind="feature", priority="P2", title="x", description="", actor="po",
            )
        self.assertEqual(raised.exception.code, "validation")

        self.store.create_product(
            product_id="secretary", projects=["secretary"], title="Secretary", description="", actor="po",
        )
        issue = self.store.create_issue(
            product="secretary", issue_kind="bug", priority="P0", title="Crash", description="", actor="po",
        )
        writer = TaskWriter(self.client, data_dir=self.root / "data")
        with self.assertRaises(TaskError) as raised:
            writer.archive(role="po", actor="po", reference=issue["ref"], reason="bypass")
        self.assertEqual(raised.exception.code, "transition_forbidden")
        self.assertFalse(any(method == "closeTask" for method, _ in self.client.calls))

    def test_issue_close_has_one_terminal_reason_and_audit_event(self) -> None:
        self.store.create_product(
            product_id="secretary", projects=["secretary"], title="Secretary", description="", actor="po",
        )
        issue = self.store.create_issue(
            product="secretary", issue_kind="bug", priority="P0", title="Crash", description="", actor="po",
        )
        self.store.close_issue(reference=issue["ref"], reason="resolved", actor="po", request_id="close")

        with self.assertRaises(TaskError) as raised:
            self.store.close_issue(reference=issue["ref"], reason="invalid", actor="po", request_id="retry")

        self.assertEqual(raised.exception.code, "closed")
        shown = self.store.show_issue(issue["ref"])
        self.assertEqual(shown["close_reason"], "resolved")
        self.assertEqual([event["kind"] for event in shown["history"]["audit"]], ["issue_created", "issue_closed"])

    def test_issue_and_task_column_guards_are_fail_closed(self) -> None:
        self.store.create_product(
            product_id="secretary", projects=["secretary"], title="Secretary", description="", actor="po",
        )
        issue = self.store.create_issue(
            product="secretary", issue_kind="question", priority="P3", title="Question", description="", actor="po",
        )
        writer = TaskWriter(self.client, data_dir=self.root / "data")
        for role in ("po", "dispatcher", "worker", "reviewer", "steward", "retro", "observer"):
            for target in ("ready", "in_progress", "validate", "blocked", "done"):
                with self.subTest(role=role, target=target), self.assertRaises(TaskError) as raised:
                    writer.move(role=role, actor=role, reference=issue["ref"], target=target, reason="")
                self.assertEqual(raised.exception.code, "transition_forbidden")
        with self.assertRaises(TaskError) as raised:
            writer.create(
                role="steward", actor="steward", project="secretary", task_type="research",
                title="Wrong column", target="issues",
            )
        self.assertEqual(raised.exception.code, "transition_forbidden")

    def test_legacy_ideas_require_po_triage_and_mark_the_execution_task(self) -> None:
        self.client.tasks[0]["column_id"] = 1
        self.client.metadata[12] = {}
        writer = TaskWriter(self.client, data_dir=self.root / "data")

        with self.assertRaises(TaskError) as raised:
            writer.move(
                role="steward", actor="steward", reference="secretary-468", target="ready", reason="",
            )
        self.assertEqual(raised.exception.code, "transition_forbidden")
        self.assertNotIn("record_type", self.client.metadata[12])

        writer.move(role="po", actor="po", reference="secretary-468", target="ready", reason="")
        self.assertEqual(self.client.metadata[12]["record_type"], "task")

    def test_unmigrated_ideas_board_requires_the_same_po_triage(self) -> None:
        class LegacyIdeasBoard(ProductBoard):
            def call(self, method: str, **params: object) -> object:
                if method == "getColumns":
                    return [
                        {"id": 1, "title": "Ideas"}, {"id": 2, "title": "Ready"},
                        {"id": 3, "title": "In progress"}, {"id": 4, "title": "Validate"},
                        {"id": 5, "title": "Blocked"}, {"id": 6, "title": "Done"},
                    ]
                return super().call(method, **params)

        client = LegacyIdeasBoard()
        client.tasks[0]["column_id"] = 1
        client.metadata[12] = {}
        writer = TaskWriter(client, data_dir=self.root / "data")

        with self.assertRaises(TaskError) as raised:
            writer.move(
                role="steward", actor="steward", reference="secretary-468", target="ready", reason="",
            )
        self.assertEqual(raised.exception.code, "transition_forbidden")
        self.assertNotIn("record_type", client.metadata[12])

        writer.move(role="po", actor="po", reference="secretary-468", target="ready", reason="")
        self.assertEqual(client.metadata[12]["record_type"], "task")

    def test_missing_issue_arguments_are_structured(self) -> None:
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = main(["issue", "create", "--role", "po"])
        self.assertEqual(code, 2)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(json.loads(errors.getvalue())["error"]["code"], "usage")

    def test_rejected_product_metadata_stays_pending_until_same_request_repairs_it(self) -> None:
        class RejectMetadataOnce(ProductBoard):
            rejected = False

            def call(self, method: str, **params: object) -> object:
                if method == "saveTaskMetadata" and not self.rejected:
                    self.rejected = True
                    self.calls.append((method, params))
                    return False
                return super().call(method, **params)

        store = ProductIssueStore(RejectMetadataOnce(), data_dir=self.root / "data", instance=self.root)
        with self.assertRaises(TaskError) as raised:
            store.create_product(
                product_id="secretary", projects=["secretary"], title="Secretary", description="", actor="po", request_id="product",
            )
        self.assertEqual(raised.exception.code, "audit_pending")
        self.assertEqual(store.audit.events(), [])
        self.assertEqual(store.audit.status(), {"ok": False, "pending": 1})
        self.assertEqual(TaskWriter(store.client, data_dir=self.root / "data").reconcile(), (0, 1))
        product = store.create_product(
            product_id="secretary", projects=["secretary"], title="Secretary", description="", actor="po", request_id="product",
        )
        self.assertEqual(product["id"], "secretary")
        self.assertEqual(store.audit.status(), {"ok": True, "pending": 0})
        self.assertEqual([event["kind"] for event in store.audit.events()], ["product_created"])

    def test_rejected_issue_metadata_stays_pending_until_same_request_repairs_it(self) -> None:
        self.store.create_product(
            product_id="secretary", projects=["secretary"], title="Secretary", description="", actor="po",
        )
        original_call = self.client.call
        rejected = False

        def reject_once(method: str, **params: object) -> object:
            nonlocal rejected
            if method == "saveTaskMetadata" and not rejected:
                rejected = True
                self.client.calls.append((method, params))
                return False
            return original_call(method, **params)

        self.client.call = reject_once  # type: ignore[method-assign]
        with self.assertRaises(TaskError) as raised:
            self.store.create_issue(
                product="secretary", issue_kind="bug", priority="P2", title="Crash", description="", actor="po", request_id="issue",
            )
        self.assertEqual(raised.exception.code, "audit_pending")
        self.assertEqual(self.store.audit.status(), {"ok": False, "pending": 1})
        issue = self.store.create_issue(
            product="secretary", issue_kind="bug", priority="P2", title="Crash", description="", actor="po", request_id="issue",
        )
        self.assertEqual(issue["kind"], "bug")
        self.assertEqual(self.store.audit.status(), {"ok": True, "pending": 0})

    def test_rejected_issue_metadata_does_not_claim_a_priority_or_close_change(self) -> None:
        self.store.create_product(
            product_id="secretary", projects=["secretary"], title="Secretary", description="", actor="po",
        )
        issue = self.store.create_issue(
            product="secretary", issue_kind="bug", priority="P2", title="Crash", description="", actor="po",
        )
        original_call = self.client.call

        def reject_metadata(method: str, **params: object) -> object:
            if method == "saveTaskMetadata":
                self.client.calls.append((method, params))
                return False
            return original_call(method, **params)

        self.client.call = reject_metadata  # type: ignore[method-assign]
        with self.assertRaises(TaskError) as raised:
            self.store.update_priority(reference=issue["ref"], priority="P0", reason="urgent", actor="po")
        self.assertEqual(raised.exception.code, "audit_pending")
        self.assertEqual(self.store.show_issue(issue["ref"])["priority"], "P2")
        with self.assertRaises(TaskError) as raised:
            self.store.close_issue(reference=issue["ref"], reason="resolved", actor="po")
        self.assertEqual(raised.exception.code, "audit_pending")
        shown = self.store.show_issue(issue["ref"])
        self.assertFalse(shown["closed"])
        self.assertIsNone(shown["close_reason"])
        self.assertEqual([event["kind"] for event in shown["history"]["audit"]], ["issue_created"])

    def test_rejected_close_comment_is_repaired_by_same_request(self) -> None:
        self.store.create_product(
            product_id="secretary", projects=["secretary"], title="Secretary", description="", actor="po",
        )
        issue = self.store.create_issue(
            product="secretary", issue_kind="bug", priority="P2", title="Crash", description="", actor="po",
        )
        original_call = self.client.call
        rejected = False

        def reject_once(method: str, **params: object) -> object:
            nonlocal rejected
            if method == "createComment" and not rejected:
                rejected = True
                self.client.calls.append((method, params))
                return 0
            return original_call(method, **params)

        self.client.call = reject_once  # type: ignore[method-assign]
        with self.assertRaises(TaskError) as raised:
            self.store.close_issue(reference=issue["ref"], reason="resolved", actor="po", request_id="close")
        self.assertEqual(raised.exception.code, "audit_pending")
        self.assertEqual(self.store.audit.status(), {"ok": False, "pending": 1})
        self.assertFalse(self.store.show_issue(issue["ref"])["closed"])
        closed = self.store.close_issue(reference=issue["ref"], reason="resolved", actor="po", request_id="close")
        self.assertTrue(closed["closed"])
        self.assertEqual(closed["close_reason"], "resolved")
        self.assertEqual(self.store.audit.status(), {"ok": True, "pending": 0})

    def test_rejected_priority_comment_keeps_auditable_backend_change(self) -> None:
        self.store.create_product(
            product_id="secretary", projects=["secretary"], title="Secretary", description="", actor="po",
        )
        issue = self.store.create_issue(
            product="secretary", issue_kind="bug", priority="P2", title="Crash", description="", actor="po",
        )
        original_call = self.client.call

        def reject_comment(method: str, **params: object) -> object:
            if method == "createComment":
                self.client.calls.append((method, params))
                return 0
            return original_call(method, **params)

        self.client.call = reject_comment  # type: ignore[method-assign]
        with self.assertRaises(TaskError) as raised:
            self.store.update_priority(reference=issue["ref"], priority="P0", reason="urgent", actor="po")
        self.assertEqual(raised.exception.code, "audit_pending")
        shown = self.store.show_issue(issue["ref"])
        self.assertEqual(shown["priority"], "P2")
        self.assertEqual([event["kind"] for event in shown["history"]["audit"]], ["issue_created"])

    def test_reused_request_id_rejects_a_different_payload_before_writes(self) -> None:
        self.store.create_product(
            product_id="secretary", projects=["secretary"], title="Secretary", description="",
            actor="po", request_id="same-request",
        )
        writes = len(self.client.calls)
        with self.assertRaises(TaskError) as raised:
            self.store.create_product(
                product_id="secretary", projects=["secretary"], title="Different", description="",
                actor="po", request_id="same-request",
            )
        self.assertEqual(raised.exception.code, "validation")
        self.assertEqual(len(self.client.calls), writes)

    def test_product_retry_precedes_mutable_registry_validation(self) -> None:
        product = self.store.create_product(
            product_id="secretary", projects=["secretary"], title="Secretary", description="",
            actor="po", request_id="completed-registry-retry",
        )
        (self.root / "projects" / "secretary.yaml").unlink()
        replayed = self.store.create_product(
            product_id="secretary", projects=["secretary"], title="Secretary", description="",
            actor="po", request_id="completed-registry-retry",
        )
        self.assertEqual(replayed, product)
        self.assertEqual(
            len([event for event in self.store.audit.events() if event["request_id"] == "completed-registry-retry"]),
            1,
        )

        (self.root / "projects" / "recovery.yaml").write_text("id: recovery\n", encoding="utf-8")
        original = self.client.call
        rejected = False

        def reject_metadata_once(method: str, **params: object) -> object:
            nonlocal rejected
            if method == "saveTaskMetadata" and not rejected:
                rejected = True
                return False
            return original(method, **params)

        self.client.call = reject_metadata_once  # type: ignore[method-assign]
        with self.assertRaises(TaskError) as raised:
            self.store.create_product(
                product_id="recovery", projects=["recovery"], title="Recovery", description="",
                actor="po", request_id="pending-registry-retry",
            )
        self.assertEqual(raised.exception.code, "audit_pending")
        (self.root / "projects" / "recovery.yaml").unlink()
        self.client.call = original  # type: ignore[method-assign]

        recovered = self.store.create_product(
            product_id="recovery", projects=["recovery"], title="Recovery", description="",
            actor="po", request_id="pending-registry-retry",
        )
        self.assertEqual(recovered["id"], "recovery")
        self.assertEqual(self.store.audit.status(), {"ok": True, "pending": 0})

    def test_retries_after_interruption_at_each_operation_boundary(self) -> None:
        def interrupt_after(method_name: str):
            original = self.client.call
            interrupted = False

            def call(method: str, **params: object) -> object:
                nonlocal interrupted
                result = original(method, **params)
                if method == method_name and not interrupted:
                    interrupted = True
                    raise TaskError("backend_unavailable", "reply lost", 1)
                return result

            self.client.call = call  # type: ignore[method-assign]
            return original

        original = interrupt_after("updateTask")
        with self.assertRaises(TaskError) as raised:
            self.store.create_product(
                product_id="secretary", projects=["secretary"], title="Secretary", description="",
                actor="po", request_id="product-interrupt",
            )
        self.assertEqual(raised.exception.code, "audit_pending")
        self.client.call = original  # type: ignore[method-assign]
        restarted = ProductIssueStore(self.client, data_dir=self.root / "data", instance=self.root)
        restarted.create_product(
            product_id="secretary", projects=["secretary"], title="Secretary", description="",
            actor="po", request_id="product-interrupt",
        )

        original = interrupt_after("createTask")
        with self.assertRaises(TaskError) as raised:
            restarted.create_issue(
                product="secretary", issue_kind="bug", priority="P2", title="Crash", description="",
                actor="po", request_id="issue-interrupt",
            )
        self.assertEqual(raised.exception.code, "audit_pending")
        self.client.call = original  # type: ignore[method-assign]
        issue = ProductIssueStore(self.client, data_dir=self.root / "data", instance=self.root).create_issue(
            product="secretary", issue_kind="bug", priority="P2", title="Crash", description="",
            actor="po", request_id="issue-interrupt",
        )

        original = interrupt_after("createComment")
        with self.assertRaises(TaskError) as raised:
            restarted.update_priority(
                reference=issue["ref"], priority="P1", reason="urgent", actor="po", request_id="priority-interrupt",
            )
        self.assertEqual(raised.exception.code, "audit_pending")
        self.client.call = original  # type: ignore[method-assign]
        restarted.update_priority(
            reference=issue["ref"], priority="P1", reason="urgent", actor="po", request_id="priority-interrupt",
        )

        original = interrupt_after("closeTask")
        with self.assertRaises(TaskError) as raised:
            restarted.close_issue(
                reference=issue["ref"], reason="resolved", actor="po", request_id="close-interrupt",
            )
        self.assertEqual(raised.exception.code, "audit_pending")
        self.client.call = original  # type: ignore[method-assign]
        closed = restarted.close_issue(
            reference=issue["ref"], reason="resolved", actor="po", request_id="close-interrupt",
        )
        self.assertTrue(closed["closed"])
        events = [event for event in restarted.audit.events() if event["request_id"].endswith("interrupt")]
        self.assertEqual(len(events), 4)
        self.assertEqual(len(self.client.comments[int(issue["ref"].split(":")[1])]), 2)

    def test_generic_reconcile_does_not_finalize_priority_intent(self) -> None:
        self.store.create_product(
            product_id="secretary", projects=["secretary"], title="Secretary", description="", actor="po",
        )
        issue = self.store.create_issue(
            product="secretary", issue_kind="bug", priority="P2", title="Crash", description="", actor="po",
        )
        original = self.client.call

        def reject_comment(method: str, **params: object) -> object:
            if method == "createComment":
                return 0
            return original(method, **params)

        self.client.call = reject_comment  # type: ignore[method-assign]
        with self.assertRaises(TaskError):
            self.store.update_priority(reference=issue["ref"], priority="P1", reason="urgent", actor="po", request_id="pending-priority")
        self.assertEqual(self.store.audit.reconcile(), (0, 1))
        self.assertEqual(TaskWriter(self.client, data_dir=self.root / "data").reconcile(), (0, 1))
        self.assertIsNotNone(self.store.audit.pending_event("pending-priority"))

    def test_concurrent_conflicting_request_id_is_rejected_before_backend_writes(self) -> None:
        first = ProductIssueStore(self.client, data_dir=self.root / "data", instance=self.root)
        second = ProductIssueStore(self.client, data_dir=self.root / "data", instance=self.root)
        claim = first.audit.claim
        barrier = threading.Barrier(2)
        results: list[object] = []

        def synchronized_claim(request_id: str, event: dict) -> tuple[dict, bool, bool]:
            barrier.wait(timeout=5)
            return claim(request_id, event)

        first.audit.claim = synchronized_claim  # type: ignore[method-assign]
        second.audit.claim = synchronized_claim  # type: ignore[method-assign]

        def create(store: ProductIssueStore, title: str) -> None:
            try:
                results.append(store.create_product(
                    product_id="secretary", projects=["secretary"], title=title, description="",
                    actor="po", request_id="concurrent-request",
                ))
            except TaskError as exc:
                results.append(exc)

        threads = [
            threading.Thread(target=create, args=(first, "First")),
            threading.Thread(target=create, args=(second, "Second")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(len([result for result in results if isinstance(result, dict)]), 1)
        errors = [result for result in results if isinstance(result, TaskError)]
        self.assertEqual([error.code for error in errors], ["validation"])
        self.assertEqual(len([call for call in self.client.calls if call[0] == "createTask"]), 1)
        self.assertEqual(
            len([event for event in first.audit.events() if event["request_id"] == "concurrent-request"]), 1,
        )

    def test_audit_append_failure_retries_without_a_second_comment_or_event(self) -> None:
        self.store.create_product(
            product_id="secretary", projects=["secretary"], title="Secretary", description="", actor="po",
        )
        issue = self.store.create_issue(
            product="secretary", issue_kind="bug", priority="P2", title="Crash", description="", actor="po",
        )
        append = self.store.audit.append
        failed = False

        def fail_once(request_id: str, event: dict) -> str:
            nonlocal failed
            if not failed:
                failed = True
                raise OSError("audit unavailable")
            return append(request_id, event)

        self.store.audit.append = fail_once  # type: ignore[method-assign]
        with self.assertRaises(TaskError) as raised:
            self.store.update_priority(
                reference=issue["ref"], priority="P1", reason="urgent", actor="po", request_id="append-failure",
            )
        self.assertEqual(raised.exception.code, "audit_pending")
        self.store.audit.append = append  # type: ignore[method-assign]
        self.store.update_priority(
            reference=issue["ref"], priority="P1", reason="urgent", actor="po", request_id="append-failure",
        )
        task_id = int(issue["ref"].split(":")[1])
        self.assertEqual(
            [comment["comment"] for comment in self.client.comments[task_id]].count(
                "[issue:priority]\nurgent\n[product-issue-request:append-failure]"
            ),
            1,
        )
        self.assertEqual(len([event for event in self.store.audit.events() if event["request_id"] == "append-failure"]), 1)

    def test_retry_cleans_product_issue_pending_after_audit_append_commits(self) -> None:
        self.store.create_product(
            product_id="secretary", projects=["secretary"], title="Secretary", description="", actor="po",
        )
        issue = self.store.create_issue(
            product="secretary", issue_kind="bug", priority="P2", title="Crash", description="", actor="po",
        )
        original_unlink = os.unlink
        failed = False

        def fail_pending_unlink(path: str, *args: object, **kwargs: object) -> None:
            nonlocal failed
            if path.endswith("pending-audit/post-commit-cleanup.json") and not failed:
                failed = True
                raise OSError("pending cleanup interrupted")
            original_unlink(path, *args, **kwargs)

        with mock.patch("secretary.tasks.os.unlink", side_effect=fail_pending_unlink):
            with self.assertRaises(TaskError) as raised:
                self.store.update_priority(
                    reference=issue["ref"], priority="P1", reason="urgent", actor="po",
                    request_id="post-commit-cleanup",
                )
        self.assertEqual(raised.exception.code, "audit_pending")
        self.assertEqual(self.store.audit.status(), {"ok": False, "pending": 1})
        self.assertEqual(
            len([event for event in self.store.audit.events() if event["request_id"] == "post-commit-cleanup"]),
            1,
        )

        restarted = ProductIssueStore(self.client, data_dir=self.root / "data", instance=self.root)
        repaired = restarted.update_priority(
            reference=issue["ref"], priority="P1", reason="urgent", actor="po",
            request_id="post-commit-cleanup",
        )
        self.assertEqual(repaired["priority"], "P1")
        self.assertEqual(restarted.audit.status(), {"ok": True, "pending": 0})
        self.assertEqual(
            len([event for event in restarted.audit.events() if event["request_id"] == "post-commit-cleanup"]),
            1,
        )

    def test_same_reason_priority_requests_write_distinct_comments(self) -> None:
        self.store.create_product(
            product_id="secretary", projects=["secretary"], title="Secretary", description="", actor="po",
        )
        issue = self.store.create_issue(
            product="secretary", issue_kind="bug", priority="P2", title="Crash", description="", actor="po",
        )
        self.store.update_priority(
            reference=issue["ref"], priority="P1", reason="urgent", actor="po", request_id="priority-one",
        )
        self.store.update_priority(
            reference=issue["ref"], priority="P0", reason="urgent", actor="po", request_id="priority-two",
        )
        task_id = int(issue["ref"].split(":")[1])
        self.assertEqual(
            [comment["comment"] for comment in self.client.comments[task_id]],
            [
                "[issue:priority]\nurgent\n[product-issue-request:priority-one]",
                "[issue:priority]\nurgent\n[product-issue-request:priority-two]",
            ],
        )

    def test_every_product_issue_write_boundary_is_repairable(self) -> None:
        """False replies, exceptions and lost replies all leave one replayable intent."""
        boundaries = {
            "product": ("createTask", "updateTask", "saveTaskMetadata", "audit"),
            "issue": ("createTask", "updateTask", "saveTaskMetadata", "audit"),
            "priority": ("createComment", "saveTaskMetadata", "audit"),
            "close": ("createComment", "saveTaskMetadata", "closeTask", "audit"),
        }

        def operation(name: str, number: int, request_id: str):
            product_id = f"product-{number}"
            if name == "product":
                return lambda store: store.create_product(
                    product_id=product_id, projects=["secretary"], title="Product", description="",
                    actor="po", request_id=request_id,
                )
            self.store.create_product(
                product_id=product_id, projects=["secretary"], title="Product", description="", actor="po",
            )
            issue = self.store.create_issue(
                product=product_id, issue_kind="bug", priority="P2", title="Crash", description="", actor="po",
            )
            if name == "issue":
                return lambda store: store.create_issue(
                    product=product_id, issue_kind="feature", priority="P2", title="Feature", description="",
                    actor="po", request_id=request_id,
                )
            if name == "priority":
                return lambda store: store.update_priority(
                    reference=issue["ref"], priority="P1", reason="urgent", actor="po", request_id=request_id,
                )
            return lambda store: store.close_issue(
                reference=issue["ref"], reason="resolved", actor="po", request_id=request_id,
            )

        number = 0
        for name, methods in boundaries.items():
            for method in methods:
                for mode in ("false", "exception", "interruption"):
                    number += 1
                    request_id = f"{name}-{method}-{mode}"
                    invoke = operation(name, number, request_id)
                    with self.subTest(operation=name, boundary=method, mode=mode):
                        if method == "audit":
                            original_append = self.store.audit.append

                            def append(request: str, event: dict, *, mode: str = mode) -> object:
                                if mode == "false":
                                    return False
                                if mode == "exception":
                                    raise OSError("audit unavailable")
                                original_append(request, event)
                                raise OSError("audit reply lost")

                            self.store.audit.append = append  # type: ignore[method-assign]
                        else:
                            original_call = self.client.call

                            def call(write: str, **params: object) -> object:
                                if write != method:
                                    return original_call(write, **params)
                                if mode == "false":
                                    return 0 if write == "createComment" else False
                                if mode == "exception":
                                    raise TaskError("backend_unavailable", "backend unavailable", 1)
                                original_call(write, **params)
                                raise TaskError("backend_unavailable", "reply lost", 1)

                            self.client.call = call  # type: ignore[method-assign]
                        try:
                            with self.assertRaises(TaskError) as raised:
                                invoke(self.store)
                            self.assertEqual(raised.exception.code, "audit_pending")
                        finally:
                            if method == "audit":
                                self.store.audit.append = original_append  # type: ignore[method-assign]
                            else:
                                self.client.call = original_call  # type: ignore[method-assign]
                        restarted = ProductIssueStore(self.client, data_dir=self.root / "data", instance=self.root)
                        invoke(restarted)
                        self.assertIsNone(restarted.audit.pending_event(request_id))
                        self.assertEqual(
                            len([event for event in restarted.audit.events() if event["request_id"] == request_id]),
                            1,
                        )


if __name__ == "__main__":
    unittest.main()
