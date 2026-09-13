"""The operator's half of the web transport: the pause, the open sprints, the history, and the
owner's two writes on a card.

Every test goes through :class:`secretary.web.app.WebApp`, the object the socket handler calls.
The four new layers are either the real ones over the fake board (`TransportFixture`, where the
question is what a write does) or recording fakes (where the question is what the transport hands
down and how a page draws what came back). Nothing here reaches a live installation.
"""

from __future__ import annotations

import json
import unittest
from typing import Any

from secretary.status import collect_status
from secretary.tasks import TaskWriter
from secretary.web.app import ROUTES, WebApp
from secretary.webproto.card_ops import MOVE_TARGETS, CardOperationLayer
from secretary.webproto.errors import InstallationUnavailable, ReadError
from secretary.webproto.reads import ReadLayer, health_summary
from tests.webproto_sprint_fixtures import SprintProtocolFixture


def available() -> dict[str, Any]:
    return {
        "state": "available",
        "reason": None,
        "data_age_seconds": 0.0,
        "observed_at": "2026-09-13T12:00:00Z",
    }


class Recording:
    """A layer that records every call and answers with the document it was given per operation."""

    def __init__(self, **answers: Any) -> None:
        self.answers = answers
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)

        def operation(*args: Any, **kwargs: Any) -> dict[str, Any]:
            self.calls.append((name, {"args": args, **kwargs}))
            answer = self.answers.get(name, {"kind": name})
            if isinstance(answer, Exception):
                raise answer
            return answer

        return operation


def pause_document(*, paused: bool = False, mode: str | None = None) -> dict[str, Any]:
    return {
        "kind": "pause_state",
        "state": {
            "paused": paused,
            "mode": mode,
            "since": "2026-09-13T11:00:00Z" if paused else None,
            "actor": "web" if paused else None,
            "pause_reason": "a release is being cut" if paused else None,
            "source": available(),
        },
        "dispatcher": {"phase": "production", "tracked_cards": 1, "source": available()},
        "heads": {
            "cards": [
                {"ref": "secretary-9", "state": "claimed", "worker": "running", "reviewer": "not-running"}
            ],
            "observers": [],
        },
    }


