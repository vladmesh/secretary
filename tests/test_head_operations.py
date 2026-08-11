"""secretary-1412: the three head operations, run against a fake session manager.

This is the contract suite of `spawn` / `nudge` / `stop`, and the point of it is what it does *not*
need: no Orca, no dispatcher, no installation. A head's whole life is exercised through a fake host
that is 60 lines of dictionary, which is only possible because the operations reach a session
manager through `SessionHost` and nothing else. That is the first piece of the backend-independent
suite Milestone 5 asks for; a second session manager would be this fake with a real backend behind
it, and these tests are what it would have to satisfy.

What is pinned here:

  * spawn brings a head up, nudge delivers to it, stop ends it, and the lifecycle moves
    `spawned → working → finishing → exited` with nothing skipping a state;
  * a stop records who initiated it, and that initiator survives the record being written and read
    back — which is what "the dispatcher restarted" looks like from inside a run;
  * a pane handle the session manager reincarnates does not create a second run: the identity is
    the run's own, the new handle is bound onto it, and the stop closes the pane that exists now;
  * a stop that is refused stays the same stop: the run keeps its identity and its first initiator
    through the retry, and the durable commit of `finishing` happens before any host call;
  * a head can be pointed at a card, a sprint entity or a role's standing instruction, because the
    observer and the mechanical roles have no card and one must not be invented for them;
  * the package reaches Orca only through the host — asserted by reading its own source. That is a
    source check and it proves only what a source check can; what binds the *production* path to
    the host seam is `WorkerPathReachesOnlyTheSessionHostTests` in `test_dispatcher_launch_intent`,
    which drives the dispatcher's own worker spawn/nudge/stop with a runner that fails if reached.
"""

from __future__ import annotations

import ast
import unittest
from collections.abc import Callable
from pathlib import Path

from triggered_agents.runtime.head import (
    EXITED,
    FINISHING,
    SPAWNED,
    WORKING,
    HeadRun,
    HeadSpawnAborted,
    HeadSpawnFailed,
    HeadSpec,
    HeadStopFailed,
    NudgePointer,
    StopInitiator,
    TaskRef,
    nudge,
    spawn,
    stop,
)
from triggered_agents.runtime.head import operations as head_operations
from triggered_agents.runtime.pane_host import Pane

HEAD_PACKAGE = Path(head_operations.__file__).parent


class FakeSessionHost:
    """A session manager with no Orca behind it: panes are rows in a list.

    It answers every method of `SessionHost` and records what it was asked, so a test can say both
    "the head came up" and "it came up through the host rather than around it".
    """

    def __init__(self) -> None:
        self.panes_by_workspace: dict[str, list[Pane]] = {}
        self.commands: dict[str, str] = {}
        self.calls: list[tuple] = []
        self.sent: list[tuple[str, str, bool]] = []
        self.closed: list[str] = []
        self.refuse_close = False
        self.next_handle = 1
        # A test that cares *when* the session manager was reached, not only whether it was.
        self.on_call: Callable[[str], None] | None = None

    def _note(self, name: str) -> None:
        if self.on_call is not None:
            self.on_call(name)

    # lifecycle ------------------------------------------------------------
    def open_pane(self, workspace: str, title: str, command: str) -> Pane:
        self.calls.append(("open_pane", workspace, title))
        self._note("open_pane")
        pane = Pane(handle=f"term:{self.next_handle}", leaf=f"leaf:{self.next_handle}")
        self.next_handle += 1
        self.panes_by_workspace.setdefault(workspace, []).append(pane)
        self.commands[pane.handle] = command
        return pane

    def split_pane(self, handle: str, command: str) -> Pane:
        self.calls.append(("split_pane", handle))
        self._note("split_pane")
        workspace = self._workspace_of(handle)
        pane = Pane(handle=f"term:{self.next_handle}", leaf=f"leaf:{self.next_handle}")
        self.next_handle += 1
        self.panes_by_workspace.setdefault(workspace, []).append(pane)
        self.commands[pane.handle] = command
        return pane

    def rename_pane(self, handle: str, title: str) -> None:
        self.calls.append(("rename_pane", handle, title))
        self._note("rename_pane")

    def close_pane(self, handle: str) -> None:
        self.calls.append(("close_pane", handle))
        self._note("close_pane")
        if self.refuse_close:
            raise RuntimeError("tab_not_found")
        self.closed.append(handle)
        for workspace, panes in self.panes_by_workspace.items():
            self.panes_by_workspace[workspace] = [p for p in panes if p.handle != handle]

    def panes(self, workspace: str) -> list[Pane]:
        self.calls.append(("panes", workspace))
        self._note("panes")
        return list(self.panes_by_workspace.get(workspace, []))

    def stop_workspace(self, workspace: str) -> None:
        self.calls.append(("stop_workspace", workspace))
        self._note("stop_workspace")
        self.panes_by_workspace[workspace] = []

    # delivery -------------------------------------------------------------
    def send(self, handle: str, text: str, *, enter: bool):
        self.calls.append(("send", handle))
        self._note("send")
        self.sent.append((handle, text, enter))
        return {"accepted": True, "bytesWritten": len(text.encode())}

    def read(self, handle: str, *, limit: int | None = None):
        self._note("read")
        return {"terminal": {"tail": ["› "], "nextCursor": f"c{len(self.sent)}"}}

    def wait_idle(self, handle: str, *, timeout_ms: int):
        self._note("wait_idle")
        return {"satisfied": True}

    def reincarnate(self, handle: str) -> str:
        """Give a pane a new handle while its pty — and so its leaf — stays exactly where it is."""
        for workspace, panes in self.panes_by_workspace.items():
            for index, pane in enumerate(panes):
                if pane.handle == handle:
                    fresh = Pane(handle=f"{handle}:alias", leaf=pane.leaf)
                    panes[index] = fresh
                    return fresh.handle
        raise AssertionError(f"no pane {handle}")

    def _workspace_of(self, handle: str) -> str:
        for workspace, panes in self.panes_by_workspace.items():
            if any(pane.handle == handle for pane in panes):
                return workspace
        return ""


