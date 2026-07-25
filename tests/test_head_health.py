from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from secretary.head_health import HeadHealth


class Catalog:
    def head_profile(self, head: str):
        return {"resource": head}

    def resource(self, resource: str):
        return {"probe": f"probe {resource}"}


class HeadHealthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.health = HeadHealth(Catalog(), Path(self.tmpdir.name))

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_auth_failure_is_cached_and_blocks_launch(self) -> None:
        failed = subprocess.CompletedProcess("probe", 1, "", "Login expired. Please run /login")
        with mock.patch("secretary.head_health.subprocess.run", return_value=failed) as run:
            first = self.health.check("openai-sub")
            second = self.health.check("openai-sub")

        self.assertEqual(first.status, "unauthenticated")
        self.assertFalse(first.launch_allowed)
        self.assertTrue(second.cached)
        run.assert_called_once()

    def test_provider_failure_is_unavailable(self) -> None:
        failed = subprocess.CompletedProcess("probe", 1, "", "503 biscuit_baker_service_me_circuit_open")
        with mock.patch("secretary.head_health.subprocess.run", return_value=failed):
            result = self.health.check("openai-sub")

        self.assertEqual(result.status, "unavailable")
        self.assertFalse(result.launch_allowed)

    def test_probe_failure_is_unknown_and_allows_launch(self) -> None:
        with mock.patch("secretary.head_health.subprocess.run", side_effect=subprocess.TimeoutExpired("probe", 20)):
            result = self.health.check("openai-sub")

        self.assertEqual(result.status, "unknown")
        self.assertTrue(result.launch_allowed)

