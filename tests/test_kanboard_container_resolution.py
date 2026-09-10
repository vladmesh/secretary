"""The raw Kanboard dump finds its container on a real Compose installation.

secretary-1607: `raw_kanboard_dump` used to copy from the literal `cp-kanboard`, a name no
installation has. The live cutover on 2026-09-09 failed at its first mutating phase because the
installed container is `secretary-kanboard-1`, and only got past it because the operator renamed
the running container by hand. These tests bring up a throwaway Compose project whose name is
nothing like `cp`, and prove the dump takes itself from that project's `kanboard` service with no
rename and no explicit container -- and that an absent or stopped service is a refusal that names
the Compose file and the service rather than a fallback to the old literal.

Docker is a hard requirement here, as it is for the other tests in this suite: there is no skip,
because a green skip is exactly how the defect survived to a live run.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from secretary.bootstrap import KANBOARD_IMAGE
from secretary.data import raw_kanboard_dump, resolve_kanboard_container
from secretary.infra.kanboard_compose import KANBOARD_COMPOSE_SERVICE

STOP_LISTING_TIMEOUT_SECONDS = 30

COMPOSE_TEXT = f"""services:
  {KANBOARD_COMPOSE_SERVICE}:
    image: {KANBOARD_IMAGE}
    restart: "no"
    environment:
      API_AUTHENTICATION_TOKEN: ${{KANBOARD_API_TOKEN}}
    volumes:
      - kanboard-data:/var/www/app/data
volumes:
  kanboard-data:
"""


class KanboardContainerResolutionTests(unittest.TestCase):
    """One throwaway Compose project, shared by the whole class: bringing it up is slow."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="kanboard-resolution-")
        root = Path(cls._tmp.name)
        # Not `cp`, and not the directory-derived default either: the container this project
        # creates is `<project>-kanboard-1`, which is the shape the installation actually has.
        cls.project = f"secretary-1607-{os.getpid()}"
        cls.compose_file = root / "kanboard-compose.yml"
        cls.compose_file.write_text(COMPOSE_TEXT, encoding="utf-8")
        cls.env_file = root / "board-transport.env"
        cls.env_file.write_text("KANBOARD_API_TOKEN=resolution-fixture\n", encoding="utf-8")
        cls.env_file.chmod(0o600)
        # Registered before the project exists: `doClassCleanups` runs them even when the bring-up
        # below raises, so a half-started fixture still gets torn down.
        cls.addClassCleanup(cls._tmp.cleanup)
        cls.addClassCleanup(cls._down)
        cls._compose("up", "--detach")

    @classmethod
    def _compose(cls, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "docker",
                "compose",
                "--project-name",
                cls.project,
                "--env-file",
                str(cls.env_file),
                "-f",
                str(cls.compose_file),
                *arguments,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=300,
        )

    @classmethod
    def _down(cls) -> None:
        subprocess.run(
            [
                "docker",
                "compose",
                "--project-name",
                cls.project,
                "--env-file",
                str(cls.env_file),
                "-f",
                str(cls.compose_file),
                "down",
                "--volumes",
                "--remove-orphans",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=300,
        )

    def setUp(self) -> None:
        # Every test but the two refusals wants the service up; restore it for the next one.
        self.addCleanup(self._compose, "start")

    def _listed(self, template: str) -> str:
        # The container listing, which is what `resolve_kanboard_container` reads too.
        listed = subprocess.run(
            [
                "docker",
                "ps",
                "--all",
                "--filter",
                f"label=com.docker.compose.project={self.project}",
                "--format",
                template,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return listed.stdout

    def _container_name(self) -> str:
        listed = self._listed("{{.Names}}")
        names = listed.split()
        self.assertEqual(len(names), 1, listed)
        return names[0]

    def _stop_until_listed_as_exited(self, container: str) -> None:
        # `docker compose stop` returns when the engine wakes its stop waiters, and an engine older
        # than 28.3 (moby#50133) does that before it records the exit in the listing: `docker ps`
        # can still say `running` for a container that has already stopped. CI runs such an engine,
        # and the dump then copied from the stopped container instead of refusing. Wait, bounded,
        # until the listing itself shows the stop.
        self._compose("stop")
        deadline = time.monotonic() + STOP_LISTING_TIMEOUT_SECONDS
        while (state := self._listed("{{.State}}").strip()) != "exited":
            if time.monotonic() >= deadline:
                inspected = subprocess.run(
                    ["docker", "inspect", "--format", "{{.State.Status}}", container],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.fail(
                    f"{container} is not listed as exited {STOP_LISTING_TIMEOUT_SECONDS}s after "
                    f"`docker compose stop`: listing says {state!r}, inspect says "
                    f"{(inspected.stdout or inspected.stderr).strip()!r}"
                )
            time.sleep(0.05)

    def test_dump_resolves_the_installed_container_without_a_rename(self) -> None:
        expected = self._container_name()
        self.assertNotEqual(expected, "cp-kanboard")
        self.assertTrue(expected.startswith(self.project), expected)

        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "secretary-data"

            dump = raw_kanboard_dump(data_dir, compose_file=self.compose_file)

            manifest = json.loads((dump.dump_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertTrue((dump.dump_dir / "data").is_dir())

        self.assertEqual(dump.source, f"{expected}:/var/www/app/data")
        self.assertEqual(manifest["container"], expected)
        self.assertEqual(manifest["container_origin"], "compose-service")
        self.assertEqual(manifest["compose_file"], str(self.compose_file))
        self.assertEqual(manifest["compose_service"], "kanboard")
        resolved = resolve_kanboard_container(compose_file=self.compose_file)
        self.assertEqual(manifest["container_id"], resolved.container_id)
        # The dump must not have touched the container's identity.
        self.assertEqual(self._container_name(), expected)

    def test_refusal_names_the_compose_file_and_service_when_the_container_is_stopped(self) -> None:
        expected = self._container_name()
        self._stop_until_listed_as_exited(expected)

        with self.assertRaises(RuntimeError) as caught:
            raw_kanboard_dump(Path(self._tmp.name) / "unused-data", compose_file=self.compose_file)

        message = str(caught.exception)
        self.assertIn(str(self.compose_file), message)
        self.assertIn("'kanboard'", message)
        self.assertIn("not running", message)
        self.assertIn(expected, message)
        self.assertNotIn("cp-kanboard", message)
        # The refusal starts nothing: the container is still stopped.
        self.assertEqual(self._container_name(), expected)

    def test_refusal_names_the_compose_file_and_service_when_the_service_is_absent(self) -> None:
        missing = Path(self._tmp.name) / "absent-compose.yml"
        missing.write_text(COMPOSE_TEXT, encoding="utf-8")

        with self.assertRaises(RuntimeError) as caught:
            resolve_kanboard_container(compose_file=missing)

        message = str(caught.exception)
        self.assertIn(str(missing), message)
        self.assertIn("'kanboard'", message)
        self.assertIn("no container", message)
        self.assertNotIn("cp-kanboard", message)


if __name__ == "__main__":  # pragma: no cover - convenience for a single-module run
    unittest.main()
