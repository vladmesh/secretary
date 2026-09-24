"""secretary-1703: a card's local-pty heads on the web, read-only, without Orca.

Hermetic: the run directories are written here the way a supervisor writes them, the card's history
is a fake audit owner, and the board is not consulted. What a live supervisor answers is
`test_web_head_view_supervised`'s; what this file holds is everything a run directory alone decides
-- which runs are the card's, what a finished head shows, what is never shown, and that a source in
any state is a page rather than a 500.
"""

from __future__ import annotations

import json
import os
import subprocess
import unittest
from typing import Any
from unittest import mock

from secretary.runtime.head_runtimes import ORCA_LEGACY_RUNTIME
from secretary.web.app import ROUTES
from secretary.webproto import head_view as head_reads
from tests.web_head_view_fixtures import (
    FINISHED_EARLIER,
    FOREIGN,
    LEGACY,
    OTHER,
    REF,
    SECRET,
    TOKEN,
    WORKER,
    HeadViewFixture,
)


class TheCardPageListsItsHeadsTests(HeadViewFixture):
    def test_every_recorded_head_is_a_row_and_only_local_pty_ones_link_to_a_view(self) -> None:
        self.run_dir(WORKER, tail=b"hello\n")
        self.run_dir(FINISHED_EARLIER, tail=b"before\n")
        status, page = self.get(f"/tasks/{REF}")
        self.assertEqual(status, 200)
        self.assertIn(f'href="/tasks/{REF}/heads/{WORKER}"', page)
        self.assertIn(f'href="/tasks/{REF}/heads/{FINISHED_EARLIER}"', page)
        self.assertNotIn(f"/heads/{LEGACY}", page, "a legacy head has no view to link to")
        self.assertIn(LEGACY, page)
        self.assertIn(head_reads.LEGACY_NOTICE, page)
        self.assertNotIn(FOREIGN, page, "another card's run is not this card's head")

    def test_a_row_carries_role_run_id_and_state(self) -> None:
        self.run_dir(WORKER)
        rows = {row["run_id"]: row for row in self.layer().task_snapshot(REF)["heads"]["items"]}
        self.assertEqual(set(rows), {WORKER, LEGACY, FINISHED_EARLIER})
        worker = rows[WORKER]
        self.assertEqual((worker["role"], worker["state"], worker["local_pty"]), ("worker", "finished", True))
        self.assertTrue(worker["current"])
        legacy = rows[LEGACY]
        self.assertEqual((legacy["role"], legacy["runtime"], legacy["local_pty"]), ("reviewer", ORCA_LEGACY_RUNTIME, False))
        self.assertEqual(legacy["reason"], head_reads.LEGACY_NOTICE)
        # Named only by the history and with no run directory: a head no supervisor ever held.
        self.assertEqual(rows[FINISHED_EARLIER]["reason"], head_reads.LEGACY_NOTICE)
        self.assertFalse(rows[FINISHED_EARLIER]["local_pty"])

    def test_a_held_supervisor_lock_reads_as_running(self) -> None:
        import fcntl

        directory = self.run_dir(WORKER)
        fd = os.open(directory / "supervisor.lock", os.O_RDWR)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        rows = {row["run_id"]: row for row in self.layer().task_snapshot(REF)["heads"]["items"]}
        self.assertEqual(rows[WORKER]["state"], head_reads.RUNNING)


class AFinishedHeadTests(HeadViewFixture):
    def test_a_finished_head_shows_its_kept_tail_and_its_journal(self) -> None:
        self.run_dir(WORKER, tail=b"\x1b[1;32mDONE\x1b[0m all good\r\n")
        status, page = self.get(f"/tasks/{REF}/heads/{WORKER}")
        self.assertEqual(status, 200)
        self.assertIn("DONE all good", page)
        self.assertIn("the tail its supervisor kept", page)
        self.assertIn("turn.finished", page)
        self.assertIn("input.accepted", page)
        self.assertNotIn("\x1b", page)

    def test_a_head_that_finished_before_tails_were_kept_says_so(self) -> None:
        self.run_dir(WORKER)
        status, page = self.get(f"/tasks/{REF}/heads/{WORKER}")
        self.assertEqual(status, 200)
        self.assertIn(head_reads.NOT_KEPT_NOTICE, page)
        self.assertIn("turn.started", page, "the journal is still shown")

    def test_the_json_twin_carries_the_same_view(self) -> None:
        self.run_dir(WORKER, tail=b"plain\n")
        response = self.app().handle("GET", f"/api/tasks/{REF}/heads/{WORKER}")
        self.assertEqual(response.status, 200)
        document = json.loads(response.body)
        self.assertEqual(document["kind"], "head_view")
        self.assertEqual(document["transcript"]["text"], "plain")
        self.assertEqual(document["transcript"]["source"], "output.tail")
        self.assertEqual({key for record in document["journal"]["tail"] for key in record} - set(head_reads.JOURNAL_KEYS), set())

    def test_a_legacy_head_s_view_says_it_has_no_local_pty_transcript(self) -> None:
        status, page = self.get(f"/tasks/{REF}/heads/{LEGACY}")
        self.assertEqual(status, 200)
        self.assertIn(head_reads.LEGACY_NOTICE, page)
        self.assertNotIn("not answering", page, "a legacy head has no source to fail")


