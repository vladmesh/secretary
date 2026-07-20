"""REVIEW.md content contract (secretary-660).

The layer-3 reviewer used to receive the card description and nothing else, so a red round's
findings and any PO amendment posted as a comment simply did not exist for the next round's fresh
head. These pin the card history landing in the prompt itself, the two sections that must not be
diluted by ordinary chatter, the size caps, and the scrub.
"""
from __future__ import annotations

import unittest
from unittest import mock

from triggered_agents.agents.pipeline import reviewer


CARD = {"project": "secretary", "title": "тест"}


def build(comments, spec="Спека карточки."):
    return reviewer.build_task(CARD, "secretary-1", "https://example/pr/1", spec, "main",
                               comments=comments)


def comment(text, ts=1_760_000_000):
    return {"ts": ts, "text": text}


class HistoryInPromptTests(unittest.TestCase):
    def test_no_comments_leaves_the_prompt_without_history_sections(self):
        md = build([])
        self.assertNotIn("Прошлые раунды ревью", md)
        self.assertNotIn("Остальная история карточки", md)
        self.assertNotIn("Уточнения спеки", md)

    def test_worker_report_reaches_the_prompt_without_being_fetched(self):
        md = build([comment("[report:done]\nПоднял стенд, curl вернул 200.")])
        self.assertIn("## Остальная история карточки", md)
        self.assertIn("Поднял стенд, curl вернул 200.", md)
        self.assertIn("[report:done]", md)

    def test_unmarked_comment_is_rendered_without_an_empty_marker_tag(self):
        md = build([comment("просто заметка")])
        self.assertIn("просто заметка", md)
        self.assertNotIn("[]", md)

    def test_empty_comment_is_dropped(self):
        md = build([comment("[po]\n   "), comment("[report:done]\nотчёт")])
        self.assertNotIn("Уточнения спеки", md)
        self.assertIn("отчёт", md)


class PreviousRoundsTests(unittest.TestCase):
    def test_prior_verdict_and_return_reason_are_marked_as_a_previous_round(self):
        md = build([
            comment("[review:red]\nСлаг может коллидировать."),
            comment("[validate:review-return]\nВозврат в In progress: красный вердикт."),
        ])
        self.assertIn("## Прошлые раунды ревью этой карточки", md)
        self.assertIn("Слаг может коллидировать.", md)
        self.assertIn("Возврат в In progress: красный вердикт.", md)
        self.assertIn("НЕ описание текущего кода", md)

    def test_round_comments_are_not_repeated_in_the_general_history(self):
        md = build([comment("[review:red]\nнаходка раунда 1")])
        self.assertEqual(md.count("находка раунда 1"), 1)
        self.assertNotIn("## Остальная история карточки", md)

    def test_rounds_section_precedes_the_lenses(self):
        md = build([comment("[review:green]\nвсё чисто")])
        self.assertLess(md.index("## Прошлые раунды ревью"), md.index("## Три линзы"))


class SpecAmendmentTests(unittest.TestCase):
    def test_po_comment_sits_next_to_the_spec_not_in_the_general_history(self):
        md = build([
            comment("[po]\nКритерий 3 снят, скоуп сузили."),
            comment("[report:done]\nотчёт воркера"),
        ])
        self.assertIn("### Уточнения спеки комментариями", md)
        self.assertLess(md.index("Критерий 3 снят"), md.index("## Три линзы"))
        self.assertLess(md.index("Критерий 3 снят"), md.index("отчёт воркера"))
        self.assertEqual(md.count("Критерий 3 снят"), 1)

    def test_reviewer_is_told_the_comment_wins_over_the_description(self):
        md = build([comment("[po]\nуточнение")])
        self.assertIn("новее описания", md)

    def test_every_operator_marker_counts_as_a_spec_amendment(self):
        for marker in ("po", "secretary", "steward", "steward:blocked-done"):
            with self.subTest(marker=marker):
                md = build([comment(f"[{marker}]\nтекст уточнения")])
                self.assertIn("### Уточнения спеки комментариями", md)


class SizeCapTests(unittest.TestCase):
    def test_long_comment_is_clipped_and_points_at_the_full_text(self):
        body = "ЛОГ " * 4000
        md = build([comment(f"[report:done]\n{body}")])
        self.assertNotIn(body.strip(), md)
        self.assertIn("обрезано, ещё", md)
        self.assertIn("`pipeline show`", md)

    def test_history_keeps_the_newest_comments_and_says_what_it_dropped(self):
        comments = [comment(f"[dispatcher]\nкоммент {i}") for i in range(reviewer._HISTORY_LIMIT + 5)]
        md = build(comments)
        self.assertNotIn("коммент 0", md)
        self.assertIn(f"коммент {reviewer._HISTORY_LIMIT + 4}", md)
        self.assertIn(f"последние {reviewer._HISTORY_LIMIT} из {len(comments)}", md)

    def test_rounds_and_spec_notes_have_their_own_budgets(self):
        # A flood of chatter must not push the verdicts or the PO's amendment out of the prompt:
        # each section is capped separately, not from one shared tail.
        comments = ([comment("[po]\nуточнение спеки"), comment("[review:red]\nнаходка раунда 1")]
                    + [comment(f"[dispatcher]\nшум {i}") for i in range(50)])
        md = build(comments)
        self.assertIn("уточнение спеки", md)
        self.assertIn("находка раунда 1", md)


class ScrubTests(unittest.TestCase):
    def test_secrets_in_comments_do_not_reach_the_prompt(self):
        md = build([comment("[report:done]\nупало с ANTHROPIC_API_KEY=sk-ant-" + "a" * 40)])
        self.assertNotIn("sk-ant-", md)
        self.assertIn("REDACTED", md)

    def test_scrub_runs_on_every_section(self):
        secret = "sk-ant-" + "b" * 40
        md = build([comment(f"[po]\n{secret}"), comment(f"[review:red]\n{secret}")])
        self.assertNotIn(secret, md)


class ReadInstructionsTests(unittest.TestCase):
    def test_prompt_stops_relying_on_the_reviewer_fetching_history(self):
        md = build([comment("[report:done]\nотчёт")])
        self.assertIn("вся история карточки вклеены ниже", md)

    def test_contrib_card_gets_the_same_history(self):
        with mock.patch.object(reviewer, "_quality_lens", return_value="lens"):
            md = reviewer.build_task(CARD, "secretary-1", None, "спека", "main",
                                     branch="pipeline/secretary-1", head_sha="abc1234",
                                     comments=[comment("[review:red]\nнаходка раунда 1")])
        self.assertIn("## Прошлые раунды ревью этой карточки", md)
        self.assertIn("находка раунда 1", md)


if __name__ == "__main__":
    unittest.main()
