from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from secretary.dispatch import host as dispatcher_host_module
from secretary.checkpoint import CheckpointResult
from secretary.dispatcher import (
    STOPPED_BY_OPERATOR,
    STOPPED_BY_RECONCILIATION,
    STOPPED_BY_REPLACEMENT,
    STOPPED_BY_REVIEW_FREEZE,
    STOPPED_BY_REVIEW_VERDICT,
    STOPPED_BY_WATCHDOG,
    CommandHostRuntime,
    DispatcherError,
    DispatcherRuntime,
    HostError,
)
from secretary.dispatcher_heartbeat import heartbeat_identity, run_heartbeat_identity
from secretary.dispatcher_review import (
    recover_review_launch,
)
from secretary.dispatcher_state import (
    DispatcherRecord,
)

GITHUB_FAILED_LOG_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "github_actions_failed_logs"
from secretary.dispatcher_types import (
    HeadLaunchAborted,
    HeadPaneNotReady,
    review_pane_label,
)
from secretary.dispatcher_watchdog import (
    WORKER_REPORT_STALL_DEFAULT,
    bind_head_heartbeat,
    head_process_status,
    initial_output_stall_seconds,
    pid_file_path,
)
from secretary.dispatcher_worker_lifecycle import (
    WorkerContinuation,
    WorkerContinuationStage,
    head_run_binding,
)
from secretary.tasks import TaskAudit, TaskReader, TaskWriter
from tests.dispatcher_fixtures import (
    PromptAfterStartCatalog,
    RecordingReviewHost,
    write_heartbeat,
)
from tests.dispatcher_fixtures import (
    clear_env as _clear_env,
)
from tests.fakes.dispatcher import (
    FakeCatalog,
    FakeCheckpoint,
    FakeHost,
    FakeKanboard,
    FakePusher,
)
from tests.fanout_fixtures import accepted_transport_run
from tests.integration_setup import require_disposable_board_fixture
from triggered_agents.runtime.agent_prompt_transport import (
    BRACKETED_PASTE_END,
    BRACKETED_PASTE_START,
)
from triggered_agents.runtime.head import operations as head_ops
from triggered_agents.runtime.head import (
    with_pid_heartbeat,
)
from triggered_agents.runtime.pane_host import PaneSplitSourceMissing
from triggered_agents.runtime.prompt_document import (
    NUDGE_FILE_MODE,
    NUDGE_MAX_BYTES,
    PromptDocumentError,
)
from triggered_agents.runtime.tui_delivery import TUI_IDLE_PROBE_TIMEOUT_MS


def setUpModule() -> None:
    """Confirm this CI shard can build its disposable board seam before tests run."""
    require_disposable_board_fixture(FakeKanboard)


