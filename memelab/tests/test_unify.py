"""Tests for the unify bridge: export memelab wallets + signature for the scanner."""

from __future__ import annotations

import unittest

from memelab.models import Chain, Signature, signature_to_json
from memelab.storage import Store
from memelab.__main__ import build_export

W1 = "0x" + "1" * 40
W2 = "0x" + "2" * 40


class TestExport(unittest.TestCase):
    def test_export_wallets_and_signature(self):
        s = Store(":memory:")
        try:
            s.add_smart_wallet(Chain.ROBINHOOD, W1, source="seed")
            s.add_smart_wallet(Chain.ROBINHOOD, W2, source="winner:0xabc")
            s.add_smart_wallet(Chain.SOLANA, "SOLW", source="seed")   # other chain excluded
            s.save_signature(signature_to_json(Signature(
                chains=[Chain.ROBINHOOD],
                rules=[{"feature": "buy_ratio_5m", "op": ">=", "value": 0.6, "weight": 1.0}],
                precision=0.7, trained_on=120)))
            data = build_export(s, Chain.ROBINHOOD)
            self.assertEqual(set(data["smart_wallets"]), {W1, W2})
            self.assertNotIn("SOLW", data["smart_wallets"])
            self.assertEqual(data["signature"]["precision"], 0.7)
            self.assertEqual(data["signature"]["rules"][0]["feature"], "buy_ratio_5m")
        finally:
            s.close()

    def test_export_empty_is_safe(self):
        s = Store(":memory:")
        try:
            data = build_export(s, Chain.ROBINHOOD)
            self.assertEqual(data["smart_wallets"], [])
            self.assertIsNone(data["signature"])
        finally:
            s.close()


if __name__ == "__main__":
    unittest.main()
