"""Regression coverage for the shared role-scoped runtime environment."""

from __future__ import annotations

import os
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from secretary.runtime import role_env
from secretary.runtime.head.command import wrap_role_command
from tests.retired_board import LEGACY_ENV, STALE_FILE, legacy_runtime_lines, write_stale_leftovers


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
                legacy_runtime_lines() + "SECRETARY_OBSERVER_SPRINT=sprint:forged\n"
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

    def test_every_merged_role_builds_an_environment_with_no_transport_requirement(self) -> None:
        """No role needs, reads or passes on the retired transport, even where its leftovers remain."""
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            env_file = instance / "runtime.env"
            env_file.write_text("EXAMPLE_API_TOKEN=secret\n" + legacy_runtime_lines(), encoding="utf-8")
            write_stale_leftovers(instance)
            for role in ("worker", "reviewer", "observer", "pipeline", "steward", "retro", "curator"):
                with self.subTest(role=role):
                    env = role_env.runtime_env(
                        role,
                        base_env={"PATH": "/usr/bin", "SECRETARY_INSTANCE": str(instance)},
                        env_file=env_file,
                    )
                    self.assertEqual(env["SECRETARY_INSTANCE"], str(instance))
                    for name in LEGACY_ENV:
                        self.assertNotIn(name, env)
                    self.assertFalse(any(STALE_FILE in value for value in env.values()))

    def test_a_role_builds_its_environment_with_no_transport_file_and_no_runtime_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            env = role_env.runtime_env(
                "worker",
                base_env={"PATH": "/usr/bin", "SECRETARY_INSTANCE": str(instance)},
                env_file=instance / "runtime.env",
            )
        self.assertEqual(env["SECRETARY_INSTANCE"], str(instance))


class NoBoardTransportGateTests(unittest.TestCase):
    """The board is the PostgreSQL store, so no role's launch demands a transport."""

    def test_every_board_role_execs_without_a_transport(self) -> None:
        for role in ("pipeline", "observer", "steward", "retro"):
            with self.subTest(role=role), tempfile.TemporaryDirectory() as tmp:
                instance = Path(tmp)
                env_file = instance / "runtime.env"
                env_file.write_text("OTHER=value\n", encoding="utf-8")
                launched: list[str] = []
                with (
                    mock.patch.dict(
                        os.environ,
                        {"PATH": "/usr/bin", "SECRETARY_INSTANCE": str(instance)},
                        clear=True,
                    ),
                    mock.patch.object(
                        role_env.os, "execvpe", side_effect=lambda file, *_, sink=launched: sink.append(file)
                    ),
                ):
                    code = role_env.main(
                        ["exec", "--role", role, "--env-file", str(env_file), "--", "/bin/true"]
                    )
                self.assertIsNone(code)
                self.assertEqual(launched, ["/bin/true"])


if __name__ == "__main__":
    unittest.main()
