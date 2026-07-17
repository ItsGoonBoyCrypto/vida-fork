"""Tests for the memelab data core: storage, labeling, feature extraction."""

from __future__ import annotations

import time
import unittest

from memelab.models import Chain, Outcome, TokenSnapshot, TokenTimeSeries
from memelab.storage import Store
from memelab.backtest.labeler import label, relabel_all
from memelab.metrics.features import extract, extract_live

TOK = "0xtoken000000000000000000000000000000abcd"


def _snap(ts, price, **kw):
    return TokenSnapshot(chain=Chain.BASE, token_address=TOK, pair_address="0xp",
                         symbol="GEM", ts=ts, price_usd=price, **kw)


class TestStore(unittest.TestCase):
    def test_snapshot_roundtrip_and_multiples(self):
        s = Store(":memory:")
        try:
            t0 = 1_000_000.0
            s.record_snapshot(_snap(t0, 1.0, liquidity_usd=10000, market_cap_usd=50000,
                                    holder_count=10))
            s.record_snapshot(_snap(t0 + 300, 3.0, liquidity_usd=12000, holder_count=40))
            s.record_snapshot(_snap(t0 + 600, 0.5, liquidity_usd=11000, holder_count=55))
            ser = s.time_series(Chain.BASE, TOK)
            self.assertEqual(len(ser.snapshots), 3)
            self.assertEqual(ser.entry_price, 1.0)
            self.assertAlmostEqual(ser.peak_multiple, 3.0)     # hit 3x
            self.assertAlmostEqual(ser.trough_multiple, 0.5)
            # full snapshot fields survive the json round-trip
            self.assertEqual(ser.snapshots[1].holder_count, 40)
        finally:
            s.close()

    def test_coverage_and_outcome(self):
        s = Store(":memory:")
        try:
            s.record_snapshot(_snap(1.0, 1.0))
            s.set_outcome(Chain.BASE, TOK, Outcome.WINNER, 5.0, 0.9)
            cov = s.coverage()
            self.assertEqual(cov["by_chain"]["base"]["tokens"], 1)
            self.assertEqual(cov["by_chain"]["base"]["winners"], 1)
        finally:
            s.close()

    def test_signature_active_is_latest(self):
        s = Store(":memory:")
        try:
            s.save_signature('{"v":1}')
            s.save_signature('{"v":2}')
            self.assertEqual(s.active_signature(), '{"v":2}')
        finally:
            s.close()


class TestLabeler(unittest.TestCase):
    def _ts(self, entry, prices, liqs=None, age_h=50):
        now = time.time()
        first = now - age_h * 3600
        snaps = []
        for i, p in enumerate(prices):
            liq = liqs[i] if liqs else 10000
            snaps.append(TokenSnapshot(chain=Chain.SOLANA, token_address=TOK,
                                       ts=first + i * 300, price_usd=p, liquidity_usd=liq))
        return TokenTimeSeries(chain=Chain.SOLANA, token_address=TOK,
                               first_seen_ts=first, entry_price=entry, snapshots=snaps)

    def test_winner(self):
        self.assertEqual(label(self._ts(1.0, [1, 2, 4, 3]), win_multiple=3.0), Outcome.WINNER)

    def test_neutral(self):
        self.assertEqual(label(self._ts(1.0, [1, 1.2, 0.9, 1.1]), win_multiple=3.0), Outcome.NEUTRAL)

    def test_rug_on_liquidity_collapse(self):
        ts = self._ts(1.0, [1, 2, 1.5], liqs=[10000, 12000, 500])  # liq −95%
        self.assertEqual(label(ts, win_multiple=3.0), Outcome.RUG)

    def test_pending_when_young(self):
        self.assertEqual(label(self._ts(1.0, [1, 5], age_h=1), win_multiple=3.0), Outcome.PENDING)

    def test_relabel_all_writes_back(self):
        s = Store(":memory:")
        try:
            now = time.time(); first = now - 50 * 3600
            s.record_snapshot(_snap(first, 1.0, liquidity_usd=10000))
            s.record_snapshot(_snap(first + 300, 4.0, liquidity_usd=11000))
            counts = relabel_all(s, win_multiple=3.0)
            self.assertEqual(counts["winner"], 1)
            self.assertEqual(s.time_series(Chain.BASE, TOK).outcome, Outcome.WINNER)
        finally:
            s.close()


class TestFeatures(unittest.TestCase):
    def _ts(self):
        first = 1_000_000.0
        snaps = [
            TokenSnapshot(chain=Chain.ETHEREUM, token_address=TOK, ts=first,
                          price_usd=1.0, liquidity_usd=8000, market_cap_usd=40000,
                          holder_count=10, volume_1h=5000),
            TokenSnapshot(chain=Chain.ETHEREUM, token_address=TOK, ts=first + 600,
                          price_usd=1.5, liquidity_usd=9000, market_cap_usd=60000,
                          holder_count=45, volume_5m=2000, volume_1h=12000,
                          buys_5m=40, sells_5m=10, smart_money_wallets=["0xa", "0xb"],
                          socials={"tg": "x"}, launchpad="pumpfun"),
            # outside the 30-min window — must be ignored
            TokenSnapshot(chain=Chain.ETHEREUM, token_address=TOK, ts=first + 4000,
                          price_usd=9.0, holder_count=900),
        ]
        return TokenTimeSeries(chain=Chain.ETHEREUM, token_address=TOK,
                               first_seen_ts=first, entry_price=1.0, snapshots=snaps,
                               peak_multiple=9.0, outcome=Outcome.WINNER)

    def test_early_window_only(self):
        fv = extract(self._ts(), early_window_min=30.0)
        # holder_growth uses first(10) -> last-in-window(45), NOT the 900 outside
        self.assertEqual(fv.get("holder_growth"), 35)
        self.assertAlmostEqual(fv.get("buy_ratio_5m"), 0.8)
        self.assertEqual(fv.get("smart_money_count"), 2)
        self.assertEqual(fv.get("launchpad_flag"), 1.0)
        self.assertEqual(fv.get("has_socials"), 1.0)
        self.assertEqual(fv.label, Outcome.WINNER)

    def test_live_matches_training(self):
        ts = self._ts()
        train = extract(ts, early_window_min=30.0)
        live = extract_live(Chain.ETHEREUM, TOK, ts.snapshots, early_window_min=30.0)
        # identical feature math on the same snapshots (no train/serve skew)
        self.assertEqual(train.features, live.features)


if __name__ == "__main__":
    unittest.main()
