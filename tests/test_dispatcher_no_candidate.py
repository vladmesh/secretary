"""The no-candidate lifecycle of research/infra cards and the stored review choice (secretary-1639).

A research or infra card goes from Ready to Done without a branch, pull request, CI run or workflow
dispatch, and reaches Done only through the one completion-evidence check. `review: skipped` means no
reviewer for any kind; a code card with it still runs the gate and merges.
"""

from __future__ import annotations

import unittest

from secretary.board.completion_evidence import (
    has_candidate,
    infra_completion_record,
    infra_report_fields,
    missing_completion_evidence,
    render_infra_completion_record,
    render_research_completion_link,
    review_required,
)
from secretary.dispatcher_observer import render_observer_prompt
from secretary.tasks import TaskError
from tests.dispatcher_fixtures import CARD_REF, DispatcherRuntimeFixture
from tests.fakes.dispatcher import FakeKanboard
from tests.integration_setup import require_disposable_board_fixture

INFRA_REPORT = "## What was done\nRotated the relay key.\n\n## How to verify\n`ssh relay true` exits 0\n"
# Host calls that would publish a branch, open a pull request, poll CI or dispatch a workflow: the
# fake host's gate and merge seams are where the real host does each of those.
CANDIDATE_CALLS = ("gate_check", "complete_green", "rerun_failed_ci")


def setUpModule() -> None:
    """Confirm this CI shard can build its disposable board seam before tests run."""
    require_disposable_board_fixture(FakeKanboard)