class NothingSecretReachesThePageTests(HeadViewFixture):
    def test_a_planted_memory_token_is_on_neither_the_page_nor_the_document(self) -> None:
        tail = (
            f"export SECRETARY_MEMORY_ACCESS_TOKEN={TOKEN}\r\n"
            f"the token is {TOKEN} ok\r\n"
        ).encode()
        directory = self.run_dir(WORKER, tail=tail, subject=f"deliver {TOKEN}")
        # The plant is really there, in both places, before the page is asked for.
        self.assertIn(TOKEN, (directory / "journal.jsonl").read_text())
        self.assertIn(TOKEN.encode(), (directory / "output.tail").read_bytes())
        for path in (f"/tasks/{REF}/heads/{WORKER}", f"/api/tasks/{REF}/heads/{WORKER}", f"/tasks/{REF}"):
            with self.subTest(path=path):
                status, body = self.get(path)
                self.assertEqual(status, 200)
                self.assertNotIn(TOKEN, body)
                self.assertNotIn(SECRET, body)
                self.assertNotIn("--dangerously", body, "run.started's command is never shown")

    def test_markup_in_the_output_is_escaped(self) -> None:
        self.run_dir(WORKER, tail=b"<script>alert('x')</script><img src=x onerror=y>\n")
        status, page = self.get(f"/tasks/{REF}/heads/{WORKER}")
        self.assertEqual(status, 200)
        self.assertNotIn("<script>alert", page)
        self.assertNotIn("<img src=x", page)
        self.assertIn("&lt;script&gt;alert(&#x27;x&#x27;)&lt;/script&gt;", page)


class OnlyTheCardsOwnRunsTests(HeadViewFixture):
    def test_another_card_s_run_is_not_found(self) -> None:
        self.run_dir(FOREIGN, ref=OTHER, tail=b"not yours\n")
        for path in (f"/tasks/{REF}/heads/{FOREIGN}", f"/api/tasks/{REF}/heads/{FOREIGN}"):
            with self.subTest(path=path):
                status, body = self.get(path)
                self.assertEqual(status, 404)
                self.assertNotIn("not yours", body)

    def test_a_random_run_id_is_not_found(self) -> None:
        self.assertEqual(self.get(f"/tasks/{REF}/heads/{'f' * 32}")[0], 404)

    def test_path_traversal_in_the_run_id_is_not_found(self) -> None:
        (self.data_dir / "secret.txt").write_text("outside the heads root")
        for run_id in ("..", "..%2F..%2Fsecret.txt", "..%2Fdispatcher", "%2Fetc%2Fpasswd", f"{WORKER}%2F..%2F.."):
            for prefix in ("/tasks", "/api/tasks"):
                with self.subTest(prefix=prefix, run_id=run_id):
                    status, body = self.get(f"{prefix}/{REF}/heads/{run_id}")
                    self.assertEqual(status, 404)
                    self.assertNotIn("outside the heads root", body)

    def test_a_card_the_board_does_not_hold_is_not_found(self) -> None:
        self.assertEqual(self.get(f"/tasks/secretary-404/heads/{WORKER}")[0], 404)

    def test_a_recorded_run_whose_directory_names_another_card_is_not_read(self) -> None:
        self.run_dir(WORKER, ref=OTHER, tail=b"somebody else's\n")
        status, page = self.get(f"/tasks/{REF}/heads/{WORKER}")
        self.assertEqual(status, 200)
        self.assertNotIn("somebody else", page)
        self.assertIn("belongs to another card", page)

    def test_the_view_is_read_only(self) -> None:
        views = [route for route in ROUTES if route.operation == "reads.head_view"]
        self.assertEqual({(route.method, route.pattern) for route in views}, {
            ("GET", "/tasks/{ref}/heads/{run_id}"),
            ("GET", "/api/tasks/{ref}/heads/{run_id}"),
        })
        self.run_dir(WORKER)
        response = self.app().handle("POST", f"/tasks/{REF}/heads/{WORKER}", headers={})
        self.assertEqual(response.status, 405)