class PidHeartbeatTests(unittest.TestCase):
    """secretary-751: the pid a head writes for itself before it execs, and how the watchdog
    reads it back. This is the signal that distinguishes a live silent head from a shell left
    behind after the head exits, without reading terminal text, title, or a generic running flag.
    """

    write_heartbeat = staticmethod(write_heartbeat)

    def test_heartbeat_writes_an_atomic_versioned_identity_then_execs_the_head(self) -> None:
        wrapped = with_pid_heartbeat(
            "codex exec --dangerously-bypass-approvals-and-sandbox",
            "/tmp/x.pid",
            identity=heartbeat_identity(run_id="run-1", role="worker", task="card:secretary-751"),
        )

        self.assertIn("python3 -P -c", wrapped)
        self.assertIn("os.replace", wrapped)
        self.assertIn("exec env codex exec --dangerously-bypass-approvals-and-sandbox", wrapped)

    def test_heartbeat_survives_a_leading_environment_assignment(self) -> None:
        """secretary-751 review: catalog commands from `head_launch` start with `NAME=value`, which
        bare `exec` cannot run directly. Executed through a real `/bin/sh` (not just string
        comparison), the wrapped command must still exec successfully and the pid file must end up
        holding the pid of the process that was actually running when it exited."""
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = os.path.join(tmp, "x.pid")
            wrapped = with_pid_heartbeat(
                'FOO=bar python3 -c "import os; print(os.getpid())"',
                pid_file,
                identity=heartbeat_identity(run_id="run-1", role="worker", task="card:secretary-751"),
            )

            result = subprocess.run(
                ["/bin/sh", "-lc", wrapped],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            reported_pid = result.stdout.strip()
            heartbeat = json.loads(Path(pid_file).read_text(encoding="utf-8"))
            self.assertEqual(reported_pid, str(heartbeat["pid"]))
            self.assertEqual(heartbeat["version"], 1)
            self.assertEqual(heartbeat["run_id"], "run-1")

    def test_heartbeat_quotes_a_pid_file_path_with_spaces(self) -> None:
        wrapped = with_pid_heartbeat(
            "codex exec",
            "/tmp/weird dir/x.pid",
            identity=heartbeat_identity(run_id="run-1", role="worker", task="card:secretary-751"),
        )

        self.assertIn(shlex.quote("/tmp/weird dir/x.pid"), wrapped)

    def test_pid_file_path_is_keyed_on_kind_and_reference_only(self) -> None:
        """A respawn in the same workspace must land on the same path as the launch before it, so
        clearing the file before a fresh launch actually removes the predecessor's pid."""
        self.assertEqual(
            pid_file_path("worker", "secretary-751"),
            pid_file_path("worker", "secretary-751"),
        )
        self.assertNotEqual(
            pid_file_path("worker", "secretary-751"),
            pid_file_path("review", "secretary-751"),
        )

    def test_pid_file_path_honours_the_body_dir_override(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.dict(os.environ, {"SECRETARY_DISPATCHER_BODY_DIR": tmp}),
        ):
            self.assertTrue(pid_file_path("worker", "secretary-751").startswith(tmp))

    def test_a_process_that_has_exited_is_not_alive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "head.pid"
            proc = subprocess.Popen(["true"])
            self.write_heartbeat(pid_file, proc.pid)
            proc.wait()

            status = head_process_status(str(pid_file))

            self.assertEqual(status["state"], "dead")

    def test_a_running_process_is_alive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "head.pid"
            proc = subprocess.Popen(["sleep", "5"])
            self.addCleanup(proc.wait)
            self.addCleanup(proc.terminate)
            self.write_heartbeat(pid_file, proc.pid)

            status = head_process_status(str(pid_file))

            self.assertEqual(status["state"], "live-match")
            self.assertFalse(status["stopped"])

    def test_a_stopped_matching_process_is_live_but_marked_stopped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "head.pid"
            proc = subprocess.Popen(["sleep", "5"])
            self.addCleanup(proc.wait)
            self.addCleanup(proc.terminate)
            identity = heartbeat_identity(run_id="stopped-run", role="worker", task="card:secretary-751")
            self.write_heartbeat(pid_file, proc.pid, identity=identity)
            os.kill(proc.pid, signal.SIGSTOP)
            try:
                # SIGSTOP is asynchronous from this test process.  Wait for the kernel state so
                # the assertion does not race the scheduler, and always resume before cleanup:
                # a stopped process cannot act on the cleanup SIGTERM.
                status = {}
                for _ in range(50):
                    status = head_process_status(str(pid_file), expected=identity)
                    if status.get("stopped"):
                        break
                    time.sleep(0.01)
                self.assertEqual(status["state"], "live-match")
                self.assertTrue(status["stopped"])
            finally:
                os.kill(proc.pid, signal.SIGCONT)

    def test_a_live_process_with_a_stale_start_or_run_is_an_identity_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "head.pid"
            proc = subprocess.Popen(["sleep", "5"])
            self.addCleanup(proc.wait)
            self.addCleanup(proc.terminate)
            identity = heartbeat_identity(
                run_id="run-a", role="worker", task="card:secretary-751", leaf="leaf-a"
            )
            self.write_heartbeat(pid_file, proc.pid, identity=identity)
            raw = json.loads(pid_file.read_text(encoding="utf-8"))
            raw["proc_starttime_ticks"] = "0"
            pid_file.write_text(json.dumps(raw), encoding="utf-8")

            stale = head_process_status(str(pid_file), expected=identity)
            self.write_heartbeat(pid_file, proc.pid, identity=identity)
            foreign_run = head_process_status(
                str(pid_file),
                expected=heartbeat_identity(
                    run_id="run-b", role="worker", task="card:secretary-751", leaf="leaf-a"
                ),
            )

            self.assertEqual(stale["state"], "identity-mismatch")
            self.assertEqual(foreign_run["state"], "identity-mismatch")

    def test_the_pane_leaf_is_bound_by_a_second_atomic_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "head.pid"
            proc = subprocess.Popen(["sleep", "5"])
            self.addCleanup(proc.wait)
            self.addCleanup(proc.terminate)
            identity = heartbeat_identity(run_id="bind-run", role="worker", task="card:secretary-751")
            self.write_heartbeat(pid_file, proc.pid, identity=identity)

            self.assertTrue(bind_head_heartbeat(str(pid_file), expected=identity, leaf="leaf-a"))
            bound = head_process_status(str(pid_file), expected={**identity, "leaf": "leaf-a"})

            self.assertEqual(bound["state"], "live-match")
            self.assertEqual(bound["record"]["leaf"], "leaf-a")

    def test_a_leaf_handoff_before_the_writer_binds_each_dispatcher_role(self) -> None:
        """Terminal create may return before the shell reaches the heartbeat preamble.

        Worker, reviewer and observer share the writer, but their HeadRun bindings differ.  The
        handoff must make the first durable base record carry the returned leaf for all three.
        """
        roles = (
            ("worker", "card:secretary-1424", "leaf-worker"),
            ("reviewer", "card:secretary-1424", "leaf-reviewer"),
            ("observer", "sprint:secretary-1424", "leaf-observer"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            for role, task, leaf in roles:
                with self.subTest(role=role):
                    pid_file = Path(tmp) / f"{role}.pid"
                    identity = heartbeat_identity(run_id=f"{role}-race", role=role, task=task)
                    # This is the create-return / writer-not-yet-observable ordering.  The bind
                    # cannot see a base record, but leaves a durable handoff for the shell.
                    self.assertTrue(bind_head_heartbeat(str(pid_file), expected=identity, leaf=leaf))
                    wrapped = with_pid_heartbeat(
                        "python3 -c 'import time; time.sleep(5)'",
                        str(pid_file),
                        identity=identity,
                    )
                    proc = subprocess.Popen(["/bin/sh", "-lc", wrapped])
                    try:
                        deadline = time.monotonic() + 2
                        status: dict[str, object] = {}
                        while time.monotonic() < deadline:
                            status = head_process_status(str(pid_file), expected={**identity, "leaf": leaf})
                            if status.get("state") == "live-match":
                                break
                            time.sleep(0.01)
                        self.assertEqual(status.get("state"), "live-match")
                        self.assertEqual(status["record"]["leaf"], leaf)  # type: ignore[index]
                    finally:
                        proc.terminate()
                        proc.wait(timeout=5)

    def test_a_pid_file_that_has_not_been_written_yet_is_not_known(self) -> None:
        """A fresh launch has not run its `echo $$` yet, and a raw
        `SECRETARY_DISPATCHER_*_COMMAND` override never will. Neither is evidence of death."""
        with tempfile.TemporaryDirectory() as tmp:
            status = head_process_status(str(Path(tmp) / "never-written.pid"))

        self.assertEqual(status["state"], "not-yet-written")

    def test_garbage_pid_file_contents_are_not_known(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "head.pid"
            pid_file.write_text("not-a-pid\n", encoding="utf-8")

            status = head_process_status(str(pid_file))

        self.assertEqual(status["state"], "unreadable")


class NudgingReviewHost(RecordingReviewHost):
    """A bring-up whose pane answers reads, so its launch delivery runs end to end.

    The screen is what the confirmation criterion falls back to when no provider session file
    names this workspace, which is every test workspace: a codex pane painting `working` above its
    composer marker is a head that took its turn.

    Either role's launch runs through it. Both are nudged at a task document — the reviewer at its
    review, the worker at the TASK.md in its checkout — and the rule under test is the same rule.
    """

    def __init__(self, root: Path, *, screen: str = "working\n› ", **kwargs) -> None:
        super().__init__(root, catalog=PromptAfterStartCatalog(), **kwargs)
        self.screen = screen

    def _run_json(self, args: list[str]) -> dict:
        if args[:3] == ["orca", "terminal", "read"]:
            self.calls.append(args)
            return {"terminal": {"tail": self.screen.splitlines(), "nextCursor": "1"}}
        return super()._run_json(args)

    def sends(self) -> list[str]:
        return [
            call[call.index("--text") + 1] for call in self.calls if call[:3] == ["orca", "terminal", "send"]
        ]

    def closed_panes(self) -> list[str]:
        return [
            call[call.index("--terminal") + 1]
            for call in self.calls
            if call[:3] == ["orca", "terminal", "close"]
        ]


class ReviewNudgeDeliveryTests(unittest.TestCase):
    """secretary-1409: the reviewer is nudged at a task document, never handed the review itself.

    A ~12 KiB review typed into a Codex pane is what produced 24 consecutive
    `payload-left-in-composer` failures on `codegen-orchestrator-1165` and stopped two products.
    The rule that replaces it: the input channel carries only bounded pointers, content lives in a
    file, and a delivery classification never decides the fate of a pane.
    """

    # An ESC, a bracketed-paste terminator and the CRLF the board's web form submits — all of it
    # arriving the way it really does, inside the card description the review prompt renders.
    HOSTILE_DESCRIPTION = "spec\r\n\x1b[201~ terminator\r\n\x1b[200~ opener\r\n\x1b]0;retitle\x07\r\n"

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name)
        self.workspace = self.root / "ws"
        self.workspace.mkdir()
        _clear_env(self, "SECRETARY_DISPATCHER_REVIEW_COMMAND")
        _clear_env(self, "SECRETARY_DISPATCHER_PROMPT_DIR")
        os.environ["SECRETARY_DISPATCHER_BODY_DIR"] = str(self.root)
        # No provider session file may name this workspace, so the confirmation falls back to the
        # screen the host paints rather than reading the developer's own codex sessions.
        os.environ["SECRETARY_CODEX_SESSIONS"] = str(self.root / "sessions")
        self.addCleanup(os.environ.pop, "SECRETARY_CODEX_SESSIONS", None)
        self.task = {
            "ref": "secretary-1409",
            "project": "secretary",
            "description": self.HOSTILE_DESCRIPTION,
            "workspace": {"base_branch": "main"},
            "routing": {},
        }

    def _record(self) -> DispatcherRecord:
        return DispatcherRecord(
            worker="secretary-1409-w",
            workspace=str(self.workspace),
            handle="term-worker",
            head="codex",
            review_head="codex-reviewer",
            attempt_id="attempt-1",
            comment_baseline=0,
            review_baseline=0,
            state="review_starting",
            claimed_at=0.0,
            review_commit="c" * 40,
            review_base_sha="b" * 40,
        )

    def _bounded_delivery(self):
        return mock.patch.multiple(
            "triggered_agents.runtime.tui_delivery",
            TUI_DELIVERY_TIMEOUT_S=0.05,
            TUI_DELIVERY_POLL_S=0.01,
            TUI_DELIVERY_RESEND_GRACE_S=0,
        )

    def _document_of(self, host: NudgingReviewHost) -> Path:
        return host._prompt_document_path("review", self.task["ref"], 0)

    def _checkout_contents(self) -> dict[str, bytes]:
        return {
            str(path.relative_to(self.workspace)): path.read_bytes()
            for path in sorted(self.workspace.rglob("*"))
            if path.is_file()
        }

    def test_the_pane_receives_a_bounded_pointer_and_never_the_review(self) -> None:
        host = NudgingReviewHost(self.root)

        with self._bounded_delivery():
            host.start_review(self.task, self._record())

        document = self._document_of(host)
        body = next(text for text in host.sends() if text)
        # The transport's bracketed-paste frame is the only escape in the write; what it wraps is
        # the nudge, and that is the thing the ceiling and the one-line rule are about.
        self.assertTrue(body.startswith(BRACKETED_PASTE_START) and body.endswith(BRACKETED_PASTE_END))
        nudge = body[len(BRACKETED_PASTE_START) : -len(BRACKETED_PASTE_END)]
        self.assertLessEqual(len(nudge.encode("utf-8")), NUDGE_MAX_BYTES)
        self.assertEqual(nudge.splitlines(), [nudge], "the pane is given one line")
        self.assertNotIn("\x1b", nudge)
        self.assertIn(str(document), nudge)
        self.assertTrue(document.is_absolute())
        # The review itself never reaches a terminal write, hostile bytes included.
        written = "".join(host.sends())
        self.assertNotIn("\r", written)
        self.assertNotIn("BLOCKER-", written, "the review prompt's own text stayed on disk")
        self.assertNotIn("terminator", written)

    def test_the_document_holds_the_whole_review_outside_the_checkout(self) -> None:
        host = NudgingReviewHost(self.root)

        with self._bounded_delivery():
            host.start_review(self.task, self._record())

        document = self._document_of(host)
        body = document.read_text(encoding="utf-8")
        self.assertIn("# Review secretary-1409", body)
        self.assertIn("\x1b[201~ terminator", body, "the description reaches the head unmodified")
        self.assertNotIn(
            str(self.workspace.resolve()),
            str(document.resolve()),
            "a prompt inside the checkout would move the identity receipts hash",
        )
        self.assertEqual(oct(document.stat().st_mode & 0o777), oct(0o600))
        self.assertFalse(
            (self.workspace / "REVIEW.md").exists(),
            "the review packet is the document, and it does not live in the worktree",
        )

    def test_the_bring_up_does_not_touch_the_candidate_checkout(self) -> None:
        """Preparing a prompt is not a licence to edit the tree the reviewer is about to judge.

        A `REVIEW.md` in the workspace can be a tracked part of a candidate as easily as a packet
        left by a dispatcher that predates this seam, and the nudge names an absolute path, so
        nothing needs deleting to be unambiguous. Removing it would be the same identity change the
        document-outside-the-worktree rule exists to prevent, made by the code enforcing that rule.
        """
        (self.workspace / "REVIEW.md").write_text("a candidate's own file\n", encoding="utf-8")
        (self.workspace / "src.py").write_text("print('candidate')\n", encoding="utf-8")
        before = self._checkout_contents()
        host = NudgingReviewHost(self.root)

        with self._bounded_delivery():
            host.start_review(self.task, self._record())

        self.assertEqual(self._checkout_contents(), before)

    def test_a_retry_rewrites_the_same_document_and_sends_a_fresh_nudge(self) -> None:
        """The pointer always names the round's current task, so a retry cannot review a stale one."""
        host = NudgingReviewHost(self.root)
        with self._bounded_delivery():
            host.start_review(self.task, self._record())
        first = list(host.sends())

        self.task["description"] = "the card was edited between attempts"
        with self._bounded_delivery():
            host.start_review(self.task, self._record())

        document = self._document_of(host)
        self.assertIn("the card was edited between attempts", document.read_text(encoding="utf-8"))
        self.assertEqual(
            [text for text in host.sends() if text],
            [text for text in first if text] * 2,
            "the same path is nudged again rather than a second document being written",
        )
        self.assertEqual(sorted(path.name for path in document.parent.iterdir()), ["review-0.md"])

    def test_a_second_round_gets_its_own_document(self) -> None:
        host = NudgingReviewHost(self.root)
        record = self._record()
        record.review_baseline = 1

        with self._bounded_delivery():
            host.start_review(self.task, record)

        self.assertTrue(host._prompt_document_path("review", self.task["ref"], 1).is_file())
        self.assertFalse(self._document_of(host).exists())

    def test_an_unconfirmed_nudge_leaves_the_pane_open_for_the_next_tick(self) -> None:
        """The invariant: no pane is closed on the strength of a delivery classification.

        That classification called 24 delivered prompts failures on the canary, and closing the
        pane behind it killed a reviewer that had the task in hand. The bring-up hands the pane
        back instead, with what the boundary saw, and the launch intent settles it next tick.
        """
        host = NudgingReviewHost(self.root, screen="idle\n› ")

        with self._bounded_delivery(), self.assertRaises(HeadLaunchAborted) as caught:
            host.start_review(self.task, self._record())

        self.assertEqual(host.closed_panes(), [], "the reviewer pane survives an unconfirmed nudge")
        self.assertEqual(caught.exception.handle, "term-review")
        self.assertEqual(caught.exception.leaf, "leaf-review")
        evidence = caught.exception.evidence
        self.assertEqual(evidence["delivery_mode"], NUDGE_FILE_MODE)
        self.assertEqual(evidence["document_path"], str(self._document_of(host)))
        self.assertLessEqual(evidence["payload_bytes"], NUDGE_MAX_BYTES)
        self.assertTrue(evidence["submit_count"], "the submits are counted, the text is not kept")
        self.assertNotIn("terminator", json.dumps(evidence), "no prompt text in the telemetry")

    def test_a_busy_readiness_wait_keeps_the_live_reviewer_run_and_pane(self) -> None:
        """A 60s `tui-idle` timeout is evidence the reviewer pane is working, not absent."""
        host = NudgingReviewHost(self.root)
        host.wait_answer = HostError(
            "orca terminal wait --terminal term-review --for tui-idle --timeout-ms 60000 "
            'failed: {"error":{"code":"timeout","message":"timeout"}}'
        )

        with self.assertRaises(HeadLaunchAborted) as caught:
            host.start_review(self.task, self._record())

        evidence = caught.exception.evidence
        self.assertEqual(evidence["readiness_state"], "busy")
        self.assertEqual(evidence["reason"], "readiness-busy")
        self.assertEqual((caught.exception.handle, caught.exception.leaf), ("term-review", "leaf-review"))
        self.assertEqual(host.closed_panes(), [], "a busy reviewer is never closed or replaced")
        self.assertNotIn("close", host.ops(), "the worker remains owned until review is settled")

        # The later retry addresses the exact run and document nudge, rather than splitting a
        # replacement reviewer or moving into the worker-freeze adoption path first.
        intent = {
            "role": "review",
            "workspace": str(self.workspace),
            "handle": caught.exception.handle,
            "leaf": caught.exception.leaf,
            "pid_file": caught.exception.pid_file,
            "head_run": dict(caught.exception.head_run),
        }
        host.wait_answer = {"wait": {"condition": "tui-idle", "satisfied": True}}
        with self._bounded_delivery():
            retried = host.nudge_review_delivery(self.task, self._record(), intent)

        self.assertEqual(retried["head_run"]["run_id"], caught.exception.head_run["run_id"])
        self.assertEqual(retried["handle"], "term-review")
        self.assertTrue(retried["delivery_evidence"]["turn_confirmed"])
        self.assertEqual(host.closed_panes(), [], "retry never closes the retained reviewer pane")

    def test_a_document_that_cannot_be_written_stops_the_bring_up_before_any_pane(self) -> None:
        """An unprompted reviewer would sit at its prompt forever; the caller's infrastructure
        retry is the right answer to a launch that never started."""
        host = NudgingReviewHost(self.root)
        with (
            mock.patch.object(
                dispatcher_host_module,
                "_write_prompt_document",
                side_effect=PromptDocumentError("read-only artifacts directory"),
            ),
            self.assertRaises(HostError) as caught,
        ):
            host.start_review(self.task, self._record())

        self.assertIn("task document could not be prepared", str(caught.exception))
        self.assertEqual(host.ops(), [], "no pane is opened for a head with nothing to read")


class WorkerNudgeDeliveryTests(unittest.TestCase):
    """secretary-1410: the same invariant, on the bring-up that was still killing its own heads.

    The worker's launch prompt has always been a pointer at the TASK.md written into its checkout —
    the reviewer's rule applied to the other role — but the bring-up was never told so, and answered
    an unconfirmed delivery by closing the pane. On 2026-08-11 that closed six consecutive live
    Claude workers on `codegen-orchestrator-1166`, each twelve seconds after it had started, taken
    its prompt and begun work: the transcripts they left behind are the proof they were healthy.
    What made the classification wrong is fixed elsewhere in this card; what this class fixes is
    that a wrong classification could carry that verdict at all.
    """

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name)
        self.workspace = self.root / "ws"
        self.workspace.mkdir()
        _clear_env(self, "SECRETARY_DISPATCHER_WORKER_COMMAND")
        os.environ["SECRETARY_DISPATCHER_BODY_DIR"] = str(self.root)
        # Neither provider may have a session file naming this workspace, so the delivery falls
        # back to the screen the host paints instead of the developer's own transcripts.
        os.environ["SECRETARY_CODEX_SESSIONS"] = str(self.root / "sessions")
        os.environ["SECRETARY_CLAUDE_PROJECTS"] = str(self.root / "claude-projects")
        self.addCleanup(os.environ.pop, "SECRETARY_CODEX_SESSIONS", None)
        self.addCleanup(os.environ.pop, "SECRETARY_CLAUDE_PROJECTS", None)
        self.task = {
            "ref": "secretary-1410",
            "project": "secretary",
            "description": "a card with an \x1b[201~ terminator in it",
            "workspace": {"base_branch": "main"},
            "routing": {},
        }

    def _record(self) -> DispatcherRecord:
        return DispatcherRecord(
            worker="secretary-1410-w",
            workspace=str(self.workspace),
            handle="term-worker",
            head="claude-opus",
            review_head="codex-reviewer",
            attempt_id="attempt-1",
            comment_baseline=0,
            review_baseline=0,
            state="claimed",
            claimed_at=0.0,
        )

    def _bounded_delivery(self):
        return mock.patch.multiple(
            "triggered_agents.runtime.tui_delivery",
            TUI_DELIVERY_TIMEOUT_S=0.05,
            TUI_DELIVERY_POLL_S=0.01,
            TUI_DELIVERY_RESEND_GRACE_S=0,
        )

    def test_an_unconfirmed_worker_nudge_leaves_the_pane_and_the_intent(self) -> None:
        """Symmetric to `ReviewNudgeDeliveryTests`: no pane dies of a delivery classification.

        The pane is handed back inside `HeadLaunchAborted`, which is what keeps the caller's launch
        intent on disk; the next tick then adopts that head or stops it by its own retained
        identity, with the cleanup recorded as the initiator.
        """
        host = NudgingReviewHost(self.root, screen="idle\n› ")

        with self._bounded_delivery(), self.assertRaises(HeadLaunchAborted) as caught:
            host.restart_worker(self.task, self._record())

        self.assertEqual(host.closed_panes(), [], "the worker pane survives an unconfirmed nudge")
        self.assertEqual(caught.exception.handle, "term-created")
        self.assertEqual(caught.exception.workspace, str(self.workspace))
        evidence = caught.exception.evidence
        self.assertEqual(evidence["delivery_mode"], NUDGE_FILE_MODE)
        self.assertEqual(evidence["document_path"], str(self.workspace / "TASK.md"))
        self.assertLessEqual(evidence["payload_bytes"], NUDGE_MAX_BYTES)
        self.assertTrue(evidence["submit_count"], "the submits are counted, the text is not kept")
        self.assertNotIn("terminator", json.dumps(evidence), "no prompt text in the telemetry")

    def test_the_task_the_head_was_pointed_at_is_on_disk_whatever_the_classification_said(
        self,
    ) -> None:
        """Why not closing it is safe: the pointer named a file, and the file is there.

        A head that took the nudge has its whole task; a head that did not can be nudged again at
        the same path next tick. Nothing about the round depends on the pane having answered.
        """
        host = NudgingReviewHost(self.root, screen="idle\n› ")

        with self._bounded_delivery(), self.assertRaises(HeadLaunchAborted):
            host.restart_worker(self.task, self._record())

        body = (self.workspace / "TASK.md").read_text(encoding="utf-8")
        self.assertIn("secretary-1410", body)
        self.assertIn("\x1b[201~ terminator", body, "the card reaches the head unmodified")
        nudge = next(text for text in host.sends() if text)
        self.assertNotIn("terminator", nudge, "the pane got the pointer, not the card")

    def test_a_confirmed_worker_nudge_reports_the_document_it_pointed_at(self) -> None:
        host = NudgingReviewHost(self.root, screen="working\n› ")

        with self._bounded_delivery():
            launched = host.restart_worker(self.task, self._record())

        self.assertEqual(host.closed_panes(), [])
        self.assertEqual(launched.delivery_evidence["document_path"], str(self.workspace / "TASK.md"))
        self.assertEqual(launched.delivery_evidence["delivery_mode"], NUDGE_FILE_MODE)


class WorkerLifecycleTests(unittest.TestCase):
    """secretary-1412: the production worker path runs on `spawn` / `nudge` / `stop`.

    The three operations own the head's life now, and the dispatcher's job is what only it can do:
    render the command, confirm a provider turn, prove a process is gone, and say who is ending a
    head. What is asserted here is that the worker really does travel through them — one run
    identity from bring-up to stop, a pane re-found by its leaf rather than by a handle Orca may
    have aliased, and an initiator that is on the record afterwards and survives being written down.
    """

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name)
        self.workspace = self.root / "ws"
        self.workspace.mkdir()
        _clear_env(self, "SECRETARY_DISPATCHER_WORKER_COMMAND")
        os.environ["SECRETARY_DISPATCHER_BODY_DIR"] = str(self.root)
        os.environ["SECRETARY_CODEX_SESSIONS"] = str(self.root / "sessions")
        os.environ["SECRETARY_CLAUDE_PROJECTS"] = str(self.root / "claude-projects")
        self.addCleanup(os.environ.pop, "SECRETARY_CODEX_SESSIONS", None)
        self.addCleanup(os.environ.pop, "SECRETARY_CLAUDE_PROJECTS", None)
        self.task = {
            "ref": "secretary-1412",
            "project": "secretary",
            "description": "a card",
            "workspace": {"base_branch": "main"},
            "routing": {},
        }

    def _record(self, **kwargs) -> DispatcherRecord:
        record = DispatcherRecord(
            worker="secretary-1412-w",
            workspace=str(self.workspace),
            handle="term-worker",
            head="codex",
            review_head="codex-reviewer",
            attempt_id="attempt-1",
            comment_baseline=0,
            review_baseline=0,
            state="claimed",
            claimed_at=0.0,
        )
        for name, value in kwargs.items():
            setattr(record, name, value)
        return record

    def _bounded_delivery(self):
        return mock.patch.multiple(
            "triggered_agents.runtime.tui_delivery",
            TUI_DELIVERY_TIMEOUT_S=0.05,
            TUI_DELIVERY_POLL_S=0.01,
            TUI_DELIVERY_RESEND_GRACE_S=0,
        )

    def test_a_worker_bring_up_hands_back_the_run_that_head_is(self) -> None:
        host = NudgingReviewHost(self.root, screen="working\n› ")

        with self._bounded_delivery():
            launched = host.restart_worker(self.task, self._record())

        run = launched.head_run
        self.assertTrue(run["run_id"], "the head has an identity of its own")
        self.assertEqual(run["lifecycle"], "working", "it was given its task")
        self.assertEqual(run["handle"], launched.handle)
        self.assertEqual(
            run["task_ref"],
            {
                "kind": "card",
                "ref": "secretary-1412",
                "document": str(self.workspace / "TASK.md"),
            },
        )
        self.assertEqual(run["spec"]["adapter"], "codex")

    def test_the_worker_report_prompt_goes_to_the_pane_the_leaf_names_now(self) -> None:
        """The reincarnation case, on the production nudge: the handle moved, the head did not."""
        host = NudgingReviewHost(self.root, screen="working\n› ")
        host.terminals = [{"handle": "term-alias", "leafId": "leaf-worker", "connected": True}]
        record = self._record(
            worker_leaf="leaf-worker",
            report_generation=2,
            worker_pid_file=str(self.root / "w.pid"),
            worker_run={"adapter": "codex", "codex_mode": "tui"},
        )
        # The heartbeat of a worker that is running: a report prompt is refused over any other.
        record.worker_head_run = head_ops.HeadRun(
            run_id="worker-report-prompt-run",
            spec=head_ops.HeadSpec(profile_id="codex", adapter="codex"),
            workspace=str(self.workspace),
            task_ref=head_ops.TaskRef.card(self.task["ref"]),
            handle=record.handle,
            leaf=record.worker_leaf,
            pid_file=record.worker_pid_file,
        ).to_json()
        PidHeartbeatTests.write_heartbeat(
            Path(record.worker_pid_file),
            os.getpid(),
            identity=run_heartbeat_identity(record.worker_head_run, role="worker"),
        )
        (self.workspace / "TASK.md").write_text("task\n", encoding="utf-8")

        with self._bounded_delivery():
            host.prompt_worker_report(self.task, record)

        sent = [call for call in host.calls if call[:3] == ["orca", "terminal", "send"]]
        self.assertTrue(sent, "the prompt was delivered")
        for call in sent:
            self.assertEqual(call[call.index("--terminal") + 1], "term-alias")
        self.assertEqual(record.worker_head_run["handle"], "term-alias")
        self.assertEqual(record.worker_head_run["lifecycle"], "working")

    def test_a_busy_continuation_wait_does_not_signal_the_retained_worker(self) -> None:
        """The signal is inside the shared delivery path, after its readiness wait."""
        host = NudgingReviewHost(self.root)
        host.wait_answer = HostError(
            'orca terminal wait --for tui-idle --timeout-ms 60000 failed: {"error":{"code":"timeout"}}'
        )
        pid_file = self.root / "retained.pid"
        record = self._record(
            worker_leaf="leaf-worker",
            worker_pid_file=str(pid_file),
            worker_run={"adapter": "codex", "codex_mode": "tui"},
            report_generation=2,
            worker_continuation=WorkerContinuation(
                stage=WorkerContinuationStage.DELIVERY_PENDING,
                phase="review",
                session_held=True,
                sent_at=time.time(),
            ),
        )
        record.worker_head_run = head_ops.HeadRun(
            run_id="retained-busy-run",
            spec=head_ops.HeadSpec(profile_id="codex", adapter="codex"),
            workspace=str(self.workspace),
            task_ref=head_ops.TaskRef.card(self.task["ref"]),
            handle=record.handle,
            leaf=record.worker_leaf,
            pid_file=str(pid_file),
        ).to_json()

        with (
            mock.patch.object(
                host,
                "_head_status",
                return_value={
                    "known": True,
                    "alive": True,
                    "match": True,
                    "state": "live-match",
                    "stopped": True,
                },
            ),
            mock.patch.object(host, "_signal_head") as signal_head,
            self.assertRaises(HostError) as raised,
        ):
            host.resume_worker(self.task, record)

        evidence = raised.exception.evidence
        self.assertEqual(evidence.readiness_state, "busy")
        self.assertEqual(evidence.reason, "readiness-busy")
        signal_head.assert_not_called()
        self.assertEqual(host.sends(), [])
        self.assertEqual(record.worker_head_run["run_id"], "retained-busy-run")

    def test_a_stopped_worker_records_who_stopped_it_and_that_survives_a_restart(self) -> None:
        host = NudgingReviewHost(self.root)
        record = self._record(worker_leaf="leaf-worker")

        host.stop_head(record, "worker", STOPPED_BY_REVIEW_FREEZE)

        self.assertEqual(record.worker_head_run["lifecycle"], "exited")
        self.assertEqual(record.worker_head_run["stopped_by"]["actor"], STOPPED_BY_REVIEW_FREEZE)
        restarted = DispatcherRecord.from_json(json.loads(json.dumps(record.to_json())))
        self.assertEqual(restarted.worker_head_run["stopped_by"]["actor"], STOPPED_BY_REVIEW_FREEZE)

    def test_a_stop_that_is_refused_still_names_its_initiator(self) -> None:
        """The dispatcher may die between the two; the record must not lose who was ending this."""
        host = NudgingReviewHost(self.root, fail_ops={"close"})
        record = self._record(worker_leaf="leaf-worker", worker_pid_file=str(self.root / "w.pid"))
        Path(record.worker_pid_file).write_text(f"{os.getpid()}\n", encoding="utf-8")

        with (
            mock.patch.object(dispatcher_host_module, "HEAD_STOP_GRACE_SECONDS", 0.05),
            mock.patch.object(host, "_signal_head", lambda *a: None),
            self.assertRaises(HostError),
        ):
            host.stop_head(record, "worker", STOPPED_BY_OPERATOR)

        self.assertEqual(record.worker_head_run["lifecycle"], "finishing")
        self.assertEqual(record.worker_head_run["stopped_by"]["actor"], STOPPED_BY_OPERATOR)

    def test_a_live_foreign_worker_heartbeat_fences_the_pane_before_close_or_signal(self) -> None:
        host = RecordingReviewHost(self.root)
        pid_file = self.root / "foreign-worker.pid"
        record = self._record(worker_leaf="leaf-worker", worker_pid_file=str(pid_file))
        record.worker_head_run = head_ops.HeadRun(
            run_id="worker-owned-run",
            spec=head_ops.HeadSpec(profile_id="codex", adapter="codex"),
            workspace=str(self.workspace),
            task_ref=head_ops.TaskRef.card(self.task["ref"]),
            handle=record.handle,
            leaf=record.worker_leaf,
            pid_file=str(pid_file),
        ).to_json()
        stored_run = json.loads(json.dumps(record.worker_head_run))
        foreign = subprocess.Popen(["sleep", "5"])

        def reap_foreign() -> None:
            if foreign.poll() is None:
                foreign.terminate()
            foreign.wait()

        self.addCleanup(reap_foreign)
        PidHeartbeatTests.write_heartbeat(
            pid_file,
            foreign.pid,
            identity=heartbeat_identity(
                run_id="foreign-worker-run",
                role="worker",
                task=f"card:{self.task['ref']}",
                leaf=record.worker_leaf,
            ),
        )

        with (
            mock.patch.object(host, "_signal_head") as signal_head,
            self.assertRaisesRegex(HostError, "mismatching launch identity"),
        ):
            host.stop_head(record, "worker", STOPPED_BY_OPERATOR)

        self.assertNotIn("list", host.ops(), "the leaf is not looked up after a mismatch")
        self.assertNotIn("close", host.ops())
        signal_head.assert_not_called()
        self.assertIsNone(foreign.poll())
        self.assertEqual(record.worker_head_run, stored_run, "a foreign process is never attributed")

    def test_the_run_identity_is_the_same_one_from_bring_up_to_stop(self) -> None:
        host = NudgingReviewHost(self.root, screen="working\n› ")
        record = self._record()

        with self._bounded_delivery():
            launched = host.restart_worker(self.task, record)
        record.worker_head_run = dict(launched.head_run)
        record.handle = launched.handle
        record.worker_leaf = launched.leaf

        host.stop_head(record, "worker", STOPPED_BY_REPLACEMENT)

        self.assertEqual(record.worker_head_run["run_id"], launched.head_run["run_id"])
        self.assertEqual(record.worker_head_run["lifecycle"], "exited")


class ReviewerLifecycleTests(unittest.TestCase):
    """secretary-1414: the reviewer path runs on `spawn` / `nudge` / `stop`, like the worker's.

    The reviewer is the head this dispatcher stops from the most places, and until it had a durable
    run of its own every one of those stops left the same record behind: a reviewer that was simply
    gone. What is asserted here is the run — one identity from bring-up to stop, re-addressed by its
    leaf rather than by a handle Orca may have aliased, an initiator written down before the pane is
    touched and still there when the stop is refused — and the one thing the reviewer's stop must
    keep doing differently: closing its own split leaf and nothing else in the worker's worktree.
    """

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name)
        self.workspace = self.root / "ws"
        self.workspace.mkdir()
        _clear_env(self, "SECRETARY_DISPATCHER_REVIEW_COMMAND")
        _clear_env(self, "SECRETARY_DISPATCHER_PROMPT_DIR")
        os.environ["SECRETARY_DISPATCHER_BODY_DIR"] = str(self.root)
        os.environ["SECRETARY_CODEX_SESSIONS"] = str(self.root / "sessions")
        self.addCleanup(os.environ.pop, "SECRETARY_CODEX_SESSIONS", None)
        self.task = {
            "ref": "secretary-1414",
            "project": "secretary",
            "description": "a card",
            "workspace": {"base_branch": "main"},
            "routing": {},
        }

    def _record(self, **fields) -> DispatcherRecord:
        record = DispatcherRecord(
            worker="secretary-1414-w",
            workspace=str(self.workspace),
            handle="term-worker",
            head="codex",
            review_head="codex-reviewer",
            attempt_id="attempt-1",
            comment_baseline=0,
            review_baseline=0,
            state="reviewing",
            claimed_at=0.0,
            review_commit="c" * 40,
            review_base_sha="b" * 40,
        )
        for name, value in fields.items():
            setattr(record, name, value)
        return record

    def _stored_run(self, **fields) -> dict:
        """A reviewer run as a previous tick wrote it down, before this one reads it back."""
        run = {
            "run_id": "run-reviewer-1",
            "spec": {"profile_id": "codex-reviewer", "adapter": "codex"},
            "workspace": str(self.workspace),
            "task_ref": {"kind": "card", "ref": "secretary-1414", "document": ""},
            "handle": "term-review-create",
            "leaf": "leaf-review",
            "pid_file": "",
            "lifecycle": "working",
            "stopped_by": {},
        }
        run.update(fields)
        return run

    def _bounded_delivery(self):
        return mock.patch.multiple(
            "triggered_agents.runtime.tui_delivery",
            TUI_DELIVERY_TIMEOUT_S=0.05,
            TUI_DELIVERY_POLL_S=0.01,
            TUI_DELIVERY_RESEND_GRACE_S=0,
        )

    def test_a_reviewer_bring_up_hands_back_the_run_that_head_is(self) -> None:
        host = NudgingReviewHost(self.root, screen="working\n› ")

        with self._bounded_delivery():
            launch = host.start_review(self.task, self._record())

        run = launch.head_run
        self.assertTrue(run["run_id"], "the reviewer has an identity of its own")
        self.assertEqual(run["lifecycle"], "working", "it was given its review")
        self.assertEqual(run["handle"], launch.handle)
        self.assertEqual(run["leaf"], launch.leaf)
        self.assertEqual(run["spec"]["profile_id"], "codex-reviewer")
        self.assertEqual(run["task_ref"]["ref"], "secretary-1414")

    def test_the_reviewer_stop_addresses_the_head_its_leaf_names_now(self) -> None:
        """The reincarnation case: the leaf is where it was, the handle now names another pane.

        Closing the recorded handle here would close a stranger's pane and leave the reviewer
        running, which is the failure the run's stable leaf exists to prevent.
        """
        host = RecordingReviewHost(
            self.root,
            terminals=[
                # Orca handed the reviewer's create-time handle to a different pty.
                {"handle": "term-review-create", "leafId": "leaf-stranger", "connected": True},
                {"handle": "term-review-alias", "leafId": "leaf-review", "connected": True},
            ],
        )
        record = self._record(
            handle="",
            review_handle="term-review-create",
            review_leaf="leaf-review",
            review_head_run=self._stored_run(),
        )

        host.stop_review(record, STOPPED_BY_REVIEW_VERDICT)

        closed = [
            call[call.index("--terminal") + 1]
            for call in host.calls
            if call[:3] == ["orca", "terminal", "close"]
        ]
        self.assertEqual(closed, ["term-review-alias"], "the stop addressed the head, not a pane")
        self.assertEqual(record.review_head_run["run_id"], "run-reviewer-1", "same head, readdressed")
        self.assertEqual(record.review_head_run["lifecycle"], "exited")

    def test_a_stopped_reviewer_records_who_stopped_it_and_that_survives_a_restart(self) -> None:
        host = RecordingReviewHost(self.root)
        record = self._record(review_handle="term-review", review_head_run=self._stored_run(leaf=""))

        host.stop_review(record, STOPPED_BY_REVIEW_VERDICT)

        self.assertEqual(record.review_head_run["lifecycle"], "exited")
        self.assertEqual(record.review_head_run["stopped_by"]["actor"], STOPPED_BY_REVIEW_VERDICT)
        restarted = DispatcherRecord.from_json(json.loads(json.dumps(record.to_json())))
        self.assertEqual(restarted.review_head_run["stopped_by"]["actor"], STOPPED_BY_REVIEW_VERDICT)

    def test_a_refused_reviewer_stop_is_continued_rather_than_begun_again(self) -> None:
        """The stop the dispatcher could not finish: `commit` runs before the pane is touched, so
        the record is in `finishing` with its initiator, and the next tick continues that stop."""
        host = RecordingReviewHost(self.root, fail_ops={"close"})
        record = self._record(
            handle="",
            review_handle="term-review",
            review_pid_file=str(self.root / "review.pid"),
            review_head_run=self._stored_run(leaf="", pid_file=str(self.root / "review.pid")),
        )
        # A reviewer whose heartbeat still answers: the close was refused and nothing here can say
        # the head is gone, which is the stop that has to survive to the next tick.
        Path(record.review_pid_file).write_text(f"{os.getpid()}\n", encoding="utf-8")

        with (
            mock.patch.object(dispatcher_host_module, "HEAD_STOP_GRACE_SECONDS", 0.05),
            mock.patch.object(host, "_signal_head", lambda *a: None),
            self.assertRaises(HostError),
        ):
            host.stop_review(record, STOPPED_BY_WATCHDOG)

        self.assertEqual(record.review_head_run["lifecycle"], "finishing")
        self.assertEqual(record.review_head_run["stopped_by"]["actor"], STOPPED_BY_WATCHDOG)

        # The next tick, through another path with another actor. It continues this stop: same
        # run, and the actor that began it is the one the record keeps.
        host.fail_ops = set()
        record.review_pid_file = ""
        record.review_head_run = json.loads(json.dumps(record.review_head_run))
        host.stop_review(record, STOPPED_BY_RECONCILIATION)

        self.assertEqual(record.review_head_run["run_id"], "run-reviewer-1")
        self.assertEqual(record.review_head_run["lifecycle"], "exited")
        self.assertEqual(record.review_head_run["stopped_by"]["actor"], STOPPED_BY_WATCHDOG)

    def test_a_live_foreign_reviewer_heartbeat_fences_the_pane_before_close_or_signal(self) -> None:
        host = RecordingReviewHost(self.root)
        pid_file = self.root / "foreign-reviewer.pid"
        record = self._record(
            review_handle="term-review",
            review_leaf="leaf-review",
            review_pid_file=str(pid_file),
            review_head_run=self._stored_run(
                handle="term-review",
                leaf="leaf-review",
                pid_file=str(pid_file),
            ),
        )
        stored_run = json.loads(json.dumps(record.review_head_run))
        foreign = subprocess.Popen(["sleep", "5"])

        def reap_foreign() -> None:
            if foreign.poll() is None:
                foreign.terminate()
            foreign.wait()

        self.addCleanup(reap_foreign)
        PidHeartbeatTests.write_heartbeat(
            pid_file,
            foreign.pid,
            identity=heartbeat_identity(
                run_id="foreign-reviewer-run",
                role="reviewer",
                task=f"card:{self.task['ref']}",
                leaf=record.review_leaf,
            ),
        )

        with (
            mock.patch.object(host, "_signal_head") as signal_head,
            self.assertRaisesRegex(HostError, "mismatching launch identity"),
        ):
            host.stop_review(record, STOPPED_BY_WATCHDOG)

        self.assertNotIn("list", host.ops(), "the leaf is not looked up after a mismatch")
        self.assertNotIn("close", host.ops())
        signal_head.assert_not_called()
        self.assertIsNone(foreign.poll())
        self.assertEqual(record.review_head_run, stored_run, "a foreign process is never attributed")

    def test_stopping_the_reviewer_leaves_the_workers_checkout_alone(self) -> None:
        """The split-leaf semantics, which this card moves onto the operations without changing.

        A red verdict hands the worktree back to the worker moments later. A reviewer stop that
        reached for the workspace would take the checkout's own terminals down with it, and the
        worker the card is about to resume would be the head that lost them.
        """
        host = RecordingReviewHost(
            self.root,
            terminals=[
                {"handle": "term-worker", "leafId": "leaf-worker", "connected": True},
                {"handle": "term-review", "leafId": "leaf-review", "connected": True},
            ],
        )
        record = self._record(
            handle="term-worker",
            worker_leaf="leaf-worker",
            review_handle="term-review",
            review_leaf="leaf-review",
            review_head_run=self._stored_run(handle="term-review"),
        )

        host.stop_review(record, STOPPED_BY_REVIEW_VERDICT)

        closed = [
            call[call.index("--terminal") + 1]
            for call in host.calls
            if call[:3] == ["orca", "terminal", "close"]
        ]
        self.assertEqual(closed, ["term-review"], "only the reviewer's own pane was closed")
        self.assertNotIn("stop", host.ops(), "the worker's worktree was never stopped")
        self.assertEqual(record.worker_head_run, {}, "the worker's own run was not touched")


