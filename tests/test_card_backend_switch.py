"""The one identity convention and the one place the card backend is chosen.

Neither needs a database, which is the point of putting them here rather than beside the
PostgreSQL parity suite: `secretary-1587` left the switch provable only where Docker is, and the
two defects this module pins — a parser that knew one backend's prefix, and consumers that named
Kanboard by default — are decidable from the source and from a refusal.
"""

from __future__ import annotations

import json
import os
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from unittest import mock

from secretary.board import backend
from secretary.tasks import TaskError


class CardBackendEnvironment(unittest.TestCase):
    """A per-process decision has to be forgotten between cases, or the first one wins."""

    def setUp(self) -> None:
        backend.reset_card_backend()
        self.addCleanup(backend.reset_card_backend)

    def _switch(self, value: str | None) -> None:
        patched = mock.patch.dict(os.environ, {}, clear=False)
        patched.start()
        self.addCleanup(patched.stop)
        if value is None:
            os.environ.pop(backend.CARD_BACKEND_ENV, None)
        else:
            os.environ[backend.CARD_BACKEND_ENV] = value


class EntityIdentityTests(unittest.TestCase):
    """`<kind>_<backend>_<n>` is minted and read back by one pair of functions."""

    def test_the_identity_is_minted_for_either_backend_and_either_kind(self) -> None:
        self.assertEqual(backend.entity_id("task", backend.KANBOARD, 12), "task_kanboard_12")
        self.assertEqual(backend.entity_id("task", backend.POSTGRES, 468), "task_postgres_468")
        self.assertEqual(backend.entity_id("sprint", backend.KANBOARD, 9), "sprint_kanboard_9")
        self.assertEqual(backend.entity_id("sprint", backend.POSTGRES, 9), "sprint_postgres_9")

    def test_every_minted_identity_reads_back_as_its_number(self) -> None:
        """The defect was a parser that knew one prefix, so the proof is the whole vocabulary."""
        for kind in backend.ENTITY_KINDS:
            for name in backend.CARD_BACKENDS:
                with self.subTest(kind=kind, backend=name):
                    self.assertEqual(backend.entity_number(kind, backend.entity_id(kind, name, 468)), 468)

    def test_an_identity_of_the_other_kind_is_not_this_kind_s_number(self) -> None:
        self.assertIsNone(backend.entity_number("task", "sprint_kanboard_9"))
        self.assertIsNone(backend.entity_number("sprint", "task_postgres_468"))

    def test_a_bare_number_is_still_read_and_anything_else_is_not(self) -> None:
        self.assertEqual(backend.entity_number("task", "468"), 468)
        for value in ("", None, "task_mysql_1", "task_kanboard_", "task_kanboard_0", "task_kanboard_x"):
            with self.subTest(value=value):
                self.assertIsNone(backend.entity_number("task", value))

    def test_minting_refuses_a_kind_or_a_backend_outside_the_vocabulary(self) -> None:
        with self.assertRaises(backend.BoardBackendError):
            backend.entity_id("product", backend.KANBOARD, 1)
        with self.assertRaises(backend.BoardBackendError):
            backend.entity_id("task", "mysql", 1)


class BoardHostIdentityTests(unittest.TestCase):
    """The `report`/`verdict`/`decide` path resolves a card number on either backend."""

    def _host(self, identity: str):
        from secretary.board.kanboard import KanboardBoardHost

        host = KanboardBoardHost.__new__(KanboardBoardHost)
        host.client = object()
        with mock.patch("secretary.board.kanboard.TaskReader") as reader:
            reader.return_value.show.return_value = {"id": identity, "ref": "secretary-468"}
            return host._card_task_id("secretary-468")

    def test_a_card_normalized_by_either_backend_resolves_to_its_number(self) -> None:
        self.assertEqual(self._host("task_kanboard_468"), 468)
        self.assertEqual(self._host("task_postgres_468"), 468)

    def test_an_identity_outside_the_convention_is_still_refused(self) -> None:
        from secretary.board.transitions import BoardProtocolError

        with self.assertRaises(BoardProtocolError):
            self._host("task_mysql_468")


class SprintIdentityTests(unittest.TestCase):
    """`_sprint_number` carried the same one-prefix defect and is fixed by the same function."""

    def test_a_sprint_normalized_by_either_backend_resolves_to_its_number(self) -> None:
        from secretary.sprints import _sprint_number

        self.assertEqual(_sprint_number({"id": "sprint_kanboard_9"}), 9)
        self.assertEqual(_sprint_number({"id": "sprint_postgres_9"}), 9)

    def test_a_missing_sprint_is_still_a_named_refusal(self) -> None:
        from secretary.sprints import _sprint_number

        with self.assertRaises(TaskError) as raised:
            _sprint_number(None)
        self.assertEqual(raised.exception.code, "backend_error")


