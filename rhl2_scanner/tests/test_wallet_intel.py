"""Wallet reputation scoring + the storage ledger it reads from."""

from __future__ import annotations

import unittest

from rhl2_scanner.storage import Storage
from rhl2_scanner.wallet_intel import (
    deployer_is_trusted, is_toxic, quality, smart_money_quality_bonus,
)


class TestQuality(unittest.TestCase):
    def test_overlap_dominates(self):
        sharp = quality({"winner_overlap": 4, "pick_wins": 0, "pick_total": 0})
        weak = quality({"winner_overlap": 1, "pick_wins": 0, "pick_total": 0})
        self.assertGreater(sharp, weak)
        self.assertGreaterEqual(sharp, 0.6)

    def test_forward_record_modulates(self):
        good = quality({"winner_overlap": 2, "pick_wins": 8, "pick_total": 10})
        bad = quality({"winner_overlap": 2, "pick_wins": 0, "pick_total": 10})
        self.assertGreater(good, bad)

    def test_fresh_harvest_floored_not_zero(self):
        # overlap 1, no forward picks yet — should be a modest positive, not ~0
        q = quality({"winner_overlap": 1, "pick_wins": 0, "pick_total": 0})
        self.assertGreaterEqual(q, 0.35)

    def test_rug_involvement_taxes(self):
        clean = quality({"winner_overlap": 2})
        dirty = quality({"winner_overlap": 2, "rug_count": 2})
        self.assertLess(dirty, clean)

    def test_recency_decay(self):
        rep = {"winner_overlap": 4}
        now = 200 * 86400.0
        fresh = quality(rep, last_ts=now, now=now)
        cold = quality(rep, last_ts=now - 120 * 86400.0, now=now)   # 120 days stale
        self.assertGreater(fresh, cold)

    def test_toxic_flag(self):
        self.assertTrue(is_toxic({"rug_count": 2, "winner_overlap": 0}, min_rugs=1))
        # a real sharp who once aped a rug is not blacklisted
        self.assertFalse(is_toxic({"rug_count": 1, "winner_overlap": 3}, min_rugs=1))

    def test_quality_bonus_weights_by_reputation(self):
        two_sharps = smart_money_quality_bonus([1.0, 1.0])
        five_weak = smart_money_quality_bonus([0.35] * 5)
        self.assertGreater(two_sharps, five_weak)
        # capped
        self.assertLessEqual(smart_money_quality_bonus([1.0] * 10), 45.0)

    def test_deployer_trust(self):
        self.assertTrue(deployer_is_trusted({"wins": 3, "rugs": 0, "total": 4}))
        self.assertFalse(deployer_is_trusted({"wins": 1, "rugs": 0, "total": 1}))  # too few
        self.assertFalse(deployer_is_trusted({"wins": 3, "rugs": 1, "total": 4}))  # rugged


class TestReputationLedger(unittest.TestCase):
    def setUp(self):
        self.s = Storage(":memory:")

    def tearDown(self):
        self.s.close()

    def test_winner_overlap_and_bulk_load(self):
        for tok in ("0xt1", "0xt2", "0xt3"):
            self.s.record_wallet_winner("0xAAA", tok, mult=5.0)
        self.s.record_wallet_winner("0xbbb", "0xt1", mult=3.0)
        rep = self.s.wallet_reputation_rows(["0xaaa", "0xbbb", "0xccc"])
        self.assertEqual(rep["0xaaa"]["winner_overlap"], 3)
        self.assertEqual(rep["0xbbb"]["winner_overlap"], 1)
        self.assertEqual(rep["0xccc"]["winner_overlap"], 0)

    def test_forward_picks_join_outcomes(self):
        # wallet bought 3 tokens; 2 have settled, 1 a winner
        for tok in ("0xp1", "0xp2", "0xp3"):
            self.s.record_smart_buy(tok, "0xAAA")
        self.s.record_token_outcome("0xp1", peak_mult=3.0, is_rug=False)   # win
        self.s.record_token_outcome("0xp2", peak_mult=1.1, is_rug=False)   # dud
        rep = self.s.wallet_reputation_rows(["0xaaa"])
        self.assertEqual(rep["0xaaa"]["pick_total"], 2)
        self.assertEqual(rep["0xaaa"]["pick_wins"], 1)

    def test_core_alpha_and_toxic_and_deployer(self):
        for tok in ("0xw1", "0xw2"):
            self.s.record_wallet_winner("0xsharp", tok, mult=4.0)
        self.assertIn("0xsharp", self.s.core_alpha_wallets(min_overlap=2))
        self.assertNotIn("0xsharp", self.s.core_alpha_wallets(min_overlap=3))
        self.s.record_wallet_rug("0xrug", "0xr1")
        self.assertIn("0xrug", self.s.toxic_wallets(min_rugs=1))
        self.s.record_deployer_token("0xdev", "0xtok", "winner")
        self.assertEqual(self.s.deployer_reputation("0xdev")["wins"], 1)

    def test_top_reputation_ranking(self):
        self.s.record_wallet_winner("0xa", "0xt1", 10.0)
        self.s.record_wallet_winner("0xa", "0xt2", 5.0)
        self.s.record_wallet_winner("0xb", "0xt1", 3.0)
        top = self.s.top_reputation_wallets(limit=5)
        self.assertEqual(top[0]["wallet"], "0xa")
        self.assertEqual(top[0]["overlap"], 2)


if __name__ == "__main__":
    unittest.main()
