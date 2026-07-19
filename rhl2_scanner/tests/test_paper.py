"""Tests for paper-trading recording + calibration report (in-memory sqlite)."""

from __future__ import annotations

import unittest

from rhl2_scanner.config import Config
from rhl2_scanner.models import AlertLevel, CategoryScore, ScoreResult, TokenSnapshot
from rhl2_scanner.paper import PaperTrader, format_digest_html, format_report, send_digest
from rhl2_scanner.storage import Storage


def _snap(symbol: str, price: float) -> TokenSnapshot:
    return TokenSnapshot(
        chain="base",
        pair_address="0x" + symbol.rjust(40, "0"),
        token_address="0x" + symbol.rjust(40, "1"),
        symbol=symbol,
        price_usd=price,
        market_cap_usd=100_000,
        liquidity_usd=30_000,
    )


def _result(score: float, level: AlertLevel, safe: bool = True) -> ScoreResult:
    return ScoreResult(composite=score, level=level, categories=[], safety_passed=safe)


class TestPaper(unittest.TestCase):
    def setUp(self):
        self.storage = Storage(":memory:")
        self.cfg = Config()
        self.trader = PaperTrader(self.cfg, self.storage)

    def tearDown(self):
        self.storage.close()

    def test_record_respects_floor_and_safety(self):
        # Below floor -> not recorded.
        self.assertIsNone(self.trader.record(_snap("AAA", 1.0), _result(40, AlertLevel.SKIP)))
        # Safety failed -> not recorded.
        self.assertIsNone(
            self.trader.record(_snap("BBB", 1.0), _result(90, AlertLevel.STRONG, safe=False))
        )
        # No price -> not recorded.
        no_price = _snap("CCC", 1.0)
        no_price.price_usd = None
        self.assertIsNone(self.trader.record(no_price, _result(80, AlertLevel.STRONG)))
        # Good candidate -> recorded once, deduped on second call.
        tid = self.trader.record(_snap("DDD", 1.0), _result(80, AlertLevel.STRONG))
        self.assertIsNotNone(tid)
        self.assertIsNone(self.trader.record(_snap("DDD", 1.1), _result(80, AlertLevel.STRONG)))

    def test_report_bands(self):
        # strong winner (3x), watch flat (1.2x), below-band winner that rugged.
        combos = [
            ("STRONGW", 80, AlertLevel.STRONG, 3.0, 0.9),
            ("WATCHFL", 65, AlertLevel.WATCH, 1.2, 0.8),
            ("BELOWRG", 50, AlertLevel.SKIP, 5.0, 0.4),
        ]
        for sym, score, level, maxm, minm in combos:
            tid = self.trader.record(_snap(sym, 1.0), _result(score, level))
            self.storage.update_paper_trade(tid, 1.0, {}, maxm, minm, settled=True)

        rep = self.trader.report(win_multiple=2.0)
        bands = {b["band"]: b for b in rep["bands"]}
        self.assertEqual(bands[">=75 (strong)"]["hit_rate"], 1.0)
        self.assertEqual(bands["60-74 (watch)"]["hit_rate"], 0.0)
        self.assertEqual(bands["45-59 (below band)"]["hit_rate"], 1.0)
        self.assertEqual(bands["45-59 (below band)"]["rug_rate"], 1.0)
        self.assertEqual(rep["total_recorded"], 3)
        self.assertEqual(rep["settled"], 3)
        # format doesn't crash and mentions the header
        self.assertIn("calibration", format_report(rep))

    def test_checkpoint_and_mult_math(self):
        # Directly exercise the update path the settler uses.
        tid = self.trader.record(_snap("EEE", 2.0), _result(70, AlertLevel.WATCH))
        # price doubled -> mult 2.0 at the 1h checkpoint
        self.storage.update_paper_trade(
            tid, 4.0, {"1h": {"ts": 1, "price": 4.0, "mult": 2.0}}, 2.0, 1.0, settled=False
        )
        rows = self.storage.all_paper_trades()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["max_mult"], 2.0)
        self.assertEqual(rows[0]["settled"], 0)

    def test_digest_html_renders(self):
        tid = self.trader.record(_snap("HGEM", 1.0), _result(82, AlertLevel.STRONG))
        self.storage.update_paper_trade(tid, 3.0, {}, 3.0, 0.9, settled=True)
        rep = self.trader.report(win_multiple=2.0)
        html = format_digest_html(rep, 2.0)
        self.assertIn("Daily Calibration", html)
        self.assertIn("<b>", html)
        self.assertIn("By score band", html)

    def test_by_signal_and_liq_breakdown(self):
        # Four trades: high-momentum winners vs low-momentum rugs, so the split
        # is unambiguous; also thin vs healthy liquidity.
        def _res(mom):
            cats = [
                CategoryScore(name="safety", raw=90, weight=0.4),
                CategoryScore(name="distribution", raw=70, weight=0.24),
                CategoryScore(name="momentum", raw=mom, weight=0.21),
                CategoryScore(name="discovery", raw=60, weight=0.15),
            ]
            return ScoreResult(composite=70, level=AlertLevel.WATCH,
                               categories=cats, safety_passed=True)

        # high momentum → winners (peak 3x); thin liq
        for i, mom in enumerate((85, 80)):
            s = _snap(f"HI{i}", 1.0)
            s.liquidity_usd, s.market_cap_usd = 1_000, 100_000   # 1% thin
            tid = self.storage.open_paper_trade(s, _res(mom))
            self.storage.update_paper_trade(tid, 3.0, {}, 3.0, 0.9, settled=True)
        # low momentum → rugs (min 0.3x); healthy liq
        for i, mom in enumerate((20, 30)):
            s = _snap(f"LO{i}", 1.0)
            s.liquidity_usd, s.market_cap_usd = 20_000, 100_000  # 20% healthy
            tid = self.storage.open_paper_trade(s, _res(mom))
            self.storage.update_paper_trade(tid, 0.3, {}, 1.0, 0.3, settled=True)

        rep = self.trader.report(win_multiple=2.0)
        mom = rep["by_signal"]["momentum"]
        self.assertEqual(mom["high"]["n"], 2)
        self.assertEqual(mom["high"]["hit_rate"], 1.0)   # high momentum → all won
        self.assertEqual(mom["low"]["n"], 2)
        self.assertEqual(mom["low"]["rug_rate"], 1.0)    # low momentum → all rugged
        self.assertEqual(rep["by_liq"]["thin (<5%)"]["n"], 2)
        self.assertEqual(rep["by_liq"]["healthy (>=5%)"]["n"], 2)

        html = format_digest_html(rep, 2.0)
        self.assertIn("By signal", html)
        self.assertIn("By liquidity", html)


class TestDigestSend(unittest.IsolatedAsyncioTestCase):
    async def test_send_digest_without_telegram_prints(self):
        # No token/chat -> returns (False, ...) and prints instead of raising.
        storage = Storage(":memory:")
        try:
            cfg = Config()
            ok, detail = await send_digest(cfg, storage)
            self.assertFalse(ok)
            self.assertIn("stdout", detail)
        finally:
            storage.close()


if __name__ == "__main__":
    unittest.main()
