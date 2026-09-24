"""Which CODEX_HOME a Codex head runs with, resolved at launch (secretary-1710).

The resolver has four rungs: the profile's `codex_home`, `TA_CODEX_HOME`, `<data_dir>/codex-home`
once it holds a login, and the legacy Orca home. The data-dir home is only chosen with a non-empty
`auth.json` in it, so the move happens when the PO logs in there and a live Codex head is never
logged out by a merge. Nothing here reads or writes the live homes: every data dir is a temp dir,
and the legacy home is only ever compared as a string.
"""

from __future__ import annotations

import ast
import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from secretary.automations.agents.pipeline import codex_sessions as pipeline_codex_sessions
from secretary.dispatch import commands as dispatch_commands
from secretary.runtime import codex_home as codex_home_module
from secretary.runtime import codex_preflight, heads
from secretary.runtime.codex_preflight import (
    CODEX_HOME_DATA_DIR,
    CODEX_HOME_DEFAULT,
    CODEX_HOME_ENV,
    CODEX_HOME_LEGACY,
    CODEX_HOME_PROFILE,
    codex_home,
    resolve_codex_home,
)
from secretary.runtime.head import render_head_command

SRC = Path(__file__).resolve().parents[1] / "src" / "secretary"
RESOLVERS = {"codex_home", "resolve_codex_home"}


def _without(*names: str) -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if key not in names}


class ResolverOrderTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data_dir = Path(tmp.name) / "data"
        self.data_home = self.data_dir / "codex-home"
        self.data_home.mkdir(parents=True)
        # The suite pins TA_CODEX_HOME for every test; the lower rungs are only reachable without it.
        env = mock.patch.dict(
            os.environ, _without("TA_CODEX_HOME", "SECRETARY_DATA_DIR", "SECRETARY_INSTANCE"), clear=True
        )
        env.start()
        self.addCleanup(env.stop)

    def log_in(self) -> None:
        (self.data_home / "auth.json").write_text('{"tokens": "fixture"}\n', encoding="utf-8")

    def test_the_profile_home_wins_over_every_other_rung(self) -> None:
        self.log_in()
        os.environ["TA_CODEX_HOME"] = "/tmp/env-home"
        home = resolve_codex_home({"codex_home": "/tmp/profile-home"}, data_dir=self.data_dir)
        self.assertEqual((home.path, home.kind), ("/tmp/profile-home", CODEX_HOME_PROFILE))

    def test_ta_codex_home_wins_over_a_logged_in_data_dir_home(self) -> None:
        self.log_in()
        os.environ["TA_CODEX_HOME"] = "/tmp/env-home"
        home = resolve_codex_home({}, data_dir=self.data_dir)
        self.assertEqual((home.path, home.kind), ("/tmp/env-home", CODEX_HOME_ENV))

    def test_without_a_login_in_the_data_dir_home_the_legacy_home_is_chosen(self) -> None:
        home = resolve_codex_home({}, data_dir=self.data_dir)
        self.assertEqual((home.path, home.kind), (CODEX_HOME_DEFAULT, CODEX_HOME_LEGACY))
        self.assertTrue(home.migration_pending)

    def test_an_empty_auth_file_is_not_a_login(self) -> None:
        (self.data_home / "auth.json").write_text("", encoding="utf-8")
        self.assertEqual(codex_home({}, data_dir=self.data_dir), CODEX_HOME_DEFAULT)

    def test_a_login_in_the_data_dir_home_selects_it(self) -> None:
        self.log_in()
        home = resolve_codex_home({}, data_dir=self.data_dir)
        self.assertEqual((home.path, home.kind), (str(self.data_home), CODEX_HOME_DATA_DIR))
        self.assertFalse(home.migration_pending)

    def test_the_data_dir_comes_from_the_environment_the_launch_carries(self) -> None:
        self.log_in()
        os.environ["SECRETARY_DATA_DIR"] = str(self.data_dir)
        self.assertEqual(codex_home({}), str(self.data_home))

    def test_the_data_dir_comes_from_an_explicitly_selected_instance(self) -> None:
        self.log_in()
        instance = self.data_dir.parent / "instance"
        instance.mkdir()
        (instance / "instance.yaml").write_text(
            "version: 1\nname: codex-home\n"
            f"data_dir: {self.data_dir}\n"
            "offsite:\n  instance_remote: https://example.invalid/instance.git\n",
            encoding="utf-8",
        )
        os.environ["SECRETARY_INSTANCE"] = str(instance)
        # The leaf resolver reads no instance file; the installation helper does, and so does a
        # launching process once it has bound the data dir.
        self.assertEqual(codex_home({}), CODEX_HOME_DEFAULT)
        self.assertEqual(codex_home_module.selected_data_dir(), self.data_dir.resolve())
        self.assertEqual(
            codex_home_module.installation_codex_home().path, str(self.data_dir.resolve() / "codex-home")
        )
        with codex_home_module.bound_data_dir():
            self.assertEqual(codex_home({}), str(self.data_dir.resolve() / "codex-home"))
        self.assertNotIn("SECRETARY_DATA_DIR", os.environ)

    def test_no_selected_installation_keeps_the_legacy_home(self) -> None:
        self.log_in()
        self.assertEqual(codex_home({}), CODEX_HOME_DEFAULT)
        self.assertIsNone(codex_home_module.selected_data_dir())
        self.assertEqual(codex_home_module.installation_codex_home().path, CODEX_HOME_DEFAULT)

    def test_a_bound_data_dir_is_scoped_and_never_replaces_the_operators(self) -> None:
        self.log_in()
        with codex_home_module.bound_data_dir(self.data_dir):
            self.assertEqual(os.environ["SECRETARY_DATA_DIR"], str(self.data_dir))
            self.assertEqual(codex_home({}), str(self.data_home))
        self.assertNotIn("SECRETARY_DATA_DIR", os.environ)

        os.environ["SECRETARY_DATA_DIR"] = "/tmp/operator-data"
        with codex_home_module.bound_data_dir(self.data_dir):
            self.assertEqual(os.environ["SECRETARY_DATA_DIR"], "/tmp/operator-data")
        self.assertEqual(os.environ["SECRETARY_DATA_DIR"], "/tmp/operator-data")

    def test_a_dispatcher_operation_launches_against_its_own_data_dir(self) -> None:
        """The production entry binds the runtime's data dir around the operation that launches."""
        self.log_in()
        seen: list[str] = []

        def operation(_runtime: object) -> dict[str, str]:
            seen.append(codex_home({}))
            return {"status": "ok"}

        runtime = mock.Mock(data_dir=self.data_dir)
        args = mock.Mock(instance="unused", data_dir=None, host_mode="noop", owner="secretary-production")
        with (
            mock.patch("secretary.dispatch.commands.runtime_from_args", return_value=runtime),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(dispatch_commands._run_production(args, operation), 0)
        self.assertEqual(seen, [str(self.data_home)])
        self.assertNotIn("SECRETARY_DATA_DIR", os.environ)

    def test_a_head_launched_after_the_login_renders_the_data_dir_home(self) -> None:
        os.environ["SECRETARY_DATA_DIR"] = str(self.data_dir)
        workspace = str(self.data_dir.parent.resolve())
        before = render_head_command({"adapter": "codex"}, workspace=workspace).command
        self.assertTrue(before.startswith(f"CODEX_HOME={CODEX_HOME_DEFAULT} codex "), before)

        self.log_in()

        after = render_head_command({"adapter": "codex"}, workspace=workspace).command
        self.assertTrue(after.startswith(f"CODEX_HOME={self.data_home} codex "), after)

    def test_the_trust_preflight_writes_into_the_home_the_launch_names(self) -> None:
        os.environ["SECRETARY_DATA_DIR"] = str(self.data_dir)
        self.log_in()
        workspace = self.data_dir.parent / "workspace"
        workspace.mkdir()
        codex_preflight.ensure_codex_workspace_trusted({}, str(workspace))
        trusted = (self.data_home / codex_preflight.CODEX_CONFIG_FILE).read_text(encoding="utf-8")
        self.assertIn(str(workspace.resolve()), trusted)

    def test_the_session_reader_follows_the_login(self) -> None:
        os.environ["SECRETARY_DATA_DIR"] = str(self.data_dir)
        self.assertEqual(pipeline_codex_sessions.sessions_root(), Path(CODEX_HOME_DEFAULT) / "sessions")
        self.log_in()
        self.assertEqual(pipeline_codex_sessions.sessions_root(), self.data_home / "sessions")


class NoImportTimeHomeTests(unittest.TestCase):
    def test_heads_has_no_import_time_codex_home(self) -> None:
        self.assertFalse(hasattr(heads, "CODEX_HOME"))

    def test_no_module_resolves_codex_home_at_import(self) -> None:
        """A module-level call would freeze the home before the PO's login could move it."""
        offenders: list[str] = []
        for path in sorted(SRC.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            offenders.extend(f"{path.relative_to(SRC)}:{node.lineno}" for node in _import_time_calls(tree))
        self.assertEqual(offenders, [])

    def test_the_scan_sees_the_constant_this_card_removed(self) -> None:
        tree = ast.parse("CODEX_HOME = codex_home({})\ndef later():\n    return codex_home({})\n")
        self.assertEqual([call.lineno for call in _import_time_calls(tree)], [1])


def _import_time_calls(tree: ast.Module) -> list[ast.Call]:
    """Resolver calls evaluated when the module is imported: anywhere outside a function body."""
    found: list[ast.Call] = []

    def visit(node: ast.AST) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            # Decorators and defaults are evaluated at import; bodies are not.
            for expression in [*getattr(node, "decorator_list", []), *node.args.defaults]:
                visit(expression)
            for expression in node.args.kw_defaults:
                if expression is not None:
                    visit(expression)
            return
        if isinstance(node, ast.Call):
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            if name in RESOLVERS:
                found.append(node)
        for child in ast.iter_child_nodes(node):
            visit(child)

    visit(tree)
    return found


if __name__ == "__main__":
    unittest.main()