class SwitchRefusalTests(CardBackendEnvironment):
    """An unknown value, and an entity this build does not serve, both refuse by name."""

    def test_an_unknown_value_refuses_where_the_client_is_built(self) -> None:
        self._switch("mysql")
        with self.assertRaises(TaskError) as raised:
            backend.board_client(Path("/nonexistent"), serves=(backend.CARD,))
        self.assertEqual(raised.exception.code, "backend_error")
        self.assertIn("mysql", raised.exception.message)
        self.assertIn("kanboard, postgres", raised.exception.message)

    def test_the_default_still_builds_a_kanboard_client_for_every_entity(self) -> None:
        self._switch(None)
        from secretary.tasks import KanboardClient

        with mock.patch.object(KanboardClient, "for_instance", return_value="kanboard") as built:
            for serves in ((backend.CARD,), (backend.SPRINT,), (backend.PRODUCT_ISSUE,)):
                with self.subTest(serves=serves):
                    self.assertEqual(backend.board_client(Path("/instance"), serves=serves), "kanboard")
            self.assertEqual(built.call_count, 3)

    def test_postgres_accepts_sprint_capability_before_store_resolution(self) -> None:
        self._switch("postgres")
        for serves in ((backend.SPRINT,), (backend.CARD, backend.SPRINT)):
            with self.subTest(serves=serves):
                backend.reset_card_backend()
                with self.assertRaises(TaskError) as raised:
                    backend.board_client(Path("/instance"), serves=serves)
                self.assertEqual(raised.exception.code, "backend_unavailable")
                self.assertNotIn("Kanboard board on this build", raised.exception.message)

    def test_postgres_accepts_product_issue_before_resolving_store_configuration(self) -> None:
        self._switch("postgres")
        with self.assertRaises(TaskError) as raised:
            backend.board_client(Path("/nonexistent-instance"), serves=(backend.PRODUCT_ISSUE,))
        self.assertEqual(raised.exception.code, "backend_unavailable")
        self.assertNotIn("Kanboard board on this build", raised.exception.message)

    def test_a_store_that_is_not_configured_refuses_with_the_store_s_own_reason(self) -> None:
        """`BoardStoreError` is a `RuntimeError`; a CLI must not answer with its traceback."""
        self._switch("postgres")
        with self.assertRaises(TaskError) as raised:
            backend.board_client(Path("/nonexistent-instance"), serves=(backend.CARD,))
        self.assertEqual(raised.exception.code, "backend_unavailable")
        self.assertIn("board store", raised.exception.message)


class SwitchRefusalRenderingTests(CardBackendEnvironment):
    """The refusal reaches the operator as the named error every task command prints."""

    def _render(self) -> tuple[int, dict]:
        from secretary import task_commands

        stderr = StringIO()
        with redirect_stderr(stderr):
            code = task_commands.run_task_command(
                lambda: task_commands.TaskReader(
                    backend.card_client(Path("/nonexistent-instance"))
                ).list()
            )
        return code, json.loads(stderr.getvalue())

    def test_an_unknown_switch_value_prints_a_named_refusal(self) -> None:
        self._switch("mysql")
        code, document = self._render()
        self.assertEqual(code, 1)
        self.assertEqual(document["error"]["code"], "backend_error")

    def test_a_missing_board_store_env_prints_a_named_refusal(self) -> None:
        self._switch("postgres")
        code, document = self._render()
        self.assertEqual(code, 1)
        self.assertEqual(document["error"]["code"], "backend_unavailable")


class DriverRefusalTests(unittest.TestCase):
    """`psycopg`'s exceptions are translated where the adapter raises them, not above it."""

    def test_a_driver_that_is_not_installed_is_a_named_refusal(self) -> None:
        from secretary.board.sql_cards import _driver_error

        error = _driver_error("open a connection", ModuleNotFoundError("No module named 'psycopg'"))
        self.assertEqual(error.code, "backend_unavailable")
        self.assertIn("driver is not installed", error.message)

    def test_the_client_s_own_refusal_is_already_in_the_dictionary(self) -> None:
        from secretary.board.sql_cards import SqlCardError

        error = SqlCardError("two cards share one card number")
        self.assertIsInstance(error, TaskError)
        self.assertEqual((error.code, error.exit_code), ("backend_error", 1))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
