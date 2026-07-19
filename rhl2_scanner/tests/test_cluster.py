"""Tests for the smart-money cluster alert (convergence detection)."""

from __future__ import annotations

import time
import unittest

from rhl2_scanner.config import Config
from rhl2_scanner.scanner import Scanner
from rhl2_scanner.storage import Storage
from rhl2_scanner.walletwatch import WhaleEvent, format_cluster_html

TOKEN = "0x" + "a" * 40
W1 = "0x" + "1" * 40
W2 = "0x" + "2" * 40
W3 = "0x" + "3" * 40


class TestClusterStorage(unittest.TestCase):
    def test_distinct_buyers_and_window(self):
        s = Storage(":memory:")
        try:
            s.record_smart_buy(TOKEN, W1)
            s.record_smart_buy(TOKEN, W1)   # dupe wallet -> still 1
            s.record_smart_buy(TOKEN, W2)
            recent = s.distinct_smart_buyers(TOKEN, since_ts=0)
            self.assertEqual(set(recent), {W1.lower(), W2.lower()})
            # window excludes old rows
            future = time.time() + 100
            self.assertEqual(s.distinct_smart_buyers(TOKEN, since_ts=future), [])
        finally:
            s.close()

    def test_cluster_dedup(self):
        s = Storage(":memory:")
        try:
            self.assertFalse(s.cluster_already_alerted(TOKEN))
            s.mark_cluster_alert(TOKEN, 2)
            self.assertTrue(s.cluster_already_alerted(TOKEN))
        finally:
            s.close()


def _ev(wallet, symbol="RUNNER") -> WhaleEvent:
    return WhaleEvent(wallet=wallet, label="x", side="buy", token_address=TOKEN,
                      symbol=symbol, amount=1.0, usd=500.0, tx_hash="0xtx")


class TestClusterDetection(unittest.IsolatedAsyncioTestCase):
    def _sc(self) -> Scanner:
        cfg = Config()
        cfg.runtime.db_path = ":memory:"
        cfg.smart_money_wallets = [W1, W2, W3]
        cfg.runtime.smart_cluster_min_wallets = 2
        sc = Scanner(cfg)
        sc._sent = []
        async def fake_send(html, reply_to=None):
            sc._sent.append(html)
        sc._send_html = fake_send  # type: ignore
        return sc

    async def test_fires_on_second_distinct_wallet(self):
        sc = self._sc()
        try:
            await sc._check_cluster(_ev(W1))
            self.assertEqual(sc._sent, [])              # 1 wallet -> no alert
            await sc._check_cluster(_ev(W2))
            self.assertEqual(len(sc._sent), 1)          # 2nd distinct -> fire
            self.assertIn("SMART MONEY CLUSTER", sc._sent[0])
            # dedup: a 3rd wallet doesn't re-fire
            await sc._check_cluster(_ev(W3))
            self.assertEqual(len(sc._sent), 1)
        finally:
            sc.storage.close()

    async def test_same_wallet_twice_no_cluster(self):
        sc = self._sc()
        try:
            await sc._check_cluster(_ev(W1))
            await sc._check_cluster(_ev(W1))            # same wallet again
            self.assertEqual(sc._sent, [])
        finally:
            sc.storage.close()

    async def test_disabled(self):
        sc = self._sc()
        sc.cfg.runtime.smart_cluster_enabled = False
        try:
            await sc._check_cluster(_ev(W1))
            await sc._check_cluster(_ev(W2))
            self.assertEqual(sc._sent, [])
        finally:
            sc.storage.close()


class TestClusterFormat(unittest.TestCase):
    def test_format(self):
        html = format_cluster_html("RUNNER", TOKEN, ["Early 1", "Early 6"], "http://c")
        self.assertIn("SMART MONEY CLUSTER", html)
        self.assertIn("Early 1", html)
        self.assertIn(TOKEN, html)


class TestSybilGrouping(unittest.IsolatedAsyncioTestCase):
    def _sc(self) -> Scanner:
        cfg = Config()
        cfg.runtime.db_path = ":memory:"
        cfg.smart_money_wallets = [W1, W2, W3]
        cfg.runtime.smart_cluster_min_wallets = 2
        # W1 and W2 are the same person -> one entity.
        cfg.wallet_watch.wallet_groups = {W1.lower(): "SybilA", W2.lower(): "SybilA"}
        sc = Scanner(cfg)
        sc._sent = []
        async def fake_send(html, reply_to=None):
            sc._sent.append(html)
        sc._send_html = fake_send  # type: ignore
        return sc

    async def test_grouped_pair_does_not_cluster(self):
        sc = self._sc()
        try:
            await sc._check_cluster(_ev(W1))
            await sc._check_cluster(_ev(W2))   # same entity -> still 1
            self.assertEqual(sc._sent, [])
            # a genuinely distinct wallet tips it to 2 entities -> fire
            await sc._check_cluster(_ev(W3))
            self.assertEqual(len(sc._sent), 1)
            self.assertIn("SMART MONEY CLUSTER", sc._sent[0])
        finally:
            sc.storage.close()


if __name__ == "__main__":
    unittest.main()
