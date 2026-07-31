from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from secretary.cli import main
from secretary.product_issues import ProductIssueStore
from secretary.tasks import TaskAudit, TaskError, TaskWriter
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
            return []
        return super().call(method, **params)


class LiveSwimlaneBoard(ProductBoard):
    """The live Pipeline layout: named project lanes, no default lane.

    Kanboard refuses a create into a lane the board does not have and answers that refusal with
    `false` instead of an error, which is what the live board does for `swimlane_id=0`.
    """

    LANES = [
        {"id": 9, "name": "service-template", "position": 3},
        {"id": 4, "name": "secretary", "position": 1},
        {"id": 7, "name": "codegen-orchestrator", "position": 2},
    ]

    def call(self, method: str, **params: object) -> object:
        if method == "getActiveSwimlanes":
            return [dict(lane) for lane in self.LANES]
        if method == "createTask" and params.get("swimlane_id") not in {lane["id"] for lane in self.LANES}:
            self.calls.append((method, params))
            return False
        return super().call(method, **params)


class NoSwimlaneBoard(ProductBoard):
    """A board with no swimlane at all, the layout the earlier fixtures describe."""

    def call(self, method: str, **params: object) -> object:
        if method == "getActiveSwimlanes":
            return []
        return super().call(method, **params)


class ProductIssueSwimlaneTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        (self.root / "projects").mkdir()
        (self.root / "projects" / "secretary.yaml").write_text("id: secretary\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def _store(self, client) -> ProductIssueStore:
        return ProductIssueStore(client, data_dir=self.root / "data", instance=self.root)

    def _created(self, client, reference: str) -> dict:
        return next(task for task in client.tasks if task.get("reference") == reference)

    def test_named_swimlanes_without_a_default_take_the_first_lane_in_board_order(self) -> None:
        client = LiveSwimlaneBoard()
        store = self._store(client)

        product = store.create_product(
            product_id="secretary", projects=["secretary"], title="Secretary", description="",
            actor="po", request_id="live-product",
        )
        issue = store.create_issue(
            product="secretary", issue_kind="bug", priority="P1", title="Crash", description="",
            actor="po", request_id="live-issue",
        )

        self.assertEqual(product["id"], "secretary")
        # Lane 4 is first by position, not by id or by list order, and both records take it.
        lanes = [params["swimlane_id"] for method, params in client.calls if method == "createTask"]
        self.assertEqual(lanes, [4, 4])
        self.assertEqual(self._created(client, "product:secretary")["swimlane_id"], 4)
        self.assertEqual(self._created(client, issue["ref"])["swimlane_id"], 4)
        self.assertEqual(store.transactions.status(), {"ok": True, "pending": 0})

    def test_board_without_swimlanes_keeps_the_implicit_default_lane(self) -> None:
        client = NoSwimlaneBoard()
        store = self._store(client)

        store.create_product(
            product_id="secretary", projects=["secretary"], title="Secretary", description="",
            actor="po", request_id="plain-product",
        )

        lanes = [params["swimlane_id"] for method, params in client.calls if method == "createTask"]
        self.assertEqual(lanes, [0])
        self.assertEqual(store.transactions.status(), {"ok": True, "pending": 0})

    def test_refused_create_is_terminal_and_leaves_no_transaction_behind(self) -> None:
        class RefusingBoard(ProductBoard):
            def call(self, method: str, **params: object) -> object:
                if method == "createTask":
                    self.calls.append((method, params))
                    return False
                return super().call(method, **params)

        client = RefusingBoard()
        store = self._store(client)

        for attempt in ("first", "second"):
            with self.subTest(attempt=attempt), self.assertRaises(TaskError) as raised:
                store.create_product(
                    product_id="secretary", projects=["secretary"], title="Secretary", description="",
                    actor="po", request_id="refused",
                )
            self.assertEqual(raised.exception.code, "backend_rejected")

        self.assertEqual(store.transactions.status(), {"ok": True, "pending": 0})
        self.assertEqual(store.list_transactions(), [])
        self.assertEqual(store.audit.events(), [])
        self.assertEqual(store.audit.status(), {"ok": True, "pending": 0})

    def test_staged_transaction_without_a_backend_row_is_discarded_by_the_operator(self) -> None:
        client = LiveSwimlaneBoard()
        store = self._store(client)
        intent = {
            "record_type": "product", "product_id": "secretary", "product_projects": '["secretary"]',
            "title": "Secretary", "description": "", "actor": "po",
        }
        event = store._transaction_event(
            kind="product_created", actor="po", reference="product:secretary",
            request_id="stuck", intent=intent,
        )
        document, _ = store.transactions.begin("stuck", kind="product_created", intent=intent, event=event)
        document["progress"] = {"create_started": True}
        store.transactions.save(document)
        self.assertEqual(store.transactions.status(), {"ok": False, "pending": 1})
        self.assertEqual(
            store.list_transactions(),
            [{"request_id": "stuck", "kind": "product_created", "ref": "product:secretary",
              "progress": ["create_started"]}],
        )

        self.assertEqual(store.discard_transaction("stuck"), {"request_id": "stuck", "discarded": True})

        self.assertEqual(store.transactions.status(), {"ok": True, "pending": 0})
        self.assertEqual(store.audit.events(), [])

    def test_discard_refuses_a_transaction_that_already_wrote_to_the_board(self) -> None:
        client = LiveSwimlaneBoard()
        store = self._store(client)
        original_call = client.call
        rejected = False

        def reject_metadata_once(method: str, **params: object) -> object:
            nonlocal rejected
            if method == "saveTaskMetadata" and not rejected:
                rejected = True
                client.calls.append((method, params))
                return False
            return original_call(method, **params)

        client.call = reject_metadata_once  # type: ignore[method-assign]
        with self.assertRaises(TaskError) as raised:
            store.create_product(
                product_id="secretary", projects=["secretary"], title="Secretary", description="",
                actor="po", request_id="half-written",
            )
        self.assertEqual(raised.exception.code, "audit_pending")

        with self.assertRaises(TaskError) as refused:
            store.discard_transaction("half-written")
        self.assertEqual(refused.exception.code, "live_write")
        self.assertEqual(store.transactions.status(), {"ok": False, "pending": 1})

        product = store.retry_transaction("half-written")

        self.assertEqual(product["id"], "secretary")
        self.assertEqual(store.transactions.status(), {"ok": True, "pending": 0})
        self.assertEqual([entry["kind"] for entry in store.audit.events()], ["product_created"])

    def test_quarantined_document_returns_through_adopt_and_retry(self) -> None:
        client = LiveSwimlaneBoard()
        store = self._store(client)
        intent = {
            "record_type": "product", "product_id": "secretary", "product_projects": '["secretary"]',
            "title": "Secretary", "description": "", "actor": "po",
        }
        event = store._transaction_event(
            kind="product_created", actor="po", reference="product:secretary",
            request_id="quarantined", intent=intent,
        )
        store.transactions.begin("quarantined", kind="product_created", intent=intent, event=event)
        staged = next((self.root / "data" / "board" / "product-issue-transactions").glob("v1-*.json"))
        quarantine = self.root / "quarantine"
        quarantine.mkdir()
        carried = quarantine / staged.name
        carried.write_text(staged.read_text(encoding="utf-8"), encoding="utf-8")
        staged.unlink()

        with mock.patch("secretary.product_issue_commands.KanboardClient", return_value=client):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                adopted = main([
                    "product", "transaction", "adopt", "--instance", str(self.root),
                    "--data-dir", str(self.root / "data"), "--path", str(carried),
                ])
            self.assertEqual(adopted, 0)
            self.assertEqual(json.loads(output.getvalue())["request_id"], "quarantined")
            self.assertFalse(carried.exists())

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                retried = main([
                    "product", "transaction", "retry", "--instance", str(self.root),
                    "--data-dir", str(self.root / "data"), "--request-id", "quarantined",
                ])
            self.assertEqual(retried, 0)
            self.assertEqual(json.loads(output.getvalue())["id"], "secretary")

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main([
                    "product", "transaction", "list", "--instance", str(self.root),
                    "--data-dir", str(self.root / "data"),
                ]), 0)
            self.assertEqual(json.loads(output.getvalue()), [])
        self.assertEqual(store.transactions.status(), {"ok": True, "pending": 0})
        self.assertEqual([entry["kind"] for entry in store.audit.events()], ["product_created"])


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
        self.assertIn("[issue:priority]\nurgent\n[request-id:priority]", [entry["text"] for entry in shown["history"]["comments"]])
        self.assertEqual(
            [entry["kind"] for entry in shown["history"]["audit"]],
            ["issue_created", "issue_priority_changed", "issue_closed"],
        )
        status_ids = [params.get("status_id") for method, params in self.client.calls if method == "getAllTasks"]
        self.assertIn(1, status_ids)
        self.assertIn(0, status_ids)
        self.assertNotIn(2, status_ids)

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

    def test_claim_rejects_a_product_or_issue_record_without_any_write(self) -> None:
        # A record dragged into Ready by hand still is not an execution task: claim has to reject
        # it the way move does, before the claim metadata, the move and the audit row.
        writer = TaskWriter(self.client, data_dir=self.root / "data")
        for record_type in ("product", "issue"):
            with self.subTest(record_type=record_type):
                self.client.tasks[0]["column_id"] = 2
                self.client.metadata[12] = {
                    "record_type": record_type, "project": "secretary", "task_type": "code", "claim": "",
                }
                self.client.calls.clear()

                with self.assertRaises(TaskError) as raised:
                    writer.claim(
                        role="dispatcher", actor="d", reference="secretary-468",
                        worker="secretary-468-runtime", request_id=f"claim-{record_type}",
                    )

                self.assertEqual(raised.exception.code, "transition_forbidden")
                self.assertIn("cannot enter execution task columns", str(raised.exception))
                self.assertEqual(self.client.metadata[12]["claim"], "")
                self.assertFalse(any(
                    method in {"saveTaskMetadata", "moveTaskPosition"} for method, _ in self.client.calls
                ))
                self.assertEqual(writer.audit.status(), {"ok": True, "pending": 0})
                self.assertFalse(Path(writer.audit.events_path).exists())

    def test_pending_claim_replay_and_reconcile_refuse_a_product_or_issue(self) -> None:
        # The supported partial-write state of a generic claim: the claim metadata committed and
        # the column move was lost. Neither the retry with the same request id nor reconcile may
        # finish that move once the card is a Product or an Issue — both replay through
        # _finish_pending_claim, which never reaches the claim mutation.
        for record_type in ("product", "issue"):
            with self.subTest(record_type=record_type):
                writer = TaskWriter(self.client, data_dir=self.root / f"data-{record_type}")
                self.client.tasks[0]["column_id"] = 2
                self.client.metadata[12] = {"project": "secretary", "task_type": "code", "claim": ""}
                self.client.fail_move = True
                request_id = f"pending-claim-{record_type}"
                claim = dict(
                    role="dispatcher", actor="d", reference="secretary-468",
                    worker="replayed-worker", request_id=request_id,
                )
                with self.assertRaisesRegex(TaskError, "audit repair"):
                    writer.claim(**claim)
                self.assertEqual(writer.audit.status(), {"ok": False, "pending": 1})

                self.client.fail_move = False
                self.client.metadata[12]["record_type"] = record_type
                self.client.calls.clear()

                with self.assertRaises(TaskError) as raised:
                    writer.claim(**claim)

                self.assertEqual(raised.exception.code, "transition_forbidden")
                self.assertEqual(writer.reader.show("secretary-468")["state"], "ready")
                self.assertFalse(any(method == "moveTaskPosition" for method, _ in self.client.calls))
                self.assertEqual(writer.reconcile(), (0, 1))
                self.assertEqual(writer.reader.show("secretary-468")["state"], "ready")
                self.assertEqual(writer.audit.status(), {"ok": False, "pending": 1})
        self.client.fail_move = False

    def test_a_card_in_issues_reaches_ready_only_through_the_po(self) -> None:
        """Issues is the untriaged backlog: the steward matrix has no exit from it."""
        self.client.tasks[0]["column_id"] = 1
        self.client.metadata[12] = {"record_type": "task"}
        writer = TaskWriter(self.client, data_dir=self.root / "data")

        with self.assertRaises(TaskError) as raised:
            writer.move(
                role="steward", actor="steward", reference="secretary-468", target="ready", reason="",
            )
        self.assertEqual(raised.exception.code, "transition_forbidden")
        self.assertEqual(writer.reader.show("secretary-468")["state"], "issues")

        writer.move(role="po", actor="po", reference="secretary-468", target="ready", reason="")
        self.assertEqual(writer.reader.show("secretary-468")["state"], "ready")

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
        self.assertEqual(store.audit.status(), {"ok": True, "pending": 0})
        self.assertEqual(TaskWriter(store.client, data_dir=self.root / "data").reconcile(), (0, 0))
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
        self.assertEqual(self.store.audit.status(), {"ok": True, "pending": 0})
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
        self.assertEqual(self.store.audit.status(), {"ok": True, "pending": 0})
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

    def test_all_operations_restart_without_duplicate_backend_writes(self) -> None:
        class LoseReplyOnce(ProductBoard):
            lost: set[str] = set()

            def call(self, method: str, **params: object) -> object:
                result = super().call(method, **params)
                if method in {"saveTaskMetadata", "createComment", "closeTask"} and method not in self.lost:
                    self.lost.add(method)
                    raise TaskError("backend_unavailable", "reply lost", 1)
                return result

        client = LoseReplyOnce()
        store = ProductIssueStore(client, data_dir=self.root / "data", instance=self.root)
        with self.assertRaises(TaskError):
            store.create_product(product_id="secretary", projects=["secretary"], title="Secretary", description="", actor="po", request_id="product-restart")
        product = ProductIssueStore(client, data_dir=self.root / "data", instance=self.root).create_product(
            product_id="secretary", projects=["secretary"], title="Secretary", description="", actor="po", request_id="product-restart"
        )
        self.assertEqual(product["id"], "secretary")
        client.lost.clear()
        with self.assertRaises(TaskError):
            store.create_issue(product="secretary", issue_kind="bug", priority="P2", title="Crash", description="", actor="po", request_id="issue-restart")
        issue = ProductIssueStore(client, data_dir=self.root / "data", instance=self.root).create_issue(
            product="secretary", issue_kind="bug", priority="P2", title="Crash", description="", actor="po", request_id="issue-restart"
        )
        with self.assertRaises(TaskError):
            store.update_priority(reference=issue["ref"], priority="P0", reason="urgent", actor="po", request_id="priority-restart")
        ProductIssueStore(client, data_dir=self.root / "data", instance=self.root).update_priority(
            reference=issue["ref"], priority="P0", reason="urgent", actor="po", request_id="priority-restart"
        )
        with self.assertRaises(TaskError):
            store.close_issue(reference=issue["ref"], reason="resolved", actor="po", request_id="close-restart")
        closed = ProductIssueStore(client, data_dir=self.root / "data", instance=self.root).close_issue(
            reference=issue["ref"], reason="resolved", actor="po", request_id="close-restart"
        )
        self.assertTrue(closed["closed"])
        self.assertEqual(len([call for call in client.calls if call[0] == "createTask"]), 2)
        self.assertEqual(len(client.comments[int(next(task for task in client.tasks if task["reference"] == issue["ref"])["id"])]), 2)
        self.assertEqual([event["kind"] for event in store.audit.events()], ["product_created", "issue_created", "issue_priority_changed", "issue_closed"])

    def test_request_id_conflicts_are_rejected_before_a_second_write(self) -> None:
        self.store.create_product(product_id="secretary", projects=["secretary"], title="Secretary", description="", actor="po", request_id="product")
        with self.assertRaises(TaskError) as raised:
            self.store.create_product(product_id="secretary", projects=["secretary"], title="Changed", description="", actor="po", request_id="product")
        self.assertEqual(raised.exception.code, "validation")
        issue = self.store.create_issue(product="secretary", issue_kind="bug", priority="P2", title="Crash", description="", actor="po", request_id="issue")
        with self.assertRaises(TaskError) as raised:
            self.store.create_issue(product="secretary", issue_kind="bug", priority="P2", title="Changed", description="", actor="po", request_id="issue")
        self.assertEqual(raised.exception.code, "validation")
        self.store.update_priority(reference=issue["ref"], priority="P0", reason="urgent", actor="po", request_id="priority")
        with self.assertRaises(TaskError) as raised:
            self.store.update_priority(reference=issue["ref"], priority="P0", reason="changed", actor="po", request_id="priority")
        self.assertEqual(raised.exception.code, "validation")
        self.store.close_issue(reference=issue["ref"], reason="resolved", actor="po", request_id="close")
        with self.assertRaises(TaskError) as raised:
            self.store.close_issue(reference=issue["ref"], reason="invalid", actor="po", request_id="close")
        self.assertEqual(raised.exception.code, "validation")

    def test_generic_reconcile_leaves_product_issue_transaction_for_its_owner(self) -> None:
        original_call = self.client.call
        failed = False

        def fail_once(method: str, **params: object) -> object:
            nonlocal failed
            if method == "saveTaskMetadata" and not failed:
                failed = True
                return False
            return original_call(method, **params)

        self.client.call = fail_once  # type: ignore[method-assign]
        with self.assertRaises(TaskError):
            self.store.create_product(product_id="secretary", projects=["secretary"], title="Secretary", description="", actor="po", request_id="isolated")
        self.assertEqual(TaskWriter(self.client, data_dir=self.root / "data").reconcile(), (0, 0))
        self.assertEqual(self.store.audit.events(), [])
        self.assertEqual(len(list((self.root / "data" / "board" / "product-issue-transactions").glob("*.json"))), 1)

    def test_existing_product_retry_uses_its_staged_projects_before_the_registry(self) -> None:
        original_call = self.client.call
        failed = False

        def fail_once(method: str, **params: object) -> object:
            nonlocal failed
            if method == "saveTaskMetadata" and not failed:
                failed = True
                return False
            return original_call(method, **params)

        self.client.call = fail_once  # type: ignore[method-assign]
        with self.assertRaises(TaskError):
            self.store.create_product(product_id="secretary", projects=["secretary"], title="Secretary", description="", actor="po", request_id="registry")
        (self.root / "projects" / "secretary.yaml").unlink()
        product = ProductIssueStore(self.client, data_dir=self.root / "data", instance=self.root).create_product(
            product_id="secretary", projects=["secretary"], title="Secretary", description="", actor="po", request_id="registry"
        )
        self.assertEqual(product["id"], "secretary")

    def test_audit_cleanup_retry_does_not_repeat_the_completed_operation(self) -> None:
        original_unlink = Path.unlink
        failed = False

        def fail_once(path: Path, *args: object, **kwargs: object) -> None:
            nonlocal failed
            if path.parent.name == "product-issue-transactions" and not failed:
                failed = True
                raise OSError("cleanup interrupted")
            original_unlink(path, *args, **kwargs)

        with mock.patch.object(Path, "unlink", fail_once), self.assertRaises(TaskError) as raised:
            self.store.create_product(product_id="secretary", projects=["secretary"], title="Secretary", description="", actor="po", request_id="cleanup")
        self.assertEqual(raised.exception.code, "audit_pending")
        self.store.create_product(product_id="secretary", projects=["secretary"], title="Secretary", description="", actor="po", request_id="cleanup")
        self.assertEqual(len([call for call in self.client.calls if call[0] == "createTask"]), 1)
        self.assertEqual([event["kind"] for event in self.store.audit.events()], ["product_created"])

    def test_request_id_never_becomes_a_pending_filename_and_generic_upgrade_is_fail_closed(self) -> None:
        self.store.create_product(product_id="secretary", projects=["secretary"], title="Secretary", description="", actor="po", request_id="../../outside")
        names = [path.name for path in (self.root / "data" / "board").rglob("*.json")]
        self.assertTrue(all("outside" not in name for name in names))
        pending = self.root / "data" / "board" / "pending-audit"
        pending.mkdir(exist_ok=True)
        (pending / "old-generic.json").write_text("{}", encoding="utf-8")
        with self.assertRaises(TaskError) as raised:
            self.store.audit.status()
        self.assertEqual(raised.exception.code, "upgrade_required")

    def test_generic_pending_request_id_blocks_product_before_backend_write(self) -> None:
        generic = {
            "event_id": "generic-shared", "request_id": "shared", "kind": "commented",
            "payload": {"body_sha256": "x"}, "ref": "secretary-468",
        }
        TaskAudit(self.root / "data").stage("shared", generic)
        with self.assertRaises(TaskError) as raised:
            self.store.create_product(
                product_id="secretary", projects=["secretary"], title="Secretary", description="",
                actor="po", request_id="shared",
            )
        self.assertEqual(raised.exception.code, "validation")
        self.assertEqual(TaskAudit(self.root / "data").pending_event("shared"), generic)
        self.assertFalse(any(method == "createTask" for method, _ in self.client.calls))

    def test_create_reply_loss_is_correlated_and_repaired_without_a_duplicate_row(self) -> None:
        original_call = self.client.call
        failed = False

        def lose_create_reply(method: str, **params: object) -> object:
            nonlocal failed
            result = original_call(method, **params)
            if method == "createTask" and not failed:
                failed = True
                raise TaskError("backend_error", "lost create reply", 1)
            return result

        self.client.call = lose_create_reply  # type: ignore[method-assign]
        with self.assertRaises(TaskError) as raised:
            self.store.create_product(
                product_id="secretary", projects=["secretary"], title="Secretary", description="",
                actor="po", request_id="lost-create",
            )
        self.assertEqual(raised.exception.code, "audit_pending")
        product = ProductIssueStore(self.client, data_dir=self.root / "data", instance=self.root).create_product(
            product_id="secretary", projects=["secretary"], title="Secretary", description="",
            actor="po", request_id="lost-create",
        )
        self.assertEqual(product["id"], "secretary")
        self.assertEqual(len([call for call in self.client.calls if call[0] == "createTask"]), 1)

    def test_new_issue_operation_rejects_until_an_older_pending_priority_is_repaired(self) -> None:
        self.store.create_product(
            product_id="secretary", projects=["secretary"], title="Secretary", description="", actor="po",
        )
        issue = self.store.create_issue(
            product="secretary", issue_kind="bug", priority="P2", title="Crash", description="", actor="po",
        )
        original_call = self.client.call
        failed = False

        def reject_priority_comment_once(method: str, **params: object) -> object:
            nonlocal failed
            if method == "createComment" and not failed:
                failed = True
                self.client.calls.append((method, params))
                return 0
            return original_call(method, **params)

        self.client.call = reject_priority_comment_once  # type: ignore[method-assign]
        with self.assertRaises(TaskError):
            self.store.update_priority(
                reference=issue["ref"], priority="P0", reason="urgent", actor="po", request_id="priority",
            )
        with self.assertRaises(TaskError) as raised:
            self.store.close_issue(reference=issue["ref"], reason="resolved", actor="po", request_id="close")
        self.assertEqual(raised.exception.code, "audit_pending")
        self.store.update_priority(
            reference=issue["ref"], priority="P0", reason="urgent", actor="po", request_id="priority",
        )
        closed = self.store.close_issue(reference=issue["ref"], reason="resolved", actor="po", request_id="close")
        self.assertTrue(closed["closed"])
        self.assertEqual(closed["priority"], "P0")
        self.assertEqual(
            [event["kind"] for event in self.store.audit.events() if event.get("ref") == issue["ref"]],
            ["issue_created", "issue_priority_changed", "issue_closed"],
        )

    def test_reference_repair_after_create_reply_persists_the_backend_id_first(self) -> None:
        original_call = self.client.call
        rejected: set[str] = set()

        def reject_reference_once(method: str, **params: object) -> object:
            reference = params.get("reference")
            if method == "updateTask" and isinstance(reference, str) and reference not in rejected:
                rejected.add(reference)
                self.client.calls.append((method, params))
                return False
            return original_call(method, **params)

        self.client.call = reject_reference_once  # type: ignore[method-assign]
        with self.assertRaises(TaskError) as raised:
            self.store.create_product(
                product_id="secretary", projects=["secretary"], title="Secretary", description="", actor="po",
                request_id="product-reference-repair",
            )
        self.assertEqual(raised.exception.code, "audit_pending")
        product = ProductIssueStore(self.client, data_dir=self.root / "data", instance=self.root).create_product(
            product_id="secretary", projects=["secretary"], title="Secretary", description="", actor="po",
            request_id="product-reference-repair",
        )
        self.assertEqual(product["id"], "secretary")
        with self.assertRaises(TaskError) as raised:
            self.store.create_issue(
                product="secretary", issue_kind="bug", priority="P2", title="Crash", description="",
                actor="po", request_id="reference-repair",
            )
        self.assertEqual(raised.exception.code, "audit_pending")
        issue = ProductIssueStore(self.client, data_dir=self.root / "data", instance=self.root).create_issue(
            product="secretary", issue_kind="bug", priority="P2", title="Crash", description="",
            actor="po", request_id="reference-repair",
        )
        self.assertEqual(issue["kind"], "bug")
        self.assertEqual(len([call for call in self.client.calls if call[0] == "createTask"]), 2)

    def test_pending_product_identity_blocks_a_second_request_before_create(self) -> None:
        request_id = "first-product"
        intent = {
            "record_type": "product", "product_id": "secretary", "product_projects": '["secretary"]',
            "title": "Secretary", "description": "", "actor": "po",
        }
        event = self.store._transaction_event(
            kind="product_created", actor="po", reference="product:secretary", request_id=request_id, intent=intent,
        )
        self.store.transactions.begin(request_id, kind="product_created", intent=intent, event=event)
        with self.assertRaises(TaskError) as raised:
            self.store.create_product(
                product_id="secretary", projects=["secretary"], title="Other", description="", actor="po",
                request_id="second-product",
            )
        self.assertEqual(raised.exception.code, "audit_pending")
        self.assertFalse(any(method == "createTask" for method, _ in self.client.calls))

    def test_pending_priority_blocks_a_second_priority_before_backend_mutation(self) -> None:
        self.store.create_product(
            product_id="secretary", projects=["secretary"], title="Secretary", description="", actor="po",
        )
        issue = self.store.create_issue(
            product="secretary", issue_kind="bug", priority="P2", title="Crash", description="", actor="po",
        )
        original_call = self.client.call
        failed = False

        def reject_comment_once(method: str, **params: object) -> object:
            nonlocal failed
            if method == "createComment" and not failed:
                failed = True
                self.client.calls.append((method, params))
                return 0
            return original_call(method, **params)

        self.client.call = reject_comment_once  # type: ignore[method-assign]
        with self.assertRaises(TaskError):
            self.store.update_priority(
                reference=issue["ref"], priority="P0", reason="urgent", actor="po", request_id="first-priority",
            )
        comments = len([call for call in self.client.calls if call[0] == "createComment"])
        with self.assertRaises(TaskError) as raised:
            self.store.update_priority(
                reference=issue["ref"], priority="P3", reason="later", actor="po", request_id="second-priority",
            )
        self.assertEqual(raised.exception.code, "audit_pending")
        self.assertEqual(len([call for call in self.client.calls if call[0] == "createComment"]), comments)
        self.store.update_priority(
            reference=issue["ref"], priority="P0", reason="urgent", actor="po", request_id="first-priority",
        )
        self.assertEqual(self.store.show_issue(issue["ref"])["priority"], "P0")

    def test_generic_upgrade_gate_runs_before_product_mutation(self) -> None:
        pending = self.root / "data" / "board" / "pending-audit"
        pending.mkdir(parents=True)
        (pending / "old-generic.json").write_text("{}", encoding="utf-8")
        with self.assertRaises(TaskError) as raised:
            self.store.create_product(
                product_id="secretary", projects=["secretary"], title="Secretary", description="",
                actor="po", request_id="upgrade",
            )
        self.assertEqual(raised.exception.code, "upgrade_required")
        self.assertFalse(any(method == "createTask" for method, _ in self.client.calls))


if __name__ == "__main__":
    unittest.main()
