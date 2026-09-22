# Hermetic test contract

Every test run (a CI suite, `python3 -m tests.broad`, a named module) must produce the
same result whether or not the host running it has Orca installed. Nothing in the
default run may discover a real host Orca executable.

That is enforced in one place: `tests/__init__.py` patches
`secretary.host_apply.find_orca_executable` and `secretary.host_apply.pinned_orca_executable`
before any `test_*` module is imported, routing every call through the repo fixture at
`tests/fixtures/legacy-orca` (or `None`, for "no pinned runtime") instead of the real
filesystem. `python -m unittest`'s discovery imports `tests/__init__.py` first, so this
patch is live before any test can reach a production call path
(`resolve_systemd_layout`, `resolve_packaged`, `secretary.bootstrap`'s install-vs-skip
check) that would otherwise look at `/usr/local/bin/orca` or a user's `~/.local/bin/orca`.

## Opting a test in to real-host-style resolution

A test that deliberately wants to model an unavailable, alternate, or otherwise
non-default executable overrides the same seam locally, inside its own `with` block:

```python
with mock.patch("secretary.host_apply.find_orca_executable", return_value=None):
    ...  # models "no Orca reachable for this user"

with mock.patch("secretary.host_apply.find_orca_executable", return_value=Path("/usr/local/bin/orca")):
    ...  # models the pinned runtime path specifically
```

`tests/orca_fixtures.py:legacy_orca_runtime` goes one step further and creates a real,
discoverable executable under a fixture-owned home directory, for tests that need
`find_orca_executable`'s actual filesystem-probing behavior rather than a canned
return value. To actually exercise that probing, restore the real resolver for the
duration of the `with` block instead of stubbing a `return_value` — a `return_value`
only proves mocking works, not that production code would have found the executable
itself:

```python
from tests import _find_orca_patcher

real_find_orca_executable = _find_orca_patcher.temp_original
with legacy_orca_runtime(root) as discoverable:
    with mock.patch(
        "secretary.host_apply.find_orca_executable",
        side_effect=real_find_orca_executable,
    ):
        ...  # exercises the real filesystem probe against the fixture-owned home
```

(`temp_original` is `unittest.mock`'s handle on the object a patcher replaced; grabbing
it off `tests/__init__.py`'s module-level patcher is how a test un-shadows the default
without stopping it process-wide.) See `tests/test_hermetic_orca.py` for a worked
example of both seams: the same discoverable executable, once shadowed by the default
fixture and once explicitly opted into via the real resolver.

Do not add a new blanket class-level or per-test patch that just re-establishes the
default fixture path (`tests/fixtures/legacy-orca`) — that duplicates
`tests/__init__.py`. Only patch locally when the scenario needs a *different*
value than the default.

## If a test fails with "Orca executable for `<user>` is unavailable"

That error comes from `resolve_packaged` when it expects to ship an `orca` unit but
`_is_executable(layout.orca_executable)` is false. In a test, seeing it unexpectedly
means a code path reached real host discovery without going through the default
fixture or an explicit local opt-in — look for a call to
`find_orca_executable`/`pinned_orca_executable` (or a caller of
`resolve_systemd_layout`/`resolve_packaged`) that runs before `tests/__init__.py` has a
chance to patch it, or that imports `secretary.host_apply`'s functions by value instead
of by module attribute (which would keep an unpatched reference).

## The pipeline pause flag is read from a state dir this run owns

The same result-must-not-depend-on-the-host rule covers the live pipeline's pause flag.
`src/triggered_agents/agents/pipeline/state.py` resolves `STATE` at import time and
`agents/pipeline/pause.py` binds `PAUSE_FILE` off it, so the file every
triggered-dispatch test runs against is fixed before any test body executes. At its
default that file is the live `<workspaces>/secretary/pipeline/state/pipeline/pause.json`
of the machine running the suite: an operator holding `secretary pause --mode freeze`
while the suite runs makes `runtime/dispatch._pipeline_paused()` true, and every dispatch
test takes the "pipeline paused — no dispatch" branch instead of the lifecycle branch it
asserts about, and the suite appends its own `runs.jsonl` records into that live directory.