class ScriptedWaitHost(CommandHostRuntime):
    """CommandHostRuntime whose Orca answers each `terminal wait` from a script.

    The first answer is the delivery's own wait for the pane; the second is the readiness question
    the bring-up asks about the pane it is about to close. An entry that is an exception is raised,
    which is how the real CLI reports a condition it could not satisfy.
    """

    def __init__(self, root: Path, *, waits: list) -> None:
        super().__init__(PromptAfterStartCatalog(), root, mode="real")  # type: ignore[arg-type]
        self.preflight_codex_run = accepted_transport_run  # type: ignore[method-assign]
        self.waits = list(waits)
        self.ops: list[str] = []
        self.closed: list[str] = []

    def _run_json(self, args: list[str]) -> dict:
        op = args[2] if args[:2] == ["orca", "terminal"] else ""
        self.ops.append(op)
        if op == "wait":
            answer = self.waits.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer
        if op == "create":
            return {"terminal": {"handle": "term-head"}}
        if op == "close":
            self.closed.append(args[args.index("--terminal") + 1])
        if op == "list":
            return {"terminals": []}
        return {}

    def _run(self, args: list[str], label: str, *, cwd: Path | None = None):
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")


class LaunchPaneReadinessTests(unittest.TestCase):
    """secretary-1163: a bring-up classifies the pane that would not take its launch prompt.

    Orca answers readiness in three states and the bring-up path used none of them: every refused
    delivery came back as one undifferentiated failure, and the card went to Blocked for a codex
    update dialog that would have been gone a minute later.
    """

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name)
        self.workspace = self.root / "ws"
        self.workspace.mkdir()
        _clear_env(self, "SECRETARY_DISPATCHER_WORKER_COMMAND")
        os.environ["SECRETARY_DISPATCHER_BODY_DIR"] = str(self.root)
        self.task = {
            "ref": "secretary-1163",
            "project": "secretary",
            "description": "spec",
            "workspace": {"base_branch": "main"},
            "routing": {},
        }

    def _launch(self, host: ScriptedWaitHost):
        return host._launch(
            str(self.workspace),
            "secretary-1163 worker",
            "codex",
            "TASK.md",
            role="worker",
            env_name="SECRETARY_DISPATCHER_WORKER_COMMAND",
            launch_prompt="go",
            task=self.task,
        )

    def _refused(self, body: dict) -> HostError:
        """A `terminal wait` the CLI exited non-zero on, carrying the body Orca printed with it."""
        return HostError(
            "orca terminal wait --terminal term-head --for tui-idle --timeout-ms 60000 failed: "
            + json.dumps(body)
        )

    def test_a_pane_held_in_a_dialog_is_a_deferrable_failure(self) -> None:
        """The canary's own failure: codex came up behind its update prompt, so the launch prompt
        went nowhere and the pane never reached idle."""
        blocked = {
            "wait": {
                "condition": "tui-idle",
                "satisfied": False,
                "status": "running",
                "blockedReason": "codex-update-prompt",
            }
        }
        host = ScriptedWaitHost(self.root, waits=[self._refused(blocked), blocked])

        with self.assertRaises(HeadPaneNotReady) as caught:
            self._launch(host)

        self.assertEqual(caught.exception.readiness, "blocked")
        self.assertEqual(caught.exception.pane, "term-head")
        self.assertIn("held in a dialog", str(caught.exception))
        self.assertIn("codex-update-prompt", str(caught.exception))
        self.assertEqual(host.closed, ["term-head"], "a deferred launch leaves no pane behind")

    def test_a_working_pane_is_a_deferrable_failure(self) -> None:
        busy = {"wait": {"condition": "tui-idle", "satisfied": False, "status": "running"}}
        host = ScriptedWaitHost(self.root, waits=[self._refused(busy), busy])

        with self.assertRaises(HeadPaneNotReady) as caught:
            self._launch(host)

        self.assertEqual(caught.exception.readiness, "busy")
        self.assertIn("busy", str(caught.exception))

    def test_a_pane_that_cannot_be_probed_stays_an_ordinary_failure(self) -> None:
        """A probe nobody answers is not a busy pane. Deferring on it would park the card on a
        readiness that can never arrive, so it keeps the failure path it always had."""
        host = ScriptedWaitHost(
            self.root,
            waits=[
                HostError("orca terminal wait failed: connection refused"),
                HostError("orca terminal wait failed: connection refused"),
            ],
        )

        with self.assertRaises(HostError) as caught:
            self._launch(host)

        self.assertNotIsInstance(caught.exception, HeadPaneNotReady)
        self.assertEqual(host.closed, ["term-head"])

    def test_a_pane_that_went_ready_after_the_failure_stays_an_ordinary_failure(self) -> None:
        """The delivery failed and the pane is idle: nothing is holding it, so there is nothing to
        wait for and the failure is about the delivery itself."""
        host = ScriptedWaitHost(
            self.root,
            waits=[
                self._refused({"wait": {"condition": "tui-idle", "satisfied": False}}),
                {"wait": {"condition": "tui-idle", "satisfied": True}},
            ],
        )

        with self.assertRaises(HostError) as caught:
            self._launch(host)

        self.assertNotIsInstance(caught.exception, HeadPaneNotReady)

    def test_a_pane_that_will_not_close_still_outranks_its_readiness(self) -> None:
        """A head that may still be running is the worse ambiguity: the caller has to keep its
        launch intent for it, which a deferred relaunch would throw away."""
        blocked = {"wait": {"satisfied": False, "blockedReason": "codex-update-prompt"}}

        class RefusingHost(ScriptedWaitHost):
            def _run_json(self, args: list[str]) -> dict:
                if args[:3] == ["orca", "terminal", "close"]:
                    raise HostError("orca terminal close failed: tab_not_found")
                return super()._run_json(args)

        host = RefusingHost(self.root, waits=[self._refused(blocked), blocked])

        with self.assertRaises(HeadLaunchAborted):
            self._launch(host)


