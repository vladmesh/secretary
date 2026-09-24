from __future__ import annotations

import unittest

from secretary.runtime.agent_prompt_transport import (
    AGENT_PROMPT_MAX_BYTES,
    BRACKETED_PASTE_END,
    BRACKETED_PASTE_START,
    AgentPromptTransportError,
    prepare_agent_prompt,
)


class AgentPromptTransportTests(unittest.TestCase):
    """The prompt validation policy. The Orca pane send it framed for went in secretary-1725."""

    def test_codex_rejects_a_body_larger_than_the_public_send_limit(self) -> None:
        maximum = AGENT_PROMPT_MAX_BYTES - len((BRACKETED_PASTE_START + BRACKETED_PASTE_END).encode())
        with self.assertRaisesRegex(AgentPromptTransportError, "prompt-body-too-large"):
            prepare_agent_prompt("x" * (maximum + 1), adapter="codex")

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


if __name__ == "__main__":
    unittest.main()