CODEX = HeadSpec(profile_id="codex-worker", adapter="codex", effort="high", codex_mode="tui")
CLAUDE = HeadSpec(profile_id="claude-default", adapter="claude")
WORKSPACE = "/tmp/does-not-need-to-exist/secretary-1412"


def confirmed(_sent_at: float) -> bool:
    """The caller's criterion, answering the way a head whose turn started answers."""
    return True


CONFIRMING = head_operations.HostTransport(confirm=confirmed)


class RefusingTransport(head_operations.HostTransport):
    """A product transport whose delivery never reaches its confirmation.

    Subclassed from the default rather than written from scratch, so what it refuses is the one
    thing under test and everything else — the close, and the fact that both go through the host
    it is handed — stays exactly what production does.
    """

    def __init__(self, reason: str = "the head never took the prompt") -> None:
        super().__init__(confirm=confirmed)
        object.__setattr__(self, "reason", reason)

    def deliver(self, run, pointer, *, host, subject):
        raise RuntimeError(self.reason)


class HeadOperationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.host = FakeSessionHost()
        self.task = TaskRef.card("secretary-1412", document=f"{WORKSPACE}/TASK.md")

    def bring_up(self, spec: HeadSpec = CODEX, **kwargs):
        kwargs.setdefault("transport", CONFIRMING)
        return spawn(
            spec,
            WORKSPACE,
            self.task,
            host=self.host,
            command="run-worker",
            title="secretary-1412 worker",
            **kwargs,
        )

    # spawn ----------------------------------------------------------------
    def test_spawn_opens_a_pane_through_the_host_and_runs_the_command(self) -> None:
        outcome = self.bring_up()

        self.assertEqual(self.host.calls[0], ("open_pane", WORKSPACE, "secretary-1412 worker"))
        self.assertEqual(self.host.commands[outcome.run.handle], "run-worker")
        self.assertEqual(outcome.run.lifecycle, SPAWNED)
        self.assertEqual(outcome.run.spec, CODEX)
        self.assertEqual(outcome.run.task_ref, self.task)

    def test_a_head_whose_prompt_arrives_after_start_is_nudged_by_the_spawn(self) -> None:
        outcome = self.bring_up(pointer=NudgePointer.at_document(self.task.document))

        self.assertEqual(outcome.run.lifecycle, WORKING)
        self.assertIn(self.task.document, self.host.sent[0][1])
        self.assertEqual(outcome.delivery.evidence.delivery_mode, "nudge-file")

    def test_a_head_given_its_task_on_the_command_line_is_not_nudged(self) -> None:
        """No pointer, no delivery: the prompt shape is the caller's to decide, not the spec's."""
        outcome = self.bring_up(spec=CLAUDE)

        self.assertEqual(self.host.sent, [])
        self.assertEqual(outcome.run.lifecycle, SPAWNED)
        self.assertIsNone(outcome.delivery)

    def test_the_task_document_itself_never_enters_the_pane(self) -> None:
        """The protocol rule the nudge exists for, held at the operation boundary too."""
        outcome = self.bring_up(pointer=NudgePointer.at_document(self.task.document))

        # The body write and the submission are separate writes; neither may carry a task.
        for _handle, text, _enter in self.host.sent:
            self.assertLess(len(text.encode()), 256)
        self.assertEqual(outcome.delivery.evidence.document_path, self.task.document)

    def test_a_split_head_is_opened_beside_its_sibling_and_labelled(self) -> None:
        anchor = self.bring_up().run

        outcome = self.bring_up(split_from=anchor.handle)

        self.assertIn(("split_pane", anchor.handle), self.host.calls)
        self.assertIn(("rename_pane", outcome.run.handle, "secretary-1412 worker"), self.host.calls)

    def test_a_bring_up_whose_pane_closes_cleanly_left_nothing_running(self) -> None:
        with self.assertRaises(HeadSpawnFailed):
            self.bring_up(
                pointer=NudgePointer.line("report now"), transport=RefusingTransport()
            )
        self.assertEqual(self.host.closed, ["term:1"])

    def test_a_bring_up_whose_pane_will_not_close_is_ambiguous_and_keeps_its_run(self) -> None:
        """The distinction the product has killed live heads by collapsing."""
        self.host.refuse_close = True
        with self.assertRaises(HeadSpawnAborted) as caught:
            self.bring_up(
                pointer=NudgePointer.line("report now"), transport=RefusingTransport()
            )

        self.assertEqual(caught.exception.run.handle, "term:1")
        self.assertEqual(caught.exception.run.workspace, WORKSPACE)

    def test_an_unconfirmed_nudge_never_closes_the_pane(self) -> None:
        """A document nudge that was not confirmed says nothing about the head in the pane."""
        with self.assertRaises(HeadSpawnAborted):
            self.bring_up(
                pointer=NudgePointer.at_document(self.task.document),
                transport=RefusingTransport("delivery was not confirmed"),
            )

        self.assertEqual(self.host.closed, [])

    # nudge ----------------------------------------------------------------
    def test_nudge_delivers_into_a_live_head_and_marks_it_working(self) -> None:
        run = self.bring_up().run

        outcome = nudge(run, NudgePointer.line("report now"), host=self.host, transport=CONFIRMING)

        self.assertEqual(outcome.run.lifecycle, WORKING)
        self.assertEqual(self.host.sent[0][0], run.handle)
        self.assertEqual(outcome.run.run_id, run.run_id)

    def test_a_head_that_is_being_stopped_is_not_given_more_work(self) -> None:
        run = self.bring_up().run.finishing(StopInitiator(actor="watchdog"))

        with self.assertRaises(head_operations.HeadNudgeFailed):
            nudge(run, NudgePointer.line("report now"), host=self.host, transport=CONFIRMING)

    # stop -----------------------------------------------------------------
    def test_stop_closes_the_pane_and_records_who_ended_the_head(self) -> None:
        run = self.bring_up().run

        outcome = stop(run, StopInitiator(actor="reviewer-freeze", reason="review took the tree"),
                       host=self.host)

        self.assertEqual(outcome.run.lifecycle, EXITED)
        self.assertEqual(outcome.run.stopped_by.actor, "reviewer-freeze")
        self.assertEqual(outcome.run.stopped_by.reason, "review took the tree")
        self.assertEqual(self.host.closed, [run.handle])

    def test_a_stop_needs_an_initiator_to_be_called_at_all(self) -> None:
        """Not a check in the body: the signature has no call without one."""
        run = self.bring_up().run

        with self.assertRaises(TypeError):
            stop(run, host=self.host)  # type: ignore[call-arg]

    def test_a_refused_close_is_a_stop_that_did_not_happen(self) -> None:
        run = self.bring_up().run
        self.host.refuse_close = True

        with self.assertRaises(HeadStopFailed) as caught:
            stop(run, StopInitiator(actor="operator"), host=self.host)

        self.assertEqual(caught.exception.run.lifecycle, FINISHING)
        self.assertEqual(caught.exception.run.stopped_by.actor, "operator")

    def test_the_initiator_is_committed_before_the_pane_is_touched(self) -> None:
        """A dispatcher that dies mid-stop still leaves a record naming who was ending this head.

        The order is the contract, so this reads the durable write and the host calls on one
        timeline: the run must be written down, in `finishing` and with its initiator, before the
        session manager has been asked for anything at all.
        """
        timeline: list[str] = []
        written: list[HeadRun] = []
        run = self.bring_up().run
        self.host.calls.clear()
        self.host.on_call = lambda name: timeline.append(f"host:{name}")

        def commit(finishing: HeadRun) -> None:
            # Read back the way a restarted dispatcher would, not kept as the object in hand.
            written.append(HeadRun.from_json(finishing.to_json()))
            timeline.append("commit")

        self.host.refuse_close = True
        with self.assertRaises(HeadStopFailed):
            stop(run, StopInitiator(actor="idle-watchdog", reason="no turn"),
                 host=self.host, commit=commit)

        self.assertEqual(written[0].stopped_by.actor, "idle-watchdog")
        self.assertEqual(written[0].lifecycle, FINISHING)
        self.assertEqual(written[0].run_id, run.run_id)
        self.assertEqual(timeline[0], "commit", timeline)

    def test_a_retried_stop_keeps_the_run_and_the_actor_that_began_it(self) -> None:
        """The refused stop is retried by another path with another actor. The record is the first.

        This is the whole of the identity invariant: reconciliation may readdress a run at the pane
        it is in now, but it may not hand a run that is still `finishing` a new identity or a new
        initiator. A record that answered "who stopped this worker" with whoever retried last would
        be answering a different question than the one it exists for.
        """
        run = self.bring_up().run
        self.host.refuse_close = True
        with self.assertRaises(HeadStopFailed) as first:
            stop(run, StopInitiator(actor="review-freeze", reason="review took the tree"),
                 host=self.host)

        stored = HeadRun.from_json(first.exception.run.to_json())
        self.assertFalse(stored.settled, "a refused stop is not finished with")
        self.host.refuse_close = False

        outcome = stop(stored, StopInitiator(actor="reconciliation"), host=self.host)

        self.assertEqual(outcome.run.run_id, run.run_id)
        self.assertEqual(outcome.run.stopped_by.actor, "review-freeze")
        self.assertEqual(outcome.run.stopped_by.reason, "review took the tree")
        self.assertEqual(outcome.run.lifecycle, EXITED)

    def test_a_head_with_neither_pane_nor_heartbeat_cannot_be_promised_gone(self) -> None:
        run = self.bring_up().run
        orphan = HeadRun(
            run_id=run.run_id, spec=CODEX, workspace="", task_ref=self.task,
        )

        with self.assertRaises(HeadStopFailed):
            stop(orphan, StopInitiator(actor="reconciliation"), host=self.host)

    # lifecycle and identity ------------------------------------------------
    def test_the_whole_lifecycle_runs_in_order(self) -> None:
        spawned = self.bring_up().run
        self.assertEqual(spawned.lifecycle, SPAWNED)

        working = nudge(spawned, NudgePointer.line("go"), host=self.host, transport=CONFIRMING).run
        self.assertEqual(working.lifecycle, WORKING)

        finishing = working.finishing(StopInitiator(actor="release"))
        self.assertEqual(finishing.lifecycle, FINISHING)

        exited = stop(working, StopInitiator(actor="release"), host=self.host).run
        self.assertEqual(exited.lifecycle, EXITED)
        self.assertEqual(exited.run_id, spawned.run_id)

    def test_a_reincarnated_pane_handle_does_not_create_a_second_run(self) -> None:
        run = self.bring_up().run
        fresh = self.host.reincarnate(run.handle)
        self.assertNotEqual(fresh, run.handle)

        pointer = NudgePointer.line("still there?")
        nudged = nudge(run, pointer, host=self.host, transport=CONFIRMING).run

        self.assertEqual(nudged.run_id, run.run_id)
        self.assertTrue(nudged.same_run(run))
        self.assertEqual(nudged.handle, fresh)
        self.assertEqual(self.host.sent[0][0], fresh)

    def test_a_stop_closes_the_pane_the_run_is_in_now(self) -> None:
        run = self.bring_up().run
        fresh = self.host.reincarnate(run.handle)

        outcome = stop(run, StopInitiator(actor="release"), host=self.host)

        self.assertEqual(self.host.closed, [fresh])
        self.assertEqual(outcome.run.run_id, run.run_id)

    def test_an_unreadable_inventory_is_not_a_pane_that_is_gone(self) -> None:
        """A stop that cannot locate its pane must refuse, not report success over a live head."""
        run = self.bring_up().run

        def refuse(_workspace: str):
            raise RuntimeError("selector_unavailable")

        self.host.panes = refuse  # type: ignore[assignment]
        with self.assertRaises(HeadStopFailed):
            stop(run, StopInitiator(actor="release"), host=self.host)

    def test_a_run_survives_being_written_down_and_read_back(self) -> None:
        run = stop(self.bring_up().run, StopInitiator(actor="watchdog", reason="idle"),
                   host=self.host).run

        restored = HeadRun.from_json(run.to_json())

        self.assertEqual(restored, run)
        self.assertEqual(restored.stopped_by.actor, "watchdog")
        self.assertEqual(restored.lifecycle, EXITED)
        self.assertEqual(restored.spec.adapter, "codex")


