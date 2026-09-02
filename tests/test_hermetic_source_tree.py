"""The hermetic family's missing member: the suite must test the checkout it lives in.

The other guards in this family (`test_hermetic_orca.py`, `test_hermetic_kanboard.py`,
`test_hermetic_codex.py`, `test_hermetic_pipeline_state.py`) all answer one question: does this
run's result depend on the host rather than on this checkout? They cover the host's Orca binary,
the host's board, the host's Codex home and the host's pipeline state. Nothing covered the most
basic dependency of all -- which *sources* the run imported.

That gap was not theoretical. A head's shell carries `PYTHONPATH=$TA_SECRETARY_REPO/src`
(`triggered_agents/runtime/launch_prefix.py`), and every worktree on this host runs on one shared
venv whose editable install points at the production checkout's `src`. Both of those outrank a
worktree's own sources for a src-layout project, which has nothing importable at its root. So a
worker could run the broad suite inside a candidate worktree and watch it pass, while the code it
exercised was production's: the candidate's test files against production's `secretary`. The
receipt written by `secretary check broad` recorded the true import and refused itself
(issue:8b39e60e4df361c6138e), which is how the defect was eventually found -- but a bare
`python -m unittest` in the worktree said only "OK" and named no checkout at all.

This test is that missing sentence. It is deliberately not clever: it asks each package where its
`__file__` is, and compares that with the directory this very file sits in. A green suite therefore
means "green for this code", and a run that imported someone else's sources fails loudly, naming
both paths and the one environment variable that fixes it, instead of quietly reporting a verdict
about a tree the reader is not looking at.

Unlike the rest of the family this one installs no seam and shadows nothing: there is no default to
patch, only a fact about the process to assert. It is also the one guard that can legitimately be
red on a correct checkout -- run a worktree's suite with the production interpreter and no
`PYTHONPATH` of your own and it will say so, which is exactly the signal it exists to give.
"""

from __future__ import annotations

import unittest
from pathlib import Path

import secretary
import triggered_agents

#: The checkout this test file belongs to: the parent of the `tests/` package.
CHECKOUT = Path(__file__).resolve().parent.parent


def _source_of(module: object, name: str) -> Path:
    location = getattr(module, "__file__", None)
    if not location:
        raise AssertionError(
            f"the imported {name!r} package reports no __file__, so the suite cannot say which "
            f"checkout it tested; expected sources under {CHECKOUT / 'src' / name}"
        )
    return Path(location).resolve()


class HermeticSourceTreeTests(unittest.TestCase):
    """Every source package this suite tests must come from the checkout that holds this file."""

    def _assert_imported_from_this_checkout(self, module: object, name: str) -> None:
        source = _source_of(module, name)
        if source.is_relative_to(CHECKOUT):
            return
        self.fail(
            f"this suite imported {name!r} from {source}, which is outside the checkout it is "
            f"part of ({CHECKOUT}). The result says nothing about the code in this checkout: the "
            f"tests are this tree's, the sources are not.\n"
            f"Point the interpreter at your own sources before running the suite, e.g.\n"
            f"    PYTHONPATH={CHECKOUT / 'src'} python3 -m unittest ...\n"
            f"An inherited PYTHONPATH from a head's shell, or a shared venv holding an editable "
            f"install of another checkout, is the usual cause "
            f"(issue:8b39e60e4df361c6138e)."
        )

    def test_the_suite_imported_secretary_from_this_checkout(self) -> None:
        self._assert_imported_from_this_checkout(secretary, "secretary")

    def test_the_suite_imported_triggered_agents_from_this_checkout(self) -> None:
        self._assert_imported_from_this_checkout(triggered_agents, "triggered_agents")

    def test_the_checkout_this_guard_measures_against_is_the_one_holding_the_tests(self) -> None:
        """Guard the guard: if `tests/` ever moves, the comparison above must not go dead.

        A `CHECKOUT` that stopped naming a real source tree would make both assertions above pass
        vacuously for anything under it, which is the failure mode a boundary check has.
        """
        self.assertTrue((CHECKOUT / "tests" / "__init__.py").is_file(), CHECKOUT)
        self.assertTrue((CHECKOUT / "src" / "secretary").is_dir(), CHECKOUT)
        self.assertTrue((CHECKOUT / "src" / "triggered_agents").is_dir(), CHECKOUT)


if __name__ == "__main__":
    unittest.main()
