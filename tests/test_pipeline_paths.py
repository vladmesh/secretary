"""Path/env resolving after the control-panel / triggered-agents decommission (secretary-624).

These cover the runtime path defaults that used to point at removed checkouts: the instance and
checkout defaults, the launcher PYTHONPATH, the OpenRouter key location, and the pause-flag
candidate lists. They pin the new behaviour so a future edit can't silently repoint them back at
a dead path.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from secretary import dispatcher_pause
from secretary import role_env as secretary_role_env
from triggered_agents.agents.pipeline import health
from triggered_agents.runtime import paths
from triggered_agents.runtime import role_env as runtime_role_env


class PortableDefaultTests(unittest.TestCase):
    """What install, upgrade, role delivery and runtime startup fall back to with nothing set.

    Every one of these used to be an absolute path under the author's home, which made the product
    installable for exactly one account on one machine.
    """

    SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"

    def test_the_instance_and_checkout_defaults_follow_the_running_user(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.dict(os.environ, {"HOME": tmp}, clear=False):
            self.assertEqual(paths.default_instance_path(), Path(tmp) / "secretary-instance")
            self.assertEqual(paths.default_product_root(), Path(tmp) / "secretary")

    def test_an_instance_is_named_by_its_directory_or_its_config_file(self):
        self.assertEqual(paths.instance_dir("/srv/inst/instance.yaml"), Path("/srv/inst"))
        self.assertEqual(paths.instance_dir("/srv/inst"), Path("/srv/inst"))

    def test_the_runtime_env_file_defaults_under_the_running_users_instance(self):
        expected = str(paths.default_instance_path() / "runtime.env")

        self.assertEqual(runtime_role_env.RUNTIME_ENV_DEFAULT, expected)
        self.assertEqual(secretary_role_env.RUNTIME_ENV_DEFAULT, expected)

    def test_no_shipped_entry_point_pins_a_particular_home(self):
        for script in sorted(self.SCRIPTS.glob("*.sh")):
            with self.subTest(script.name):
                body = script.read_text(encoding="utf-8")
                self.assertNotIn("/home/", body)
                self.assertIn("$HOME/", body)


class LauncherCheckoutTests(unittest.TestCase):
    """Which checkout a launched process imports the product from.

    All three launchers answer the same question and have to answer it the same way: the explicit
    ``TA_RUNTIME_PYTHONPATH``, then the checkout the installation is configured with, then a home
    default. A launcher that skipped the configured name would start an installation materialized
    from an alternate checkout out of ``~/secretary`` — a version nobody upgraded, or nothing.
    """

    SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
    LAUNCHERS = ("secretary-agent-gate.sh", "secretary-start.sh")

    def script_pythonpath(self, script: str, env: dict[str, str]) -> str:
        """Run the shipped PYTHONPATH assignment itself, in a shell with only this environment."""
        line = next(
            line
            for line in (self.SCRIPTS / script).read_text(encoding="utf-8").splitlines()
            if line.startswith("export PYTHONPATH=")
        )
        result = subprocess.run(
            ["/bin/sh", "-c", f'set -u\n{line}\nprintf "%s" "$PYTHONPATH"'],
            capture_output=True, text=True, env=env, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_every_launcher_prefers_the_explicit_runtime_path(self):
        env = {"HOME": "/home/nobody", "TA_RUNTIME_PYTHONPATH": "/srv/named",
               "TA_SECRETARY_REPO": "/srv/configured"}
        for script in self.LAUNCHERS:
            with self.subTest(script):
                self.assertEqual(self.script_pythonpath(script, env), "/srv/named")
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(runtime_role_env.runtime_pythonpath(), "/srv/named")

    def test_every_launcher_falls_back_to_the_configured_checkout(self):
        env = {"HOME": "/home/nobody", "TA_SECRETARY_REPO": "/srv/configured"}
        for script in self.LAUNCHERS:
            with self.subTest(script):
                self.assertEqual(self.script_pythonpath(script, env), "/srv/configured")
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(runtime_role_env.runtime_pythonpath(), "/srv/configured")

    def test_a_shell_launcher_with_nothing_configured_uses_the_running_users_home(self):
        env = {"HOME": "/home/nobody"}
        for script in self.LAUNCHERS:
            with self.subTest(script):
                self.assertEqual(
                    self.script_pythonpath(script, env), "/home/nobody/secretary"
                )

    def test_the_module_launcher_with_nothing_configured_uses_its_own_checkout(self):
        """A module already imported knows its tree is importable; a home path may not exist."""
        with mock.patch.dict(os.environ, {"HOME": "/home/nobody"}, clear=True):
            self.assertEqual(
                runtime_role_env.runtime_pythonpath(), str(runtime_role_env.REPO_ROOT)
            )

    def test_a_shell_launcher_keeps_an_inherited_pythonpath_behind_the_checkout(self):
        env = {"HOME": "/home/nobody", "TA_SECRETARY_REPO": "/srv/configured",
               "PYTHONPATH": "/srv/extra"}
        for script in self.LAUNCHERS:
            with self.subTest(script):
                self.assertEqual(
                    self.script_pythonpath(script, env), "/srv/configured:/srv/extra"
                )

    def test_the_launched_role_command_carries_the_configured_checkout(self):
        """`wrap_shell_command` renders the path the launcher resolved into the command itself."""
        with mock.patch.dict(os.environ, {"HOME": "/home/nobody",
                                          "TA_SECRETARY_REPO": "/srv/configured"}, clear=True):
            command = runtime_role_env.wrap_shell_command("steward", "true")

        self.assertIn("PYTHONPATH=/srv/configured", command)


class OpenRouterKeyTests(unittest.TestCase):
    def test_default_env_file_is_hermes(self):
        self.assertEqual(health._OPENROUTER_ENV_FILE, Path.home() / ".hermes" / ".env")

    def test_reads_hermes_style_key_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text("# comment\nOPENROUTER_API_KEY=sk-or-hermes\n", encoding="utf-8")
            with mock.patch.object(health, "_OPENROUTER_ENV_FILE", env), \
                 mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("TA_OPENROUTER_KEY", None)
                self.assertEqual(health._read_openrouter_key(), "sk-or-hermes")

    def test_reads_legacy_key_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text('open_router_key="sk-or-legacy"\n', encoding="utf-8")
            with mock.patch.object(health, "_OPENROUTER_ENV_FILE", env), \
                 mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("TA_OPENROUTER_KEY", None)
                self.assertEqual(health._read_openrouter_key(), "sk-or-legacy")

    def test_env_key_override_wins(self):
        override = os.environ.get("TA_OPENROUTER_KEY")
        with mock.patch.dict(os.environ, {"TA_OPENROUTER_KEY": "sk-or-override"}):
            self.assertEqual(health._read_openrouter_key(), "sk-or-override")
        self.assertEqual(os.environ.get("TA_OPENROUTER_KEY"), override)

    def test_missing_file_yields_no_key(self):
        with mock.patch.object(health, "_OPENROUTER_ENV_FILE", Path("/no/such/hermes/.env")), \
             mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TA_OPENROUTER_KEY", None)
            self.assertIsNone(health._read_openrouter_key())


class LegacyMirrorPathTests(unittest.TestCase):
    """Where a pause mirrors its legacy flag. The removed `triggered-agents` checkout is not it:
    a mirror written there is a file none of the background roles read."""

    ENV = ("SECRETARY_LEGACY_PAUSE_FILE", "SECRETARY_LEGACY_PIPELINE_STATE_DIR",
           "TA_PIPELINE_STATE_DIR", "TA_STATE")

    def test_legacy_mirror_path_is_not_the_removed_checkout(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            for name in self.ENV:
                os.environ.pop(name, None)
            path = dispatcher_pause.legacy_mirror_path()
        self.assertFalse(str(path).endswith("triggered-agents/state/pipeline/pause.json"))
        self.assertNotIn(Path.home() / "triggered-agents", path.parents)

    def test_legacy_mirror_path_points_at_secretary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.dict(os.environ, {"TA_WORKSPACES_ROOT": str(root)}, clear=False):
                for name in self.ENV:
                    os.environ.pop(name, None)
                path = dispatcher_pause.legacy_mirror_path()
        self.assertEqual(path, root / "secretary" / "pipeline" / "state" / "pipeline" / "pause.json")


if __name__ == "__main__":
    unittest.main()
