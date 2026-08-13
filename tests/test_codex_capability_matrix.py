from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import unittest


_SCRIPT = Path(__file__).parents[1] / "scripts" / "codex_capability_matrix.py"
_SPEC = importlib.util.spec_from_file_location("codex_capability_matrix", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
probe = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(probe)


class RolloutSummaryTests(unittest.TestCase):
    def test_real_collaboration_call_is_not_mistaken_for_schema_absence(self) -> None:
        rollout = b"\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": "parent"}).encode(),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "id": "item_0",
                            "type": "collab_tool_call",
                            "tool": "wait",
                            "sender_thread_id": "parent",
                            "receiver_thread_ids": [],
                            "status": "completed",
                        },
                    }
                ).encode(),
                json.dumps(
                    {"type": "item.completed", "item": {"type": "agent_message", "text": "PARENT_OK"}}
                ).encode(),
            ]
        )

        result = probe.summarize_rollout(rollout, b"", exit_status=0)

        self.assertEqual(result["parent_thread_ids"], ["parent"])
        self.assertEqual(result["collaboration_tool_records"][0]["tool"], "wait")
        self.assertFalse(result["spawn_attempt_observed"])
        self.assertFalse(result["actual_child_edge_observed"])
        self.assertEqual(result["spawn_schema_availability"], "unknown")
        self.assertEqual(result["model_spawn_compliance"], "model_did_not_issue_spawn_tool_call")
        self.assertEqual(
            result["policy_result"], "collaboration_call_observed_without_child_edge: reject_as_native_boundary"
        )

    def test_child_edge_is_a_typed_policy_violation(self) -> None:
        rollout = json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "id": "item_7",
                    "type": "collab_tool_call",
                    "tool": "spawn_agent",
                    "sender_thread_id": "parent",
                    "receiver_thread_ids": ["child"],
                    "status": "completed",
                },
            }
        ).encode()

        result = probe.summarize_rollout(rollout, b"stderr", exit_status=0)

        self.assertTrue(result["spawn_attempt_observed"])
        self.assertTrue(result["actual_child_edge_observed"])
        self.assertEqual(result["all_observed_thread_or_session_ids"], ["child", "parent"])
        self.assertEqual(
            result["child_thread_edges"],
            [{"sequence": 0, "tool": "spawn_agent", "parent_thread_id": "parent", "child_thread_ids": ["child"]}],
        )
        self.assertEqual(result["policy_result"], "child_edge_observed: reject_as_native_boundary")

    def test_non_json_output_does_not_become_negative_tool_evidence(self) -> None:
        result = probe.summarize_rollout(b"not json\n", b"provider error", exit_status=1)

        self.assertEqual(result["json_event_count"], 0)
        self.assertEqual(result["malformed_jsonl_lines"], 1)
        self.assertEqual(result["spawn_schema_availability"], "unknown")
        self.assertEqual(
            result["policy_result"], "no_collaboration_call_observed: schema_unavailable_not_proof_of_absence"
        )
