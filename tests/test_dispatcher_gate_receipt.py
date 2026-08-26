from __future__ import annotations

import unittest

from secretary.dispatcher_gate_receipt import (
    AcceptedGreenGate,
    GateReceipt,
    mint_gate_receipt,
)


class GateReceiptPolicyTests(unittest.TestCase):
    def receipt(self, *, mode: str = "local") -> dict[str, object]:
        receipt = mint_gate_receipt(
            validated_sha="a" * 40,
            base_sha="b" * 40,
            gate_mode=mode,
            required_checks=[
                {
                    "name": "unit",
                    "conclusion": "SUCCESS",
                    "url": "https://ci.invalid/1",
                }
            ],
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

    def test_none_and_noop_accept_only_receiptless_results(self) -> None:
        candidate = self.receipt()

        for gate_mode, noop in (("none", False), ("local", True)):
            with self.subTest(gate_mode=gate_mode, noop=noop):
                receiptless = AcceptedGreenGate.accept(
                    None, current_sha="a" * 40, gate_mode=gate_mode, noop=noop
                )
                forged = AcceptedGreenGate.accept(
                    candidate, current_sha="a" * 40, gate_mode=gate_mode, noop=noop
                )
                self.assertTrue(receiptless.valid)
                self.assertEqual(receiptless.persisted_payload(), {})
                self.assertFalse(forged.valid)

    def test_unknown_mode_is_invalid_even_when_receiptless_or_noop(self) -> None:
        for noop in (False, True):
            accepted = AcceptedGreenGate.accept(None, current_sha="a" * 40, gate_mode="alternate", noop=noop)
            self.assertFalse(accepted.valid)

    def test_only_full_sha1_or_sha256_object_ids_are_receipts(self) -> None:
        for length in (7, 12, 39, 41, 63, 65):
            with self.subTest(length=length):
                self.assertIsNone(
                    mint_gate_receipt(
                        validated_sha="a" * length,
                        base_sha="b" * 40,
                        gate_mode="local",
                        required_checks=[{"name": "unit", "conclusion": "SUCCESS", "url": ""}],
                        check_set_identity="unit",
                    )
                )
        abbreviated_base = self.receipt()
        abbreviated_base["base_sha"] = "b" * 12
        self.assertIsNone(GateReceipt.accept(abbreviated_base, current_sha="a" * 40))
        sha256 = mint_gate_receipt(
            validated_sha="a" * 64,
            base_sha="b" * 64,
            gate_mode="local",
            required_checks=[{"name": "unit", "conclusion": "SUCCESS", "url": ""}],
            check_set_identity="unit",
        )
        self.assertIsNotNone(GateReceipt.accept(sha256, current_sha="a" * 64))


if __name__ == "__main__":
    unittest.main()
