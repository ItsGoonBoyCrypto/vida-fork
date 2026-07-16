"""emit_alerts toggle: no whale pings, but smart-money data still used."""

from __future__ import annotations

import unittest

from rhl2_scanner.config import Config
from rhl2_scanner.scanner import Scanner
from rhl2_scanner.walletwatch import WhaleEvent

W1 = "0x" + "1" * 40
W2 = "0x" + "2" * 40
TOKEN = "0x" + "a" * 40


class _FakeWatcher:
    def __init__(self, events):
        self._events = events
    async def poll(self):
        return self._events


def _buy(wallet, symbol="RUN") -> WhaleEvent:
    return WhaleEvent(wallet=wallet, label="x", side="buy", token_address=TOKEN,
                      symbol=symbol, amount=1.0, usd=500.0, tx_hash="0xtx" + wallet[-3:])


class TestEmitToggle(unittest.IsolatedAsyncioTestCase):
    def _sc(self, emit: bool) -> Scanner:
        cfg = Config()
        cfg.runtime.db_path = ":memory:"
        cfg.wallet_watch.enabled = True
        cfg.wallet_watch.wallets = [W1, W2]
        cfg.wallet_watch.emit_alerts = emit
        cfg.smart_money_wallets = [W1, W2]
        cfg.runtime.smart_cluster_min_wallets = 2
        sc = Scanner(cfg)
        sc._sent = []
        async def fake_send(html):
            sc._sent.append(html)
        sc._send_html = fake_send  # type: ignore
        return sc

    async def test_silent_but_cluster_fires(self):
        sc = self._sc(emit=False)
        sc._wallet_watcher = _FakeWatcher([_buy(W1), _buy(W2)])
        try:
            await sc._poll_wallets()
            # No individual whale pings...
            self.assertFalse(any("WHALE" in h for h in sc._sent))
            # ...but the buys were recorded and the cluster alert fired.
            self.assertTrue(any("SMART MONEY CLUSTER" in h for h in sc._sent))
            self.assertEqual(set(sc.storage.distinct_smart_buyers(TOKEN, 0)),
                             {W1.lower(), W2.lower()})
        finally:
            sc.storage.close()

    async def test_emit_sends_whale_pings(self):
        sc = self._sc(emit=True)
        sc._wallet_watcher = _FakeWatcher([_buy(W1)])
        try:
            await sc._poll_wallets()
            self.assertTrue(any("WHALE" in h for h in sc._sent))
        finally:
            sc.storage.close()


if __name__ == "__main__":
    unittest.main()
