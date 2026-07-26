"""Hermetic default for the unit-test run: Orca discovery never leaves the repo.

``python -m unittest`` imports this package before it imports any ``test_*``
module, so the two patches below are live before a single test can reach
``resolve_systemd_layout``/``resolve_packaged`` and discover a real host
executable. Without this, the same checkout is green on a developer box with
Orca installed and red on a bare CI runner (or vice versa) purely from
process discovery order (secretary-705, secretary-738, secretary-748).

A test that needs real host resolution opts in locally, the same way the
rest of the suite already overrides other host-apply seams: wrap the call in
its own ``mock.patch("secretary.host_apply.find_orca_executable", ...)`` (or
``...pinned_orca_executable``) with whatever value the scenario needs. That
local patch simply shadows the process-wide default for the duration of the
``with`` block; nothing needs to be undone.

If a test fails with "Orca executable for <user> is unavailable", it means
production code reached real host discovery without going through either
this default or a local opt-in patch: look for a code path that calls
``find_orca_executable``/``pinned_orca_executable`` directly instead of
through ``resolve_systemd_layout``/``resolve_packaged``.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

_FIXTURE_ORCA = Path(__file__).resolve().parent / "fixtures" / "legacy-orca"

_find_orca_patcher = mock.patch(
    "secretary.host_apply.find_orca_executable", return_value=_FIXTURE_ORCA
)
_find_orca_patcher.start()

# pinned_orca_executable() reads /usr/local/bin/orca straight off the host,
# outside the find_orca_executable seam above. Default it to "no pinned
# runtime" so secretary.bootstrap's install-vs-skip branch is deterministic
# regardless of whether this machine happens to have Orca installed there.
_pinned_orca_patcher = mock.patch(
    "secretary.host_apply.pinned_orca_executable", return_value=None
)
_pinned_orca_patcher.start()
