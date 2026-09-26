"""A sprint's PO session and allowed productions on PostgreSQL: create, refuse, replay, read back, allow.

The unit suite (`tests/test_sprint_po_channel.py`) holds the rules over fakes; this is the same create
through the real `sprints` columns of revision 0016 and the real `po_sessions` table.
"""

from __future__ import annotations

import contextlib
import io
import os
import unittest
from unittest import mock

from secretary.cli import main
from secretary.sprint_observer import head_choice
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

    def allow(self, project: str, request_id: str, **fields: str) -> dict:
        call = {"role": "po", "actor": "po", "reference": "sprint:allow", "project": project,
                "reason": "secretary is the development server", "request_id": request_id, **fields}
        return self.writer.allow_production(**call)  # type: ignore[arg-type]

    def allowances(self) -> list[dict]:
        return [event for event in self._events() if event.get("kind") == "production_allowed"]

    def test_allow_production_extends_the_column_once_with_its_audit_event(self) -> None:
        """`sprint allow-production` (secretary-1769): the column, the event, the replays, the no-op."""
        self._create(goal="allow", reference="sprint:allow", allowed_productions=["other"])

        answer = self.allow("secretary", "allow-1")

        self.assertEqual(answer["action"], "production_allowed")
        self.assertEqual(answer["sprint"]["allowed_productions"], ["other", "secretary"])
        self.assertEqual(
            self.client._query("SELECT allowed_productions FROM sprints WHERE ref = 'sprint:allow'"),
            [(["other", "secretary"],)],
        )
        self.assertEqual(self.sprint("sprint:allow")["allowed_productions"], ["other", "secretary"])
        status = SprintReader(self.client, data_dir=self.tmp.name).status("sprint:allow")  # type: ignore[arg-type]
        self.assertEqual(status["allowed_productions"], ["other", "secretary"])
        [event] = self.allowances()
        self.assertEqual(
            (event["ref"], event["actor"], event["payload"], event["request_id"]),
            ("sprint:allow", {"role": "po", "id": "po"},
             {"project": "secretary", "reason": "secretary is the development server"}, "allow-1"),
        )
        # The same request id is the same write; a project already allowed writes nothing new.
        self.assertEqual(self.allow("secretary", "allow-1")["event_id"], answer["event_id"])
        self.assertEqual(self.allow("secretary", "allow-2")["action"], "already_allowed")
        self.assertEqual(len(self.allowances()), 1)
        with self.assertRaises(TaskError) as raised:
            self.allow("other", "allow-1")
        self.assertEqual(raised.exception.code, "validation")
        self.assertEqual(self.sprint("sprint:allow")["allowed_productions"], ["other", "secretary"])

    def test_allow_production_refusals_write_nothing(self) -> None:
        self._create(goal="allow", reference="sprint:allow")
        for fields, code in (
            ({"role": "observer", "actor": "observer"}, "role_forbidden"),
            ({"role": "po", "actor": "observer"}, "role_masquerade"),
            ({"project": "not-registered"}, "validation"),
        ):
            with self.subTest(fields=fields), self.assertRaises(TaskError) as raised:
                self.allow(str(fields.pop("project", "secretary")), "refused-1", **fields)
            self.assertEqual(raised.exception.code, code)
        for status in ("closed", "stopped"):
            reference = f"sprint:allow-{status}"
            self.writer.restore_create(
                reference=reference,
                goal="seeded",
                observer=head_choice("codex-observer"),
                status=status,
                request_id=f"fixture-{reference}",
            )
            with self.subTest(status=status), self.assertRaises(TaskError) as raised:
                self.allow("secretary", f"refused-{status}", reference=reference)
            self.assertEqual((raised.exception.code, raised.exception.exit_code), ("closed", 3))
            self.assertEqual(self.sprint(reference)["allowed_productions"], [])
        self.assertEqual(self.allowances(), [])
        self.assertEqual(self.sprint("sprint:allow")["allowed_productions"], [])

    def test_the_command_takes_the_actor_from_board_actor(self) -> None:
        self._create(goal="allow", reference="sprint:allow")
        out = io.StringIO()
        with (
            self.board_injected(),
            mock.patch.dict(os.environ, {"BOARD_ACTOR": "po"}),
            contextlib.redirect_stdout(out),
        ):
            code = main(
                ["sprint", "allow-production", "--ref", "sprint:allow", "--role", "po", "--project", "secretary",
                 "--reason", "the development server", "--request-id", "cli-allow-1",
                 "--instance", str(self.instance), "--data-dir", self.tmp.name]
            )
        self.assertEqual(code, 0, out.getvalue())
        [event] = self.allowances()
        self.assertEqual(event["actor"], {"role": "po", "id": "po"})
        self.assertEqual(self.sprint("sprint:allow")["allowed_productions"], ["secretary"])


if __name__ == "__main__":
    unittest.main()
