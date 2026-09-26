"""A sprint's PO session and allowed productions on PostgreSQL: create, refuse, replay, read back.

The unit suite (`tests/test_sprint_po_channel.py`) holds the rules over fakes; this is the same create
through the real `sprints` columns of revision 0016 and the real `po_sessions` table.
"""

from __future__ import annotations

import unittest

from secretary.sprints import SprintReader
from secretary.tasks import TaskError
from tests.fakes.sprints import SprintFixture


class SprintPoChannelBackendTests(SprintFixture):
    def add_session(self, session_id: str, state: str = "open") -> None:
        with self.client.transaction():
            self.client._execute(
                "INSERT INTO po_sessions (session_id, cli, model, cwd, created_at, state, closed_at, closed_by) "
                "VALUES (%s, 'claude', 'opus', '/po', now(), %s, "
                "CASE WHEN %s = 'closed' THEN now() END, CASE WHEN %s = 'closed' THEN 'owner' END)",
                (session_id, state, state, state),
            )

    def assert_nothing_was_written(self) -> None:
        self.assertEqual(self._events(), [])
        self.assertEqual(self.sprint_record_count(), 0)

    def test_both_fields_are_stored_and_read_back_by_show_and_status(self) -> None:
        self.add_session("s-open")
        created = self._create(
            goal="with both",
            reference="sprint:both",
            po_session="s-open",
            allowed_productions=["secretary", "other"],
        )
        self.assertEqual(created["sprint"]["po_session"], "s-open")
        shown = self.sprint("sprint:both")
        self.assertEqual(
            (shown["po_session"], shown["allowed_productions"]), ("s-open", ["secretary", "other"])
        )
        status = SprintReader(self.client, data_dir=self.tmp.name).status("sprint:both")  # type: ignore[arg-type]
        self.assertEqual(
            (status["po_session"], status["allowed_productions"]), ("s-open", ["secretary", "other"])
        )

    def test_a_sprint_created_with_neither_reads_null_and_empty(self) -> None:
        self._create(goal="with neither", reference="sprint:neither")
        shown = self.sprint("sprint:neither")
        self.assertEqual((shown["po_session"], shown["allowed_productions"]), (None, []))
        self.assertEqual(
            self.client._query(
                "SELECT po_session, allowed_productions FROM sprints WHERE ref = 'sprint:neither'"
            ),
            [(None, [])],
        )

    def test_an_unknown_or_closed_session_and_an_unknown_production_write_nothing(self) -> None:
        self.add_session("s-closed", "closed")
        for options in (
            {"po_session": "s-nowhere"},
            {"po_session": "s-closed"},
            {"allowed_productions": ["not-registered"]},
        ):
            with self.subTest(options=options):
                with self.assertRaises(TaskError) as raised:
                    self._create(goal="refused", reference="sprint:refused", **options)
                self.assertEqual(raised.exception.code, "validation")
                self.assert_nothing_was_written()

    def test_a_repeat_replays_and_the_same_id_with_another_session_is_refused(self) -> None:
        self.add_session("s-1")
        self.add_session("s-2")
        first = self._create(goal="once", reference="sprint:once", request_id="create-once", po_session="s-1")
        again = self._create(goal="once", reference="sprint:once", request_id="create-once", po_session="s-1")
        self.assertEqual(again["event_id"], first["event_id"])
        with self.assertRaises(TaskError) as raised:
            self._create(goal="once", reference="sprint:once", request_id="create-once", po_session="s-2")
        self.assertEqual(raised.exception.code, "validation")
        self.assertEqual(self.sprint("sprint:once")["po_session"], "s-1")

    def test_the_resolver_s_record_replaces_the_session_once(self) -> None:
        self._create(goal="resolved", reference="sprint:resolved")
        for _ in range(2):
            self.writer.set_po_session(
                role="po",
                actor="po-service",
                reference="sprint:resolved",
                session_id="s-new",
                request_id="rec-1",
            )
        self.assertEqual(self.sprint("sprint:resolved")["po_session"], "s-new")
        recorded = [event for event in self._events() if event.get("kind") == "po_session_set"]
        self.assertEqual(len(recorded), 1)
        self.assertEqual(recorded[0]["actor"], {"role": "po", "id": "po-service"})


if __name__ == "__main__":
    unittest.main()