class NoCandidateLifecycleTests(DispatcherRuntimeFixture, unittest.TestCase):
    def _kind(self, kind: str, *, review: str = "") -> None:
        """The card as `task create --type` stores it, with the kind's default review choice."""
        self.board.metadata[12]["task_type"] = kind
        self.board.metadata[12]["review"] = review or ("required" if kind == "code" else "skipped")

    def _unobserved(self) -> None:
        self.board.metadata[12].pop("sprint_ref", None)
        self.sprints.rows.clear()
        self.board.sprints.clear()

    def _comments(self, marker_line: str) -> list[str]:
        return [
            comment["body"]
            for comment in self.reader.show(CARD_REF)["comments"]
            if marker_line in comment["body"].splitlines()
        ]

    def _assert_no_candidate_calls(self) -> None:
        for call in CANDIDATE_CALLS:
            self.assertNotIn(call, self.host.calls)
        self.assertEqual(self.host.gate_calls, [])
        self.assertEqual(self.host.completed, [])

    def _link_research_report(self) -> None:
        self.writer.comment(
            role="dispatcher",
            actor="dispatcher",
            reference=CARD_REF,
            body=render_research_completion_link(CARD_REF),
            request_id="test-research-link",
        )

    def test_infra_with_review_skipped_goes_from_ready_to_done_without_a_candidate(self) -> None:
        self._kind("infra", review="skipped")
        self.start_dispatcher()
        self._unobserved()
        self.tick()
        self.assertIn("no branch is published", self._task_document())
        self.assertIn("## How to verify", self._task_document())
        self.host.retained_workspace_dirty = True
        self._report_done(INFRA_REPORT)

        self.assertEqual(self.tick()["to"], "validate")
        done = self.tick()

        self.assertEqual(done["to"], "done")
        self.assertEqual(self.reader.show(CARD_REF)["state"], "done")
        self._assert_no_candidate_calls()
        self.assertEqual(self.host.reviews, [], "review: skipped launches no reviewer")
        self.assertIn("teardown", self.host.calls)
        (record,) = self._comments("[completion:infra]")
        self.assertIn("Rotated the relay key.", record)
        self.assertIn("`ssh relay true` exits 0", record)
        self.assertNotIn(CARD_REF, self.runtime.production_state.load()["records"])

    def test_infra_in_a_parking_sprint_waits_in_assessment_for_the_release(self) -> None:
        self._kind("infra")
        self.start_dispatcher()
        self.tick()
        self._report_done(INFRA_REPORT)
        self.assertEqual(self.tick()["to"], "validate")

        released = self._park_and_decide("release")

        self.assertEqual(released["to"], "done")
        self.assertEqual(self.reader.show(CARD_REF)["state"], "done")
        self._assert_no_candidate_calls()
        self.assertEqual(self.host.reviews, [])
        self.assertEqual(len(self._comments("[completion:infra]")), 1)

    def test_an_infra_report_without_both_sections_is_refused_and_the_card_does_not_advance(self) -> None:
        self._kind("infra")
        self.start_dispatcher()
        self.tick()
        for body in ("## What was done\nRotated the relay key.\n", "## How to verify\n`ssh relay true`\n"):
            with self.subTest(body=body), self.assertRaises(TaskError) as caught:
                self._report_done(body)
            self.assertIn("## What was done", caught.exception.message)

        waiting = self.tick()

        self.assertEqual(waiting["action"], "waiting-worker-report")
        self.assertEqual(self.reader.show(CARD_REF)["state"], "in_progress")
        self.assertEqual(self._comments("[completion:infra]"), [])

    def test_an_infra_release_without_its_record_is_blocked_with_the_missing_evidence_named(self) -> None:
        self._kind("infra")
        self.start_dispatcher()
        self.tick()
        self._report_done(INFRA_REPORT)
        self.assertEqual(self.tick()["to"], "validate")
        self.assertEqual(self.tick()["to"], "assessment")
        # The record is gone by the time the observer releases: the check reads the board, not the
        # acceptance that wrote it.
        self.board.comments[12] = [
            comment for comment in self.board.comments[12] if "[completion:infra]" not in comment["comment"]
        ]
        self._decide("release")

        blocked = self.tick()

        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(blocked["reason"], "completion evidence missing")
        self.assertEqual(blocked["missing_evidence"], "completion:infra")
        card = self.reader.show(CARD_REF)
        self.assertEqual(card["state"], "blocked")
        self.assertIn("`[completion:infra]`", card["comments"][-1]["body"])
        self.assertNotIn("teardown", self.host.calls, "a card without evidence keeps its workspace")
        self._assert_no_candidate_calls()

    def test_a_research_card_without_a_completion_link_is_blocked_on_release(self) -> None:
        self._kind("research")
        self.start_dispatcher()
        self._unobserved()
        self.tick()
        self._report_done("findings written up")
        self.assertEqual(self.tick()["to"], "validate")

        blocked = self.tick()

        self.assertEqual(blocked["reason"], "completion evidence missing")
        self.assertEqual(blocked["missing_evidence"], "completion:research")
        self.assertEqual(self.reader.show(CARD_REF)["state"], "blocked")
        self._assert_no_candidate_calls()

    def test_a_research_card_with_its_completion_link_reaches_done_without_a_candidate(self) -> None:
        self._kind("research")
        self.start_dispatcher()
        self.tick()
        self._report_done("findings written up")
        self.assertEqual(self.tick()["to"], "validate")
        self.assertEqual(self.tick()["to"], "assessment")
        self._link_research_report()
        self._decide("release")

        self.assertEqual(self.tick()["to"], "done")
        self.assertEqual(self.reader.show(CARD_REF)["state"], "done")
        self._assert_no_candidate_calls()

    def test_a_link_written_by_anyone_but_the_dispatcher_is_not_evidence(self) -> None:
        self._kind("research")
        self.start_dispatcher()
        self._unobserved()
        self.tick()
        self.writer.comment(
            role="worker",
            actor="worker",
            reference=CARD_REF,
            body=render_research_completion_link(CARD_REF),
            request_id="forged-link",
        )
        self._report_done("findings written up")
        self.assertEqual(self.tick()["to"], "validate")

        self.assertEqual(self.tick()["reason"], "completion evidence missing")

    def _assert_review_required_launches_a_reviewer_and_no_gate(self, kind: str) -> None:
        self._kind(kind, review="required")
        self.start_dispatcher()
        self._unobserved()
        self.tick()
        self._report_done(INFRA_REPORT)
        self.assertEqual(self.tick()["to"], "validate")

        self.assertEqual(self.tick()["action"], "review-started")
        self.assertEqual(len(self.host.reviews), 1)
        self._assert_no_candidate_calls()

    def test_review_required_on_a_research_card_launches_a_reviewer_and_no_gate(self) -> None:
        self._assert_review_required_launches_a_reviewer_and_no_gate("research")

    def test_review_required_on_an_infra_card_launches_a_reviewer_and_no_gate(self) -> None:
        self._assert_review_required_launches_a_reviewer_and_no_gate("infra")

    def test_a_green_review_on_an_unobserved_infra_card_releases_through_the_evidence_check(self) -> None:
        self._kind("infra", review="required")
        self.start_dispatcher()
        self._unobserved()
        self.tick()
        self._report_done(INFRA_REPORT)
        self.assertEqual(self.tick()["to"], "validate")
        self.assertEqual(self.tick()["action"], "review-started")
        self.writer.verdict(
            role="reviewer",
            actor="reviewer",
            reference=CARD_REF,
            kind="green",
            body="the verification command is sound",
            request_id=self._review_verdict_request_id("green"),
        )

        self.assertEqual(self.tick()["to"], "done")
        self._assert_no_candidate_calls()

    def test_review_skipped_on_a_code_card_runs_the_gate_and_the_merge_without_a_reviewer(self) -> None:
        self._kind("code", review="skipped")
        self.start_dispatcher()
        self._unobserved()
        self._run_worker_to_validate()

        done = self.tick()

        self.assertEqual(done["to"], "done")
        self.assertEqual(self.host.reviews, [])
        self.assertIn("gate_check", self.host.calls)
        self.assertEqual(self.host.completed, [CARD_REF])

    def test_review_skipped_on_a_parked_code_card_merges_only_on_release(self) -> None:
        self._kind("code", review="skipped")
        self.start_dispatcher()
        self._run_worker_to_validate()

        released = self._park_and_decide("release")

        self.assertEqual(released["to"], "done")
        self.assertEqual(self.host.reviews, [])
        self.assertEqual(self.host.completed, [CARD_REF])

    def test_a_research_rework_round_is_accepted_with_an_unchanged_head(self) -> None:
        """The reviewer rejected the report, not a commit: the same HEAD is a fresh report."""
        self._kind("research", review="required")
        self.host.fail_resume_worker_reason = ""
        self.start_dispatcher()
        self.tick()
        self._report_done("first write-up")
        self.assertEqual(self.tick()["to"], "validate")
        self.assertEqual(self.tick()["action"], "review-started")
        self._review_red(body="the report misses the budget section")
        self.assertEqual(self._park_and_decide("rework")["action"], "review-red-reused-worker")
        self.assertEqual(self._pilot_record()["rejected_sha"], self.host.commit)

        self._report_done("second write-up, same checkout")
        advanced = self.tick()

        self.assertEqual(advanced["to"], "validate")
        self.assertEqual(self.reader.show(CARD_REF)["state"], "validate")
        self._assert_no_candidate_calls()

    def test_an_infra_rework_round_writes_a_fresh_completion_record(self) -> None:
        self._kind("infra")
        self.host.fail_resume_worker_reason = ""
        self.start_dispatcher()
        self.tick()
        self._report_done(INFRA_REPORT)
        self.assertEqual(self.tick()["to"], "validate")
        self.assertEqual(self._park_and_decide("rework")["action"], "review-red-reused-worker")

        self._report_done(INFRA_REPORT.replace("exits 0", "prints ok"))
        self.assertEqual(self.tick()["to"], "validate")
        released = self._park_and_decide("release")

        self.assertEqual(released["to"], "done")
        records = self._comments("[completion:infra]")
        self.assertEqual(len(records), 2)
        self.assertIn("prints ok", records[-1])


