from __future__ import annotations

import unittest

from secretary.dispatcher_gate_receipt import AcceptedGreenGate, GateReceipt, mint_gate_receipt


class GateReceiptPolicyTests(unittest.TestCase):
    def receipt(self, *, mode: str = "local") -> dict[str, object]:
        receipt = mint_gate_receipt(
            validated_sha="a" * 40,
            base_sha="b" * 40,
            gate_mode=mode,
            required_checks=[{
                "name": "unit", "conclusion": "SUCCESS", "url": "https://ci.invalid/1",
            }],
            check_set_identity="python3 -m unittest",
        )
        assert receipt is not None
        return receipt

    def test_exact_sha_passing_receipt_is_typed_and_renderable(self) -> None:
        accepted = AcceptedGreenGate.accept(
            self.receipt(), current_sha="a" * 40, gate_mode="local", noop=False
        )

        self.assertTrue(accepted.valid)
        self.assertIsInstance(accepted.receipt, GateReceipt)
        assert accepted.receipt is not None
        self.assertIn("unit: SUCCESS", accepted.receipt.render())

    def test_mismatch_or_nonpassing_receipt_fails_closed_for_executed_gate(self) -> None:
        failing = self.receipt()
        failing["required_checks"] = [{"name": "unit", "conclusion": "FAILURE", "url": ""}]

        for payload, current_sha, gate_mode in (
            (self.receipt(), "c" * 40, "local"),
            (self.receipt(mode="github"), "a" * 40, "local"),
            (failing, "a" * 40, "local"),
        ):
            with self.subTest(current_sha=current_sha, gate_mode=gate_mode):
                accepted = AcceptedGreenGate.accept(
                    payload, current_sha=current_sha, gate_mode=gate_mode, noop=False
                )
                self.assertFalse(accepted.valid)
                self.assertIsNone(accepted.receipt)

    def test_none_and_noop_never_turn_candidate_data_into_evidence(self) -> None:
        candidate = self.receipt()

        none_gate = AcceptedGreenGate.accept(
            candidate, current_sha="a" * 40, gate_mode="none", noop=False
        )
        noop_gate = AcceptedGreenGate.accept(
            candidate, current_sha="a" * 40, gate_mode="local", noop=True
        )

        self.assertTrue(none_gate.valid)
        self.assertTrue(noop_gate.valid)
        self.assertEqual(none_gate.persisted_payload(), {})
        self.assertEqual(noop_gate.persisted_payload(), {})


if __name__ == "__main__":
    unittest.main()
