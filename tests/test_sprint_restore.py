"""Sprint entities through export, checkpoint and restore (secretary-819).

Recovery used to rebuild the Pipeline cards of a sprint and drop the sprint itself:
goal, Definition of Done, repositories, status, budget, current task, resume and every
record to the entity lived only on the live board. These pin the whole path: a filled
closed sprint is exported, restored into a separate empty backend, and compared field by
field against its source.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from secretary.data import export_board, init_layout, normalize_sprint_entity
from secretary.restore import RestoreError, import_normalized_board, restore_findings, restore_state
from secretary.sprints import SprintReader, SprintWriter, ensure_sprint_board
from secretary.tasks import TaskWriter

from tests.restore_fixtures import _EmptyBoardsKanboard
from tests.test_sprints import SprintKanboard


CARD_EXPORT = {
    "id": 12, "reference": "secretary-12", "title": "Linked card", "description": "card body",
    "column": "Ready", "swimlane": "", "position": 1, "task_type": "code", "project": "secretary",
    "metadata": {"complexity": "standard", "family_preference": "auto"}, "comments": [],
}
RESUME = {
    "selected_step": "restore the entity", "selected_why": "the checkpoint carries it",
    "rejected_alternatives": "recreate it by hand", "current_task": "secretary-12",
    "dod_state": "tests pending", "next_safe_step": "run the suite",
    "recorded_at": "2026-07-20T00:00:00Z",
}


class SprintRestoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source_data = self.root / "source-data"
        self.target_data = self.root / "target-data"
        init_layout(self.source_data)
        init_layout(self.target_data)
        self.source = SprintKanboard()
        self.ref = self._seed_closed_sprint()
        self._export()

    def _seed_closed_sprint(self) -> str:
        writer = SprintWriter(self.source, data_dir=self.source_data)  # type: ignore[arg-type]
        ref = writer.create(
            role="po", actor="operator", goal="Ship sprint entities into recovery",
            definition_of_done="restore rebuilds the entity", reference="sprint:entity",
            repositories=["secretary", "secretary-instance"], request_id="seed-create",
        )["sprint"]["ref"]
        card = TaskWriter(self.source, data_dir=self.source_data).create(  # type: ignore[arg-type]
            # The sprint holds `secretary`, so its own observer is the writer of its cards.
            role="observer", actor="observer", project="secretary", task_type="code", title="linked",
            sprint=ref, request_id="seed-card",
        )["task"]
        writer.comment(role="po", actor="operator", reference=ref, body="first note", request_id="seed-comment")
        writer.record_budget(role="po", actor="operator", reference=ref, event_type="red_ci", request_id="seed-budget")
        writer.set_current_task(
            role="po", actor="operator", reference=ref, task_reference=card["ref"], request_id="seed-current",
        )
        writer.resume(role="po", actor="operator", reference=ref, entry=RESUME, request_id="seed-resume")
        writer.close(role="po", actor="operator", reference=ref, request_id="seed-close")
        return ref

    def _pipeline_command(self) -> list[str]:
        """A stand-in for `triggered_agents pipeline export`; cards are not the subject here."""
        script = self.root / "fake_pipeline.py"
        script.write_text("print(%r)" % json.dumps([CARD_EXPORT]), encoding="utf-8")
        return [sys.executable, str(script)]

    def _export(self) -> None:
        export_board(self.source_data, command=self._pipeline_command(), sprint_client=self.source)
        # Only the normalized export travels; the target backend starts from nothing else.
        for name in ("cards.json", "sprints.json"):
            shutil.copy(self.source_data / "board" / name, self.target_data / "board" / name)

    def _exported_sprint(self) -> dict:
        payload = json.loads((self.target_data / "board" / "sprints.json").read_text(encoding="utf-8"))
        return payload["sprints"][0]

    def _restore(self, client: object | None = None) -> tuple[object, int]:
        client = client or _EmptyBoardsKanboard()
        return client, import_normalized_board(self.target_data, client=client)  # type: ignore[arg-type]

    def test_export_carries_the_sprint_set_next_to_the_cards(self) -> None:
        summary = json.loads((self.source_data / "board" / "export.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["card_count"], 1)
        self.assertEqual(summary["sprint_count"], 1)
        lines = (self.source_data / "board" / "sprints.ndjson").read_text(encoding="utf-8").splitlines()
        self.assertEqual([json.loads(line)["reference"] for line in lines], [self.ref])
        exported = self._exported_sprint()
        self.assertEqual(exported["status"], "closed")
        self.assertEqual(exported["repositories"], ["secretary", "secretary-instance"])
        self.assertEqual(exported["budget"]["by_type"]["red_ci"], 1)
        self.assertEqual(exported["current_task"], "secretary-14")
        self.assertEqual(exported["resume"]["selected_step"], RESUME["selected_step"])
        self.assertEqual([comment["text"] for comment in exported["comments"]],
                         ["[po]\nfirst note", "[sprint:resume]\n" + RESUME["selected_step"]])

    def test_closed_sprint_is_rebuilt_field_by_field_in_an_empty_backend(self) -> None:
        client, cards = self._restore()

        self.assertEqual(cards, 1)
        exported = self._exported_sprint()
        live = SprintReader(client, data_dir=self.target_data).show(self.ref)  # type: ignore[arg-type]
        self.assertEqual(live["goal"], exported["goal"])
        self.assertEqual(live["definition_of_done"], exported["definition_of_done"])
        self.assertEqual(live["repositories"], exported["repositories"])
        self.assertEqual(live["status"], "closed")
        self.assertEqual(live["budget"]["by_type"], exported["budget"]["by_type"])
        self.assertEqual(live["budget"]["total"], 1)
        self.assertEqual(live["current_task"], exported["current_task"])
        self.assertEqual(live["resume"], exported["resume"])
        self.assertEqual(
            [comment["body"] for comment in live["comments"]],
            [comment["text"] for comment in exported["comments"]],
        )
        # The entity came back on a new Kanboard row, so its own dates describe the
        # recovery; the dates it was restored from stay readable and compare exactly.
        self.assertEqual(live["audit"]["source"], exported["audit"])
        self.assertNotEqual(live["audit"]["created_at"], exported["audit"]["created_at"])
        self.assertEqual(normalize_sprint_entity(live), exported)
        self.assertEqual(restore_state(self.target_data)["sprint_count"], 1)
        self.assertEqual(restore_state(self.target_data)["sprint_parity"], "complete")
        self.assertEqual(restore_findings(self.target_data), ["memory index has not been rebuilt",
                                                             "managed reconcile has not been applied"])

    def test_pipeline_cards_still_restore_alongside_the_entities(self) -> None:
        client, cards = self._restore()

        self.assertEqual(cards, 1)
        restored = [task for task in client.tasks if task["reference"] == "secretary-12"]  # type: ignore[attr-defined]
        self.assertEqual(len(restored), 1)
        self.assertEqual(restore_state(self.target_data)["board_parity"], "complete")

    def test_repeated_restore_creates_one_entity_and_no_duplicate_records(self) -> None:
        client, _ = self._restore()
        # A recovery retry rematerializes the canonical events.ndjson, which does not
        # carry the derived restore audit records of the failed attempt.
        (self.target_data / "board" / "events.ndjson").write_text("", encoding="utf-8")

        self._restore(client)

        sprint_board = ensure_sprint_board(client)  # type: ignore[arg-type]
        entities = [task for task in client.tasks if task["project_id"] == sprint_board]  # type: ignore[attr-defined]
        self.assertEqual([task["reference"] for task in entities], [self.ref])
        live = SprintReader(client, data_dir=self.target_data).show(self.ref)  # type: ignore[arg-type]
        self.assertEqual(
            [comment["body"] for comment in live["comments"]],
            [comment["text"] for comment in self._exported_sprint()["comments"]],
        )

    def test_parity_failure_leaves_recovery_incomplete_with_a_named_error(self) -> None:
        client = _EmptyBoardsKanboard()
        original = client.call

        def lossy(method: str, **params: object) -> object:
            if method == "saveTaskMetadata" and "sprint_status" in dict(params["values"]):  # type: ignore[arg-type]
                values = {k: v for k, v in dict(params["values"]).items() if k != "sprint_status"}  # type: ignore[arg-type]
                return original(method, task_id=params["task_id"], values=values)
            return original(method, **params)

        with mock.patch.object(client, "call", side_effect=lossy):
            with self.assertRaisesRegex(RestoreError, "sprint parity check failed"):
                import_normalized_board(self.target_data, client=client)  # type: ignore[arg-type]

        state = restore_state(self.target_data)
        self.assertEqual(state["sprint_parity"], "failed")
        self.assertEqual(state["sprints"], "failed")
        self.assertIn("sprint restore parity failed", restore_findings(self.target_data))

    def test_foreign_sprint_on_the_target_board_stops_the_restore(self) -> None:
        (self.target_data / "board" / "cards.json").write_text(
            json.dumps({"version": 1, "cards": []}), encoding="utf-8"
        )
        client = _EmptyBoardsKanboard()
        SprintWriter(client, data_dir=self.target_data).create(  # type: ignore[arg-type]
            role="po", actor="operator", goal="someone else's sprint", reference="sprint:foreign",
            request_id="foreign",
        )

        with self.assertRaisesRegex(RestoreError, "sprint board is not empty"):
            import_normalized_board(self.target_data, client=client)  # type: ignore[arg-type]

    def test_invalid_sprint_export_is_refused_before_any_live_write(self) -> None:
        path = self.target_data / "board" / "sprints.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["sprints"][0]["status"] = "archived"
        path.write_text(json.dumps(payload), encoding="utf-8")
        client = _EmptyBoardsKanboard()

        with self.assertRaisesRegex(RestoreError, "invalid status"):
            import_normalized_board(self.target_data, client=client)  # type: ignore[arg-type]

        self.assertEqual(client.tasks, [])

    def test_export_without_a_sprint_board_leaves_the_target_untouched(self) -> None:
        (self.target_data / "board" / "sprints.json").unlink()
        client, cards = self._restore()

        self.assertEqual(cards, 1)
        self.assertFalse(
            any(
                method == "createProject" and params.get("name") == "Secretary sprints"
                for method, params in client.calls  # type: ignore[attr-defined]
            )
        )
        self.assertEqual(restore_state(self.target_data)["sprint_count"], 0)


if __name__ == "__main__":
    unittest.main()
