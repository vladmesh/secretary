"""REVIEW.md content contract.

The layer-3 reviewer used to receive the card description and nothing else, so a red round's
findings and any PO amendment posted as a comment simply did not exist for the next round's fresh
head. These pin the card history landing in the prompt itself, the two sections that must not be
diluted by ordinary chatter, the size caps, and the scrub.
"""
from __future__ import annotations

import unittest
from unittest import mock

from triggered_agents.agents.pipeline import reviewer


CARD = {"project": "secretary", "title": "test"}


def build(comments, spec="The card spec."):
    return reviewer.build_task(CARD, "secretary-1", "https://example/pr/1", spec, "main",
                               comments=comments)


def comment(text, ts=1_760_000_000):
    return {"ts": ts, "text": text}


class HistoryInPromptTests(unittest.TestCase):
    def test_no_comments_leaves_the_prompt_without_history_sections(self):
        md = build([])
        self.assertNotIn("Earlier review rounds", md)
        self.assertNotIn("The rest of the card's history", md)
        self.assertNotIn("Spec amendments", md)

    def test_worker_report_reaches_the_prompt_without_being_fetched(self):
        md = build([comment("[report:done]\nBrought the stand up, curl returned 200.")])
        self.assertIn("## The rest of the card's history", md)
        self.assertIn("Brought the stand up, curl returned 200.", md)
        self.assertIn("[report:done]", md)

    def test_unmarked_comment_is_rendered_without_an_empty_marker_tag(self):
        md = build([comment("just a note")])
        self.assertIn("just a note", md)
        self.assertNotIn("[]", md)

    def test_empty_comment_is_dropped(self):
        md = build([comment("[po]\n   "), comment("[report:done]\nthe report")])
        self.assertNotIn("Spec amendments", md)
        self.assertIn("the report", md)


class PreviousRoundsTests(unittest.TestCase):
    def test_prior_verdict_and_return_reason_are_marked_as_a_previous_round(self):
        md = build([
            comment("[review:red]\nThe slug may collide."),
            comment("[validate:review-return]\nReturned to In progress: red verdict."),
        ])
        self.assertIn("## Earlier review rounds on this card", md)
        self.assertIn("The slug may collide.", md)
        self.assertIn("Returned to In progress: red verdict.", md)
        self.assertIn("NOT a description of the current code", md)

    def test_round_comments_are_not_repeated_in_the_general_history(self):
        md = build([comment("[review:red]\nfinding of round 1")])
        self.assertEqual(md.count("finding of round 1"), 1)
        self.assertNotIn("## The rest of the card's history", md)

    def test_rounds_section_precedes_the_lenses(self):
        md = build([comment("[review:green]\nall clean")])
        self.assertLess(md.index("## Earlier review rounds"), md.index("## Three lenses"))


class SpecAmendmentTests(unittest.TestCase):
    def test_po_comment_sits_next_to_the_spec_not_in_the_general_history(self):
        md = build([
            comment("[po]\nCriterion 3 dropped, scope narrowed."),
            comment("[report:done]\nthe worker report"),
        ])
        self.assertIn("### Spec amendments in comments", md)
        self.assertLess(md.index("Criterion 3 dropped"), md.index("## Three lenses"))
        self.assertLess(md.index("Criterion 3 dropped"), md.index("the worker report"))
        self.assertEqual(md.count("Criterion 3 dropped"), 1)

    def test_reviewer_is_told_the_comment_wins_over_the_description(self):
        md = build([comment("[po]\nan amendment")])
        self.assertIn("newer than the description", md)

    def test_every_operator_marker_counts_as_a_spec_amendment(self):
        for marker in ("po", "secretary", "steward", "steward:blocked-done"):
            with self.subTest(marker=marker):
                md = build([comment(f"[{marker}]\namendment text")])
                self.assertIn("### Spec amendments in comments", md)


class SizeCapTests(unittest.TestCase):
    def test_long_comment_is_clipped_and_points_at_the_full_text(self):
        body = "LOG " * 4000
        md = build([comment(f"[report:done]\n{body}")])
        self.assertNotIn(body.strip(), md)
        self.assertIn("clipped,", md)
        self.assertIn("`pipeline show`", md)

    def test_history_keeps_the_newest_comments_and_says_what_it_dropped(self):
        comments = [comment(f"[dispatcher]\ncomment {i}") for i in range(reviewer._HISTORY_LIMIT + 5)]
        md = build(comments)
        self.assertNotIn("comment 0", md)
        self.assertIn(f"comment {reviewer._HISTORY_LIMIT + 4}", md)
        self.assertIn(f"the last {reviewer._HISTORY_LIMIT} of {len(comments)}", md)

    def test_rounds_and_spec_notes_have_their_own_budgets(self):
        # A flood of chatter must not push the verdicts or the PO's amendment out of the prompt:
        # each section is capped separately, not from one shared tail.
        comments = ([comment("[po]\nspec amendment"), comment("[review:red]\nfinding of round 1")]
                    + [comment(f"[dispatcher]\nnoise {i}") for i in range(50)])
        md = build(comments)
        self.assertIn("spec amendment", md)
        self.assertIn("finding of round 1", md)


class ScrubTests(unittest.TestCase):
    def test_secrets_in_comments_do_not_reach_the_prompt(self):
        md = build([comment("[report:done]\nfailed with ANTHROPIC_API_KEY=sk-ant-" + "a" * 40)])
        self.assertNotIn("sk-ant-", md)
        self.assertIn("REDACTED", md)

    def test_scrub_runs_on_every_section(self):
        secret = "sk-ant-" + "b" * 40
        md = build([comment(f"[po]\n{secret}"), comment(f"[review:red]\n{secret}")])
        self.assertNotIn(secret, md)


class ReadInstructionsTests(unittest.TestCase):
    def test_prompt_stops_relying_on_the_reviewer_fetching_history(self):
        md = build([comment("[report:done]\nthe report")])
        self.assertIn("the whole card history are pasted below", md)

    def test_contrib_card_gets_the_same_history(self):
        with mock.patch.object(reviewer, "_quality_lens", return_value="lens"):
            md = reviewer.build_task(CARD, "secretary-1", None, "the spec", "main",
                                     branch="pipeline/secretary-1", head_sha="abc1234",
                                     comments=[comment("[review:red]\nfinding of round 1")])
        self.assertIn("## Earlier review rounds on this card", md)
        self.assertIn("finding of round 1", md)


if __name__ == "__main__":
    unittest.main()
