"""Cross-chain wallet identity: EVM reputation is pooled across EVM chains."""

from __future__ import annotations

import unittest

from memelab.models import Chain
from memelab.storage import Store

W = "0x" + "1" * 40
SOL_W = "solwallet111"


class TestCrossChainIdentity(unittest.TestCase):
    def setUp(self):
        self.s = Store(":memory:")

    def tearDown(self):
        self.s.close()

    def test_evm_smart_set_pooled_across_chains(self):
        # proven smart on Base only
        self.s.add_smart_wallet(Chain.BASE, W, source="winner:0xt")
        # …counts on RH and BNB too (same EVM address)
        self.assertIn(W.lower(), self.s.smart_wallets_effective(Chain.ROBINHOOD))
        self.assertIn(W.lower(), self.s.smart_wallets_effective(Chain.BNB))
        # …but NOT on Solana (different address space)
        self.assertNotIn(W.lower(), self.s.smart_wallets_effective(Chain.SOLANA))

    def test_winner_overlap_pooled_across_evm(self):
        self.s.record_wallet_winner(Chain.BASE, W, "0xa", 5.0)
        self.s.record_wallet_winner(Chain.ETHEREUM, W, "0xb", 4.0)
        self.s.record_wallet_winner(Chain.BNB, W, "0xc", 3.0)
        # pooled across EVM = 3 distinct winners, credited to any EVM chain
        self.assertEqual(self.s.wallet_winner_overlap_cross(Chain.ROBINHOOD, W), 3)

    def test_solana_counted_alone(self):
        self.s.record_wallet_winner(Chain.SOLANA, SOL_W, "mintA", 5.0)
        self.s.record_wallet_winner(Chain.SOLANA, SOL_W, "mintB", 4.0)
        self.assertEqual(self.s.wallet_winner_overlap_cross(Chain.SOLANA, SOL_W), 2)


if __name__ == "__main__":
    unittest.main()
