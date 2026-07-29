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
python3 -m pip install '.[memory]'
python3 -m unittest
```

The unit suite is hermetic: it does not need Kanboard, Orca, a network or a configured instance.
Tests that need a live stack are better documented as operator runbooks than added to the unit suite.

## Preparing a change for merge

- A diff scoped to one problem is easier to understand and review.
- Tests help preserve changed behaviour.
- When product behaviour changes, updating the affected document under `docs/` keeps the description
  and implementation together.
- Prefer the standard library; if a new runtime dependency helps, note why.
- Write commit messages and code comments in English.

## Pull requests

If the change is ready for a full check, run:

```bash
python3 -m unittest
python3 -m secretary role-skills audit --check
```

CI runs the unit suite on every pull request. A short note about what changed and what you verified
helps with review; if the suite does not cover it, describe any manual testing that was useful.

## License

Contributions are accepted under the [Apache License 2.0](LICENSE).