class ASourceInAnyStateIsAPageTests(HeadViewFixture):
    def test_a_damaged_journal_and_an_unreadable_tail_are_said_not_answering(self) -> None:
        directory = self.run_dir(WORKER)
        with open(directory / "journal.jsonl", "ab") as journal:
            journal.write(b"{not json\n\xff\xfe torn")
        (directory / "output.tail").mkdir()
        status, page = self.get(f"/tasks/{REF}/heads/{WORKER}")
        self.assertEqual(status, 200)
        self.assertIn("output is not answering", page)
        self.assertIn("the journal answered in part", page)

    def test_a_socket_that_nobody_answers_is_the_supervisor_not_answering(self) -> None:
        directory = self.run_dir(WORKER)
        (directory / "head.sock").write_text("debris of a killed supervisor")
        status, page = self.get(f"/tasks/{REF}/heads/{WORKER}")
        self.assertEqual(status, 200)
        self.assertIn("output is not answering", page)
        self.assertIn("supervisor output could not be read", page)

    def test_a_journal_that_is_not_a_file_and_a_lock_that_is_garbage_are_still_a_page(self) -> None:
        directory = self.root / WORKER
        directory.mkdir()
        (directory / "journal.jsonl").mkdir()
        (directory / "supervisor.lock").write_bytes(b"\xff\xfe")
        for path in (f"/tasks/{REF}", f"/tasks/{REF}/heads/{WORKER}", f"/api/tasks/{REF}/heads/{WORKER}"):
            with self.subTest(path=path):
                self.assertEqual(self.get(path)[0], 200)

    def test_every_reader_raising_is_still_a_page(self) -> None:
        self.run_dir(WORKER, tail=b"x\n")
        for name in (
            "head_run_first_record",
            "head_run_supervisor_lease",
            "head_run_output_tail",
            "head_run_journal_tail",
            "head_run_socket_present",
        ):
            with self.subTest(reader=name), mock.patch.object(head_reads, name, side_effect=RuntimeError("boom")):
                for path in (f"/tasks/{REF}", f"/tasks/{REF}/heads/{WORKER}"):
                    status, _body = self.get(path)
                    self.assertEqual(status, 200, f"{path} with {name} raising")


class NoOrcaOnThesePathsTests(HeadViewFixture):
    def test_nothing_on_the_card_page_or_the_head_view_runs_a_process(self) -> None:
        self.run_dir(WORKER, tail=b"x\n")
        (self.root / WORKER / "head.sock").write_text("")
        calls: list[Any] = []

        def record(*args: Any, **kwargs: Any) -> Any:
            calls.append(args[0] if args else kwargs.get("args"))
            raise AssertionError(f"a process was started: {args!r}")

        with (
            mock.patch.object(subprocess, "Popen", side_effect=record),
            mock.patch.object(subprocess, "run", side_effect=record),
            mock.patch.object(os, "execvp", side_effect=record),
            mock.patch.object(os, "execvpe", side_effect=record),
            mock.patch.object(os, "posix_spawn", side_effect=record),
            mock.patch.object(os, "posix_spawnp", side_effect=record),
            mock.patch.object(os, "system", side_effect=record),
        ):
            for path in (
                f"/tasks/{REF}",
                f"/api/tasks/{REF}",
                f"/tasks/{REF}/heads/{WORKER}",
                f"/api/tasks/{REF}/heads/{WORKER}",
                f"/tasks/{REF}/heads/{LEGACY}",
            ):
                with self.subTest(path=path):
                    self.assertEqual(self.get(path)[0], 200)
        self.assertEqual([call for call in calls if "orca" in json.dumps(call, default=str)], [])
        self.assertEqual(calls, [])


class TerminalTextTests(unittest.TestCase):
    def test_colour_and_title_sequences_are_removed(self) -> None:
        raw = b"\x1b]0;window title\x07\x1b[38;5;208mwarm\x1b[0m \x1b[1mbold\x1b[22m\x1b(B"
        self.assertEqual(head_reads.terminal_text(raw), "warm bold")

    def test_a_carriage_return_overwrites_the_line_and_crlf_is_one_break(self) -> None:
        self.assertEqual(head_reads.terminal_text(b"50%\r100%\r\ndone\r\n"), "100%\ndone")

    def test_a_backspace_steps_back(self) -> None:
        self.assertEqual(head_reads.terminal_text(b"cax\b\bat"), "cat")

    def test_cursor_forward_keeps_words_apart_and_placement_breaks_lines(self) -> None:
        self.assertEqual(head_reads.terminal_text(b"one\x1b[1Ctwo\x1b[5;1Hthree"), "one two\nthree")

    def test_an_unterminated_sequence_at_the_end_is_dropped(self) -> None:
        self.assertEqual(head_reads.terminal_text(b"ok\x1b]8;;http://x"), "ok")
        self.assertEqual(head_reads.terminal_text(b"ok\x1b[3"), "ok")

    def test_controls_are_removed_and_invalid_utf8_is_replaced(self) -> None:
        self.assertEqual(head_reads.terminal_text(b"a\x00b\x07c\x7f\xffd"), "abc�d")

    def test_blank_runs_collapse(self) -> None:
        self.assertEqual(head_reads.terminal_text(b"a\n\n\n\n\nb"), "a\n\nb")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
