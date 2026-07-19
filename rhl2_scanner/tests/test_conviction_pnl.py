"""Conviction fusion + strategy P&L simulator."""

from __future__ import annotations

import unittest

from rhl2_scanner.conviction import fuse
from rhl2_scanner.config import Config
from rhl2_scanner.models import AlertLevel, CategoryScore, ScoreResult, TokenSnapshot
from rhl2_scanner.paper import PaperTrader, format_pnl_html, strategy_pnl
from rhl2_scanner.storage import Storage


class TestConviction(unittest.TestCase):
    def test_confluence_raises_conviction(self):
        base = fuse({"composite": 70, "safety_passed": True})
        stacked = fuse({"composite": 70, "safety_passed": True,
                        "signature_match": 0.9, "curve_match": True,
                        "core_alpha": 2, "smart_quality": 30})
        self.assertGreater(stacked.score, base.score)
        self.assertGreaterEqual(stacked.score, 90)

    def test_unsafe_floors_conviction(self):
        c = fuse({"composite": 95, "safety_passed": False})
        self.assertLessEqual(c.score, 15)

    def test_toxic_is_a_hard_negative(self):
        clean = fuse({"composite": 70, "safety_passed": True, "core_alpha": 1})
        toxic = fuse({"composite": 70, "safety_passed": True, "core_alpha": 1,
                      "toxic": True})
        self.assertLess(toxic.score, clean.score - 20)

    def test_summary_lists_factors(self):
        c = fuse({"composite": 70, "safety_passed": True, "core_alpha": 2})
        self.assertIn("core-alpha", c.summary())


def _res(score):
    return ScoreResult(composite=score, level=AlertLevel.STRONG,
                       categories=[CategoryScore(name="safety", raw=80, weight=0.4)],
                       safety_passed=True)


def _snap(sym, price=1.0):
    return TokenSnapshot(chain="robinhood", pair_address="0x" + sym.rjust(40, "0"),
                         token_address="0x" + sym.rjust(40, "1"), symbol=sym,
                         price_usd=price, market_cap_usd=50000, liquidity_usd=8000)


class TestStrategyPnl(unittest.TestCase):
    def setUp(self):
        self.s = Storage(":memory:")
        self.t = PaperTrader(Config(), self.s)

    def tearDown(self):
        self.s.close()

    def _add(self, sym, conviction, max_mult, min_mult):
        tid = self.t.record_alerted(_snap(sym), _res(80), conviction=conviction)
        self.s.update_paper_trade(tid, 1.0, {}, max_mult, min_mult, settled=True)

    def test_pnl_tp_sl_and_expectancy(self):
        # winner hits 3x (TP 2.5x), loser rugs to 0.3 (SL 0.55), flat exits ~1x
        self._add("WIN", 80, 3.0, 0.9)
        self._add("RUG", 30, 1.1, 0.3)
        self._add("FLAT", 55, 1.2, 0.8)
        p = strategy_pnl(self.s.all_paper_trades(), tp=2.5, sl=0.55, fee=0.0)
        self.assertEqual(p["trades"], 3)
        self.assertEqual(p["tp_hits"], 1)
        self.assertEqual(p["sl_hits"], 1)
        # TP win = +1.5R, SL = -0.45R, flat exit ~ +0.2R (last_price/entry = 1.0 → 0R)
        self.assertGreater(p["total_return"], 0)
        self.assertIn("by_conviction", p)

    def test_high_conviction_bucket_reported(self):
        self._add("A", 80, 4.0, 0.9)
        self._add("B", 80, 3.0, 0.9)
        p = strategy_pnl(self.s.all_paper_trades(), tp=2.5, sl=0.55)
        buckets = {b["band"]: b for b in p["by_conviction"]}
        self.assertIn("75+", buckets)
        self.assertEqual(buckets["75+"]["n"], 2)

    def test_format_and_empty(self):
        self.assertIn("No settled", format_pnl_html(strategy_pnl([])))
        self._add("W", 80, 3.0, 0.9)
        html = format_pnl_html(strategy_pnl(self.s.all_paper_trades()))
        self.assertIn("Strategy P&amp;L", html)
        self.assertIn("expectancy", html)


if __name__ == "__main__":
    unittest.main()
