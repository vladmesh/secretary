"""`transition from-<old> --repair-products [--apply]` against PostgreSQL (ummanu-5).

CI integration shard only: a fresh migrated store from `PostgresBoard`, seeded with plain owner
SQL, and the command run through the CLI against an instance whose `board-store.env` names it.
Every product name comes from the transition's own table (`names`).
"""

from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from typing import Any

from tests.sql_backend_fixtures import PostgresBoard
from ummanu import cli
from ummanu.board.backend import record_key
from ummanu.board.store import BoardStoreConfig
from ummanu.transition import board as transition_board
from ummanu.transition.names import INSTANCE_PROJECT, NEW, OLD
from ummanu.transition.products import CANARY_PRODUCT

COMMAND = ["transition", f"from-{OLD.package}", "--repair-products"]


class RepairProductsTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.instance = Path(temporary.name) / "instance"
        self.instance.mkdir()
        board = PostgresBoard.shared()
        self.config: BoardStoreConfig = board.fresh_database()
        self.addCleanup(board.release_database, self.config.dbname)
        path = self.instance / transition_board.STORE_FILE
        path.write_text(transition_board.store_env_text(self.config, NEW), encoding="utf-8")
        path.chmod(0o600)

    def _sql(self, statement: str, parameters: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        import psycopg

        with psycopg.connect(self.config.for_role("owner").conninfo()) as connection:
            cursor = connection.execute(statement, parameters)
            return list(cursor.fetchall()) if cursor.description else []

    def _product(self, product_id: str, title: str, projects: list[str], state: str = "active") -> None:
        self._sql(
            "INSERT INTO products (product_id, board_key, title, description, state, created_at, updated_at) "
            "VALUES (%s, %s, %s, 'body', %s, now() - interval '1 day', now() - interval '1 day')",
            (product_id, record_key("product", product_id), title, state),
        )
        for project in projects:
            self._sql("INSERT INTO projects (project_id) VALUES (%s) ON CONFLICT DO NOTHING", (project,))
            self._sql("INSERT INTO product_projects (product_id, project_id) VALUES (%s, %s)", (product_id, project))

    def _seed(self, *, projects: list[str] | None = None, state: str = "active") -> None:
        self._product(OLD.product_id, OLD.product_title, [OLD.project_id, INSTANCE_PROJECT], "archived")
        self._product(NEW.product_id, NEW.product_title, [NEW.project_id, INSTANCE_PROJECT])
        self._product(CANARY_PRODUCT, "PostgreSQL cutover acceptance",
                      [OLD.project_id] if projects is None else projects, state)
        issue = "0e2e62ea330f5ca45428"
        self._sql(
            "INSERT INTO issues (issue_id, board_key, product_id, title, issue_kind, priority, state, close_reason, "
            "created_at, updated_at) VALUES (%s, %s, %s, 'Verify', 'feature', 'P2', 'closed', 'resolved', now(), now())",
            (issue, record_key("issue", issue), CANARY_PRODUCT),
        )
        self._sql("INSERT INTO issue_comments (issue_id, body, created_at) VALUES (%s, 'verified', now())", (issue,))
        self._sql(
            "INSERT INTO product_comments (product_id, body, created_at) VALUES (%s, 'canary', now())",
            (CANARY_PRODUCT,),
        )

    def _snapshot(self) -> dict[str, list[tuple[Any, ...]]]:
        tables = [
            str(row[0])
            for row in self._sql(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
            )
        ]
        return {table: self._sql(f'SELECT * FROM "{table}" ORDER BY 1') for table in tables}

    def _run(self, *extra: str) -> tuple[int, str, str]:
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            code = cli.main([*COMMAND, *extra, "--instance", str(self.instance)])
        return code, out.getvalue(), err.getvalue()

    def _state(self) -> str:
        return str(self._sql("SELECT state FROM products WHERE product_id = %s", (CANARY_PRODUCT,))[0][0])

    def test_plan_writes_nothing(self) -> None:
        self._seed()
        before = self._snapshot()

        code, output, _ = self._run()

        self.assertEqual(code, 0, output)
        self.assertIn("plan only, nothing is written", output)
        self.assertIn(f"projects=['{OLD.project_id}']", output)
        self.assertIn("would set state active -> archived", output)
        self.assertEqual(self._snapshot(), before)

    def test_apply_archives_exactly_the_canary_and_a_second_apply_is_a_no_op(self) -> None:
        self._seed()
        before = self._snapshot()

        code, output, _ = self._run("--apply")

        self.assertEqual(code, 0, output)
        self.assertIn("archived: state active -> archived", output)
        after = self._snapshot()
        self.assertEqual(self._state(), "archived")
        columns = [str(row[0]) for row in self._sql(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'products' "
            "ORDER BY ordinal_position"
        )]
        state = columns.index("state")
        expected = [
            tuple("archived" if index == state and row[0] == CANARY_PRODUCT else value
                  for index, value in enumerate(row))
            for row in before["products"]
        ]
        self.assertEqual(after["products"], expected, "only the canary's state changes, not updated_at")
        self.assertEqual({k: v for k, v in after.items() if k != "products"},
                         {k: v for k, v in before.items() if k != "products"})

        code, output, _ = self._run("--apply")

        self.assertEqual(code, 0, output)
        self.assertIn("already archived: nothing to do", output)
        self.assertEqual(self._snapshot(), after)

    def test_refusals_write_nothing_and_exit_non_zero(self) -> None:
        cases = {
            "missing": lambda: self._product(NEW.product_id, NEW.product_title, [NEW.project_id]),
            "unexpected projects": lambda: self._seed(projects=[OLD.project_id, INSTANCE_PROJECT]),
            "archived on unexpected projects": lambda: self._seed(projects=[NEW.project_id], state="archived"),
        }
        for name, seed in cases.items():
            with self.subTest(name):
                self._sql("TRUNCATE products, projects CASCADE")
                seed()
                before = self._snapshot()
                for extra in ((), ("--apply",)):
                    code, output, error = self._run(*extra)
                    self.assertEqual(code, 3, output)
                    self.assertIn("transition_refused", error)
                    self.assertIn("nothing written", error)
                    self.assertEqual(self._snapshot(), before)


if __name__ == "__main__":
    unittest.main()
