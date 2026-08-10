"""The candidate-history preflight's own decisions (secretary-1401).

The gate-level tests — where the check sits relative to the branch push — live next to the other
gate tests in `tests/test_dispatcher.py::DispatcherGateTests`. These are about the decision itself:
which trailers are forbidden, which are ordinary collaboration, and what the worker is told.
"""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

import secretary
from secretary.candidate_history import Commit, ai_attributions, parse_log, repair_message


def _commit(message: str, sha: str = "a" * 40) -> Commit:
    return Commit(sha=sha, message=message)


class CandidateHistoryTests(unittest.TestCase):
    def test_a_clean_message_carries_no_attribution(self) -> None:
        commits = [_commit("Fix the boundary\n\nA body that mentions Claude in prose.\n")]

        self.assertEqual(ai_attributions(commits), [])

    def test_a_claude_trailer_is_reported_with_its_commit(self) -> None:
        commits = [
            _commit(
                "Add the preflight\n\nCo-Authored-By: Claude <noreply@anthropic.com>\n",
                sha="1" * 40,
            )
        ]

        found = ai_attributions(commits)

        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].sha, "1" * 40)
        self.assertEqual(found[0].subject, "Add the preflight")
        self.assertEqual(found[0].identity, "claude")
        self.assertIn("noreply@anthropic.com", found[0].trailer)

    def test_a_codex_trailer_is_reported_too(self) -> None:
        commits = [_commit("Work\n\nCo-authored-by: Codex <codex@openai.com>\n")]

        self.assertEqual([item.identity for item in ai_attributions(commits)], ["codex"])

    def test_casing_and_leading_space_do_not_hide_a_trailer(self) -> None:
        commits = [_commit("Work\n\n  CO-AUTHORED-BY:  Claude Code <bot@example.invalid>\n")]

        self.assertEqual([item.identity for item in ai_attributions(commits)], ["claude"])

    def test_an_ordinary_human_co_author_stays_legal(self) -> None:
        commits = [
            _commit(
                "Pair on the parser\n\n"
                "Co-Authored-By: Claudia Ramirez <claudia@example.invalid>\n"
                "Co-Authored-By: A. Codexter <a@codexpertise.example>\n"
                "Co-Authored-By: Vlad <vlad@example.invalid>\n"
            )
        ]

        self.assertEqual(ai_attributions(commits), [])

    def test_every_forbidden_trailer_across_every_commit_is_named_at_once(self) -> None:
        commits = [
            _commit("First\n\nCo-Authored-By: Claude <noreply@anthropic.com>\n", sha="1" * 40),
            _commit("Second\n\nno trailer here\n", sha="2" * 40),
            _commit(
                "Third\n\n"
                "Co-Authored-By: Vlad <vlad@example.invalid>\n"
                "Co-Authored-By: Codex <codex@openai.com>\n"
                "Co-Authored-By: GitHub Copilot <copilot@example.invalid>\n",
                sha="3" * 40,
            ),
        ]

        found = ai_attributions(commits)

        self.assertEqual(
            [(item.sha[:1], item.identity) for item in found],
            [("1", "claude"), ("3", "codex"), ("3", "copilot")],
        )

    def test_the_log_format_round_trips_multi_line_messages(self) -> None:
        text = (
            "1111111111111111111111111111111111111111\x1fFirst\n\nbody\n\x1e"
            "2222222222222222222222222222222222222222\x1fSecond\n\x1e"
        )

        commits = parse_log(text)

        self.assertEqual([commit.sha for commit in commits], ["1" * 40, "2" * 40])
        self.assertEqual([commit.subject for commit in commits], ["First", "Second"])
        self.assertIn("body", commits[0].message)

    def test_the_repair_message_asks_for_a_local_rewrite_and_no_force_push(self) -> None:
        found = ai_attributions(
            [_commit("Add the preflight\n\nCo-Authored-By: Claude <noreply@anthropic.com>\n")]
        )

        message = repair_message(found, "main")

        self.assertIn("Add the preflight", message)
        self.assertIn("git commit --amend", message)
        self.assertIn("git rebase -i origin/main", message)
        self.assertIn("Nothing has been published", message)
        self.assertIn("do not force-push", message)


class GeneratedPacketInvariantTests(unittest.TestCase):
    """The other half of keeping generated packets out of a candidate (#181): this repository
    ignores them, so a worker that runs `git add -A` cannot commit the round's own TASK.md."""

    def test_the_product_repository_ignores_the_generated_handoff_packets(self) -> None:
        repo = Path(secretary.__file__).resolve().parent.parent
        if not (repo / ".git").exists():
            self.skipTest("not running from a git checkout of the product repository")

        ignored = subprocess.run(
            ["git", "-C", str(repo), "check-ignore", "TASK.md", "REVIEW.md"],
            text=True, capture_output=True,
        )

        self.assertEqual(ignored.returncode, 0, ignored.stderr)
        self.assertEqual(sorted(ignored.stdout.split()), ["REVIEW.md", "TASK.md"])
        tracked = subprocess.run(
            ["git", "-C", str(repo), "ls-files", "TASK.md", "REVIEW.md"],
            text=True, capture_output=True, check=True,
        )
        self.assertEqual(tracked.stdout.strip(), "")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
