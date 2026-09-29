"""Real SQL card producers, sprint read layer, rendered chip/bell and settlement."""

from __future__ import annotations

import re
from unittest import mock

from secretary.board.owner_events import OwnerEventStore, OwnerEventsUnavailable, ReadRefused, record
from secretary.tasks import TaskWriter
from secretary.web.app import WebApp
from secretary.webproto.owner_events import OwnerEventLayer
from tests.web_fakes import Recording, system_snapshot
from tests.webproto_sprint_fixtures import SprintProtocolFixture


class SprintAttentionTests(SprintProtocolFixture):
    def setUp(self):
        super().setUp()
        self.add_sprint_row("sprint:1", current_task="secretary-12")
        self.store = OwnerEventStore(self.board.credentials, client=self.board)
        self.events = OwnerEventLayer(self.instance, store=self.store)
        self.sprints = self.reads(owner_events=self.events)
        self.writer = TaskWriter(self.board, data_dir=self.data_dir, workspace=self.tmp)
        self.app = WebApp(Recording(system_snapshot=system_snapshot()), Recording(), self.sprints,
                          Recording(), Recording(pause_state={}), Recording(), Recording(), Recording(),
                          owner_events=self.events)

    def page(self):
        response = self.app.handle("GET", "/")
        self.assertEqual(response.status, 200)
        return response.body.decode()

    def attention(self):
        return self.sprints.sprint_list(statuses=["open"])["sprints"]["items"][0]["attention"]

    def assert_wait(self, expected):
        page = self.page()
        card = next(article for article in re.findall(r'<article class="compact-sprint">.*?</article>', page, re.DOTALL)
                    if 'href="/sprints/sprint%3A1"' in article)
        self.assertEqual("attention required" in card, expected)
        count = re.search(r'<span class="bell-count">(\d+)</span>', page)
        self.assertIsNotNone(count)
        if expected:
            self.assertGreater(int(count[1]), 0)
            identifiers = self.attention()["event_ids"]
            listed = {event["id"] for event in self.events.owner_event_list(unread_only=True)["events"]}
            self.assertTrue(set(identifiers) <= listed)
        return page

    def claim(self, kind="code"):
        self.board.save_metadata(12, task_type=kind)
        return self.writer.claim(role="dispatcher", actor="dispatcher", reference="secretary-12",
                                 worker="fixture", request_id="claim-12")

    def move(self, target, request="move-12"):
        return self.writer.move(role="dispatcher", actor="dispatcher", reference="secretary-12",
                                target=target, reason="fixture decision", request_id=request)

    def test_between_cards_empty_waiting_on_and_an_unrelated_notice_are_neutral(self):
        self.board.move(12, "done")
        item = self.sprints.sprint_list()["sprints"]["items"][0]
        self.assertEqual(item["waiting"]["state"], "waiting")
        self.assertEqual(item["waiting_on"], [])
        self.assert_wait(False)
        record("provider_red", None, "unrelated provider notice", "notice", to=self.store)
        self.assert_wait(False)
        self.assertEqual(self.events.unread_count()["count"], 1)

    def test_open_needs_owner_is_scoped_to_its_real_card_and_sprint(self):
        self.board.add_card(90, "other-90", metadata={"task_type": "code", "sprint_ref": "sprint:90"})
        record("e2e_budget_spent", "other-90", "other sprint decision", "other", to=self.store)
        self.assert_wait(False)
        record("e2e_budget_spent", "secretary-12", "this sprint decision", "local", to=self.store)
        self.assert_wait(True)
        self.assertEqual(self.attention()["event_ids"], [self.store.events()[0].id])
        self.events.mark_read(self.attention()["event_ids"][0])
        self.assert_wait(False)

    def test_po_submission_handover_and_completion_use_real_held_events_and_settle(self):
        self.claim("decision")
        self.assert_wait(True)
        [po] = self.store.events(unread_only=True)
        self.assertEqual(po.kind, "card_waits_for_person")
        self.assertTrue(po.held)
        self.assertEqual(self.sprints.sprint_state("sprint:1")["work"]["waiting_on"][0]["kind"], "po")
        with self.assertRaises(ReadRefused):
            self.store.mark_read(po.id)
        self.writer.handover(role="po", actor="po", reference="secretary-12", to="owner",
                             reason="Choose the fixture option", request_id="handover-12")
        self.assert_wait(True)
        [owner] = self.store.events(unread_only=True)
        self.assertEqual(owner.kind, "card_handed_to_owner")
        self.assertTrue(owner.held)
        self.assertIsNotNone(next(event for event in self.store.events() if event.id == po.id).read_at)
        with self.assertRaises(ReadRefused):
            self.store.mark_read(owner.id)
        self.assertEqual(self.store.mark_all_read(), 0)
        self.writer.complete(role="po", actor="po", reference="secretary-12", kind="decision",
                             body="## Decision\nChoose option A.\n\n## How to verify\nRead the fixture.\n",
                             request_id="complete-12")
        self.assert_wait(False)
        self.assertEqual(self.events.unread_count()["count"], 0)

    def test_a_blocked_decision_uses_the_transition_producer_and_clears_on_unblock(self):
        self.claim()
        self.move("blocked")
        self.assert_wait(True)
        [event] = self.store.events(unread_only=True)
        self.assertEqual((event.kind, event.subject_ref), ("card_waits_for_person", "secretary-12"))
        with self.assertRaises(ReadRefused):
            self.store.mark_read(event.id)
        self.writer.move(role="po", actor="po", reference="secretary-12", target="ready",
                         reason="decision taken", sprint_override=True, sprint_override_reason="fixture decision",
                         request_id="unblock-12")
        self.assert_wait(False)
        self.assertEqual(self.events.unread_count()["count"], 0)

    def test_superseded_blocked_card_unavailable_and_unknown_sources_do_not_warn(self):
        self.claim()
        self.move("blocked")
        self.assert_wait(True)
        self.board.add_card(91, "secretary-91", metadata={"task_type": "code", "sprint_ref": "sprint:1", "supersedes": "secretary-12"})
        self.assert_wait(False)
        with mock.patch.object(self.store, "snapshot", side_effect=OwnerEventsUnavailable("board unavailable")):
            page = self.page()
        self.assertNotIn("attention required", page)
        self.assertIn('<span class="bell-count">?</span>', page)
        from secretary.sprints import SprintReader
        from secretary.tasks import TaskError
        with mock.patch.object(SprintReader, "linked_cards", side_effect=TaskError("unavailable", "cannot read cards", 4)):
            self.assert_wait(False)
            self.assertEqual(self.attention()["state"], "unknown")
        self.board.move(12, "in_progress")
        (self.data_dir / "dispatcher" / "production-state.json").unlink()
        self.assertIn("observer unknown", self.assert_wait(False))

    def test_wait_cards_and_ci_waits_do_not_mint_human_events(self):
        from secretary.board.wait_card import TARGET_TIME, WaitSpec, WaitTarget

        spec = WaitSpec(WaitTarget(TARGET_TIME, at="2026-09-29T01:00:00Z"),
                        deadline="2026-09-29T02:00:00Z", returns=("observer",),
                        created_at="2026-09-29T00:00:00Z")
        self.board.save_metadata(12, wait=spec.text())
        self.claim("wait")
        self.assert_wait(False)
        self.assertEqual(self.sprints.sprint_state("sprint:1")["work"]["waiting_on"][0]["kind"], "run")
        self.move("blocked")
        self.assert_wait(False)
        self.assertEqual(self.events.unread_count()["count"], 0)

    def test_a_known_pending_ci_run_is_neutral_through_the_real_card_read(self):
        import json

        from secretary.board.e2e_record import E2eRun, E2eState

        self.claim()
        run = E2eRun(dispatch_id="ci-fixture", sha="a" * 40, repo="example/fixture", branch="main",
                     workflow="ci.yml", intent_at="2026-09-29T00:00:00Z", run_id=42,
                     head_sha="a" * 40, wait_ref="secretary-wait",
                     run_url="https://github.com/example/fixture/actions/runs/42")
        self.board.save_metadata(12, e2e=json.dumps(E2eState(runs=[run]).to_json()))
        self._production({}, {"secretary-12": {"state": "validate", "gate_state": "pending"}})
        work = self.sprints.sprint_state("sprint:1")["work"]
        self.assertEqual(work["waiting_on"][0]["kind"], "run")
        self.assertIn(run.run_url, work["waiting_on"][0]["detail"])
        self.assertNotIn('chip warn', self.assert_wait(False).split('<article class="compact-sprint">')[1].split('</article>')[0])
        self.assertEqual(self.events.unread_count()["count"], 0)

    def test_unread_list_keeps_old_unread_notices_when_the_recent_list_is_full(self):
        record("provider_red", None, "old unread notice", "old", to=self.store)
        with self.store._connection() as connection:
            connection.execute(
                "INSERT INTO owner_events (kind, class, text, created_at, read_at, dedup_key) "
                "SELECT 'provider_red', 'notice', 'read notice', now() + interval '1 hour', now(), "
                "'read-' || i FROM generate_series(1,500) AS i"
            )
        self.assertEqual(len(self.events.owner_event_list()["events"]), 500)
        unread = self.events.owner_event_list(unread_only=True)
        self.assertEqual((unread["unread"], len(unread["events"])), (1, 1))
        self.assertEqual(unread["events"][0]["dedup_key"], "old")
        self.assert_wait(False)

    def test_render_pins_one_statement_for_the_chip_and_bell_and_gets_do_not_write(self):
        self.claim("decision")
        snapshot = self.store.snapshot
        calls = []

        def read_once():
            calls.append(1)
            answer = snapshot()
            # A real settlement after the reading cannot split this response's chip/count.
            self.store.settle_subject("secretary-12")
            return answer

        with mock.patch.object(self.store, "snapshot", side_effect=read_once):
            page = self.page()
        self.assertEqual(len(calls), 1)
        self.assertIn("attention required", page)
        self.assertIn('<span class="bell-count">1</span>', page)
        self.assert_wait(False)
        self.assertEqual(len(self.store.events()), 1)


if __name__ == "__main__":
    import unittest
    unittest.main()
