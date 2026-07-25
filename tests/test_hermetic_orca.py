"""The default Orca discovery fixture is suite-wide and overridable.

secretary-748: the default unit-test run must not depend on whether the host
running it happens to have Orca installed. tests/__init__.py patches
Orca discovery to the repo fixture before any test module is imported; these
tests prove that default wins even when a real, discoverable executable is
present, and that a test can still opt in to a different value locally.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from secretary.host_apply import resolve_systemd_layout
from tests.orca_fixtures import legacy_orca_runtime

_FIXTURE_ORCA = Path(__file__).resolve().parent / "fixtures" / "legacy-orca"


class HermeticOrcaDiscoveryTests(unittest.TestCase):
    def test_default_fixture_wins_over_a_discoverable_host_executable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance_path = root / "instance"
            instance_path.mkdir()
            with legacy_orca_runtime(root) as discoverable:
                # No local find_orca_executable patch here: this exercises the
                # process-wide default installed by tests/__init__.py, with a
                # real executable sitting exactly where real discovery would
                # find it (operator/.local/bin/orca).
                layout = resolve_systemd_layout(
                    {}, instance_path=instance_path, runtime_user="operator",
                )

            self.assertEqual(layout.orca_executable, _FIXTURE_ORCA)
            self.assertNotEqual(layout.orca_executable, discoverable)

    def test_a_test_can_still_opt_in_to_real_host_style_discovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance_path = root / "instance"
            instance_path.mkdir()
            with legacy_orca_runtime(root) as discoverable:
                with mock.patch(
                    "secretary.host_apply.find_orca_executable", return_value=discoverable
                ):
                    layout = resolve_systemd_layout(
                        {}, instance_path=instance_path, runtime_user="operator",
                    )

            self.assertEqual(layout.orca_executable, discoverable)


if __name__ == "__main__":
    unittest.main()
