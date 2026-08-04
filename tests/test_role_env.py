"""Regression coverage for the shared role-scoped runtime environment."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from secretary import role_env as secretary_role_env
from secretary.board_transport import ensure as ensure_board_transport
from triggered_agents.runtime import role_env
from triggered_agents.runtime import kanboard


class RuntimeEnvPathTests(unittest.TestCase):
    def test_both_documented_override_names_resolve_the_runtime_env_file(self) -> None:
        with mock.patch.dict(
            os.environ, {"SECRETARY_RUNTIME_ENV_FILE": "/tmp/secretary-runtime.env"}, clear=True
        ):
            self.assertEqual(role_env.runtime_env_path(), Path("/tmp/secretary-runtime.env"))
        with mock.patch.dict(
            os.environ, {"TA_RUNTIME_ENV_FILE": "/tmp/ta-runtime.env"}, clear=True
        ):
            self.assertEqual(role_env.runtime_env_path(), Path("/tmp/ta-runtime.env"))
        with mock.patch.dict(
            os.environ,
            {
                "TA_RUNTIME_ENV_FILE": "/tmp/ta-runtime.env",
                "SECRETARY_RUNTIME_ENV_FILE": "/tmp/secretary-runtime.env",
            },
            clear=True,
        ):
            self.assertEqual(role_env.runtime_env_path(), Path("/tmp/secretary-runtime.env"))

    def test_secretary_reexports_the_shared_runtime_environment(self) -> None:
        self.assertIs(secretary_role_env.runtime_env, role_env.runtime_env)
        self.assertIs(secretary_role_env.ROLE_ALLOWLIST, role_env.ROLE_ALLOWLIST)
        self.assertFalse(hasattr(secretary_role_env, "RUNTIME_ENV_FILE_ENV"))


class RuntimeEnvRoleTests(unittest.TestCase):
    def test_launcher_only_observer_identity_cannot_come_from_runtime_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "runtime.env"
            env_file.write_text(
                "KANBOARD_URL=https://board.invalid\n"
                "KANBOARD_API_USER=bot\n"
                "KANBOARD_API_TOKEN=token\n"
                "SECRETARY_OBSERVER_SPRINT=sprint:forged\n"
                "SECRETARY_OBSERVER_GENERATION=forged\n",
                encoding="utf-8",
            )
            env = role_env.runtime_env("observer", base_env={"PATH": "/usr/bin"}, env_file=env_file)

        self.assertNotIn(role_env.OBSERVER_SPRINT_ENV, env)
        self.assertNotIn(role_env.OBSERVER_GENERATION_ENV, env)

    def test_every_merged_role_builds_an_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            env_file = instance / "runtime.env"
            env_file.write_text("EXAMPLE_API_TOKEN=secret\n", encoding="utf-8")
            transport, _ = ensure_board_transport(instance)
            for role in ("worker", "reviewer", "observer", "pipeline", "steward", "retro", "curator"):
                with self.subTest(role=role):
                    env = role_env.runtime_env(
                        role,
                        base_env={"PATH": "/usr/bin", "SECRETARY_INSTANCE": str(instance)},
                        env_file=env_file,
                        require=True,
                    )
                    self.assertNotIn("KANBOARD_API_TOKEN", env)
                    with mock.patch.dict(os.environ, env, clear=True):
                        self.assertEqual(kanboard._creds(), (transport.url, transport.user, transport.token))


if __name__ == "__main__":
    unittest.main()
