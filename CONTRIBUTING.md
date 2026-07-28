# Contributing

Thanks for looking at `secretary`. The project is developed in the open, but it is built around one
opinionated deployment profile: a single trusted owner running one appliance on one host. Changes
that widen that profile need a design discussion before code.

## Before you start

Open an issue describing the problem and the behaviour you expect. For anything that touches task
lifecycle, recovery, host ownership or the security boundary, agree on the contract in the issue
first. A pull request that changes those contracts without a documented rationale will be sent back.

## Development setup

```bash
python3 -m pip install '.[memory]'
python3 -m unittest
```

The unit suite is hermetic: it does not need Kanboard, Orca, a network or a configured instance.
Keep it that way. Anything that needs a live stack belongs in an operator runbook, not in the suite.

## Making a change

- Keep the diff scoped to one problem. Unrelated cleanups make review slower, not faster.
- Add or update tests with the change. Behaviour that is not covered will regress.
- Update the affected document under `docs/` in the same pull request. Documentation describes
  product behaviour, so a contract change that leaves the docs behind is incomplete.
- Match the surrounding code: standard library first, no new runtime dependency without a reason
  stated in the pull request.
- Write commit messages and code comments in English.

## Pull requests

Run the checks below before you open the pull request:

```bash
python3 -m unittest
python3 -m secretary role-skills audit --check
```

CI runs the unit suite on every pull request. Describe what you changed, why, and what you verified.
If a change is not covered by the suite, say how you tested it.

## Reporting security problems

Do not open a public issue for a vulnerability. See [SECURITY.md](SECURITY.md).

## License

Contributions are accepted under the [Apache License 2.0](LICENSE).