class ObserverReviewChoiceWordingTests(unittest.TestCase):
    def test_whether_review_runs_is_the_card_s_review_choice_not_a_reviewer_head(self) -> None:
        document = render_observer_prompt({"ref": "sprint:1", "comments": []})
        self.assertIn("`task create --review skipped`", document)
        self.assertIn("skipped for `research` and `infra`", document)
        self.assertNotIn("--review-head none", document)


class CompletionEvidenceParsingTests(unittest.TestCase):
    def test_infra_fields_ignore_headings_inside_a_fence_and_keep_subheadings(self) -> None:
        body = (
            "## What was done\nRotated.\n### Detail\nboth relays\n\n"
            "## How to verify\n```\n## not a heading\nssh relay true\n```\n"
        )
        fields, refusal = infra_report_fields(body)
        self.assertEqual(refusal, "")
        self.assertIn("both relays", fields["What was done"])
        self.assertIn("## not a heading", fields["How to verify"])

    def test_the_record_reads_back_what_it_renders(self) -> None:
        fields, _ = infra_report_fields(INFRA_REPORT)
        task = {
            "ref": CARD_REF,
            "type": "infra",
            "comments": [
                {"marker": "dispatcher", "body": "[dispatcher]\n" + render_infra_completion_record(fields)}
            ],
        }
        self.assertEqual(infra_completion_record(task), fields)
        self.assertEqual(missing_completion_evidence(task), "")
        self.assertEqual(missing_completion_evidence({**task, "comments": []}), "completion:infra")

    def test_a_research_link_must_name_this_cards_report_directory(self) -> None:
        other = {
            "marker": "dispatcher",
            "body": "[dispatcher]\n" + render_research_completion_link("other-1"),
        }
        own = {"marker": "dispatcher", "body": "[dispatcher]\n" + render_research_completion_link(CARD_REF)}
        task = {"ref": CARD_REF, "type": "research", "comments": [other]}
        self.assertEqual(missing_completion_evidence(task), "completion:research")
        self.assertEqual(missing_completion_evidence({**task, "comments": [other, own]}), "")

    def test_a_code_card_needs_no_marker_and_the_review_choice_defaults_to_required(self) -> None:
        self.assertEqual(missing_completion_evidence({"ref": CARD_REF, "type": "code", "comments": []}), "")
        self.assertTrue(review_required({"type": "infra"}))
        self.assertFalse(review_required({"type": "code", "review": "skipped"}))
        self.assertTrue(has_candidate({"type": ""}))
        self.assertFalse(has_candidate({"type": "research"}))


if __name__ == "__main__":
    unittest.main()
