"""The cutover write barrier's verdict, and the price every board writer pays for it.

The fence itself is proved end to end against the real controller in
`tests.test_cutover`.  What is decidable without a database — and what was not
proved anywhere — is that the barrier reads the durable document once per
version of it: a frozen installation's transcript grows to megabytes, and it was
decoded again for every `TaskWriter`, `SprintWriter` and Product/Issue mutation
the process built.  Remembering a verdict is only safe if a rewrite is seen on
the very next call and if an unreadable or in-flight document still refuses, so
those are pinned here beside it.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from secretary.board import write_barrier
from secretary.tasks import TaskError

APPLYING = {
    "version": 1,
    "status": "applying",
    "identity": "cutover-identity-1",
    "phases": {"global_freeze": {"status": "complete"}},
}


class WriteBarrierTests(unittest.TestCase):
    def setUp(self) -> None:
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.data = Path(root.name).resolve()
        self.state = self.data / "cutover" / "postgres-v1.json"
        self.state.parent.mkdir(parents=True)
        write_barrier.forget_board_write_verdicts()
        self.addCleanup(write_barrier.forget_board_write_verdicts)

    def _write(self, document: object) -> None:
        """Publish a document the way the controller does, atomically and in place."""
        temporary = self.state.with_suffix(".tmp")
        temporary.write_text(
            document if isinstance(document, str) else json.dumps(document), encoding="utf-8"
        )
        os.replace(temporary, self.state)

    def _counted_reads(self):
        return mock.patch.object(write_barrier, "_read_state", wraps=write_barrier._read_state)

    def test_an_absent_document_admits_every_writer(self) -> None:
        write_barrier.require_board_write_allowed(self.data)

    def test_an_unchanged_document_is_decoded_once_however_many_writers_ask(self) -> None:
        self._write({"version": 1, "status": "resume-ready", "phases": {}})
        with self._counted_reads() as read_state:
            for _ in range(5):
                write_barrier.require_board_write_allowed(self.data)
        self.assertEqual(read_state.call_count, 1)

    def test_a_rewritten_document_is_re_evaluated_on_the_very_next_call(self) -> None:
        self._write({"version": 1, "status": "resume-ready", "phases": {}})
        write_barrier.require_board_write_allowed(self.data)
        self._write(APPLYING)
        with self._counted_reads() as read_state, self.assertRaisesRegex(TaskError, "writes are fenced"):
            write_barrier.require_board_write_allowed(self.data)
        self.assertEqual(read_state.call_count, 1)

    def test_a_remembered_fence_still_refuses_without_re_reading_the_document(self) -> None:
        self._write(APPLYING)
        with self._counted_reads() as read_state:
            for _ in range(3):
                with self.assertRaisesRegex(TaskError, "writes are fenced"):
                    write_barrier.require_board_write_allowed(self.data)
        self.assertEqual(read_state.call_count, 1)

    def test_the_controller_exception_is_this_processs_answer_not_the_files(self) -> None:
        """The cached verdict must not bake in an environment the next caller may not share."""
        self._write({**APPLYING, "controller_pid": os.getpid()})
        with self.assertRaisesRegex(TaskError, "writes are fenced"):
            write_barrier.require_board_write_allowed(self.data)
        with mock.patch.dict(
            os.environ, {write_barrier.CONTROLLER_ID_ENV: APPLYING["identity"]}, clear=False
        ):
            write_barrier.require_board_write_allowed(self.data)
        with self.assertRaisesRegex(TaskError, "writes are fenced"):
            write_barrier.require_board_write_allowed(self.data)

    def test_a_foreign_identity_or_an_unrelated_pid_is_refused(self) -> None:
        self._write({**APPLYING, "controller_pid": os.getpid()})
        with (
            mock.patch.dict(os.environ, {write_barrier.CONTROLLER_ID_ENV: "other"}, clear=False),
            self.assertRaisesRegex(TaskError, "writes are fenced"),
        ):
            write_barrier.require_board_write_allowed(self.data)
        self._write({**APPLYING, "controller_pid": -1})
        with (
            mock.patch.dict(os.environ, {write_barrier.CONTROLLER_ID_ENV: APPLYING["identity"]}, clear=False),
            self.assertRaisesRegex(TaskError, "writes are fenced"),
        ):
            write_barrier.require_board_write_allowed(self.data)

    def test_every_in_flight_status_with_a_live_freeze_phase_refuses(self) -> None:
        for status in sorted(write_barrier.IN_FLIGHT_STATUSES):
            for phase in ("running", "failed", "complete"):
                with self.subTest(status=status, phase=phase):
                    self._write(
                        {**APPLYING, "status": status, "phases": {"global_freeze": {"status": phase}}}
                    )
                    write_barrier.forget_board_write_verdicts()
                    with self.assertRaisesRegex(TaskError, "writes are fenced"):
                        write_barrier.require_board_write_allowed(self.data)

    def test_terminal_state_is_evidence_only_and_never_re_arms(self) -> None:
        for status in sorted(write_barrier.TERMINAL_STATUSES):
            with self.subTest(status=status):
                self._write({**APPLYING, "status": status})
                write_barrier.forget_board_write_verdicts()
                write_barrier.require_board_write_allowed(self.data)

    def test_an_undecodable_document_fences(self) -> None:
        self._write("{not json")
        with self.assertRaisesRegex(TaskError, "unreadable"):
            write_barrier.require_board_write_allowed(self.data)

    def test_a_document_that_is_not_an_object_fences(self) -> None:
        self._write([1, 2, 3])
        with self.assertRaisesRegex(TaskError, "invalid"):
            write_barrier.require_board_write_allowed(self.data)

    def test_an_unstattable_document_fences_before_any_decode(self) -> None:
        self._write(APPLYING)
        with (
            mock.patch.object(Path, "stat", side_effect=PermissionError("injected")),
            self.assertRaisesRegex(TaskError, "unreadable"),
        ):
            write_barrier.require_board_write_allowed(self.data)

    def test_a_removed_document_forgets_its_verdict(self) -> None:
        self._write(APPLYING)
        with self.assertRaisesRegex(TaskError, "writes are fenced"):
            write_barrier.require_board_write_allowed(self.data)
        self.state.unlink()
        write_barrier.require_board_write_allowed(self.data)
        self.assertEqual(write_barrier._VERDICTS, {})


if __name__ == "__main__":
    unittest.main()
