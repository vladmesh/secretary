"""Regression coverage for secretary-748: the documented health-suite command
must import ``tests`` (and therefore the hermetic Orca default it installs)
before any test module can reach production Orca discovery.

Prior incident: docs/OPERATIONS.md and the steward skill documented
``python3 -m unittest discover -s tests``. That invocation's top-level
directory defaults to the start directory itself, so unittest's discovery
does not treat ``tests`` as a dotted package rooted one level up; whether the
hermetic default in ``tests/__init__.py`` ends up applied at all then depends
on some *other* test module happening to import ``tests`` explicitly (as
``tests/test_hermetic_orca.py`` does) before any test runs, purely by
incidental module ordering, not by design. The fix was to document
``python3 -m unittest`` instead, whose default discovery (``discover('.')``)
imports ``tests`` as a package on its own, unconditionally.

The documented command has since changed again, and the invariant has not.
Since issue:8b39e60e4df361c6138e the documented local broad run is
``python3 -m tests.broad`` — the manifest's ``unit`` and ``component`` suites,
about 1440 tests in ~77s, rather than repository-wide discovery's 3782 tests in
~402s. It satisfies the same rule, and it satisfies it more directly than the
form it replaces: ``tests/broad.py`` lives *inside* the ``tests`` package, so
``python -m`` imports ``tests/__init__.py`` before that module's own body runs,
and therefore before it can name a single test module. This file pins that,
plus the standing rule that no committed document may recommend the broken
``-s tests`` form again.
"""

from __future__ import annotations

import re
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Imports the documented entry point and nothing else, then reports three facts about the
#: process: that the ``tests`` package was imported, that its hermetic default is live, and that
#: no test module had to be imported first for either to be true.
_PROBE = """
import sys
from unittest import mock

import tests.broad

import secretary.host_apply as host_apply

print("tests-imported=%s" % ("tests" in sys.modules))
print("patched=%s" % isinstance(host_apply.find_orca_executable, mock.Mock))
print("no-test-module-imported=%s" % (not any(
    name == "tests.test" or name.startswith("tests.test_") for name in sys.modules
)))
print("names-a-suite=%s" % bool(tests.broad.broad_modules()))
"""

_BROKEN_FORM = re.compile(r"unittest\s+discover\s+-s\s+tests(?!\s+-t\b)")

_DOCUMENTED_ELSEWHERE = (
    REPO_ROOT / "docs" / "OPERATIONS.md",
    REPO_ROOT / "skills" / "roles" / "steward" / "steward" / "SKILL.md",
    REPO_ROOT / "tests" / "README.md",
    REPO_ROOT / ".github" / "workflows" / "ci.yml",
)

#: Where the local broad run is documented. Each of these named bare ``python3 -m unittest`` before
#: issue:8b39e60e4df361c6138e, which is repository-wide discovery: all seven CI suites in one
#: ~402-second process. A document that recommends it again puts the cost back.
_DOCUMENTED_BROAD_RUN = (
    REPO_ROOT / "CONTRIBUTING.md",
    REPO_ROOT / "docs" / "OPERATIONS.md",
    REPO_ROOT / "skills" / "roles" / "steward" / "steward" / "SKILL.md",
)

#: Bare `python3 -m unittest` with no module after it. Naming a module (`-m unittest tests.test_x`)
#: is a focused run and always fine; it is the argument-less form that is the 402-second one.
_REPOSITORY_WIDE_DISCOVERY = re.compile(r"python3? -m unittest(?:\s+-[vqf]+)*\s*(?:$|[\n`'\"])")


def _run_probe() -> dict:
    result = subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=REPO_ROOT,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    lines = dict(line.split("=", 1) for line in result.stdout.strip().splitlines())
    return {key: value == "True" for key, value in lines.items()}


class DocumentedHealthSuiteCommandTests(unittest.TestCase):
    def test_documented_command_imports_tests_and_applies_the_hermetic_default(self):
        # `python3 -m tests.broad` imports the `tests` package before executing `tests/broad.py`,
        # because that is what importing a submodule means. The hermetic default is therefore live
        # before the module can name a test, rather than depending on collection order.
        outcome = _run_probe()

        self.assertTrue(outcome["tests-imported"])
        self.assertTrue(outcome["patched"])
        self.assertTrue(
            outcome["no-test-module-imported"],
            "the default must be installed by the package import itself, not by whichever test "
            "module happens to be imported first — that accident is secretary-748",
        )
        self.assertTrue(
            outcome["names-a-suite"],
            "an entry point that resolves to no modules would satisfy every assertion above while "
            "running nothing",
        )

    def test_no_committed_doc_or_skill_still_recommends_the_broken_form(self):
        # `-s tests` without `-t` sets top_level_dir to the start dir itself,
        # so `tests/__init__.py` is not guaranteed to run before test
        # collection: today it happens to run anyway only because
        # `tests/test_hermetic_orca.py` imports the `tests` package as a
        # side effect at module scope, and unittest's discovery eagerly
        # imports every test module before running any of them. That is an
        # accident of which test modules currently exist, not a property of
        # the `-s tests` invocation itself, so pin the fix at the
        # documentation layer: nothing should tell a contributor or CI to
        # run the unsafe form again.
        offenders = []
        for path in _DOCUMENTED_ELSEWHERE:
            text = path.read_text(encoding="utf-8")
            if _BROKEN_FORM.search(text):
                offenders.append(str(path.relative_to(REPO_ROOT)))
        self.assertEqual(offenders, [])

    def test_no_committed_doc_or_skill_recommends_repository_wide_discovery(self):
        """The documented broad run is the profile, not every suite in one process.

        Bare `python3 -m unittest` is 3782 tests and ~402 seconds, which is a check nobody runs
        between edits — so in practice it was skipped, or paid for once and stretched far past the
        content it described. `python3 -m tests.broad` is the same guarantee about `tests/__init__`
        at a twentieth of the cost, and the complete gate stays dispatcher-owned exact-SHA CI.
        """
        offenders = []
        for path in _DOCUMENTED_BROAD_RUN:
            text = path.read_text(encoding="utf-8")
            if _REPOSITORY_WIDE_DISCOVERY.search(text):
                offenders.append(str(path.relative_to(REPO_ROOT)))
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
