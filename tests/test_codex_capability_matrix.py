from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path

_SCRIPT = Path(__file__).parents[1] / "scripts" / "codex_capability_matrix.py"
_EVIDENCE = Path(__file__).parents[1] / "docs" / "evidence" / "codex-provider-fanout-2026-08-13.json"
_REPORT = Path(__file__).parents[1] / "docs" / "evidence" / "codex-provider-fanout-2026-08-13.md"
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
            result["policy_result"],
            "collaboration_call_observed_without_child_edge: reject_as_native_boundary",
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
            [
                {
                    "sequence": 0,
                    "tool": "spawn_agent",
                    "parent_thread_id": "parent",
                    "child_thread_ids": ["child"],
                }
            ],
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

    def test_config_rejection_is_not_reported_as_missing_collaboration(self) -> None:
        result = probe.summarize_rollout(
            b"",
            b"Error loading config.toml: unknown configuration field `not_a_field`\n",
            exit_status=1,
            strict_config_flags=("-c", "not_a_field=true"),
        )

        self.assertEqual(result["configuration_evidence"]["strict_config_status"], "rejected")
        self.assertEqual(
            result["configuration_evidence"]["strict_config_rejections"],
            ["unknown configuration field `not_a_field`"],
        )
        self.assertEqual(result["policy_result"], "configuration_rejected: native_boundary_not_evaluated")

    def test_ignored_role_is_not_reported_as_a_configured_candidate(self) -> None:
        result = probe.summarize_rollout(
            b"",
            b"Ignoring malformed agent role definition: agent role default must define a description\n",
            exit_status=0,
            strict_config_flags=("-c", "agents.default.wait_agent_enabled=false"),
        )

        self.assertEqual(result["configuration_evidence"]["agent_role_status"], "ignored_role_definition")
        self.assertEqual(
            result["configuration_evidence"]["ignored_role_definitions"],
            ["agent role default must define a description"],
        )
        self.assertEqual(
            result["policy_result"], "role_configuration_not_applied: native_boundary_not_evaluated"
        )


class CandidateInventoryTests(unittest.TestCase):
    def test_feature_inventory_parser_pins_stable_default_disabled_v2(self) -> None:
        features = probe._parse_feature_list(
            b"multi_agent                          stable             true\n"
            b"multi_agent_v2                       stable             false\n"
        )

        self.assertEqual(features["multi_agent_v2"], {"status": "stable", "default_enabled": False})

    def test_v2_global_wait_candidate_and_valid_role_tables_are_required(self) -> None:
        variants = dict(probe.VARIANTS)
        self.assertIn("v2_feature_wait_disabled", variants)
        self.assertIn(
            "features.multi_agent_v2.wait_agent_enabled=false",
            variants["v2_feature_wait_disabled"],
        )
        for name, flags in variants.items():
            if name.startswith("v2_role_"):
                self.assertIn(probe.ROLE_DESCRIPTION, flags)

    def test_committed_evidence_records_every_candidate_configuration_state(self) -> None:
        payload = json.loads(_EVIDENCE.read_text(encoding="utf-8"))
        rows = {row["variant"]: row for row in payload["variants"]}
        self.assertEqual(set(rows), {name for name, _flags in probe.VARIANTS})
        report = _REPORT.read_text(encoding="utf-8")
        self.assertIn("v2_feature_wait_disabled", report)
        self.assertIn("no collaboration call", report)
        for name, flags in probe.VARIANTS:
            row = rows[name]
            self.assertEqual(row["strict_config_flags"], list(flags))
            config = row["configuration_evidence"]
            self.assertEqual(config["strict_config_status"], "no_rejection_observed")
            self.assertEqual(config["strict_config_rejections"], [])
            if name.startswith("v2_role_"):
                self.assertEqual(config["agent_role_status"], "description_supplied_no_ignored_role_warning")
            else:
                self.assertEqual(config["agent_role_status"], "not_requested")
            self.assertEqual(config["ignored_role_definitions"], [])
        self.assertEqual(rows["v2_feature_wait_disabled"]["collaboration_tool_records"], [])
        self.assertEqual(
            rows["v2_feature_wait_disabled"]["policy_result"],
            "no_collaboration_call_observed: schema_unavailable_not_proof_of_absence",
        )

    def test_committed_feature_inventory_pins_v2_status_and_report_uses_it(self) -> None:
        inventory = Path("docs/evidence/codex-feature-inventory-2026-08-13.json")
        payload = json.loads(inventory.read_text(encoding="utf-8"))

        self.assertTrue(payload["disposable_codex_home"])
        self.assertFalse(payload["auth_source_used"])
        self.assertEqual(payload["codex_version"], "codex-cli 0.147.0")
        self.assertEqual(
            payload["command_shape"],
            ["CODEX_HOME=<disposable-empty-home>", "codex", "features", "list"],
        )
        self.assertEqual(payload["exit_status"], 0)
        self.assertEqual(payload["missing_pinned_features"], [])
        self.assertEqual(
            payload["pinned_features"]["multi_agent_v2"],
            {"default_enabled": False, "status": "stable"},
        )
        report = _REPORT.read_text(encoding="utf-8")
        self.assertIn("multi_agent_v2` as stable and disabled by default", report)
        self.assertIn("codex-feature-inventory-2026-08-13.json", report)
