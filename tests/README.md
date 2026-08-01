# Hermetic test contract

The default unit-test run (`python3 -m unittest`, the suite CI runs) must produce the
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
        "secretary.host_apply.find_orca_executable", side_effect=real_find_orca_executable,
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
`tests/__init__.py` and is exactly the pattern secretary-705/secretary-738/secretary-748
kept re-fixing piecemeal. Only patch locally when the scenario needs a *different*
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

## The default Kanboard fake

`secretary.status.collect_status`'s sprint read builds a `KanboardClient()` from bare
`os.environ`, with no seam of its own to opt out. A worker, reviewer or operator shell
that inherits a live installation's `KANBOARD_*` variables must not turn the unit suite
into a client of that board — `test_status.py` alone cost ~231s doing exactly that
against a live Kanboard before this was fixed (secretary-1026). `tests/__init__.py`
patches `secretary.status.KanboardClient` to `tests.kanboard_fixtures.OfflineKanboard`
(an in-memory stand-in that reports "no sprint board", never touching the network)
before any test module is imported, so this default wins regardless of what the
environment holds. `tests/test_hermetic_kanboard.py` proves that even with
live-looking credentials present, an actual network call would fail the test rather
than reach the real endpoint.

A test with sprint content of its own opts in locally, the same way as the Orca seam
above: patch `secretary.status.KanboardClient` to return
`tests.test_dispatcher.FakeKanboard` (or another explicit fake) for the duration of its
own `with` block. `tests/test_status.py`'s `test_status_json_includes_stopped_sprint_and_stale_resume`
is a worked example. Do not construct a real, environment-backed `KanboardClient()` in
a `test_*` module the default `python -m unittest` run discovers; a live canary belongs
in an operator runbook or an explicit, separately opted-in integration test against a
disposable endpoint instead.
