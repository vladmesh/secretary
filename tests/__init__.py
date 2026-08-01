"""Hermetic defaults for the unit-test run: Orca discovery and Kanboard reads
never leave the repo/process.

``python -m unittest`` imports this package before it imports any ``test_*``
module, so the patches below are live before a single test can reach
``resolve_systemd_layout``/``resolve_packaged`` and discover a real host
executable, or reach ``secretary.status.collect_status`` and dial a real
Kanboard. Without this, the same checkout is green on a developer box with
Orca installed and red on a bare CI runner (or vice versa) purely from
process discovery order (secretary-705, secretary-738, secretary-748), and a
worker/reviewer/operator shell that inherits a live installation's
``KANBOARD_*`` variables turns the unit suite into a client of that board
(secretary-1026).

A test that needs real host resolution or a real sprint board opts in
locally, the same way the rest of the suite already overrides other
host-facing seams: wrap the call in its own
``mock.patch("secretary.host_apply.find_orca_executable", ...)`` (or
``...pinned_orca_executable``, or ``mock.patch("secretary.status.KanboardClient", return_value=FakeKanboard())``)
with whatever value the scenario needs. That local patch simply shadows the
process-wide default for the duration of the ``with`` block; nothing needs
to be undone.

If a test fails with "Orca executable for <user> is unavailable", it means
production code reached real host discovery without going through either
this default or a local opt-in patch: look for a code path that calls
``find_orca_executable``/``pinned_orca_executable`` directly instead of
through ``resolve_systemd_layout``/``resolve_packaged``.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest import mock

from tests.kanboard_fixtures import OfflineKanboard

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

# secretary.status.collect_status's sprint read builds a KanboardClient()
# from bare os.environ with no test seam of its own. Route it to an
# in-memory, zero-network fake by default so ambient KANBOARD_* credentials
# (real or live-looking) can never make a unit test read or write a real
# board (secretary-1026). See tests/kanboard_fixtures.py.
_status_kanboard_patcher = mock.patch(
    "secretary.status.KanboardClient", return_value=OfflineKanboard()
)
_status_kanboard_patcher.start()

# `git fetch` ends with `git maintenance run --auto`, and gc detaches by default,
# so a repository a test built in a temporary directory can still be written to
# after the test body returns. The write lands in the middle of
# TemporaryDirectory cleanup and the run dies with "Directory not empty:
# .../target/.git" against whichever test was running (seen on CI in
# tests.test_secret_recover, secretary-806). The GIT_CONFIG_* trio is honoured by
# every git the suite starts, including those production code spawns.
os.environ.update(
    GIT_CONFIG_COUNT="2",
    GIT_CONFIG_KEY_0="gc.auto",
    GIT_CONFIG_VALUE_0="0",
    GIT_CONFIG_KEY_1="maintenance.auto",
    GIT_CONFIG_VALUE_1="false",
)
