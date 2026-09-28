"""The schema gate's decisions without a server (`board/schema_gate.py`).

The real-PostgreSQL proofs — every entry point, the transaction hygiene, doctor's text and JSON —
are `tests/test_board_schema_gate_backend.py`. Here: the revision expectation against the packaged
graph, the three verdicts, the healthy path that never touches Alembic, the pool's handling of a
refused connection, and doctor's rendering of each state.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

import psycopg

from secretary import cli
from secretary.board import migrate, schema_gate
from secretary.board.sql_cards import CardSchemaOwed, SqlCardClient
from secretary.tasks import TaskError


class _Credentials:
    def conninfo(self) -> str:
        return "host=board dbname=board user=app password=secret"


class _VersionConnection:
    """A stand-in that answers only the version read, with `revisions` (None: no version table)."""

    def __init__(self, revisions: list[str] | None) -> None:
        self.revisions = revisions
        self.closed = False
        self.executed: list[str] = []

    def execute(self, sql: str, params: Any = ()) -> Any:
        self.executed.append(sql)
        if self.revisions is None:
            raise psycopg.errors.UndefinedTable('relation "alembic_version" does not exist')
        return SimpleNamespace(fetchall=lambda: [(revision,) for revision in self.revisions])

    def close(self) -> None:
        self.closed = True


class RevisionExpectationTests(unittest.TestCase):
    def test_the_lineage_is_the_packaged_migrations_in_order_and_ends_at_the_expectation(self) -> None:
        shipped = tuple(
            sorted(path.stem for path in (migrate.SCRIPT_LOCATION / "versions").glob("[0-9][0-9][0-9][0-9]_*.py"))
        )
        self.assertEqual(migrate.lineage(), shipped)
        self.assertEqual(migrate.lineage()[-1], migrate.EXPECTED_SCHEMA_REVISION)
        self.assertEqual(migrate.head_revision(), migrate.EXPECTED_SCHEMA_REVISION)


class ClassifyTests(unittest.TestCase):
    def test_the_expected_revision_is_current_without_consulting_the_graph(self) -> None:
        with mock.patch.object(migrate, "lineage", side_effect=AssertionError("the graph was read")):
            assessment = schema_gate.classify((migrate.EXPECTED_SCHEMA_REVISION,))
        self.assertEqual(assessment.state, schema_gate.CURRENT)
        self.assertEqual(assessment.pending, ())
        self.assertFalse(assessment.owed)

    def test_an_earlier_revision_owes_everything_after_it_in_order(self) -> None:
        lineage = migrate.lineage()
        assessment = schema_gate.classify((lineage[-4],))
        self.assertEqual(assessment.state, schema_gate.OWED)
        self.assertEqual(assessment.actual_text, lineage[-4])
        self.assertEqual(assessment.pending, lineage[-3:])
        self.assertIn(", ".join(lineage[-3:]), assessment.describe())

    def test_no_version_table_owes_the_whole_lineage(self) -> None:
        assessment = schema_gate.classify(())
        self.assertEqual(assessment.state, schema_gate.OWED)
        self.assertIsNone(assessment.actual_text)
        self.assertEqual(assessment.pending, migrate.lineage())
        self.assertIn("no schema at all", assessment.describe())

    def test_a_revision_outside_the_lineage_is_a_later_builds_and_not_refused(self) -> None:
        assessment = schema_gate.classify(("0099_a_later_build",))
        self.assertEqual(assessment.state, schema_gate.AHEAD)
        self.assertFalse(assessment.owed)
        self.assertEqual(assessment.pending, ())

    def test_assess_reads_one_statement_and_a_missing_table_is_a_store_never_migrated(self) -> None:
        connection = _VersionConnection(None)
        self.assertEqual(schema_gate.assess(connection).pending, migrate.lineage())
        self.assertEqual(connection.executed, [schema_gate.VERSION_QUERY])

    def test_require_raises_the_callers_family_and_passes_a_current_store(self) -> None:
        current = _VersionConnection([migrate.EXPECTED_SCHEMA_REVISION])
        self.assertEqual(schema_gate.require(current, CardSchemaOwed).state, schema_gate.CURRENT)
        with self.assertRaises(CardSchemaOwed) as raised:
            schema_gate.require(_VersionConnection([migrate.lineage()[0]]), CardSchemaOwed)
        self.assertIsInstance(raised.exception, TaskError)
        self.assertEqual((raised.exception.code, raised.exception.exit_code), ("schema_owed", 1))
        self.assertEqual(raised.exception.pending, migrate.lineage()[1:])


class PoolRefusalTests(unittest.TestCase):
    """A refused connection is closed and its slot freed; the next borrow reads again."""

    def setUp(self) -> None:
        self.opened: list[_VersionConnection] = []
        self.revisions: list[str] | None = [migrate.lineage()[-2]]

        def connect(conninfo: str, **options: Any) -> _VersionConnection:
            connection = _VersionConnection(self.revisions)
            self.opened.append(connection)
            return connection

        self.enterContext(mock.patch("psycopg.connect", side_effect=connect))
        scratch = self.enterContext(tempfile.TemporaryDirectory())
        self.client = SqlCardClient(_Credentials(), scratch, pool_size=1)  # type: ignore[arg-type]

    def test_a_refusal_holds_no_slot_and_is_not_remembered(self) -> None:
        for _ in range(3):
            with self.assertRaises(CardSchemaOwed):
                self.client._query("SELECT 1")
            self.assertEqual((self.client._open, self.client._idle), (0, []))
        self.assertEqual(len(self.opened), 3, "each borrow opened and read again")
        self.assertTrue(all(connection.closed for connection in self.opened))

        self.revisions = [migrate.EXPECTED_SCHEMA_REVISION]
        self.client._borrow()
        self.assertEqual(self.client._open, 1)
        self.assertFalse(self.opened[-1].closed)


class DoctorRenderingTests(unittest.TestCase):
    def text(self, board_schema: dict[str, object]) -> str:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            cli.print_board_schema_status(board_schema)
        return output.getvalue()

    def owed(self) -> dict[str, object]:
        assessment = schema_gate.classify((migrate.lineage()[-3],))
        return {**assessment.to_json(), "message": assessment.describe()}

    def test_an_owed_schema_is_one_finding_carrying_what_the_text_prints(self) -> None:
        board_schema = self.owed()
        [finding] = cli._board_schema_findings(board_schema)
        text = self.text(board_schema)
        self.assertEqual(finding["code"], "schema_owed")
        self.assertEqual(finding["pending"], list(migrate.lineage()[-2:]))
        self.assertEqual(finding["actual"], migrate.lineage()[-3])
        self.assertEqual(finding["expected"], migrate.EXPECTED_SCHEMA_REVISION)
        self.assertIn(f"board schema: owed: {finding['message']}", text)
        self.assertIn(f"board schema pending: {', '.join(migrate.lineage()[-2:])}", text)

    def test_current_ahead_and_unconfigured_add_no_finding_and_unavailable_does(self) -> None:
        for state in ("current", "ahead", "not_configured", "not_inspected"):
            with self.subTest(state=state):
                self.assertEqual(cli._board_schema_findings({"state": state}), [])
        [finding] = cli._board_schema_findings({"state": "unavailable", "reason": "refused"})
        self.assertEqual(finding, {"code": "board_schema_unavailable", "message": "refused"})
        self.assertEqual(
            self.text({"state": "unavailable", "reason": "refused"}), "board schema: unavailable: refused\n"
        )

    def test_an_installation_with_no_store_is_not_configured_and_reads_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch("psycopg.connect", side_effect=AssertionError):
            answer = schema_gate.inspect_instance(Path(tmp))
        self.assertEqual(answer["state"], schema_gate.NOT_CONFIGURED)

    def test_offline_and_fixture_runs_are_not_inspected(self) -> None:
        report = SimpleNamespace(instance_path=Path("/nonexistent/instance.yaml"))
        with mock.patch.object(schema_gate, "inspect_instance", side_effect=AssertionError("read live")):
            offline = cli.board_schema_inspection(report, argparse.Namespace(offline=True, host_fixture=None))
            fixture = cli.board_schema_inspection(report, argparse.Namespace(offline=False, host_fixture="dir"))
        self.assertEqual((offline["state"], fixture["state"]), ("not_inspected", "not_inspected"))


if __name__ == "__main__":
    unittest.main()
