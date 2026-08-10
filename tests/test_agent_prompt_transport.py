from __future__ import annotations

import threading
import unittest
from unittest import mock

from secretary.dispatcher import _safe_orca_command_label
from triggered_agents.runtime.agent_prompt_transport import (
    AGENT_PROMPT_MAX_BYTES,
    BRACKETED_PASTE_END,
    BRACKETED_PASTE_START,
    AgentPromptTransportError,
    prepare_agent_prompt,
    send_agent_prompt,
)
from triggered_agents.runtime.dispatch import _safe_orca_args_label
from triggered_agents.runtime.tui_delivery import read_pane, wait_for_tui_idle


class AgentPromptTransportTests(unittest.TestCase):
    def send(self, adapter: str, text: str):
        calls: list[list[str]] = []

        def run_json(args: list[str]) -> dict:
            calls.append(args)
            return {"send": {"accepted": True, "bytesWritten": len(args[args.index("--text") + 1].encode()) + (1 if "--enter" in args else 0)}}

        prepared = prepare_agent_prompt(text, adapter=adapter)
        with mock.patch("triggered_agents.runtime.agent_prompt_transport.AGENT_PROMPT_SUBMIT_DELAY_S", 0):
            receipt = send_agent_prompt("term-1", prepared, run_json=run_json)
        return calls, receipt

    def test_codex_size_matrix_is_one_framed_body_and_one_submit(self) -> None:
        maximum = AGENT_PROMPT_MAX_BYTES - len(
            (BRACKETED_PASTE_START + BRACKETED_PASTE_END).encode()
        )
        for size in (1023, 1024, 1025, 4095, 4096, 4097, 6679, maximum):
            with self.subTest(size=size):
                calls, receipt = self.send("codex", "x" * size)
                self.assertEqual(len(calls), 2)
                body, submit = calls
                self.assertNotIn("--enter", body)
                self.assertEqual(body[body.index("--text") + 1], f"{BRACKETED_PASTE_START}{'x' * size}{BRACKETED_PASTE_END}")
                self.assertEqual(submit[submit.index("--text") + 1], "")
                self.assertIn("--enter", submit)
                self.assertTrue(receipt.body_write_accepted)
                self.assertTrue(receipt.submit_write_accepted)
                self.assertEqual((receipt.body_write_count, receipt.submit_count), (1, 1))

    def test_codex_rejects_a_body_larger_than_the_public_send_limit(self) -> None:
        maximum = AGENT_PROMPT_MAX_BYTES - len(
            (BRACKETED_PASTE_START + BRACKETED_PASTE_END).encode()
        )
        with self.assertRaisesRegex(AgentPromptTransportError, "prompt-body-too-large"):
            prepare_agent_prompt("x" * (maximum + 1), adapter="codex")

    def test_unicode_newline_and_tab_stay_data_inside_the_frame(self) -> None:
        prompt = "Résumé\t😀\n第二行"
        calls, receipt = self.send("codex", prompt)
        self.assertEqual(
            calls[0][calls[0].index("--text") + 1],
            f"{BRACKETED_PASTE_START}{prompt}{BRACKETED_PASTE_END}",
        )
        self.assertEqual(receipt.framing, "bracketed-paste-v1")
        self.assertNotIn(prompt, str(receipt.to_json()))

    def test_a_board_card_written_through_a_web_form_still_delivers(self) -> None:
        """CRLF is what an HTML textarea submits, so it cannot be a permanent delivery refusal."""
        calls, receipt = self.send("codex", "# Review\r\n\r\nline one\r\nline two")
        self.assertEqual(
            calls[0][calls[0].index("--text") + 1],
            f"{BRACKETED_PASTE_START}# Review\n\nline one\nline two{BRACKETED_PASTE_END}",
        )
        self.assertTrue(receipt.body_write_accepted)

    def test_a_lone_carriage_return_becomes_a_newline_rather_than_a_submission(self) -> None:
        prepared = prepare_agent_prompt("first\rsecond", adapter="codex")
        self.assertEqual(prepared.text, "first\nsecond")
        self.assertNotIn("\r", prepared.body)

    def test_controls_cannot_break_the_frame_or_write_a_second_command(self) -> None:
        for hostile in ("bad\x1b[201~submit", "bad\x1b[200~", "bad\x00", "bad\x07bell"):
            with self.subTest(hostile=repr(hostile)):
                calls: list[list[str]] = []
                with self.assertRaisesRegex(AgentPromptTransportError, "prompt-body-rejected-control"):
                    prepare_agent_prompt(hostile, adapter="codex")
                self.assertEqual(calls, [])

    def test_claude_keeps_plain_body_but_uses_the_same_two_write_contract(self) -> None:
        calls, receipt = self.send("claude", "Read TASK.md\nthen report")
        self.assertEqual(calls[0][calls[0].index("--text") + 1], "Read TASK.md\nthen report")
        self.assertNotIn("--enter", calls[0])
        self.assertIn("--enter", calls[1])
        self.assertEqual(receipt.framing, "plain-v1")
        self.assertEqual((receipt.body_write_count, receipt.submit_count), (1, 1))

    def test_submit_refusal_keeps_the_accepted_body_and_submit_attempt(self) -> None:
        prepared = prepare_agent_prompt("hello", adapter="codex")
        calls: list[list[str]] = []

        def run_json(args: list[str]) -> dict:
            calls.append(args)
            accepted = "--enter" not in args
            return {"send": {"accepted": accepted, "bytesWritten": 5 if accepted else 0}}

        with mock.patch("triggered_agents.runtime.agent_prompt_transport.AGENT_PROMPT_SUBMIT_DELAY_S", 0), \
             self.assertRaises(AgentPromptTransportError) as raised:
            send_agent_prompt("term-1", prepared, run_json=run_json)
        receipt = raised.exception.receipt
        self.assertEqual(len(calls), 2)
        self.assertTrue(receipt.body_write_accepted)
        self.assertFalse(receipt.submit_write_accepted)
        self.assertEqual((receipt.body_write_count, receipt.submit_count), (1, 1))

    def test_same_terminal_cannot_interleave_a_body_and_submit(self) -> None:
        first_body_written = threading.Event()
        release_first_submit = threading.Event()
        calls: list[tuple[str, bool]] = []
        calls_lock = threading.Lock()
        prepared = prepare_agent_prompt("message", adapter="codex")

        def runner(name: str):
            def run_json(args: list[str]) -> dict:
                enter = "--enter" in args
                with calls_lock:
                    calls.append((name, enter))
                if name == "first" and not enter:
                    first_body_written.set()
                    self.assertTrue(release_first_submit.wait(1))
                return {"send": {"accepted": True, "bytesWritten": 1}}

            send_agent_prompt("term-shared", prepared, run_json=run_json)

        with mock.patch("triggered_agents.runtime.agent_prompt_transport.AGENT_PROMPT_SUBMIT_DELAY_S", 0):
            first = threading.Thread(target=runner, args=("first",))
            second = threading.Thread(target=runner, args=("second",))
            first.start()
            self.assertTrue(first_body_written.wait(1))
            second.start()
            self.assertEqual(calls, [("first", False)])
            release_first_submit.set()
            first.join(1)
            second.join(1)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(calls, [("first", False), ("first", True), ("second", False), ("second", True)])

    def test_a_session_manager_that_is_not_orca_can_carry_the_same_delivery(self) -> None:
        """The seam is only real if a host with no argument vectors at all still delivers."""
        writes: list[tuple[str, str, bool]] = []
        probes: list[tuple[str, int]] = []

        class FakePaneHost:
            def send(self, handle: str, text: str, *, enter: bool):
                writes.append((handle, text, enter))
                return {"send": {"accepted": True, "bytesWritten": len(text.encode())}}

            def read(self, handle: str, *, limit: int | None = None):
                return {"terminal": {"tail": ["ready"], "nextCursor": "7"}}

            def wait_idle(self, handle: str, *, timeout_ms: int):
                probes.append((handle, timeout_ms))
                return {"wait": {"satisfied": True}}

        host = FakePaneHost()
        prepared = prepare_agent_prompt("hello", adapter="codex")
        with mock.patch("triggered_agents.runtime.agent_prompt_transport.AGENT_PROMPT_SUBMIT_DELAY_S", 0):
            receipt = send_agent_prompt("term-1", prepared, host=host)

        self.assertEqual(
            writes,
            [
                ("term-1", f"{BRACKETED_PASTE_START}hello{BRACKETED_PASTE_END}", False),
                ("term-1", "", True),
            ],
        )
        self.assertEqual((receipt.body_write_count, receipt.submit_count), (1, 1))
        self.assertEqual(read_pane("term-1", host=host).cursor, "7")
        wait_for_tui_idle("term-1", host=host, timeout_ms=1234)
        self.assertEqual(probes, [("term-1", 1234)])

    def test_a_delivery_with_neither_a_host_nor_a_runner_is_refused(self) -> None:
        prepared = prepare_agent_prompt("hello", adapter="codex")
        with self.assertRaises(ValueError):
            send_agent_prompt("term-1", prepared)

    def test_public_runner_labels_never_include_the_prompt_body(self) -> None:
        prompt = "do not retain this 🔐\nsecond line"
        dispatcher_label = _safe_orca_command_label(
            ["orca", "terminal", "send", "--terminal", "term-1", "--text", prompt, "--json"]
        )
        service_label = _safe_orca_args_label(
            ["terminal", "send", "--terminal", "term-1", "--text", prompt]
        )

        self.assertNotIn(prompt, dispatcher_label)
        self.assertNotIn(prompt, service_label)
        self.assertIn("<prompt-redacted>", dispatcher_label)
        self.assertIn("<prompt-redacted>", service_label)
