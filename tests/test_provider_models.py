"""What a provider journal says actually ran: the resolved model ids and the reasoning effort."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from secretary.runtime.provider_models import (
    ProviderModels,
    claude_session_models,
    codex_rollout_path,
    codex_session_models,
)


def claude(model: str, effort: str = "") -> dict:
    record: dict = {"type": "assistant", "message": {"id": model, "model": model}}
    if effort:
        record["effort"] = effort
    return record


class ClaudeSessionModelsTests(unittest.TestCase):
    def test_models_are_ordered_by_last_use_and_the_last_is_the_model(self) -> None:
        found = claude_session_models(
            [claude("claude-opus-5-5", "medium"), claude("claude-haiku-4-5"), claude("claude-opus-5-5")]
        )

        self.assertEqual(found.models, ("claude-haiku-4-5", "claude-opus-5-5"))
        self.assertEqual(found.model, "claude-opus-5-5")
        self.assertEqual(found.effort, "medium")

    def test_a_synthetic_message_and_other_records_name_no_model(self) -> None:
        found = claude_session_models(
            [
                claude("claude-opus-5-5"),
                claude("<synthetic>"),
                {"type": "user", "message": {"model": "not-an-answer"}},
                "not a record",
                {"type": "assistant", "message": []},
            ]
        )

        self.assertEqual(found, ProviderModels(("claude-opus-5-5",), ""))

    def test_an_empty_journal_names_nothing(self) -> None:
        self.assertEqual(claude_session_models([]).model, "")


class CodexSessionModelsTests(unittest.TestCase):
    def test_turn_context_names_the_model_and_effort_of_each_turn(self) -> None:
        found = codex_session_models(
            [
                {"type": "session_meta", "payload": {"model_provider": "openai"}},
                {"type": "turn_context", "payload": {"model": "gpt-5.6-sol", "effort": "medium"}},
                {"type": "turn_context", "payload": {"model": "gpt-5.6-terra", "effort": "xhigh"}},
                {"type": "event_msg", "payload": {"type": "token_count"}},
            ]
        )

        self.assertEqual(found.models, ("gpt-5.6-sol", "gpt-5.6-terra"))
        self.assertEqual(found.effort, "xhigh")

    def test_an_older_rollout_spells_the_effort_reasoning_effort(self) -> None:
        found = codex_session_models(
            [{"type": "turn_context", "payload": {"model": "gpt-5.5", "reasoning_effort": "high"}}]
        )

        self.assertEqual((found.model, found.effort), ("gpt-5.5", "high"))


class CodexRolloutPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.home = Path(self.tmpdir.name)

    def rollout(self, day: str, thread: str) -> Path:
        path = self.home / "sessions" / day / f"rollout-2026-09-14T09-43-36-{thread}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
        return path

    def test_the_thread_id_is_the_rollout_file_name_suffix(self) -> None:
        path = self.rollout("2026/09/14", "01a09f4c-f6ce")
        self.rollout("2026/09/14", "01a09f4c-0000")

        self.assertEqual(codex_rollout_path(self.home, "01a09f4c-f6ce"), path)

    def test_no_file_or_an_unsafe_id_answers_none(self) -> None:
        self.rollout("2026/09/14", "thread")

        self.assertIsNone(codex_rollout_path(self.home, "other"))
        self.assertIsNone(codex_rollout_path(self.home, ""))
        self.assertIsNone(codex_rollout_path(self.home, "*"))


if __name__ == "__main__":
    unittest.main()
