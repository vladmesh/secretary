"""Regression coverage for the shared role-scoped runtime environment."""

from __future__ import annotations

import os
import subprocess
import tempfile
import tomllib
import unittest
from io import StringIO
from pathlib import Path
from unittest import mock

from secretary import role_env as secretary_role_env
from secretary.board_transport import ensure as ensure_board_transport
from triggered_agents.runtime import kanboard, role_env
from triggered_agents.runtime.head.command import wrap_role_command


class RuntimeEnvPathTests(unittest.TestCase):
    def test_both_documented_override_names_resolve_the_runtime_env_file(self) -> None:
        with mock.patch.dict(
            os.environ, {"SECRETARY_RUNTIME_ENV_FILE": "/tmp/secretary-runtime.env"}, clear=True
        ):
            self.assertEqual(role_env.runtime_env_path(), Path("/tmp/secretary-runtime.env"))
        with mock.patch.dict(os.environ, {"TA_RUNTIME_ENV_FILE": "/tmp/ta-runtime.env"}, clear=True):
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
    @staticmethod
    def _ruff_version(root: Path) -> str:
        pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        dependency = next(
            item for item in pyproject["project"]["optional-dependencies"]["dev"] if item.startswith("ruff==")
        )
        return dependency.removeprefix("ruff==")

    def test_worker_and_reviewer_role_paths_expose_the_workspace_pinned_ruff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            expected = self._ruff_version(Path(__file__).resolve().parents[1])
            (root / "src").symlink_to(Path(__file__).resolve().parents[1] / "src", target_is_directory=True)
            ruff = root / role_env.WORKSPACE_ENV_DIR / "bin" / "ruff"
            ruff.parent.mkdir(parents=True)
            ruff.write_text(
                "#!/bin/sh\n"
                'case "$1" in\n'
                f"  --version) echo 'ruff {expected}' ;;\n"
                '  check) test "$2" = changed.py ;;\n'
                '  format) test "$2" = --check && test "$3" = changed.py ;;\n'
                "  *) exit 2 ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            ruff.chmod(0o755)
            python = root / role_env.WORKSPACE_ENV_DIR / "bin" / "python3"
            python.symlink_to("/usr/bin/python3")
            ensure_board_transport(root, allow_default=True)
            base_env = {
                "PATH": os.environ["PATH"],
                "SECRETARY_INSTANCE": str(root),
                "SECRETARY_RUNTIME_ENV_FILE": str(root / "runtime.env"),
                "TA_SECRETARY_REPO": str(root),
            }
            for role in ("worker", "reviewer"):
                with self.subTest(role=role):
                    with mock.patch.dict(os.environ, base_env, clear=True):
                        version_command = wrap_role_command(role, "ruff --version", workspace=str(root))
                        lint_commands = (
                            wrap_role_command(
                                role,
                                "printf '%s\\0' changed.py | xargs -0r ruff check",
                                workspace=str(root),
                            ),
                            wrap_role_command(
                                role,
                                "printf '%s\\0' changed.py | xargs -0r ruff format --check",
                                workspace=str(root),
                            ),
                        )
                    version = subprocess.run(
                        ["/bin/sh", "-c", version_command],
                        cwd=root,
                        env=base_env,
                        check=False,
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(version.returncode, 0, version.stderr)
                    self.assertEqual(version.stdout.strip(), f"ruff {expected}")
                    for command in lint_commands:
                        completed = subprocess.run(
                            ["/bin/sh", "-c", command],
                            cwd=root,
                            env=base_env,
                            check=False,
                            capture_output=True,
                            text=True,
                        )
                        self.assertEqual(completed.returncode, 0, completed.stderr)

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

    def test_memory_bearer_capability_can_only_come_from_the_launch_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "runtime.env"
            env_file.write_text("SECRETARY_MEMORY_ACCESS_TOKEN=forged\n", encoding="utf-8")
            env = role_env.runtime_env("worker", base_env={"PATH": "/usr/bin"}, env_file=env_file)
            launched = role_env.runtime_env(
                "worker",
                base_env={"PATH": "/usr/bin", role_env.MEMORY_ACCESS_TOKEN_ENV: "launch-bound"},
                env_file=env_file,
            )

        self.assertNotIn(role_env.MEMORY_ACCESS_TOKEN_ENV, env)
        self.assertEqual(launched[role_env.MEMORY_ACCESS_TOKEN_ENV], "launch-bound")

    def test_scheduled_memory_roles_accept_only_a_launch_bound_bearer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "runtime.env"
            env_file.write_text("SECRETARY_MEMORY_ACCESS_TOKEN=forged\n", encoding="utf-8")
            for role in ("curator", "retro", "steward"):
                with self.subTest(role=role):
                    env = role_env.runtime_env(role, base_env={"PATH": "/usr/bin"}, env_file=env_file)
                    launched = role_env.runtime_env(
                        role,
                        base_env={"PATH": "/usr/bin", role_env.MEMORY_ACCESS_TOKEN_ENV: "launch-bound"},
                        env_file=env_file,
                    )
                    self.assertNotIn(role_env.MEMORY_ACCESS_TOKEN_ENV, env)
                    self.assertEqual(launched[role_env.MEMORY_ACCESS_TOKEN_ENV], "launch-bound")

    def test_every_merged_role_builds_an_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            env_file = instance / "runtime.env"
            env_file.write_text("EXAMPLE_API_TOKEN=secret\n", encoding="utf-8")
            transport = ensure_board_transport(instance, allow_default=True).transport
            for role in ("worker", "reviewer", "observer", "pipeline", "steward", "retro", "curator"):
                with self.subTest(role=role):
                    env = role_env.runtime_env(
                        role,
                        base_env={"PATH": "/usr/bin", "SECRETARY_INSTANCE": str(instance)},
                        env_file=env_file,
                    )
                    self.assertNotIn("KANBOARD_API_TOKEN", env)
                    with mock.patch.dict(os.environ, env, clear=True):
                        self.assertEqual(kanboard._creds(), transport)

    def test_pipeline_is_explicitly_subject_to_the_transport_gate(self) -> None:
        self.assertIn("pipeline", role_env.BOARD_TRANSPORT_ROLES)

    def test_required_role_rendering_does_not_probe_board_transport(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            with mock.patch("triggered_agents.runtime.board_transport.resolve", side_effect=AssertionError):
                env = role_env.runtime_env(
                    "worker",
                    base_env={"PATH": "/usr/bin", "SECRETARY_INSTANCE": str(instance)},
                    env_file=instance / "runtime.env",
                )
        self.assertEqual(env["SECRETARY_INSTANCE"], str(instance))


class BoardTransportGateTests(unittest.TestCase):
    """The Kanboard tuple is a requirement of one backend, not of every launch."""

    def test_only_an_explicit_postgres_selector_lifts_the_requirement(self) -> None:
        self.assertFalse(role_env.board_transport_required({role_env.CARD_BACKEND_ENV: "postgres"}))
        self.assertFalse(role_env.board_transport_required({role_env.CARD_BACKEND_ENV: " postgres "}))
        for value in ("kanboard", "", "Postgres", "postgresql", "nonsense"):
            with self.subTest(value=value):
                self.assertTrue(role_env.board_transport_required({role_env.CARD_BACKEND_ENV: value}))
        self.assertTrue(role_env.board_transport_required({}))

    def _exec(self, instance: Path, backend: str) -> tuple[int, list[str]]:
        """Run the exec boundary for a transport role without letting it replace the process."""
        env_file = instance / "runtime.env"
        env_file.write_text(f"{role_env.CARD_BACKEND_ENV}={backend}\n", encoding="utf-8")
        launched: list[str] = []
        with (
            mock.patch.dict(
                os.environ,
                {"PATH": "/usr/bin", "SECRETARY_INSTANCE": str(instance)},
                clear=True,
            ),
            mock.patch.object(role_env.os, "execvpe", side_effect=lambda file, *_: launched.append(file)),
        ):
            code = role_env.main(
                ["exec", "--role", "pipeline", "--env-file", str(env_file), "--", "/bin/true"]
            )
        return code, launched

    def test_a_postgres_installation_execs_without_a_kanboard_tuple(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            code, launched = self._exec(Path(tmp), "postgres")
        self.assertEqual(launched, ["/bin/true"])
        self.assertIsNone(code)

    def test_a_kanboard_installation_still_refuses_without_a_tuple(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch("sys.stderr", new_callable=StringIO) as stderr,
        ):
            code, launched = self._exec(Path(tmp), "kanboard")
        self.assertEqual(code, 125)
        self.assertEqual(launched, [])
        self.assertIn("board transport configuration is unavailable", stderr.getvalue())

    def test_a_kanboard_installation_with_a_tuple_execs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            ensure_board_transport(instance, allow_default=True)
            code, launched = self._exec(instance, "kanboard")
        self.assertEqual(launched, ["/bin/true"])
        self.assertIsNone(code)


if __name__ == "__main__":
    unittest.main()
