"""The default Orca discovery fixture is suite-wide and overridable.

secretary-748: the default unit-test run must not depend on whether the host
running it happens to have Orca installed. tests/__init__.py patches
Orca discovery to the repo fixture before any test module is imported; these
tests prove that default wins even when a real, discoverable executable is
present, and that a test can still opt in to the real resolver's actual
filesystem-probing behaviour locally.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from secretary import host_apply
from secretary.host_apply import resolve_systemd_layout
from tests import _find_orca_patcher
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
                    {},
                    instance_path=instance_path,
                    data_dir=root / "data",
                    runtime_user="operator",
                )

            self.assertEqual(layout.orca_executable, _FIXTURE_ORCA)
            self.assertNotEqual(layout.orca_executable, discoverable)

    def test_a_test_can_still_opt_in_to_real_host_style_discovery(self):
        # This opt-in restores the *real* find_orca_executable (undoing the
        # tests/__init__.py default for the duration of the `with` block)
        # rather than stubbing a return_value, so it exercises the actual
        # filesystem probe against the fixture-owned home that
        # legacy_orca_runtime sets up, proving the escape hatch reaches real
        # resolver code and not just a second layer of mocking.
        #
        # find_orca_executable checks /usr/local/bin/orca *before* the
        # runtime user's legacy path (secretary/host_apply.py). That pinned
        # candidate is a real, unsandboxed host path outside anything
        # legacy_orca_runtime controls: on a host that happens to have Orca
        # installed there, the unpatched real resolver would return it
        # instead of the fixture-owned legacy executable this test asserts
        # on, making the test's outcome depend on host state again. Model
        # that candidate as unavailable explicitly so the opt-in's result
        # depends only on the fixture, not on whether this machine has a
        # pinned Orca.
        real_find_orca_executable = _find_orca_patcher.temp_original
        real_is_executable = host_apply._is_executable
        pinned_candidate = Path("/usr/local/bin/orca")

        def _is_executable_without_pinned_candidate(path):
            if path == pinned_candidate:
                return False
            return real_is_executable(path)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance_path = root / "instance"
            instance_path.mkdir()
            with (
                legacy_orca_runtime(root) as discoverable,
                mock.patch(
                    "secretary.host_apply.find_orca_executable",
                    side_effect=real_find_orca_executable,
                ),
                mock.patch(
                    "secretary.host_apply._is_executable",
                    side_effect=_is_executable_without_pinned_candidate,
                ),
            ):
                layout = resolve_systemd_layout(
                    {},
                    instance_path=instance_path,
                    data_dir=root / "data",
                    runtime_user="operator",
                )

            self.assertEqual(layout.orca_executable, discoverable)


if __name__ == "__main__":
    unittest.main()
