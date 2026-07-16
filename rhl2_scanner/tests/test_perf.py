"""Tests for live alert performance tracking (reusing PaperTrader)."""

from __future__ import annotations

import unittest

from rhl2_scanner.config import Config
from rhl2_scanner.models import AlertLevel, CategoryScore, ScoreResult, TokenSnapshot
from rhl2_scanner.paper import PaperTrader, format_digest_html
from rhl2_scanner.scanner import Scanner
from rhl2_scanner.storage import Storage


def _snap(price=0.001) -> TokenSnapshot:
    return TokenSnapshot(chain="robinhood", pair_address="0xpair",
                         token_address="0xtok", symbol="RUN", price_usd=price,
                         market_cap_usd=50000, liquidity_usd=20000)


def _res(level=AlertLevel.STRONG, score=80.0) -> ScoreResult:
    return ScoreResult(composite=score, level=level,
                       categories=[CategoryScore("momentum", 60, 0.25)],
                       safety_passed=True)


class TestRecordAlerted(unittest.TestCase):
    def test_records_regardless_of_floor(self):
        cfg = Config()
        cfg.runtime.paper_record_floor = 99   # would block normal record()
        s = Storage(":memory:")
        try:
            trader = PaperTrader(cfg, s)
            # normal record() blocked by floor
            self.assertIsNone(trader.record(_snap(), _res(score=40)))
            # record_alerted ignores the floor
            tid = trader.record_alerted(_snap(), _res(score=40))
            self.assertIsNotNone(tid)
            self.assertTrue(s.has_open_paper_trade("0xpair"))
        finally:
            s.close()

    def test_needs_price(self):
        cfg = Config()
        s = Storage(":memory:")
        try:
            trader = PaperTrader(cfg, s)
            self.assertIsNone(trader.record_alerted(_snap(price=None), _res()))
        finally:
            s.close()


class TestPerfLifecycle(unittest.TestCase):
    def test_live_scanner_has_perf_not_paper(self):
        cfg = Config()
        cfg.runtime.db_path = ":memory:"
        cfg.runtime.dry_run = False
        cfg.runtime.paper_mode = False
        sc = Scanner(cfg)
        try:
            self.assertIsNotNone(sc.perf)
            self.assertIsNone(sc.paper)
        finally:
            sc.storage.close()

    def test_dry_run_has_neither(self):
        cfg = Config()
        cfg.runtime.db_path = ":memory:"
        cfg.runtime.dry_run = True
        sc = Scanner(cfg)
        try:
            self.assertIsNone(sc.perf)
        finally:
            sc.storage.close()


class TestPerfDigest(unittest.TestCase):
    def test_perf_title(self):
        cfg = Config()
        s = Storage(":memory:")
        try:
            PaperTrader(cfg, s).record_alerted(_snap(), _res())
            rep = PaperTrader(cfg, s).report()
            html = format_digest_html(rep, title="Alert Performance", noun="alerts")
            self.assertIn("Alert Performance", html)
            self.assertIn("alerts", html)
        finally:
            s.close()


if __name__ == "__main__":
    unittest.main()