class ReviewLivenessTests(unittest.TestCase):
    """Which pane counts as "the reviewer" for lifecycle checks."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name)
        self.workspace = self.root / "ws"
        self.workspace.mkdir()
        self.task = {"ref": "secretary-651", "project": "secretary", "routing": {}}
        _clear_env(self, "SECRETARY_DISPATCHER_BODY_DIR")
        os.environ["SECRETARY_DISPATCHER_BODY_DIR"] = str(self.root)

    def _dead_pid(self) -> int:
        proc = subprocess.Popen(["true"])
        proc.wait()
        return proc.pid

    def _live_pid(self) -> int:
        proc = subprocess.Popen(["sleep", "5"])
        self.addCleanup(proc.wait)
        self.addCleanup(proc.terminate)
        return proc.pid

    def _host(self, terminals: list[dict]) -> RecordingReviewHost:
        return RecordingReviewHost(self.root, terminals=terminals)

    def _record(self, **fields) -> DispatcherRecord:
        record = DispatcherRecord(
            worker="secretary-651-w",
            workspace=str(self.workspace),
            handle="term-worker",
            head="codex",
            review_head="codex-reviewer",
            attempt_id="attempt-1",
            comment_baseline=0,
            review_baseline=0,
            state="reviewing",
            claimed_at=0.0,
        )
        for name, value in fields.items():
            setattr(record, name, value)
        record.worker_head_run = head_ops.HeadRun(
            run_id="worker-liveness-run",
            spec=head_ops.HeadSpec(profile_id=record.head, adapter="codex"),
            workspace=record.workspace,
            task_ref=head_ops.TaskRef.card(self.task["ref"]),
            handle=record.handle,
            leaf=record.worker_leaf,
            pid_file=pid_file_path("worker", self.task["ref"]),
        ).to_json()
        record.review_head_run = head_ops.HeadRun(
            run_id="review-liveness-run",
            spec=head_ops.HeadSpec(profile_id=record.review_head, adapter="codex"),
            workspace=record.workspace,
            task_ref=head_ops.TaskRef.card(self.task["ref"]),
            handle=record.review_handle,
            leaf=record.review_leaf,
            pid_file=pid_file_path("review", self.task["ref"]),
        ).to_json()
        return record

    def _write_heartbeat(self, kind: str, pid: int, record: DispatcherRecord | None = None) -> None:
        record = record or self._record()
        run = record.review_head_run if kind == "review" else record.worker_head_run
        leaf = record.review_leaf if kind == "review" else record.worker_leaf
        heartbeat = run_heartbeat_identity(run, role=kind, task=f"card:{self.task['ref']}", leaf=leaf)
        heartbeat.update({"version": 1, "pid": pid})
        stat_path = Path(f"/proc/{pid}/stat")
        if stat_path.exists():
            stat = stat_path.read_text(encoding="utf-8")
            heartbeat.update(
                {
                    "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip(),
                    "proc_starttime_ticks": stat[stat.rfind(")") + 2 :].split()[19],
                }
            )
        else:
            heartbeat.update({"boot_id": "dead-process", "proc_starttime_ticks": "0"})
        Path(pid_file_path(kind, self.task["ref"])).write_text(json.dumps(heartbeat), encoding="utf-8")

    def test_an_unreadable_inventory_raises_for_either_role(self) -> None:
        """secretary-1414: the inventory is the session host's now, and what it cannot read it
        refuses. A shape this cannot parse says nothing about which panes exist, so it must not
        arrive as `missing-terminal`: that reads as a dead head and respawns over a live one."""
        for kind, status in (("worker", "worker_status"), ("review", "review_status")):
            with self.subTest(kind=kind):
                host = self._host([])
                host._run_json = lambda _args: {"ok": False}  # type: ignore[method-assign]
                record = self._record(review_handle="term-review", review_leaf="leaf-review")

                with self.assertRaises(HostError):
                    getattr(host, status)(self.task, record)

    def test_an_inventory_of_an_unsupported_shape_is_not_an_empty_worktree(self) -> None:
        for status in ("worker_status", "review_status"):
            with self.subTest(status=status):
                host = self._host([])
                host._run_json = lambda _args: {"terminals": "not-a-list"}  # type: ignore[method-assign]

                with self.assertRaises(HostError):
                    getattr(host, status)(self.task, self._record(review_handle="term-review"))

    def test_persisted_handle_survives_the_heads_own_title_rewrite(self) -> None:
        """A codex head overwrites the terminal title with its own OSC sequence seconds after
        launch. A title-only check then reads the live reviewer as gone and splits a second one."""
        host = self._host(
            [
                {"handle": "term-review", "leafId": "leaf-review", "title": "codex", "connected": True},
            ]
        )

        status = host.review_status(self.task, self._record(review_handle="term-review"))
        self.assertTrue(status["live"])
        self.assertFalse(status.get("identity_mismatch"))

    def test_leaf_identifies_the_pane_when_the_handle_alias_changed(self) -> None:
        """`terminal list` can answer with a different handle alias for the same pty, so the leaf
        is the token that survives it."""
        host = self._host(
            [
                {"handle": "term-alias", "leafId": "leaf-review", "title": "codex", "connected": True},
            ]
        )

        record = self._record(review_handle="term-review", review_leaf="leaf-review")
        status = host.review_status(self.task, record)
        self.assertTrue(status["live"])
        self.assertFalse(status.get("identity_mismatch"))

    def test_worker_leaf_identifies_the_pane_when_the_handle_alias_changed(self) -> None:
        host = self._host(
            [
                {"handle": "term-alias", "leafId": "leaf-worker", "connected": True},
            ]
        )

        record = self._record(worker_leaf="leaf-worker")

        self.assertTrue(host.worker_status(self.task, record)["live"])

    def test_last_output_at_is_converted_from_milliseconds_to_epoch_seconds(self) -> None:
        host = self._host(
            [
                {
                    "handle": "term-worker",
                    "leafId": "leaf-worker",
                    "connected": True,
                    "lastOutputAt": 1_753_456_789_123,
                },
            ]
        )

        status = host.worker_status(self.task, self._record())

        self.assertEqual(status["last_activity"], 1_753_456_789.123)

    def test_invalid_or_missing_last_output_at_has_no_activity(self) -> None:
        for terminal in (
            {"handle": "term-worker", "leafId": "leaf-worker", "connected": True},
            {
                "handle": "term-worker",
                "leafId": "leaf-worker",
                "connected": True,
                "lastOutputAt": "not-a-time",
            },
        ):
            with self.subTest(terminal=terminal):
                status = self._host([terminal]).worker_status(self.task, self._record())
                self.assertIsNone(status["last_activity"])

    def test_admitted_provider_progress_newer_than_last_output_at_wins(self) -> None:
        host = self._host(
            [
                {
                    "handle": "term-worker",
                    "leafId": "leaf-worker",
                    "connected": True,
                    "lastOutputAt": 1_753_456_789_123,
                },
            ]
        )
        host.provider_progress = lambda _task, record, _kind: {
            "state": "observed",
            "admission": "accepted",
            "source": "codex-session",
            "source_fingerprint": "a" * 32,
            "cursor": "3:cursor",
            "head_run_id": record.worker_head_run["run_id"],
            "head_run_fingerprint": head_run_binding(record.worker_head_run)[1],
            "observed_at": "1753456800.0",
        }  # type: ignore[method-assign]

        status = host.worker_status(self.task, self._record())

        self.assertEqual(status["last_activity"], 1_753_456_800.0)

    def test_foreign_or_incomplete_provider_evidence_does_not_renew_either_role(self) -> None:
        """The shared status seam has the same exact-HeadRun admission as continuation liveness."""
        cases = (
            (
                "worker",
                {},
                {
                    "handle": "term-worker",
                    "leafId": "leaf-worker",
                    "connected": True,
                    "lastOutputAt": 1_753_456_789_123,
                },
                "identity_mismatch",
            ),
            (
                "review",
                {"review_handle": "term-review", "review_leaf": "leaf-review"},
                {
                    "handle": "term-review",
                    "leafId": "leaf-review",
                    "connected": True,
                    "lastOutputAt": 1_753_456_789_123,
                },
                "unavailable",
            ),
        )
        for kind, fields, terminal, expected_state in cases:
            with self.subTest(kind=kind):
                record = self._record(**fields)
                run = record.review_head_run if kind == "review" else record.worker_head_run
                _, fingerprint = head_run_binding(run)
                host = self._host([terminal])
                provider = {
                    "state": "observed",
                    "admission": "accepted",
                    "source": "codex-session",
                    "source_fingerprint": "a" * 32,
                    "cursor": "3:cursor",
                    "head_run_id": run["run_id"],
                    "head_run_fingerprint": fingerprint,
                    "observed_at": "1753456800.0",
                }
                if kind == "worker":
                    provider["head_run_id"] = "foreign-worker-run"
                else:
                    provider["cursor"] = ""
                host.provider_progress = lambda _task, _record, _kind, value=provider: value  # type: ignore[method-assign]

                status = getattr(host, f"{kind}_status")(self.task, record)

                self.assertEqual(status["provider_progress"]["state"], expected_state)
                self.assertEqual(status["last_activity"], 1_753_456_789.123)

    def test_disconnected_reviewer_pane_is_not_running(self) -> None:
        host = self._host(
            [
                {"handle": "term-review", "leafId": "leaf-review", "connected": False},
            ]
        )

        status = host.review_status(self.task, self._record(review_handle="term-review"))
        self.assertFalse(status["live"])

    def test_disconnected_pane_preserves_a_foreign_heartbeat_fence_for_both_roles(self) -> None:
        """The shared status seam reads identity before an inventory result can authorize a
        replacement. A disconnected pane must not hide a live process from another HeadRun."""
        for kind, status_name, record_fields, terminal in (
            (
                "worker",
                "worker_status",
                {"worker_leaf": "leaf-worker"},
                {"handle": "term-worker", "leafId": "leaf-worker", "connected": False},
            ),
            (
                "review",
                "review_status",
                {"review_handle": "term-review", "review_leaf": "leaf-review"},
                {"handle": "term-review", "leafId": "leaf-review", "connected": False},
            ),
        ):
            with self.subTest(kind=kind):
                record = self._record(**record_fields)
                self._write_heartbeat(kind, self._live_pid(), record)
                path = Path(pid_file_path(kind, self.task["ref"]))
                heartbeat = json.loads(path.read_text(encoding="utf-8"))
                heartbeat["run_id"] = f"foreign-{kind}-run"
                path.write_text(json.dumps(heartbeat), encoding="utf-8")

                status = getattr(self._host([terminal]), status_name)(self.task, record)

                self.assertTrue(status["live"])
                self.assertTrue(status["identity_mismatch"])
                self.assertEqual(status["reason"], "heartbeat-identity-mismatch")

    def test_worker_pane_is_never_mistaken_for_the_reviewer(self) -> None:
        host = self._host(
            [
                {"handle": "term-worker", "leafId": "leaf-worker", "title": "codex", "connected": True},
            ]
        )

        status = host.review_status(self.task, self._record(review_handle="term-review"))
        self.assertFalse(status["live"])

    def test_label_finds_an_orphan_pane_when_no_handle_was_persisted(self) -> None:
        """The tick that split the pane died before writing the handle to state, so the label is
        all that is left to recognise it by — and a duplicate reviewer is the cost of missing it."""
        host = self._host(
            [
                {
                    "handle": "term-review",
                    "leafId": "leaf-review",
                    "title": "secretary-651 reviewer",
                    "connected": True,
                },
            ]
        )

        status = host.review_status(self.task, self._record())
        self.assertTrue(status["live"])
        self.assertFalse(status.get("identity_mismatch"))

    def test_connected_worker_pane_with_an_exited_head_process_is_not_live(self) -> None:
        """secretary-751: Codex crashed and Orca kept the pane's own workspace shell alive. The
        pane answers connected and even keeps producing output (the shell's own prompt), so only
        the pid heartbeat tells the watchdog the head itself is gone."""
        self._write_heartbeat("worker", self._dead_pid())
        host = self._host(
            [
                {
                    "handle": "term-worker",
                    "leafId": "leaf-worker",
                    "connected": True,
                    "lastOutputAt": 1_753_456_789_123,
                },
            ]
        )

        status = host.worker_status(self.task, self._record())

        self.assertFalse(status["live"])
        self.assertEqual(status["reason"], "process-exited")

    def test_connected_reviewer_pane_with_an_exited_head_process_is_not_live(self) -> None:
        self._write_heartbeat("review", self._dead_pid())
        host = self._host(
            [
                {"handle": "term-review", "leafId": "leaf-review", "connected": True},
            ]
        )

        status = host.review_status(self.task, self._record(review_handle="term-review"))

        self.assertFalse(status["live"])
        self.assertEqual(status["reason"], "process-exited")

    def test_foreign_reviewer_heartbeat_cannot_adopt_a_review_launch(self) -> None:
        """Recovery sees full status, so a live foreign PID is not a reviewing reviewer."""
        record = self._record(review_handle="term-review", review_leaf="leaf-review")
        record.state = "review_starting"
        record.review_launch_aborts = 2
        record.review_infra_failures = 3
        record.review_infra_error = "previous launch failure"
        foreign = self._live_pid()
        self._write_heartbeat("review", foreign, record)
        path = Path(pid_file_path("review", self.task["ref"]))
        heartbeat = json.loads(path.read_text(encoding="utf-8"))
        heartbeat["run_id"] = "foreign-reviewer-run"
        path.write_text(json.dumps(heartbeat), encoding="utf-8")
        host = self._host(
            [
                {"handle": "term-review", "leafId": "leaf-review", "connected": True},
            ]
        )
        runtime = mock.Mock()
        runtime.host = host

        status = host.review_status(self.task, record)
        outcome = recover_review_launch(
            runtime,
            self.task,
            {self.task["ref"]: record},
            record,
            "attempt-1",
            payload={},
        )

        self.assertTrue(status["live"])
        self.assertTrue(status["identity_mismatch"])
        self.assertFalse(status["live"] and not status.get("identity_mismatch"))
        self.assertEqual(outcome["action"], "review-heartbeat-identity-mismatch")
        self.assertEqual(record.state, "review_starting")
        self.assertEqual(record.review_launch_aborts, 2)
        self.assertEqual(record.review_infra_failures, 3)
        self.assertEqual(record.review_infra_error, "previous launch failure")
        os.kill(foreign, 0)
        runtime.save_records.assert_not_called()

    def test_disconnected_foreign_reviewer_heartbeat_cannot_adopt_a_review_launch(self) -> None:
        """A disconnected pane still preserves a live foreign PID's no-replacement fence."""
        record = self._record(review_handle="term-review", review_leaf="leaf-review")
        record.state = "review_starting"
        foreign = self._live_pid()
        self._write_heartbeat("review", foreign, record)
        path = Path(pid_file_path("review", self.task["ref"]))
        heartbeat = json.loads(path.read_text(encoding="utf-8"))
        heartbeat["run_id"] = "foreign-reviewer-run"
        path.write_text(json.dumps(heartbeat), encoding="utf-8")
        host = self._host(
            [
                {"handle": "term-review", "leafId": "leaf-review", "connected": False},
            ]
        )
        runtime = mock.Mock()
        runtime.host = host

        status = host.review_status(self.task, record)
        with mock.patch("secretary.dispatcher_review.start_review") as start_review:
            outcome = recover_review_launch(
                runtime,
                self.task,
                {self.task["ref"]: record},
                record,
                "attempt-1",
                payload={},
            )

        self.assertTrue(status["live"])
        self.assertTrue(status["identity_mismatch"])
        self.assertEqual(outcome["action"], "review-heartbeat-identity-mismatch")
        self.assertEqual(record.state, "review_starting")
        os.kill(foreign, 0)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["run_id"], "foreign-reviewer-run")
        start_review.assert_not_called()
        runtime.save_records.assert_not_called()

    def test_connected_pane_with_a_live_head_process_stays_live(self) -> None:
        self._write_heartbeat("worker", self._live_pid())
        host = self._host(
            [
                {"handle": "term-worker", "leafId": "leaf-worker", "connected": True},
            ]
        )

        status = host.worker_status(self.task, self._record())

        self.assertTrue(status["live"])

    def test_an_adopted_head_with_no_pane_identity_is_live_on_its_heartbeat(self) -> None:
        """secretary-820: a head adopted from a launch intent never had its handle persisted, so
        the inventory cannot name its pane. Its heartbeat can, and reading it as a missing terminal
        would respawn a working head: the second launch the intent exists to prevent."""
        record = self._record(handle="", worker_leaf="")
        self._write_heartbeat("worker", self._live_pid(), record)
        host = self._host([{"handle": "term-other", "leafId": "leaf-other", "connected": True}])

        status = host.worker_status(self.task, record)

        self.assertTrue(status["live"])
        self.assertEqual(status["reason"], "pid")
        self.assertTrue(status["pid_confirmed"])

    def test_a_record_with_no_pane_identity_and_a_dead_head_is_still_missing(self) -> None:
        record = self._record(handle="", worker_leaf="")
        self._write_heartbeat("worker", self._dead_pid(), record)
        host = self._host([{"handle": "term-other", "leafId": "leaf-other", "connected": True}])

        status = host.worker_status(self.task, record)

        self.assertFalse(status["live"])
        self.assertEqual(status["reason"], "missing-terminal")

    def test_a_persisted_handle_the_inventory_never_lists_is_live_on_its_heartbeat(self) -> None:
        """secretary-1158: `orca terminal create` can return a handle `terminal list` never lists
        back, and the leaf lookup that would have saved us keys on that same handle, so
        `worker_leaf` stays empty. A persisted-but-unmatchable identity used to make the heartbeat
        unreachable and killed three live heads in a row, 1-2 minutes into each round."""
        record = self._record(handle="term-worker", worker_leaf="")
        self._write_heartbeat("worker", self._live_pid(), record)
        host = self._host([{"handle": "term-alias", "leafId": "leaf-alias", "connected": True}])

        status = host.worker_status(self.task, record)

        self.assertTrue(status["live"])
        self.assertEqual(status["reason"], "pid")
        self.assertTrue(status["pid_confirmed"])

    def test_a_freshly_respawned_head_with_no_pid_file_yet_is_live_within_the_launch_grace_window(
        self,
    ) -> None:
        """secretary-1158: the dispatcher clears the pid file before a fresh launch and the new
        head has not written its own yet, so right after a respawn neither identity answers. A
        watchdog tick landing in that window used to read a live head as missing-terminal and,
        being the second one, escalated straight to Blocked without the head ever failing."""
        host = self._host([{"handle": "term-alias", "leafId": "leaf-alias", "connected": True}])

        status = host.worker_status(
            self.task,
            self._record(handle="term-worker", worker_leaf="", worker_started_at=time.time()),
        )

        self.assertTrue(status["live"])
        self.assertEqual(status["reason"], "pid-not-written-yet")
        self.assertFalse(status["pid_confirmed"])

    def test_a_reviewer_with_no_pid_file_yet_is_live_within_the_launch_grace_window(self) -> None:
        host = self._host([{"handle": "term-alias", "leafId": "leaf-alias", "connected": True}])

        status = host.review_status(
            self.task,
            self._record(review_handle="term-review", review_leaf="", review_started_at=time.time()),
        )

        self.assertTrue(status["live"])
        self.assertEqual(status["reason"], "pid-not-written-yet")

    def test_a_head_with_no_pid_file_past_the_launch_grace_window_is_missing(self) -> None:
        """The grace window is short and bounded: once it has passed, a still-unwritten pid file
        goes back to being read as a dead head, same as before this fix."""
        host = self._host([{"handle": "term-alias", "leafId": "leaf-alias", "connected": True}])

        status = host.worker_status(
            self.task,
            self._record(
                handle="term-worker",
                worker_leaf="",
                worker_started_at=time.time() - initial_output_stall_seconds() - 1,
            ),
        )

        self.assertFalse(status["live"])
        self.assertEqual(status["reason"], "missing-terminal")

    def test_a_persisted_handle_that_matches_nothing_with_a_dead_head_is_missing(self) -> None:
        """The heartbeat is evidence, not an amnesty: without it the verdict stays unchanged."""
        record = self._record(handle="term-worker", worker_leaf="")
        self._write_heartbeat("worker", self._dead_pid(), record)
        host = self._host([{"handle": "term-alias", "leafId": "leaf-alias", "connected": True}])

        status = host.worker_status(self.task, record)

        self.assertFalse(status["live"])
        self.assertEqual(status["reason"], "missing-terminal")

    def test_a_head_silent_since_launch_is_still_live_while_its_process_runs(self) -> None:
        """The pid signal must not read silence as death: a head that has said nothing since it
        started is a separate, pre-existing case (secretary-726's short initial-output window),
        not this one."""
        self._write_heartbeat("worker", self._live_pid())
        host = self._host(
            [
                {
                    "handle": "term-worker",
                    "leafId": "leaf-worker",
                    "connected": True,
                    "lastOutputAt": 1_000_000,
                },
            ]
        )

        status = host.worker_status(self.task, self._record())

        self.assertTrue(status["live"])

    def test_a_live_head_reports_whether_its_pane_is_waiting_for_input(self) -> None:
        """secretary-1063: the timing ceilings do not apply to a pid-confirmed head, so the wait
        needs the one signal that separates a finished turn from a thinking one."""
        self._write_heartbeat("worker", self._live_pid())
        host = self._host(
            [
                {"handle": "term-worker", "leafId": "leaf-worker", "connected": True},
            ]
        )

        status = host.worker_status(self.task, self._record())

        self.assertTrue(status["idle"])
        self.assertIn(
            [
                "orca",
                "terminal",
                "wait",
                "--terminal",
                "term-worker",
                "--for",
                "tui-idle",
                "--timeout-ms",
                str(TUI_IDLE_PROBE_TIMEOUT_MS),
                "--json",
            ],
            host.calls,
        )

    def test_an_adopted_head_with_no_pane_identity_answers_no_work_state(self) -> None:
        """Nothing to probe, so the status says so instead of guessing: the caller falls back to
        its ceilings rather than treating an unprobed head as one that is working."""
        record = self._record(handle="", worker_leaf="")
        self._write_heartbeat("worker", self._live_pid(), record)
        host = self._host([{"handle": "term-other", "leafId": "leaf-other", "connected": True}])

        status = host.worker_status(self.task, record)

        self.assertTrue(status["pid_confirmed"])
        self.assertNotIn("idle", status)

    def test_a_refused_readiness_probe_answers_no_work_state(self) -> None:
        """A live pane whose binding the runtime has lost: `terminal list` still names it, and the
        readiness probe fails with `terminal_handle_stale`. That is not a busy head and not an idle
        one, so no work state is reported for it."""
        self._write_heartbeat("worker", self._live_pid())
        host = self._host(
            [
                {"handle": "term-worker", "leafId": "leaf-worker", "connected": True},
            ]
        )
        host.fail_ops = {"wait"}

        status = host.worker_status(self.task, self._record())

        self.assertTrue(status["pid_confirmed"])
        self.assertNotIn("idle", status)

    def test_a_pane_held_in_a_dialog_is_not_working(self) -> None:
        self._write_heartbeat("worker", self._live_pid())
        host = self._host(
            [
                {"handle": "term-worker", "leafId": "leaf-worker", "connected": True},
            ]
        )
        host.wait_answer = {"wait": {"satisfied": False, "blockedReason": "trust dialog"}}

        status = host.worker_status(self.task, self._record())

        self.assertTrue(status["idle"])
        self.assertEqual(status["idle_reason"], "dialog")

    def test_a_working_pane_is_not_idle(self) -> None:
        self._write_heartbeat("worker", self._live_pid())
        host = self._host(
            [
                {"handle": "term-worker", "leafId": "leaf-worker", "connected": True},
            ]
        )
        host.wait_answer = {"wait": {"satisfied": False}}

        self.assertFalse(host.worker_status(self.task, self._record())["idle"])

    def test_readiness_is_not_probed_without_a_confirmed_head_process(self) -> None:
        """Without the heartbeat the ordinary ceilings still run, and a probe per waiting tick
        would buy nothing."""
        host = self._host(
            [
                {"handle": "term-worker", "leafId": "leaf-worker", "connected": True},
            ]
        )

        status = host.worker_status(self.task, self._record())

        self.assertNotIn("idle", status)
        self.assertNotIn("wait", [call[2] for call in host.calls if call[:2] == ["orca", "terminal"]])

    def test_pid_file_not_written_yet_falls_back_to_ordinary_liveness(self) -> None:
        """Nothing has written the heartbeat file yet (a launch mid-flight, or a raw
        SECRETARY_DISPATCHER_*_COMMAND override that never will). That is not evidence of death."""
        host = self._host(
            [
                {"handle": "term-worker", "leafId": "leaf-worker", "connected": True},
            ]
        )

        status = host.worker_status(self.task, self._record())

        self.assertTrue(status["live"])