`tests/__init__.py` therefore claims one throwaway `TA_PIPELINE_STATE_DIR` for the whole
run and removes it at exit, set before any `test_*` module is imported and set
unconditionally — an ambient `TA_PIPELINE_STATE_DIR` inherited from a worker, reviewer or
operator shell names exactly the live directory that must not be touched.
`tests/test_hermetic_pipeline_state.py` is the proof, in both directions: a hard freeze in
a production-like `<workspaces>/secretary/pipeline/state/pipeline` (built under a
temporary root, never the live one) leaves a warm-reuse dispatch reusing rather than
skipping, while a freeze written into the suite's *own* state dir still pauses — so the
"not paused" half cannot pass by the reader having gone dead.

A focused test that needs a different value overrides the variable locally, the usual way
(`mock.patch.dict(os.environ, {"TA_PIPELINE_STATE_DIR": str(tmp)})`), or pops it to
exercise the default-path computation; both still work, since the suite default is just an
ordinary process environment value the `with` block shadows. Note that a test which pops
it and then *reads* a pause flag is back to reading the host — pop it only to assert about
a resolved path, as `test_pipeline_paths.py:LegacyMirrorPathTests` does.

## The suite must have imported the checkout it lives in

Every seam above keeps a *host* fact out of the run. This one checks which sources the run
imported. A head's shell carries `PYTHONPATH=$TA_SECRETARY_REPO/src`
(`src/triggered_agents/runtime/launch_prefix.py`), and every worktree on the pipeline host runs on
one shared venv whose editable install points at the production checkout's `src`. Both outrank a
worktree's own sources for a src-layout project, which has nothing importable at its root -- so a
suite run inside a candidate worktree can pass while exercising production's `secretary` with
the candidate's test files.

`tests/test_hermetic_source_tree.py` asserts it: `secretary.__file__` and
`triggered_agents.__file__` must resolve inside the checkout that contains `tests/`. It installs no
seam and shadows nothing -- there is no default to patch here, only a fact about the process.

It is also the one guard in this family that is legitimately red on a perfectly good checkout: run
a worktree's suite with the production interpreter and no `PYTHONPATH` of your own, and it fails
and names both paths. That is the signal, not a defect. Point the interpreter at your own sources:

```
PYTHONPATH=$PWD/src python3 -m unittest ...
```

## Board reads are hermetic by construction, not by a patch

There is no default board fake to install, because there is nothing to shadow. A board client cannot be built from ambient environment variables at all: every
client comes from `secretary.board.backend.board_client(<instance dir>)`, which resolves that
instance's local `board-store.env` and raises `backend_unavailable` when it is absent. Nothing
selects a backend; there is one.
A worker, reviewer or operator shell that inherits a live installation's `KANBOARD_*`
variables therefore cannot turn the unit suite into a client of that board — the variables
are simply not a source of transport configuration.

`tests/test_hermetic_kanboard.py` is the proof, and it asserts both halves: with live-looking
`KANBOARD_*` in the environment and a temporary instance that has no transport file, the
status read fails closed with `backend_unavailable` while a patched `urlopen` turns any
accidental dial-out into a loud failure.

A CLI test that needs a board injects it where the client is built — the
`board_client`/`card_client` name the command or its layer binds (for example
`secretary.task_commands.card_client`, or `board_injected()` on the sprint fixtures).

A test with sprint content of its own injects it explicitly rather than patching a global:
`collect_status(report, offline=True, sprint_client=sprint_store(self, status_seed()))` is the seam, and
`tests/test_hermetic_kanboard.py:test_a_test_can_still_opt_in_to_a_real_sprint_boards_shape`
is the worked example. Do not build a client against a real endpoint in a `test_*` module the
default `python -m unittest` run discovers; a live canary belongs in an operator runbook or an
explicit, separately opted-in integration test against a disposable endpoint instead.

Cards have one implementation, PostgreSQL, and there is no in-memory card board. A test that
needs cards gets a real store of its own: `card_store(self, seed)` (or `CardStoreCase`) from
`tests/sql_backend_fixtures.py`, seeded from a `CardSeed` of `tests/fakes/tasks.py` and dropped
when the test ends; `tests/fakes/dispatcher.py:dispatcher_seed()` is the dispatcher's board. The
server is one throwaway `postgres:16` per test process, so such a module belongs to a Docker shard
(`integration-board`), never to `unit`.
