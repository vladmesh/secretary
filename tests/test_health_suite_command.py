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
incidental module ordering, not by design. Both docs now say
``python3 -m unittest`` instead, whose default discovery (``discover('.')``)
imports ``tests`` as a package on its own, unconditionally.
"""

from __future__ import annotations

import re
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

_PROBE = """
import sys
import unittest
from unittest import mock

unittest.TestLoader().discover({start_dir!r})

import secretary.host_apply as host_apply

print("tests-imported=%s" % ("tests" in sys.modules))
print("patched=%s" % isinstance(host_apply.find_orca_executable, mock.Mock))
"""

_BROKEN_FORM = re.compile(r"unittest\s+discover\s+-s\s+tests(?!\s+-t\b)")

_DOCUMENTED_ELSEWHERE = (
    REPO_ROOT / "docs" / "OPERATIONS.md",
    REPO_ROOT / "skills" / "roles" / "steward" / "steward" / "SKILL.md",
    REPO_ROOT / "tests" / "README.md",
    REPO_ROOT / ".github" / "workflows" / "ci.yml",
)


def _run_probe(start_dir: str) -> dict:
    result = subprocess.run(
        [sys.executable, "-c", _PROBE.format(start_dir=start_dir)],
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
        # `python3 -m unittest` (no args) discovers from '.', matching this
        # probe. Root discovery always imports `tests` as a package because
        # `tests/` has an `__init__.py`; it does not depend on any other
        # module happening to import `tests` first.
        outcome = _run_probe(".")
        self.assertTrue(outcome["tests-imported"])
        self.assertTrue(outcome["patched"])

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


if __name__ == "__main__":
    unittest.main()
