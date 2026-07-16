"""Tests for post-alert monitoring: smart-money exits, milestones, dumps."""

from __future__ import annotations

import unittest

from rhl2_scanner.config import Config
from rhl2_scanner.models import AlertLevel, CategoryScore, ScoreResult, TokenSnapshot
from rhl2_scanner.scanner import Scanner
from rhl2_scanner.walletwatch import WhaleEvent

TOKEN = "0x" + "a" * 40
W1 = "0x" + "1" * 40


def _sc() -> Scanner:
    cfg = Config()
    cfg.runtime.db_path = ":memory:"
    cfg.smart_money_wallets = [W1]
    sc = Scanner(cfg)
    sc._sent = []
    async def fake_send(html):
        sc._sent.append(html)
    sc._send_html = fake_send  # type: ignore
    return sc


def _sell(symbol="RUN") -> WhaleEvent:
    return WhaleEvent(wallet=W1, label="Early 1", side="sell", token_address=TOKEN,
                      symbol=symbol, amount=1.0, usd=800.0, tx_hash="0xtx")


def _res() -> ScoreResult:
    return ScoreResult(composite=80, level=AlertLevel.STRONG,
                       categories=[CategoryScore("m", 60, 0.25)], safety_passed=True)


def _open_trade(sc, entry=0.001, last=0.001, peak=1.0):
    snap = TokenSnapshot(chain="robinhood", pair_address="0xpair",
                         token_address=TOKEN, symbol="RUN", price_usd=entry,
                         market_cap_usd=50000, liquidity_usd=20000)
    tid = sc.storage.open_paper_trade(snap, _res())
    sc.storage.update_paper_trade(tid, last, {}, peak, min(1.0, last / entry), False)
    return tid


class TestExit(unittest.IsolatedAsyncioTestCase):
    async def test_exit_only_for_tracked_token(self):
        sc = _sc()
        try:
            # No smart buy on record -> no exit alert.
            await sc._check_exit(_sell())
            self.assertEqual(sc._sent, [])
            # Record a smart buy -> now a sell fires the exit alert, once.
            sc.storage.record_smart_buy(TOKEN, W1)
            await sc._check_exit(_sell())
            self.assertEqual(len(sc._sent), 1)
            self.assertIn("SMART MONEY EXIT", sc._sent[0])
            await sc._check_exit(_sell())          # dedup per wallet
            self.assertEqual(len(sc._sent), 1)
        finally:
            sc.storage.close()

    async def test_exit_disabled(self):
        sc = _sc()
        sc.cfg.runtime.smart_exit_enabled = False
        try:
            sc.storage.record_smart_buy(TOKEN, W1)
            await sc._check_exit(_sell())
            self.assertEqual(sc._sent, [])
        finally:
            sc.storage.close()


class TestMilestoneDump(unittest.IsolatedAsyncioTestCase):
    async def test_milestone_fires_once_per_level(self):
        sc = _sc()
        try:
            _open_trade(sc, entry=0.001, last=0.006, peak=6.0)  # 6x now
            await sc._monitor_positions(dex=None)
            fired = [h for h in sc._sent if "hit" in h]
            self.assertTrue(any("2x" in h for h in fired))
            self.assertTrue(any("5x" in h for h in fired))
            self.assertFalse(any("10x" in h for h in fired))   # not reached
            # re-run: no duplicate milestone pings
            n = len(sc._sent)
            await sc._monitor_positions(dex=None)
            self.assertEqual(len(sc._sent), n)
        finally:
            sc.storage.close()

    async def test_dump_after_runup(self):
        sc = _sc()
        try:
            # peaked at 4x, now back to 1.2x -> 70% drawdown from peak
            _open_trade(sc, entry=0.001, last=0.0012, peak=4.0)
            await sc._monitor_positions(dex=None)
            self.assertTrue(any("DUMPING" in h for h in sc._sent))
        finally:
            sc.storage.close()

    async def test_no_dump_without_runup(self):
        sc = _sc()
        try:
            # never ran up (peak 1.1x) then dropped -> no dump alert
            _open_trade(sc, entry=0.001, last=0.0004, peak=1.1)
            await sc._monitor_positions(dex=None)
            self.assertFalse(any("DUMPING" in h for h in sc._sent))
        finally:
            sc.storage.close()


if __name__ == "__main__":
    unittest.main()