def sprint_listing(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {"kind": "sprint_list", "sprints": {"source": available(), "items": items}}


def sprint_item(ref: str = "sprint:7") -> dict[str, Any]:
    return {
        "ref": ref,
        "status": "open",
        "product": "secretary",
        "goal": "a goal " * 60,
        "current_task": {"ref": "secretary-9", "live": True, "reason": "cut", "source": available()},
        "observer": {
            "declared": {"profile": "claude-observer", "state": "declared"},
            "launch": {"state": "running", "reason": "an observer head is up", "source": available()},
        },
        "waiting": {
            "state": "waiting",
            "reason": "the observer is waiting for the reviewer",
            "source": available(),
        },
        "checks": {"gate": {"state": "green"}, "reason": "the gate is green for abc", "source": available()},
        "budget": {
            "total": 13,
            "thresholds": {"signal": 12, "hard": 30},
            "signal_reached": True,
            "hard_reached": False,
            "by_type": {"red_review": 13},
        },
        "cards": {"states": {"assessment": ["secretary-9"]}, "source": available()},
        "decision": {
            "entry": {
                "selected_step": "rework secretary-9",
                "selected_why": "the review is red",
                "next_safe_step": "wait",
            },
            "freshness": {"value": {"fresh": False, "error": "resume_stale"}},
        },
    }


def history(items: list[dict[str, Any]], *, more: bool = False) -> dict[str, Any]:
    return {
        "kind": "command_history",
        "observed_at": "2026-09-13T12:00:00Z",
        "limit": 25,
        "commands": {
            "source": available(),
            "items": items,
            "has_more": more,
            "next_cursor": "c-2" if more else None,
        },
    }


def command(action: str, ref: str, *, outcome: str = "success", reason: str | None = None) -> dict[str, Any]:
    return {
        "occurred_at": "2026-09-13T11:59:00Z",
        "actor": {"role": "dispatcher", "id": "secretary-production"},
        "action": action,
        "entity": {"kind": "card", "ref": ref},
        "result": {"outcome": outcome, "reason": reason},
        "request_id": "r-1",
        "event_id": "evt-1",
    }


class FakeAppFixture(unittest.TestCase):
    """The application over recording fakes for every layer."""

    def setUp(self) -> None:
        self.reads = Recording(
            system_snapshot={
                "observed_at": "2026-09-13T12:00:00Z",
                "installation": {
                    "instance": "/i",
                    "name": "test",
                    "data_dir": "/d",
                    "health": {"source": available(), "status": health_summary({})},
                },
                "projects": {"source": available(), "items": []},
                "tasks": {"source": available(), "items": []},
                "agents": {"source": available(), "items": []},
            }
        )
        self.ops = Recording(run_list={"items": []})
        self.sprint_reads = Recording(sprint_list=sprint_listing([sprint_item()]))
        self.sprint_ops = Recording(
            sprint_comment={"kind": "sprint_comment", "comment_id": "evt-c", "saved": True},
            sprint_close={"kind": "sprint_closed"},
        )
        self.pause_reads = Recording(pause_state=pause_document())
        self.pause_ops = Recording(pause_drain={"kind": "pause_drain"}, pause_resume={"kind": "pause_resume"})
        self.command_reads = Recording(
            command_history=history(
                [command("card.assessed", "secretary-9", outcome="", reason="review:red")]
            )
        )
        self.card_ops = Recording(task_comment={"kind": "card_commented"}, task_move={"kind": "card_moved"})

    def app(self) -> WebApp:
        return WebApp(
            self.reads,
            self.ops,
            self.sprint_reads,
            self.sprint_ops,
            self.pause_reads,
            self.pause_ops,
            self.command_reads,
            self.card_ops,
        )

    def get(self, path: str, query: str = ""):
        return self.app().handle("GET", path, query=query)

    def post(self, path: str, payload: dict[str, Any]):
        return self.app().handle("POST", path, body=json.dumps(payload).encode("utf-8"))

    def text(self, response) -> str:
        return response.body.decode("utf-8")

    def error(self, response) -> dict[str, Any]:
        return json.loads(response.body)["error"]


# -- criterion 1: the routes name their operations and hand down exactly what was sent ------------


class RouteTests(FakeAppFixture):
    def test_the_operator_routes_are_in_the_table_naming_their_operation(self) -> None:
        table = {(route.method, route.pattern): route.operation for route in ROUTES}
        self.assertEqual(table[("GET", "/api/pause")], "pause_reads.pause_state")
        self.assertEqual(table[("GET", "/api/pause/scope")], "pause_reads.pause_scope")
        self.assertEqual(table[("POST", "/api/pause/drain")], "pause_ops.pause_drain")
        self.assertEqual(table[("POST", "/api/pause/resume")], "pause_ops.pause_resume")
        self.assertEqual(table[("GET", "/api/sprints")], "sprint_reads.sprint_list")
        self.assertEqual(table[("POST", "/api/sprints/{ref}/comment")], "sprint_ops.sprint_comment")
        self.assertEqual(table[("POST", "/api/sprints/{ref}/close")], "sprint_ops.sprint_close")
        self.assertEqual(table[("GET", "/api/history")], "command_reads.command_history")
        self.assertEqual(table[("GET", "/api/history/{request_id}")], "command_reads.command_request")
        self.assertEqual(table[("GET", "/history")], "command_reads.command_history")
        self.assertEqual(table[("POST", "/api/tasks/{ref}/comment")], "card_ops.task_comment")
        self.assertEqual(table[("POST", "/api/tasks/{ref}/move")], "card_ops.task_move")

    def test_a_drain_carries_the_reason_and_names_the_web_as_the_actor(self) -> None:
        response = self.post("/api/pause/drain", {"reason": "cutting a release"})
        self.assertEqual(response.status, 200)
        self.assertEqual(
            self.pause_ops.calls,
            [("pause_drain", {"args": (), "actor": "web", "reason": "cutting a release"})],
        )

    def test_a_drain_without_a_reason_is_refused_before_the_layer(self) -> None:
        self.assertEqual(self.post("/api/pause/drain", {}).status, 400)
        self.assertEqual(self.post("/api/pause/drain", {"reason": "x", "mode": "freeze"}).status, 400)
        self.assertEqual(self.pause_ops.calls, [])

    def test_a_resume_takes_an_empty_object_and_nothing_else(self) -> None:
        self.assertEqual(self.post("/api/pause/resume", {}).status, 200)
        self.assertEqual(self.pause_ops.calls, [("pause_resume", {"args": (), "actor": "web"})])
        self.assertEqual(self.post("/api/pause/resume", {"reason": "x"}).status, 400)

    def test_the_sprint_listing_passes_the_status_filter_through(self) -> None:
        self.assertEqual(self.get("/api/sprints").status, 200)
        self.assertEqual(self.get("/api/sprints", "status=open&status=stopped").status, 200)
        self.assertEqual(
            [call[1]["statuses"] for call in self.sprint_reads.calls], [None, ["open", "stopped"]]
        )

    def test_a_sprint_comment_is_made_as_the_po_from_the_web(self) -> None:
        response = self.post("/api/sprints/sprint:7/comment", {"request_id": "r-1", "body": "hold on"})
        self.assertEqual(response.status, 200)
        self.assertEqual(
            self.sprint_ops.calls,
            [
                (
                    "sprint_comment",
                    {
                        "args": (),
                        "request_id": "r-1",
                        "actor": "web",
                        "role": "po",
                        "reference": "sprint:7",
                        "body": "hold on",
                    },
                )
            ],
        )

    def test_a_sprint_close_hands_the_decisions_down_unparsed(self) -> None:
        text = "cards:\n  - {ref: secretary-9, verdict: drop, reason: not now}\n"
        response = self.post(
            "/api/sprints/sprint:7/close",
            {"request_id": "r-1", "reason": "why", "closeout": "what became", "decisions": text},
        )
        self.assertEqual(response.status, 200)
        ((name, call),) = self.sprint_ops.calls
        self.assertEqual(name, "sprint_close")
        self.assertEqual(call["decisions"], text)
        self.assertEqual((call["actor"], call["role"], call["reference"]), ("web", "po", "sprint:7"))

    def test_a_sprint_close_without_decisions_hands_down_none(self) -> None:
        self.post(
            "/api/sprints/sprint:7/close",
            {"request_id": "r-1", "reason": "why", "closeout": "done", "decisions": ""},
        )
        self.assertIsNone(self.sprint_ops.calls[0][1]["decisions"])
        self.assertEqual(
            self.post(
                "/api/sprints/sprint:7/close",
                {"request_id": "r-1", "reason": "why", "closeout": "d", "decisions": 3},
            ).status,
            400,
        )

    def test_a_close_missing_its_closeout_is_refused_by_name(self) -> None:
        response = self.post("/api/sprints/sprint:7/close", {"request_id": "r-1", "reason": "why"})
        self.assertEqual(response.status, 400)
        self.assertIn("closeout", self.error(response)["message"])
        self.assertEqual(self.sprint_ops.calls, [])

    def test_the_history_routes_pass_cursor_and_limit(self) -> None:
        self.assertEqual(self.get("/api/history", "cursor=c-1&limit=5").status, 200)
        self.assertEqual(self.command_reads.calls, [("command_history", {"args": ("c-1",), "limit": 5})])
        self.assertEqual(self.get("/api/history/r-9").status, 200)
        self.assertEqual(self.command_reads.calls[-1], ("command_request", {"args": ("r-9",)}))

    def test_a_card_comment_and_a_move_are_made_as_the_po_from_the_web(self) -> None:
        self.assertEqual(
            self.post("/api/tasks/secretary-9/comment", {"request_id": "r-1", "body": "hi"}).status, 200
        )
        self.assertEqual(
            self.card_ops.calls[-1],
            (
                "task_comment",
                {
                    "args": (),
                    "request_id": "r-1",
                    "actor": "web",
                    "role": "po",
                    "reference": "secretary-9",
                    "body": "hi",
                },
            ),
        )
        response = self.post(
            "/api/tasks/secretary-9/move",
            {
                "request_id": "r-2",
                "target": "ready",
                "reason": "reslice",
                "sprint_override": True,
                "sprint_override_reason": "owner",
            },
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(
            self.card_ops.calls[-1],
            (
                "task_move",
                {
                    "args": (),
                    "request_id": "r-2",
                    "actor": "web",
                    "role": "po",
                    "reference": "secretary-9",
                    "target": "ready",
                    "reason": "reslice",
                    "sprint_override": True,
                    "sprint_override_reason": "owner",
                },
            ),
        )

    def test_a_move_without_the_override_hands_down_false(self) -> None:
        self.post("/api/tasks/secretary-9/move", {"request_id": "r-2", "target": "ready", "reason": "x"})
        self.assertIs(self.card_ops.calls[-1][1]["sprint_override"], False)
        self.assertEqual(self.card_ops.calls[-1][1]["sprint_override_reason"], "")

    def test_a_move_with_a_non_boolean_override_is_refused(self) -> None:
        response = self.post(
            "/api/tasks/secretary-9/move",
            {"request_id": "r-2", "target": "ready", "reason": "x", "sprint_override": "maybe"},
        )
        self.assertEqual(response.status, 400)
        self.assertEqual(self.card_ops.calls, [])

    def test_a_refusal_of_a_new_route_keeps_the_layer_code(self) -> None:
        self.pause_ops = Recording(pause_drain=InstallationUnavailable("no instance"))
        response = self.post("/api/pause/drain", {"reason": "x"})
        self.assertEqual(response.status, 503)
        self.assertEqual(self.error(response)["code"], "backend_unavailable")


# -- criterion 2: the dashboard draws each read, and one refusing marks its own section -----------


class DashboardPageTests(FakeAppFixture):
    def test_the_dashboard_reads_the_four_documents_and_draws_each(self) -> None:
        page = self.text(self.get("/"))
        self.assertIn("running — the dispatcher claims cards and raises heads", page)
        self.assertIn('data-action="/api/pause/drain"', page)
        self.assertIn("sprint:7", page)
        self.assertIn("the observer is waiting for the reviewer", page)
        self.assertIn("13 of 30 (signal at 12)", page)
        self.assertIn("rework secretary-9", page)
        self.assertIn("card.assessed", page)
        self.assertIn("review:red", page)
        self.assertIn('href="/history"', page)
        self.assertEqual([name for name, _ in self.sprint_reads.calls], ["sprint_list"])
        self.assertEqual(self.sprint_reads.calls[0][1]["statuses"], ["open"])
        self.assertEqual([name for name, _ in self.command_reads.calls], ["command_history"])

    def test_a_paused_pipeline_offers_resume_and_says_since_when_and_why(self) -> None:
        self.pause_reads = Recording(pause_state=pause_document(paused=True, mode="drain"))
        page = self.text(self.get("/"))
        self.assertIn("drained — no new card is claimed", page)
        self.assertIn('data-action="/api/pause/resume"', page)
        self.assertNotIn('data-action="/api/pause/drain"', page)
        self.assertIn("2026-09-13T11:00:00Z", page)
        self.assertIn("a release is being cut", page)

    def test_a_side_read_that_refuses_marks_its_section_and_the_page_stands(self) -> None:
        self.pause_reads = Recording(pause_state=InstallationUnavailable("the pause flag is unreadable"))
        self.command_reads = Recording(command_history=ReadError("the audit refused"))
        response = self.get("/")
        page = self.text(response)
        self.assertEqual(response.status, 200)
        self.assertIn("could not read whether the pipeline is paused", page)
        self.assertIn("the pause flag is unreadable", page)
        self.assertIn("could not read the last commands", page)
        self.assertIn("sprint:7", page)

    def test_the_snapshot_refusing_is_the_page_refusing(self) -> None:
        self.reads = Recording(system_snapshot=InstallationUnavailable("no installation"))
        response = self.get("/")
        self.assertEqual(response.status, 503)
        self.assertIn("no installation", self.text(response))

    def test_no_open_sprint_is_said_in_words(self) -> None:
        self.sprint_reads = Recording(sprint_list=sprint_listing([]))
        self.assertIn("no sprint is open.", self.text(self.get("/")))

    def test_health_problems_are_listed_by_name(self) -> None:
        status = health_summary(
            {
                "host": {
                    "units": [
                        {
                            "name": "secretary-web.service",
                            "kind": "service",
                            "present": True,
                            "active": "failed",
                        }
                    ]
                },
                "dispatcher": {"pause": {"paused": True, "mode": "freeze"}},
            }
        )
        self.reads.answers["system_snapshot"]["installation"]["health"]["status"] = status
        page = self.text(self.get("/"))
        self.assertIn("attention", page)
        self.assertIn("secretary-web.service is failed", page)
        self.assertIn("the pipeline is paused (freeze)", page)

    def test_the_history_page_links_to_the_older_page_by_cursor(self) -> None:
        self.command_reads = Recording(
            command_history=history([command("routing", "secretary-9")], more=True)
        )
        page = self.text(self.get("/history"))
        self.assertIn("routing", page)
        self.assertIn('href="/history?cursor=c-2&amp;limit=25"', page)

    def test_the_sprints_page_lists_every_sprint_and_marks_the_filter(self) -> None:
        page = self.text(self.get("/sprints", "status=open"))
        self.assertIn("sprint:7", page)
        self.assertIn('href="/sprints?status=open" aria-current="true"', page)
        self.assertIn('href="/sprints/sprint%3A7"', page)
        self.assertEqual(self.sprint_reads.calls[-1][1]["statuses"], ["open"])
        self.assertIn('aria-current="page"', self.text(self.get("/sprints")))

    def test_every_page_carries_the_primary_navigation(self) -> None:
        for path in ("/", "/sprints", "/history", "/sprints/sprint:7"):
            with self.subTest(path=path):
                page = self.text(self.get(path))
                self.assertIn('<nav class="primary"', page)
                self.assertIn('href="/sprints/new"', page)

    def test_the_sprint_page_carries_the_comment_and_close_forms_on_an_open_sprint(self) -> None:
        self.sprint_reads = Recording(
            sprint_state={
                "ref": "sprint:7",
                "sprint": {
                    "source": available(),
                    "value": {"ref": "sprint:7", "status": "open", "goal": "g"},
                },
                "observer": {},
                "work": {"waiting": {"state": "working", "reason": "busy"}},
            }
        )
        page = self.text(self.get("/sprints/sprint:7"))
        self.assertIn('data-action="/api/sprints/sprint%3A7/comment"', page)
        self.assertIn('data-action="/api/sprints/sprint%3A7/close"', page)
        self.assertIn("busy", page)
        self.sprint_reads.answers["sprint_state"]["sprint"]["value"]["status"] = "closed"
        self.assertNotIn("/close", self.text(self.get("/sprints/sprint:7")))


# -- criterion 3: the card writes really write, under the writer's own rules ---------------------


class CardOperationTests(SprintProtocolFixture):
    def layer(self) -> CardOperationLayer:
        return CardOperationLayer(
            self.instance, data_dir=self.data_dir, board_client=self.board, clock=lambda: self.clock
        )

    def writer(self) -> TaskWriter:
        return TaskWriter(self.board, data_dir=self.data_dir)

    def card(self) -> tuple[str, str]:
        """The card the fake board seeds, and the state it is in: `secretary-12`, in Ready."""
        return "secretary-12", self.state_of("secretary-12")

    def state_of(self, ref: str) -> str:
        return str(self.writer().reader.show(ref)["state"])

    def test_a_comment_lands_on_the_card_as_the_po(self) -> None:
        ref, _state = self.card()
        answer = self.layer().task_comment(request_id="r-1", actor="web", reference=ref, body="hold on")
        self.assertEqual(answer["kind"], "card_commented")
        self.assertEqual(answer["ref"], ref)
        comments = [
            str(comment.get("comment") if isinstance(comment, dict) else comment)
            for held in getattr(self.board, "comments", {}).values()
            for comment in held
        ]
        self.assertTrue(any("hold on" in comment for comment in comments), comments)

    def test_a_comment_on_no_card_is_not_found(self) -> None:
        with self.assertRaises(ReadError) as caught:
            self.layer().task_comment(request_id="r-1", actor="web", reference="secretary-none", body="x")
        self.assertEqual(caught.exception.code, "not_found")

    def test_an_empty_comment_and_a_missing_request_id_are_refused_before_the_board(self) -> None:
        ref, _state = self.card()
        for kwargs in ({"request_id": "", "body": "x"}, {"request_id": "r", "body": "  "}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ReadError) as caught:
                self.layer().task_comment(actor="web", reference=ref, **kwargs)
            self.assertEqual(caught.exception.code, "validation")

    def test_a_move_to_a_state_that_is_not_one_is_refused_naming_the_choices(self) -> None:
        ref, _state = self.card()
        with self.assertRaises(ReadError) as caught:
            self.layer().task_move(request_id="r-1", actor="web", reference=ref, target="Done", reason="x")
        self.assertEqual(caught.exception.code, "validation")
        for target in MOVE_TARGETS:
            self.assertIn(target, caught.exception.message)

    def test_an_override_without_its_own_reason_is_refused(self) -> None:
        ref, _state = self.card()
        with self.assertRaises(ReadError) as caught:
            self.layer().task_move(
                request_id="r-1",
                actor="web",
                reference=ref,
                target="blocked",
                reason="x",
                sprint_override=True,
            )
        self.assertEqual(caught.exception.code, "validation")

    def test_a_move_the_writer_permits_moves_the_card(self) -> None:
        ref, state = self.card()
        self.assertNotEqual(state, "blocked")
        answer = self.layer().task_move(
            request_id="r-1", actor="web", reference=ref, target="blocked", reason="hold"
        )
        self.assertEqual(answer["kind"], "card_moved")
        self.assertEqual(self.state_of(ref), "blocked")

    def test_a_move_the_writer_forbids_is_an_owner_conflict(self) -> None:
        """A PO may move a card between any two states; to the state it is in is not a move."""
        ref, state = self.card()
        with self.assertRaises(ReadError) as caught:
            self.layer().task_move(request_id="r-1", actor="web", reference=ref, target=state, reason="x")
        self.assertEqual(caught.exception.code, "owner_conflict")
        self.assertEqual(self.state_of(ref), state)


# -- the health summary and the cheaper status read ----------------------------------------------


class HealthSummaryTests(unittest.TestCase):
    def test_nothing_wrong_is_ok_with_no_problems(self) -> None:
        summary = health_summary(
            {"host": {"units": [{"name": "a.timer", "kind": "timer", "present": True, "active": "active"}]}}
        )
        self.assertEqual((summary["state"], summary["problems"]), ("ok", []))

    def test_each_failure_the_collector_marks_is_a_problem_by_name(self) -> None:
        summary = health_summary(
            {
                "host": {
                    "units": [{"name": "x.service", "kind": "service", "present": False, "active": None}],
                    "external_runtime": {"name": "orca-server.service", "active": "inactive"},
                    "inventory_errors": {"units": "systemctl timed out"},
                },
                "dispatcher": {"pause": {"paused": True, "mode": "drain"}, "divergences": {"open_count": 2}},
                "checkpoint": {
                    "checkpoint_status": "failed",
                    "checkpoint_last_failure_reason": "push refused",
                    "blocked_reason": "no remote",
                },
                "card_backend": {"backend": "postgres", "findings": ["x"]},
                "secret_store": {"installation_key": {"present": True, "usable": False}},
                "memory": {"index_present": False},
            }
        )
        self.assertEqual(summary["state"], "attention")
        self.assertEqual(
            summary["problems"],
            [
                "x.service is not installed on this host",
                "orca-server.service is inactive",
                "the host inventory could not read units: systemctl timed out",
                "the pipeline is paused (drain)",
                "2 dispatcher divergence(s) are open",
                "the checkpoint is blocked: no remote",
                "the last checkpoint failed: push refused",
                "card_backend has 1 finding(s)",
                "the secret store's installation key is not usable",
                "the memory index is missing",
            ],
        )
        self.assertEqual(summary["card_backend"], "postgres")
        self.assertTrue(summary["dispatcher"]["paused"])

    def test_an_inactive_oneshot_service_is_not_a_problem(self) -> None:
        summary = health_summary(
            {
                "host": {
                    "units": [
                        {
                            "name": "secretary-curator.service",
                            "kind": "service",
                            "present": True,
                            "active": "inactive",
                        }
                    ]
                }
            }
        )
        self.assertEqual(summary["problems"], [])


class StatusWithoutSprintsTests(SprintProtocolFixture):
    def test_the_web_reads_status_without_the_sprints_or_the_panel_probes(self) -> None:
        from secretary.config import validate_instance

        report = validate_instance(self.instance)
        snapshot = collect_status(
            report, offline=True, sprint_client=self.board, sprints=False, probe_panels=False
        )
        self.assertEqual(snapshot["installation"]["sprints"]["items"], [])
        self.assertIn("skipped", snapshot["installation"]["sprints"])
        whole = collect_status(report, offline=True, sprint_client=self.board)
        self.assertNotIn("skipped", whole["installation"]["sprints"])

    def test_the_snapshot_carries_the_summary_and_not_the_whole_status(self) -> None:
        layer = ReadLayer(
            self.instance,
            data_dir=self.data_dir,
            board_client=self.board,
            status_reader=lambda: {"host": {"units": []}, "dispatcher": {"active_attempts": [1, 2]}},
        )
        health = layer.system_snapshot()["installation"]["health"]
        self.assertEqual(health["source"]["state"], "available")
        self.assertEqual(health["status"]["state"], "ok")
        self.assertEqual(health["status"]["dispatcher"]["active_attempts"], 2)
        self.assertNotIn("host", health["status"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