class TaskPointerTests(unittest.TestCase):
    """A head is pointed at a durable document, and a card is only one kind of one."""

    def setUp(self) -> None:
        self.host = FakeSessionHost()

    def spawn_at(self, task_ref: TaskRef) -> HeadRun:
        return spawn(
            CODEX, WORKSPACE, task_ref, host=self.host, command="run", title="head",
            transport=CONFIRMING,
        ).run

    def test_all_three_kinds_of_task_document_can_carry_a_head(self) -> None:
        for task_ref in (
            TaskRef.card("secretary-1412", document=f"{WORKSPACE}/TASK.md"),
            TaskRef.sprint("sprint:848"),
            TaskRef.standing("observer", document="/var/lib/secretary/observer.md"),
        ):
            with self.subTest(kind=task_ref.kind):
                run = self.spawn_at(task_ref)
                self.assertEqual(run.task_ref, task_ref)
                self.assertEqual(HeadRun.from_json(run.to_json()).task_ref, task_ref)

    def test_a_sprint_head_needs_no_card_and_no_document_on_disk(self) -> None:
        run = self.spawn_at(TaskRef.sprint("sprint:848"))

        self.assertEqual(run.task_ref.kind, "sprint")
        self.assertEqual(run.task_ref.document, "")

    def test_a_pointer_of_no_known_kind_is_refused(self) -> None:
        from triggered_agents.runtime.head import TaskRefError

        with self.assertRaises(TaskRefError):
            TaskRef(kind="whatever", ref="x")
        with self.assertRaises(TaskRefError):
            TaskRef.card("")


class BackendIndependenceTests(unittest.TestCase):
    """The property that makes the suite above possible, asserted rather than assumed."""

    def test_the_head_package_names_no_session_manager_and_spawns_no_process(self) -> None:
        """Prose may discuss Orca; code may not reach it. The check reads the syntax tree, so a
        docstring naming the session manager it is deliberately independent of does not pass for a
        command that runs it."""
        for source in sorted(HEAD_PACKAGE.glob("*.py")):
            tree = ast.parse(source.read_text(encoding="utf-8"))
            docstrings = {
                id(node.value)
                for node in ast.walk(tree)
                if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
            }
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    names = [alias.name for alias in node.names]
                    names.append(getattr(node, "module", "") or "")
                    for name in names:
                        self.assertNotIn(
                            "subprocess", name, f"{source.name} spawns processes of its own"
                        )
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    if id(node) in docstrings:
                        continue
                    self.assertNotIn(
                        "orca", node.value.lower(),
                        f"{source.name} reaches a session manager by name: {node.value!r}",
                    )
