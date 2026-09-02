# Contributing

Thanks for looking at `secretary`. The project is developed in the open, but it is built around one
opinionated deployment profile: a single trusted owner running one appliance on one host.

## Feedback is welcome

Issues, pull requests, security reports, rough ideas and incomplete notes are all welcome. Use
whatever format is convenient; there is no required template. Please keep real credentials and
other private data out of public reports.

If a change touches task lifecycle, recovery, host ownership or the security boundary, explaining
the intended contract in an issue or pull request will make it easier to work through together.

## Development setup

```bash
python3 -m pip install -e '.[memory]'
python3 -m pip install -e '.[dev]'
python3 -m tests.broad
```

The `dev` extra is the pinned `ruff`. The pin is enforced by `required-version` in
`pyproject.toml`: another version refuses to run instead of reporting a different set of findings.
Run it only on non-deleted changed and untracked Python paths using the canonical command in
[Testing](docs/TESTING.md#changed-python-lint), never across the repository.

Keep the package installed in editable mode: tests and runtime commands resolve deployment assets
from the repository checkout.

The unit suite is hermetic: it does not need Kanboard, Orca, a network or a configured instance. That
holds even when the process inherits a live installation's `KANBOARD_*` variables, for example in a
worker, reviewer or operator shell: a board client is always built from an explicit instance's local
`board-transport.env` (`KanboardClient.for_instance`), never from ambient environment variables, so a
test without a configured instance fails closed instead of reaching a real board (see
`tests/README.md` and `tests/test_hermetic_kanboard.py`). This matters beyond the unit suite too: during
sprint:1024 review, a reviewer session inherited live `KANBOARD_*` credentials and an exploratory CLI
smoke command ended up migrating the production board (secretary-1026) — the same class of ambient-
credential risk that hermetic tests protect against. A live canary belongs in an operator runbook or an
explicit, separately opted-in integration test against a disposable endpoint, never disguised as a
`test_*` module the default run discovers.

## Preparing a change for merge

- A diff scoped to one problem is easier to understand and review.
- Tests help preserve changed behaviour.
- When product behaviour changes, updating the affected document under `docs/` keeps the description
  and implementation together.
- Prefer the standard library; if a new runtime dependency helps, note why.
- Write commit messages and code comments in English.

## Pull requests

If the change is ready for a full check, run the local broad profile and the skills audit:

```bash
python3 -m tests.broad
python3 -m secretary role-skills audit --check
```

`python3 -m tests.broad` is the `unit` and `component` suites, about 1440 tests in ~77 seconds. It
is a local profile, not the gate: the complete test contract is the dispatcher-owned exact-SHA
GitHub CI run described in [Testing](docs/TESTING.md), which runs all seven suites in parallel and
is what a pull request is judged by. Run a suite outside the profile directly by name
(`python3 -m unittest tests.test_bootstrap`) when a change touches it; do not reach for
repository-wide discovery, which is the same seven suites in one 402-second process.

To keep the suite's result after the terminal scrolls, run it through the receipt wrapper instead;
it streams the same output, exits with the same status, and leaves a structured summary in the
ignored `state/checks/` path that `check show` can read back without running anything again:

```bash
python3 -m secretary check broad --module tests.broad
python3 -m secretary check show --module tests.broad
```

A registered project can name its own broad suite in its adapter's `broad_check` block, and then
the flag is not needed at all: `secretary check broad --reuse` and `secretary check show` run
exactly the suite that project declared.

`--module` is the shape to prefer: the wrapper builds the command itself, so the suite runs in a
process that records which project it imported, and the receipt can be read back in place of a
second run while that import resolved inside this checkout. `--command '<any shell>'` is available for checks that need a shell, and its receipt is
a summary only — a shell can change directory or import environment before an interpreter starts,
so that receipt attests no import and never stands in for running the check again.

CI runs the unit suite on every pull request. A short note about what changed and what you verified
helps with review; if the suite does not cover it, describe any manual testing that was useful.

## License

Contributions are accepted under the [Apache License 2.0](LICENSE).