class ProductionPauseTests(unittest.TestCase):
    """The pause the operator actually presses (secretary-731).

    The bug this covers: `pause` wrote a flag the production dispatcher never read, so the operator
    watched the pipeline claim new cards straight through a successful pause.
    """

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.data_dir = Path(self.tmpdir.name)
        # The legacy mirror is written next to the live pipeline worktree by default; keep every
        # test's copy inside its own tmpdir.
        env = mock.patch.dict(
            os.environ,
            {
                "SECRETARY_LEGACY_PAUSE_FILE": str(self.data_dir / "legacy-pause.json"),
                "SECRETARY_DISPATCHER_BODY_DIR": str(self.data_dir / "bodies"),
            },
        )
        env.start()
        self.addCleanup(env.stop)
        self.legacy_mirror = self.data_dir / "legacy-pause.json"
        self.board = FakeKanboard()
        self.reader = TaskReader(self.board)  # type: ignore[arg-type]
        self.writer = TaskWriter(self.board, data_dir=self.data_dir, workspace=self.data_dir)  # type: ignore[arg-type]
        self.catalog = FakeCatalog(instance_dir=self.data_dir)
        self.host = FakeHost(self.data_dir / "workspaces", self.catalog)
        self.runtime = DispatcherRuntime(
            self.reader,
            self.writer,
            TaskAudit(self.data_dir),
            self.data_dir,
            self.catalog,  # type: ignore[arg-type]
            self.host,  # type: ignore[arg-type]
            owner="secretary-pilot",
        )
        self.ref = "secretary-510-pilot"

    def pause(self, mode: str, **kwargs) -> dict:
        return self.runtime.pause_pipeline(mode=mode, actor="operator", reason="host maintenance", **kwargs)

    def report_done(self) -> None:
        """Report through the command in the checkout: that id is what names the round."""
        document = (Path(self.record().workspace) / "TASK.md").read_text(encoding="utf-8")
        line = next(line for line in document.splitlines() if "--kind done" in line)
        self.writer.report(
            role="worker",
            actor="worker",
            reference=self.ref,
            kind="done",
            body="ready for review",
            request_id=line.split("--request-id ", 1)[1].split()[0],
        )

    def drive_into_review(self) -> None:
        self.runtime.production_tick()
        self.report_done()
        self.runtime.production_tick()
        self.assertEqual(self.runtime.production_tick()["actions"][0]["action"], "review-started")

    def record(self) -> DispatcherRecord:
        payload = self.runtime.production_state.load()
        return self.runtime.production_state.records(payload)[self.ref]

    def test_drain_stops_new_claims(self) -> None:
        self.pause("drain")

        result = self.runtime.production_tick()

        self.assertEqual(result["pause"]["mode"], "drain")
        self.assertEqual(self.reader.show(self.ref)["state"], "ready")
        self.assertEqual(self.host.prepared, [])
        self.assertEqual(self.runtime.production_state.load().get("records") or {}, {})

    def test_unreadable_pause_file_freezes_dispatch(self) -> None:
        self.runtime.pause.path.parent.mkdir(parents=True, exist_ok=True)
        self.runtime.pause.path.write_text("{not-json", encoding="utf-8")

        result = self.runtime.production_tick()

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["pause"]["mode"], "freeze")
        self.assertIn("pause file is unreadable and read as frozen", result["pause"]["warnings"][0])
        self.assertEqual(self.reader.show(self.ref)["state"], "ready")

    def test_paused_tick_does_not_claim_a_new_card(self) -> None:
        """Regression for the reported bug: pause, tick, and the card must still be Ready."""
        for mode in ("drain", "freeze"):
            with self.subTest(mode=mode):
                self.pause(mode)
                self.runtime.production_tick()
                self.assertEqual(self.reader.show(self.ref)["state"], "ready")
                self.assertEqual(self.reader.show("secretary-510-neighbor")["state"], "ready")
                self.runtime.resume_pipeline(actor="operator")

    def test_drain_keeps_driving_the_card_already_in_flight(self) -> None:
        self.runtime.production_tick()
        self.report_done()
        self.pause("drain")

        result = self.runtime.production_tick()

        self.assertEqual(result["actions"][0]["to"], "validate")
        self.assertEqual(self.reader.show(self.ref)["state"], "validate")
        # ...and the Ready neighbour is still not claimed while the drain holds.
        self.assertEqual(self.reader.show("secretary-510-neighbor")["state"], "ready")

    def test_freeze_stops_the_worker_head_without_touching_the_workspace(self) -> None:
        self.runtime.production_tick()
        workspace = self.record().workspace

        status = self.pause("freeze")

        self.assertEqual(status["stopped_worker"], [self.ref])
        self.assertIn("stop_head:worker", self.host.calls)
        self.assertNotIn("stop_workspace", self.host.calls)
        self.assertEqual(self.host.torn_down, [])
        self.assertTrue(Path(workspace).is_dir())
        record = self.record()
        self.assertEqual(record.handle, "")
        self.assertEqual(record.workspace, workspace)
        self.assertGreater(record.paused_worker_at, 0)

    def test_freeze_stops_the_reviewer_head(self) -> None:
        self.drive_into_review()

        status = self.pause("freeze")

        self.assertEqual(status["stopped_reviewer"], [self.ref])
        self.assertEqual(self.host.stopped_reviews, [f"review:{self.ref}"])
        self.assertEqual(self.host.torn_down, [])
        record = self.record()
        self.assertEqual(record.review_handle, "")
        self.assertGreater(record.paused_reviewer_at, 0)

    def test_freeze_advances_nothing(self) -> None:
        self.runtime.production_tick()
        self.report_done()
        self.pause("freeze")

        result = self.runtime.production_tick()

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "pipeline is frozen by pause")
        self.assertEqual(self.reader.show(self.ref)["state"], "in_progress")

    def test_resume_relaunches_the_worker_in_the_same_workspace(self) -> None:
        self.runtime.production_tick()
        workspace = self.record().workspace
        self.pause("freeze")

        result = self.runtime.resume_pipeline(actor="operator")

        self.assertEqual(result["relaunched"], [f"{self.ref}:worker"])
        self.assertIn("restart_worker", self.host.calls)
        record = self.record()
        self.assertEqual(record.handle, f"rework:{self.ref}")
        self.assertEqual(record.workspace, workspace)
        self.assertEqual(record.paused_worker_at, 0.0)
        self.assertFalse(self.runtime.pause.path.exists())

    def test_resume_relaunches_the_reviewer(self) -> None:
        self.drive_into_review()
        self.pause("freeze")

        result = self.runtime.resume_pipeline(actor="operator")

        self.assertEqual(result["relaunched"], [f"{self.ref}:reviewer"])
        record = self.record()
        self.assertEqual(record.review_handle, f"review:{self.ref}")
        self.assertEqual(record.state, "reviewing")
        self.assertEqual(record.paused_reviewer_at, 0.0)

    def test_a_resume_relaunch_that_dies_mid_flight_is_adopted_by_the_next_tick(self) -> None:
        """secretary-820: the resume is a bring-up like any other, so it writes its intent first.

        A resume killed between the head coming up and the state write that records it would
        otherwise leave a live worker with no handle in the record, and the next tick's watchdog
        would respawn a head that is working.
        """
        self.runtime.production_tick()
        self.pause("freeze")
        real_save = self.runtime.production_state.save
        real_restart = self.host.restart_worker
        launched = {"yet": False}

        def save(payload: dict) -> None:
            if launched["yet"]:
                raise OSError("production state is not writable")
            real_save(payload)

        def restart(task: dict, record, **kwargs):
            result = real_restart(task, record, **kwargs)
            launched["yet"] = True
            return result

        with (
            mock.patch.object(self.runtime.production_state, "save", save),
            mock.patch.object(self.host, "restart_worker", restart),
            self.assertRaises(OSError),
        ):
            self.runtime.resume_pipeline(actor="operator")

        self.assertEqual(self.host.calls.count("restart_worker"), 1)
        self.assertEqual(self.record().launch_intent["action"], "worker-resume")

        # The operator retries the resume. It parks the card rather than guessing at the head the
        # dead run may have left: the tick's recovery is the one place that decides.
        retried = self.runtime.resume_pipeline(actor="operator")

        self.assertEqual(retried["parked"], [f"{self.ref}:worker"])
        self.assertEqual(self.host.calls.count("restart_worker"), 1)

        adopted = self.runtime.production_tick()["actions"][0]

        self.assertEqual(adopted["action"], "worker-launch-adopted")
        self.assertEqual(self.host.calls.count("restart_worker"), 1)
        self.assertEqual(self.record().state, "claimed")

    def test_resume_leaves_a_card_that_reported_during_the_freeze_to_the_tick(self) -> None:
        """A relaunched head would start a fresh turn on work that is already finished."""
        self.runtime.production_tick()
        self.pause("freeze")
        self.report_done()

        result = self.runtime.resume_pipeline(actor="operator")

        self.assertEqual(result["parked"], [f"{self.ref}:worker"])
        self.assertNotIn("restart_worker", self.host.calls)
        self.assertEqual(self.runtime.production_tick()["actions"][0]["to"], "validate")

    def test_resume_hands_the_wait_watchdog_a_fresh_window(self) -> None:
        """A freeze advances nothing, so its whole length would otherwise count as head silence."""
        self.runtime.production_tick()
        self.runtime.production_tick()
        payload = self.runtime.production_state.load()
        records = self.runtime.production_state.records(payload)
        stale = time.time() - (WORKER_REPORT_STALL_DEFAULT * 2)
        records[self.ref].worker_waiting_since = stale
        records[self.ref].worker_progress_at = stale
        # A head seen at its prompt before the freeze is given its idle window back too, or the
        # freeze itself reads as a head that stopped working and delivered nothing.
        records[self.ref].worker_idle_since = stale
        self.runtime.production_state.put_records(payload, records)
        self.runtime.production_state.save(payload)
        self.pause("freeze")

        for _ in range(3):
            self.assertEqual(self.runtime.production_tick()["status"], "skipped")
        paused = self.record()
        self.assertEqual(paused.worker_respawns, 0)
        self.assertEqual(self.reader.show(self.ref)["state"], "in_progress")

        self.runtime.resume_pipeline(actor="operator")

        self.assertGreater(self.record().worker_waiting_since, stale)
        self.assertEqual(self.record().worker_idle_since, 0.0)
        # The watchdog did not read the paused head as a stall: no respawn, no Blocked.
        self.assertEqual(self.runtime.production_tick()["actions"][0]["action"], "waiting-worker-report")
        self.assertEqual(self.reader.show(self.ref)["state"], "in_progress")

    def test_pause_status_reports_the_live_data_plane_and_stopped_heads(self) -> None:
        self.runtime.production_tick()
        self.pause("freeze")

        status = self.runtime.pause_status()

        self.assertTrue(status["paused"])
        self.assertEqual(status["mode"], "freeze")
        self.assertEqual(status["pause_file"], str(self.data_dir / "dispatcher" / "pause.json"))
        self.assertEqual(
            status["dispatcher"]["state_file"], str(self.data_dir / "dispatcher" / "production-state.json")
        )
        self.assertEqual(status["dispatcher"]["owner"], "secretary-pilot")
        head = next(entry for entry in status["heads"] if entry["ref"] == self.ref)
        self.assertEqual(head["worker"], "stopped-by-pause")

    def test_a_head_that_was_never_up_is_not_reported_as_pause_stopped(self) -> None:
        self.runtime.production_tick()
        self.pause("freeze")

        head = next(entry for entry in self.runtime.pause_status()["heads"] if entry["ref"] == self.ref)

        self.assertEqual(head["worker"], "stopped-by-pause")
        self.assertEqual(head["reviewer"], "not-running")

    def test_repeated_pause_in_the_same_mode_is_a_noop(self) -> None:
        self.pause("drain")

        again = self.pause("drain")

        self.assertEqual(again["action"], "noop")
        self.assertEqual(again["mode"], "drain")

    def test_switching_mode_while_paused_is_refused(self) -> None:
        self.pause("drain")

        with self.assertRaisesRegex(DispatcherError, "already paused"):
            self.pause("freeze")

    def test_legacy_aliases_still_parse(self) -> None:
        self.assertEqual(self.pause("soft")["mode"], "drain")
        self.runtime.resume_pipeline(actor="operator")
        self.assertEqual(self.pause("hard")["mode"], "freeze")

    def test_unknown_mode_is_refused(self) -> None:
        with self.assertRaisesRegex(DispatcherError, "unknown pause mode"):
            self.pause("halt")

    def test_resume_without_a_pause_is_a_noop(self) -> None:
        result = self.runtime.resume_pipeline(actor="operator")

        self.assertEqual(result["action"], "noop")
        self.assertFalse(result["paused"])

    def test_pause_mirrors_the_flag_the_background_roles_read(self) -> None:
        """steward/curator/retro still read the legacy flag, so a pause has to reach it too."""
        self.pause("freeze")

        mirrored = json.loads(self.legacy_mirror.read_text(encoding="utf-8"))
        self.assertEqual(mirrored["mode"], "hard")
        self.assertEqual(mirrored["actor"], "operator")

        self.runtime.resume_pipeline(actor="operator")

        self.assertFalse(self.legacy_mirror.exists())

    def test_pause_never_takes_over_a_legacy_flag_it_did_not_write(self) -> None:
        self.legacy_mirror.write_text(json.dumps({"mode": "hard", "actor": "someone-else"}), encoding="utf-8")

        status = self.pause("freeze")

        self.assertFalse(status["legacy_mirror"]["written"])
        self.runtime.resume_pipeline(actor="operator")
        self.assertEqual(json.loads(self.legacy_mirror.read_text(encoding="utf-8"))["actor"], "someone-else")

    def test_freeze_leaves_an_excluded_workspace_running(self) -> None:
        """The backup worker freezes the pipeline from inside its own workspace."""
        self.runtime.production_tick()
        workspace = self.record().workspace

        status = self.pause("freeze", exclude_workspaces=[workspace])

        self.assertEqual(status["excluded_worker"], [self.ref])
        self.assertEqual(status["stopped_worker"], [])
        self.assertEqual(self.host.stopped, [])
        self.assertEqual(self.record().handle, f"term:{self.ref}-pilot")

    def test_probe_reports_a_freeze_instead_of_a_stuck_dispatcher(self) -> None:
        self.pause("freeze")

        result = self.runtime.production_probe()

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["pause"]["mode"], "freeze")
        self.assertEqual(result["would"], [])

    def test_freeze_over_unreadable_state_sets_the_flag_without_touching_it(self) -> None:
        """An unreadable state file must not be replaced by an empty one on the way to a freeze."""
        self.runtime.production_state.path.parent.mkdir(parents=True, exist_ok=True)
        self.runtime.production_state.path.write_text("{ not json", encoding="utf-8")

        status = self.pause("freeze")

        self.assertTrue(status["paused"])
        self.assertIn("production state is unreadable", " ".join(status["warnings"]))
        self.assertEqual(self.runtime.production_state.path.read_text(encoding="utf-8"), "{ not json")
        self.assertEqual(self.runtime.production_tick()["status"], "skipped")

    def age_the_pause(self, seconds: int) -> None:
        state = self.runtime.pause.load()
        state["since"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - seconds))
        self.runtime.pause.save(state)

    def test_an_expired_automation_freeze_is_resumed_by_the_next_tick(self) -> None:
        """A backup killed before its `finally` must not freeze the dispatcher forever."""
        self.runtime.production_tick()
        workspace = self.record().workspace
        self.runtime.pause_pipeline(mode="freeze", actor="secretary-backup", reason="backup snapshot")
        self.age_the_pause(3600)

        result = self.runtime.production_tick()

        self.assertEqual(result["auto_resume"]["reason"], "stale-automation-freeze")
        self.assertEqual(result["auto_resume"]["relaunched"], [f"{self.ref}:worker"])
        self.assertFalse(self.runtime.pause.path.exists())
        self.assertNotEqual(result["status"], "skipped")
        record = self.record()
        self.assertEqual(record.handle, f"rework:{self.ref}")
        self.assertEqual(record.workspace, workspace)
        self.assertEqual(record.paused_worker_at, 0.0)

    def test_a_fresh_automation_freeze_is_left_alone(self) -> None:
        self.runtime.production_tick()
        self.runtime.pause_pipeline(mode="freeze", actor="secretary-backup", reason="backup snapshot")

        result = self.runtime.production_tick()

        self.assertEqual(result["status"], "skipped")
        self.assertIsNone(result.get("auto_resume"))
        self.assertEqual(result["pause"]["auto_resume"]["reason"], "fresh")
        self.assertTrue(self.runtime.pause.path.exists())

    def test_an_operator_freeze_never_expires(self) -> None:
        """A person holding a maintenance window decides when it ends, however long it runs."""
        self.runtime.production_tick()
        self.pause("freeze")
        self.age_the_pause(3600 * 12)

        result = self.runtime.production_tick()

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["pause"]["auto_resume"]["reason"], "manual-or-unknown-actor")
        self.assertTrue(self.runtime.pause.path.exists())

    def test_auto_resume_honours_the_ttl_override(self) -> None:
        self.runtime.pause_pipeline(mode="freeze", actor="secretary-backup", reason="backup")
        self.age_the_pause(3600)

        with mock.patch.dict(os.environ, {"TA_HARD_PAUSE_AUTO_RESUME_TTL_S": "0"}):
            self.assertEqual(self.runtime.production_tick()["status"], "skipped")

        self.assertEqual(self.runtime.production_tick()["auto_resume"]["resumed"], True)

    def test_a_failed_auto_resume_holds_the_freeze_and_says_why(self) -> None:
        self.runtime.production_tick()
        self.runtime.pause_pipeline(mode="freeze", actor="secretary-backup", reason="backup")
        self.age_the_pause(3600)

        with mock.patch.object(self.runtime.pause, "clear", side_effect=OSError("read-only fs")):
            result = self.runtime.production_tick()

        self.assertEqual(result["status"], "skipped")
        self.assertFalse(result["auto_resume"]["resumed"])
        self.assertIn("read-only fs", result["auto_resume"]["error"])
        self.assertTrue(self.runtime.pause.path.exists())
        # The retry on the next tick does not launch a second head on top of the one it just put back.
        retry = self.runtime.production_tick()
        self.assertEqual(retry["auto_resume"]["parked"], [f"{self.ref}:worker"])

    def test_a_frozen_tick_still_writes_and_pushes_the_checkpoint(self) -> None:
        """Freeze stops cards moving, not durability: a long freeze must not be a snapshot hole."""
        self.runtime.checkpoint = FakeCheckpoint(
            CheckpointResult(status="committed", commit="abc123", board_cards=2)
        )
        self.runtime.checkpoint_push = FakePusher({"status": "pushed", "last_push_commit": "abc123"})
        self.pause("freeze")

        result = self.runtime.production_tick()

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["checkpoint"]["commit"], "abc123")
        self.assertEqual(result["checkpoint_push"]["status"], "pushed")
        payload = self.runtime.production_state.load()
        self.assertEqual(payload["checkpoint"]["commit"], "abc123")
        self.assertEqual(payload["checkpoint_push"]["last_push_commit"], "abc123")
        # ...and the frozen tick still moved nothing.
        self.assertEqual(self.reader.show(self.ref)["state"], "ready")

    def test_a_failing_push_on_a_frozen_tick_is_reported_not_raised(self) -> None:
        self.runtime.checkpoint_push = FakePusher(RuntimeError("ssh agent is gone"))
        self.pause("freeze")

        result = self.runtime.production_tick()

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["checkpoint_push"]["status"], "failed")
        self.assertIn("ssh agent is gone", result["checkpoint_push"]["reason"])

    def test_the_mirror_lands_where_the_background_roles_look(self) -> None:
        """resolve_pipeline_state_dir's own order: a mirror written elsewhere sheds nothing."""
        state_dir = self.data_dir / "ta-state"
        with mock.patch.dict(os.environ, {"TA_PIPELINE_STATE_DIR": str(state_dir)}):
            os.environ.pop("SECRETARY_LEGACY_PAUSE_FILE", None)
            self.pause("drain")

        self.assertTrue((state_dir / "pause.json").is_file())


