"""A wait-watchdog respawn tells the successor which command was interrupted (secretary-1692).

The successor's TASK.md carries one factual line taken from the stopped run's own vitality episode
when that episode kept a child reading, and nothing new when it did not.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("SECRETARY_DISPATCHER_BODY_DIR", tempfile.mkdtemp())

from tests.dispatcher_fixtures import DispatcherRuntimeFixture


class RespawnNamesTheInterruptedCommandTests(DispatcherRuntimeFixture, unittest.TestCase):
    """The successor's TASK.md carries the one factual line, and only when there is one."""

    def _stall_to_the_respawn(self, *, command: str) -> dict:
        self._open_the_second_round()
        self._head_at_its_prompt()
        self.tick()
        self._rewind_idle()
        self.tick()  # the round's one prompt
        self._rewind_idle()
        if command:
            payload = self.runtime.production_state.load()
            episode = payload["records"]["secretary-510"]["worker_vitality_episode"]
            episode["last_child_key"] = "4321.99"
            episode["last_child_command"] = command
            episode["last_child_output"] = "/tmp/shards.log"
            self.runtime.production_state.save(payload)
        outcome = self.tick()
        self.assertEqual(outcome["action"], "worker-respawned")
        return outcome

    def _task_doc(self) -> str:
        payload = self.runtime.production_state.load()
        workspace = payload["records"]["secretary-510"]["workspace"]
        return (Path(workspace) / "TASK.md").read_text(encoding="utf-8")

    def test_the_successor_is_told_which_command_was_interrupted(self) -> None:
        self._stall_to_the_respawn(command="timeout 580 python -m pytest tests/integration -k shard1")
        document = self._task_doc()
        self.assertIn("## Interrupted command", document)
        self.assertIn(
            "The previous head was stopped while running: timeout 580 python -m pytest "
            "tests/integration -k shard1 (its output was redirected to /tmp/shards.log)",
            document,
        )
        # Transient: nothing about it lands on the durable record.
        payload = self.runtime.production_state.load()
        self.assertNotIn("respawn_interrupted_command", payload["records"]["secretary-510"])

    def test_no_child_reading_means_nothing_new(self) -> None:
        self._stall_to_the_respawn(command="")
        self.assertNotIn("Interrupted command", self._task_doc())
        self.assertNotIn("stopped while running", self._task_doc())


if __name__ == "__main__":
    unittest.main()
