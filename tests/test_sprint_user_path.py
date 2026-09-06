"""The walkthrough of `tests/sprint_user_path.py`, run as a test so it cannot rot quietly.

The transcript that acceptance quotes (`docs/evidence/sprint-user-path-2026-09-06.md`) was produced
by the same function this runs. A script that is only ever run by hand records the day it was run
and nothing after it; running it here means a change that breaks the owner's path breaks the suite
on the branch that made it, and the recorded transcript stays a description of something true.
"""

from __future__ import annotations

import unittest

from tests.sprint_user_path import Step, transcript, walk


class SprintUserPathTests(unittest.TestCase):
    def test_every_step_of_the_owners_path_answers_what_the_path_says(self) -> None:
        record: list[Step] = []
        try:
            walk(record)
        finally:
            self.report = transcript(record)
        failed = [f"{step.number}. {step.title}: {failure}" for step in record for failure in step.failures]
        self.assertEqual(failed, [], self.report)
        # A walk that silently stopped doing anything would otherwise pass with an empty record.
        self.assertGreaterEqual(len(record), 16)
        self.assertGreaterEqual(sum(len(step.notes) for step in record), 60)


if __name__ == "__main__":
    unittest.main()