class _SelectorNotFoundHost(CommandHostRuntime):
    """Stubs the orca CLI to answer `selector_not_found` for a terminal stop, as it does for a
    workspace already removed out from under the dispatcher."""

    def __init__(self, root: Path, *, reply: str = "selector_not_found") -> None:
        super().__init__(FakeCatalog(), root, mode="real")  # type: ignore[arg-type]
        self.calls: list[list[str]] = []
        self._reply = reply

    def _run_json(self, args: list[str]) -> dict:
        self.calls.append(args)
        if args[:3] == ["orca", "terminal", "stop"]:
            raise HostError(self._reply)
        return {}


class CommandHostStopWorkspaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name)
        self.record = DispatcherRecord(
            worker="secretary-997-w1",
            workspace=str(self.root / "workspaces" / "secretary-997"),
            handle="",
            head="head",
            review_head="review-head",
            attempt_id="attempt-1",
            comment_baseline=0,
            review_baseline=0,
            state="working",
            claimed_at=0.0,
        )

    def test_a_worktree_orca_no_longer_knows_reads_as_already_stopped(self) -> None:
        host = _SelectorNotFoundHost(self.root)

        host.stop_workspace(self.record)  # must not raise

        self.assertTrue(any(call[:3] == ["orca", "terminal", "stop"] for call in host.calls))

    def test_any_other_stop_refusal_still_raises(self) -> None:
        host = _SelectorNotFoundHost(self.root, reply="orca terminal stop failed")

        with self.assertRaises(HostError):
            host.stop_workspace(self.record)

    def test_a_live_foreign_heartbeat_fences_a_workspace_before_its_first_stop(self) -> None:
        host = _SelectorNotFoundHost(self.root)
        pid_file = self.root / "foreign-workspace.pid"
        self.record.worker_pid_file = str(pid_file)
        self.record.worker_leaf = "leaf-worker"
        self.record.worker_head_run = head_ops.HeadRun(
            run_id="workspace-owned-run",
            spec=head_ops.HeadSpec(profile_id="head", adapter="unknown"),
            workspace=self.record.workspace,
            task_ref=head_ops.TaskRef.card("secretary-997"),
            leaf=self.record.worker_leaf,
            pid_file=str(pid_file),
        ).to_json()
        foreign = subprocess.Popen(["sleep", "5"])

        def reap_foreign() -> None:
            if foreign.poll() is None:
                foreign.terminate()
            foreign.wait()

        self.addCleanup(reap_foreign)
        PidHeartbeatTests.write_heartbeat(
            pid_file,
            foreign.pid,
            identity=heartbeat_identity(
                run_id="foreign-workspace-run",
                role="worker",
                task="card:secretary-997",
                leaf=self.record.worker_leaf,
            ),
        )

        with (
            mock.patch.object(host, "_signal_head") as signal_head,
            self.assertRaisesRegex(HostError, "mismatching launch identity"),
        ):
            host.stop_workspace(self.record)

        self.assertFalse(host.calls, "the workspace stop is fenced before Orca is called")
        signal_head.assert_not_called()
        self.assertIsNone(foreign.poll())


