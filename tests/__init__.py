"""Hermetic defaults for the unit-test run: Orca discovery never leaves the
repo/process.

``python -m unittest`` imports this package before it imports any ``test_*``
module, so the patches below are live before a single test can reach
``resolve_systemd_layout``/``resolve_packaged`` and discover a real host
executable. Without this, the same checkout is green on a developer box with
Orca installed and red on a bare CI runner (or vice versa) purely from
process discovery order (secretary-705, secretary-738, secretary-748).

Board reads need no patch here. A client is built only by
``KanboardClient.for_instance(<instance dir>)``, from that instance's local
``board-transport.env``; ambient ``KANBOARD_*`` variables are not a source of
transport configuration, so a worker/reviewer/operator shell that inherits a
live installation's environment cannot turn the unit suite into a client of
that board (secretary-1026). ``tests/test_hermetic_kanboard.py`` proves it.

Codex runtime state needs one more default, for the same reason and in the
same shape. Since secretary-1173 every Codex head is an interactive TUI, so
every Codex bring-up answers the directory-trust dialog *before* the pane
exists, by appending a ``[projects."<workspace>"]`` table to ``config.toml``
inside the ``CODEX_HOME`` that head will run with. On a developer box that
home defaults to ``~/.config/orca/codex-runtime-home/home`` -- installation
state shared by every Codex head on the host -- so any test that reaches a
worker/reviewer/service bring-up without saying otherwise would record a
permanent ``trusted`` grant for its own throwaway workspace there, and
nothing prunes it. ``TA_CODEX_HOME`` is the single seam every one of those
paths reads (``codex_preflight.codex_home``, ``heads.CODEX_HOME``,
``dispatcher_tui._sessions_root``), so the suite claims one throwaway home of
its own for the whole run, before any test module is imported.
``tests/test_hermetic_codex.py`` proves it.

The live pipeline's state dir needs the same treatment, and needs it early.
``triggered_agents.agents.pipeline.state`` resolves ``STATE`` at import time,
and ``agents.pipeline.pause`` binds ``PAUSE_FILE`` off it, so by the time any
test body runs the pause path is already fixed. Left at its default that path
is the live ``<workspaces>/secretary/pipeline/state/pipeline`` of the machine
running the suite: a ``secretary pause --mode freeze`` held there while the
suite runs makes ``runtime/dispatch._pipeline_paused()`` true, and every
triggered-dispatch test silently takes the "pipeline paused -- no dispatch"
branch instead of the lifecycle branch it was written for. The same binding
also had the suite appending its own ``runs.jsonl`` records to that live
directory. ``TA_PIPELINE_STATE_DIR`` is the single seam both readers go
through (``shared_state.resolve_pipeline_state_dir``, and
``dispatcher_pause.legacy_mirror_path`` for the mirror), so the suite claims
one throwaway state dir of its own for the whole run, before any test module
is imported (secretary-1403). ``tests/test_hermetic_pipeline_state.py``
proves it.

A test that needs real host resolution or a real sprint board opts in
locally, the same way the rest of the suite already overrides other
host-facing seams: wrap the call in its own
``mock.patch("secretary.host_apply.find_orca_executable", ...)`` (or
``...pinned_orca_executable``), or pass an explicit board through
``collect_status(..., sprint_client=FakeKanboard())``. A local patch simply
shadows the process-wide default for the duration of the ``with`` block;
nothing needs to be undone.

If a test fails with "Orca executable for <user> is unavailable", it means
production code reached real host discovery without going through either
this default or a local opt-in patch: look for a code path that calls
``find_orca_executable``/``pinned_orca_executable`` directly instead of
through ``resolve_systemd_layout``/``resolve_packaged``.
"""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile
from pathlib import Path
from unittest import mock

_FIXTURE_ORCA = Path(__file__).resolve().parent / "fixtures" / "legacy-orca"

# The throwaway CODEX_HOME described above. Created here rather than per test so
# that a bring-up reached from anywhere in the suite -- including one whose
# fixture registry names no `codex_home` and which never thought about trust at
# all -- writes into a directory this run owns and removes. Set unconditionally:
# an ambient TA_CODEX_HOME inherited from a worker/reviewer shell names that
# installation's real home, which is exactly what must not be written.
_SUITE_CODEX_HOME = Path(tempfile.mkdtemp(prefix="secretary-tests-codex-home."))
os.environ["TA_CODEX_HOME"] = str(_SUITE_CODEX_HOME)
atexit.register(shutil.rmtree, _SUITE_CODEX_HOME, ignore_errors=True)

# The throwaway pipeline state dir described above, claimed before any test module
# -- and therefore before `triggered_agents.agents.pipeline.state` -- is imported,
# because that module binds `STATE` (and through it `pause.PAUSE_FILE`) to whatever
# `resolve_pipeline_state_dir()` answers at import time. Set unconditionally, for
# the same reason as TA_CODEX_HOME above: an ambient TA_PIPELINE_STATE_DIR
# inherited from a worker/reviewer/operator shell names the live installation's
# state dir, which is exactly what the suite must neither read nor write.
# `tests/test_hermetic_pipeline_state.py` proves both halves.
_SUITE_PIPELINE_STATE_DIR = Path(tempfile.mkdtemp(prefix="secretary-tests-pipeline-state."))
os.environ["TA_PIPELINE_STATE_DIR"] = str(_SUITE_PIPELINE_STATE_DIR)
atexit.register(shutil.rmtree, _SUITE_PIPELINE_STATE_DIR, ignore_errors=True)

_find_orca_patcher = mock.patch("secretary.host_apply.find_orca_executable", return_value=_FIXTURE_ORCA)
_find_orca_patcher.start()

# pinned_orca_executable() reads /usr/local/bin/orca straight off the host,
# outside the find_orca_executable seam above. Default it to "no pinned
# runtime" so secretary.bootstrap's install-vs-skip branch is deterministic
# regardless of whether this machine happens to have Orca installed there.
_pinned_orca_patcher = mock.patch("secretary.host_apply.pinned_orca_executable", return_value=None)
_pinned_orca_patcher.start()

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
