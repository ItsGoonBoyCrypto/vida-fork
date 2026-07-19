"""Cross-chain wallet reputation → quality-weighted smart-money feature."""

from __future__ import annotations

import time
import unittest

from memelab.metrics.features import _features_from
from memelab.models import Chain, Outcome, TokenSnapshot
from memelab.smartmoney import SmartMoney
from memelab.storage import Store
from memelab.wallet_intel import quality, quality_sum

TOKEN = "0x" + "a" * 40
W_SHARP = "0x" + "1" * 40
W_NEW = "0x" + "2" * 40


class TestQuality(unittest.TestCase):
    def test_overlap_dominates_and_floor(self):
        self.assertGreater(quality({"winner_overlap": 4}), quality({"winner_overlap": 1}))
        self.assertGreaterEqual(quality({"winner_overlap": 1}), 0.35)   # harvested floor
        # an unknown-but-in-set wallet gets a small neutral baseline, not zero,
        # and far below a proven wallet
        self.assertLess(quality({}), 0.2)
        self.assertLess(quality({}), quality({"winner_overlap": 1}))

    def test_forward_record_modulates(self):
        good = quality({"winner_overlap": 2, "pick_wins": 8, "pick_total": 10})
        bad = quality({"winner_overlap": 2, "pick_wins": 0, "pick_total": 10})
        self.assertGreater(good, bad)

    def test_quality_sum(self):
        self.assertAlmostEqual(quality_sum([1.0, 0.5, 0.35]), 1.85)
        self.assertEqual(quality_sum([]), 0.0)


class TestLedger(unittest.TestCase):
    def setUp(self):
        self.s = Store(":memory:")

    def tearDown(self):
        self.s.close()

    def test_winner_overlap_per_chain(self):
        self.s.record_wallet_winner(Chain.SOLANA, W_SHARP, "0xt1", 5.0)
        self.s.record_wallet_winner(Chain.SOLANA, W_SHARP, "0xt2", 3.0)
        self.s.record_wallet_winner(Chain.BASE, W_SHARP, "0xt3", 4.0)
        sol = self.s.wallet_reputation_rows(Chain.SOLANA, [W_SHARP])
        self.assertEqual(sol[W_SHARP.lower()]["winner_overlap"], 2)   # chain-scoped
        base = self.s.wallet_reputation_rows(Chain.BASE, [W_SHARP])
        self.assertEqual(base[W_SHARP.lower()]["winner_overlap"], 1)

    def test_forward_picks_join_outcomes(self):
        now = time.time()
        for tok, peak, oc in (("0xp1", 3.0, Outcome.WINNER), ("0xp2", 1.1, Outcome.NEUTRAL)):
            self.s.record_snapshot(TokenSnapshot(chain=Chain.BASE, token_address=tok,
                                                 ts=now, price_usd=1.0))
            self.s.set_outcome(Chain.BASE, tok, oc, peak=peak, trough=1.0)
            self.s.record_smart_buy(Chain.BASE, W_SHARP, tok)
        rep = self.s.wallet_reputation_rows(Chain.BASE, [W_SHARP])
        self.assertEqual(rep[W_SHARP.lower()]["pick_total"], 2)
        self.assertEqual(rep[W_SHARP.lower()]["pick_wins"], 1)


class TestAnnotateAndFeature(unittest.IsolatedAsyncioTestCase):
    async def test_annotate_sets_quality_and_records_buys(self):
        s = Store(":memory:")
        try:
            # W_SHARP is smart on BASE and already early on 3 winners → high quality
            sm = SmartMoney(s, session=None, seeds=[(Chain.BASE, W_SHARP)])
            for i in range(3):
                s.record_wallet_winner(Chain.BASE, W_SHARP, f"0xw{i}", 5.0)

            async def fake_buyers(chain, token):
                return [W_SHARP.lower(), W_NEW.lower()]     # one smart, one not
            sm._buyers = fake_buyers  # type: ignore
            snap = TokenSnapshot(chain=Chain.BASE, token_address=TOKEN)
            await sm.annotate(snap)
            self.assertEqual(snap.smart_money_wallets, [W_SHARP.lower()])
            self.assertIsNotNone(snap.smart_money_quality)
            self.assertGreater(snap.smart_money_quality, 0.5)   # proven sharp
            # forward-pick ledger recorded the buy
            rep = s.wallet_reputation_rows(Chain.BASE, [W_SHARP])
            # (pick_total needs a settled outcome; here just assert the buy row exists)
            row = s._conn.execute(
                "SELECT 1 FROM smart_buys WHERE wallet = ? AND token = ?",
                (W_SHARP.lower(), TOKEN.lower())).fetchone()
            self.assertIsNotNone(row)
        finally:
            s.close()

    def test_feature_includes_quality(self):
        now = time.time()
        snap = TokenSnapshot(chain=Chain.BASE, token_address=TOKEN, ts=now,
                             price_usd=1.0, smart_money_wallets=[W_SHARP.lower()],
                             smart_money_quality=1.85)
        f = _features_from([snap], snap, snap)
        self.assertEqual(f["smart_money_count"], 1)
        self.assertEqual(f["smart_money_quality"], 1.85)


if __name__ == "__main__":
    unittest.main()