class ReviewPaneTests(unittest.TestCase):
    """secretary-651: the reviewer runs in a visible split pane of the worker's own worktree."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name)
        self.workspace = self.root / "ws"
        self.workspace.mkdir()
        _clear_env(self, "SECRETARY_DISPATCHER_REVIEW_COMMAND")
        os.environ["SECRETARY_DISPATCHER_BODY_DIR"] = str(self.root)
        self.task = {
            "ref": "secretary-651",
            "project": "secretary",
            "description": "spec",
            "workspace": {"base_branch": "main"},
            "routing": {},
        }

    def _record(self, handle: str = "term-worker") -> DispatcherRecord:
        return DispatcherRecord(
            worker="secretary-651-w",
            workspace=str(self.workspace),
            handle=handle,
            head="codex",
            review_head="codex-reviewer",
            attempt_id="attempt-1",
            comment_baseline=0,
            review_baseline=0,
            state="review_starting",
            claimed_at=0.0,
            review_commit="c" * 40,
            review_base_sha="b" * 40,
        )

    def test_reviewer_is_split_off_the_worker_pane_and_gets_its_leaf_from_inventory(self) -> None:
        """Real `terminal split --json` omits paneKey, so the fresh inventory supplies its leaf."""
        host = RecordingReviewHost(self.root)

        launch = host.start_review(self.task, self._record())

        split = host.call_for("split")
        self.assertEqual(split[split.index("--terminal") + 1], "term-worker")
        self.assertIn("--command", split)
        rename = host.call_for("rename")
        self.assertEqual(rename[rename.index("--terminal") + 1], "term-review")
        self.assertEqual(rename[rename.index("--title") + 1], review_pane_label("secretary-651"))
        self.assertEqual(launch.handle, "term-review")
        self.assertEqual(launch.leaf, "leaf-review")
        self.assertEqual(launch.commit, "deadbeefcafe0000")
        self.assertNotIn("create", host.ops(), "the reviewer must not open its own terminal tab")
        self.assertFalse(
            [call for call in host.calls if "worktree" in call and "create" in call],
            "the reviewer must reuse the worker's worktree, never make its own",
        )

    def test_split_uses_pane_key_directly_when_the_backend_supplies_one(self) -> None:
        host = RecordingReviewHost(self.root, split_pane_key="tab-1:leaf-from-reply")

        launch = host.start_review(self.task, self._record())

        self.assertEqual(launch.leaf, "leaf-from-reply")
        self.assertEqual(host.ops().count("list"), 2, "the split safety inventory is mandatory")

    def test_reviewer_pane_carries_the_reference_and_the_role(self) -> None:
        host = RecordingReviewHost(self.root)

        host.start_review(self.task, self._record())

        label = host.call_for("rename")[host.call_for("rename").index("--title") + 1]
        self.assertIn("secretary-651", label)
        self.assertIn("reviewer", label)

    def test_worker_pane_is_shut_down_once_the_reviewer_is_up(self) -> None:
        """Nothing else stops the worker head from editing the checkout mid-review."""
        host = RecordingReviewHost(self.root)

        host.start_review(self.task, self._record())

        closed = host.call_for("close")
        self.assertEqual(closed[closed.index("--terminal") + 1], "term-worker")
        self.assertLess(host.ops().index("split"), host.ops().index("close"), "split needs a live pane")

    def test_reviewer_falls_back_to_its_own_terminal_without_a_live_pane(self) -> None:
        """A worktree whose panes all died still has to get its card reviewed; a background
        terminal is less visible than a split but better than a card parked forever."""
        host = RecordingReviewHost(self.root, terminals=[])

        launch = host.start_review(self.task, self._record(handle=""))

        self.assertEqual(launch.handle, "term-created")
        self.assertEqual(launch.leaf, "leaf-created")
        self.assertNotIn("split", host.ops())

    def test_reviewer_falls_back_when_connected_anchor_is_not_split_capable(self) -> None:
        """An addressable PTY may outlive its renderer node; that cannot park review forever."""
        host = RecordingReviewHost(self.root, split_source_missing=True)

        launch = host.start_review(self.task, self._record())

        self.assertEqual(launch.handle, "term-created")
        self.assertEqual(launch.leaf, "leaf-created")
        self.assertEqual(host.ops().count("split"), 1)
        self.assertEqual(host.ops().count("create"), 1)
        self.assertLess(host.ops().index("create"), host.ops().index("close"))
        self.assertEqual(launch.fallback_reason, "terminal_split_source_not_found")

    def test_reviewer_does_not_fall_back_when_the_split_left_a_pane(self) -> None:
        host = RecordingReviewHost(self.root, split_source_missing=True, split_source_missing_after_open=True)

        with self.assertRaises(PaneSplitSourceMissing):
            host.start_review(self.task, self._record())

        self.assertEqual(host.ops().count("split"), 1)
        self.assertNotIn("create", host.ops())
        self.assertNotIn("close", host.ops(), "a failed reviewer must not kill the worker head")

    def test_create_terminal_returns_the_leaf_from_its_pane_key(self) -> None:
        host = RecordingReviewHost(self.root)
        run = head_ops.HeadRun(
            run_id=head_ops.new_run_id(),
            spec=head_ops.HeadSpec(profile_id="worker", adapter="codex"),
            workspace=str(self.workspace),
            task_ref=head_ops.TaskRef.standing("worker"),
        )

        pane = host._open_head_pane(run, "worker", "run-worker")

        self.assertEqual((pane.handle, pane.leaf), ("term-created", "leaf-created"))

    def test_worker_leaf_selects_the_split_anchor_after_handle_aliasing(self) -> None:
        host = RecordingReviewHost(
            self.root,
            terminals=[
                {"handle": "term-alias", "leafId": "leaf-worker", "connected": True},
                {"handle": "term-other", "leafId": "leaf-other", "connected": True},
            ],
        )
        record = self._record(handle="term-create")
        record.worker_leaf = "leaf-worker"

        host.start_review(self.task, record)

        split = host.call_for("split")
        self.assertEqual(split[split.index("--terminal") + 1], "term-alias")

    def test_leaf_resolves_the_current_alias_for_worker_and_reviewer_stop(self) -> None:
        host = RecordingReviewHost(
            self.root,
            terminals=[
                {"handle": "term-worker-alias", "leafId": "leaf-worker", "connected": True},
                {"handle": "term-review-alias", "leafId": "leaf-review", "connected": True},
            ],
        )
        record = self._record(handle="term-worker-create")
        record.worker_leaf = "leaf-worker"
        record.review_handle = "term-review-create"
        record.review_leaf = "leaf-review"

        host.stop_head(record, "worker")
        host.stop_review(record)

        closed = [
            call[call.index("--terminal") + 1]
            for call in host.calls
            if call[:3] == ["orca", "terminal", "close"]
        ]
        self.assertEqual(closed, ["term-worker-alias", "term-review-alias"])

    def test_dead_worker_pane_is_not_used_as_the_split_anchor(self) -> None:
        host = RecordingReviewHost(
            self.root,
            terminals=[
                {"handle": "term-worker", "leafId": "leaf-worker", "connected": False},
                {"handle": "term-other", "leafId": "leaf-other", "connected": True},
            ],
        )

        host.start_review(self.task, self._record())

        split = host.call_for("split")
        self.assertEqual(split[split.index("--terminal") + 1], "term-other")

    def test_split_failure_raises_and_leaves_the_worker_pane_alone(self) -> None:
        host = RecordingReviewHost(self.root, fail_ops={"split"})

        with self.assertRaises(HostError):
            host.start_review(self.task, self._record())

        self.assertNotIn("close", host.ops(), "a failed reviewer must not kill the worker head")

    def test_label_failure_closes_the_new_pane(self) -> None:
        """Half a bring-up is worse than none: an unlabelled pane is indistinguishable from the
        worker's, and the card would go to Blocked with a live reviewer still running in it."""
        host = RecordingReviewHost(self.root, fail_ops={"rename"})

        with self.assertRaises(HostError):
            host.start_review(self.task, self._record())

        closed = host.call_for("close")
        self.assertEqual(closed[closed.index("--terminal") + 1], "term-review")

    def test_stop_review_closes_only_the_reviewer_pane(self) -> None:
        host = RecordingReviewHost(self.root)
        record = self._record()
        record.review_handle = "term-review"

        host.stop_review(record)

        self.assertEqual(host.ops(), ["close"])
        self.assertEqual(
            host.call_for("close")[host.call_for("close").index("--terminal") + 1], "term-review"
        )
