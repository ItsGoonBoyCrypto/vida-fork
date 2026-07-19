"""Unified feed: memelab alerts only on its chains (scanner owns robinhood)."""

from __future__ import annotations

import time
import unittest

from memelab.collector import Collector, CollectorConfig
from memelab.models import Chain, TokenSnapshot
from memelab.storage import Store


class _Alerter:
    enabled = True

    def __init__(self):
        self.sent = []

    async def send(self, html, reply_markup=None):
        self.sent.append(html)


class TestAlertChainGating(unittest.IsolatedAsyncioTestCase):
    def _col(self, alert_chains):
        store = Store(":memory:")
        alerter = _Alerter()
        col = Collector(CollectorConfig(chains=list(Chain), alert_chains=alert_chains),
                        store, {}, None, alerter=alerter)
        return store, col, alerter

    async def _seed(self, store, chain, token):
        # a token with a core-alpha buyer so _maybe_alert has something to fire
        for i in range(3):
            store.record_wallet_winner(chain, "0xsharp", f"0xw{i}", 5.0)
        snap = TokenSnapshot(chain=chain, token_address=token, ts=time.time(),
                             price_usd=1.0, symbol="X", core_alpha_buyer="0xsharp…")
        store.record_snapshot(snap)

    async def test_robinhood_suppressed_when_scanner_owns_it(self):
        store, col, alerter = self._col([Chain.SOLANA, Chain.ETHEREUM, Chain.BASE])
        try:
            await self._seed(store, Chain.ROBINHOOD, "0xrh")
            await col._maybe_alert(Chain.ROBINHOOD, "0xrh")
            self.assertEqual(alerter.sent, [])          # scanner's job, not memelab's
        finally:
            store.close()

    async def test_other_chain_alerts(self):
        store, col, alerter = self._col([Chain.SOLANA, Chain.ETHEREUM, Chain.BASE])
        try:
            await self._seed(store, Chain.BASE, "0xbase")
            await col._maybe_alert(Chain.BASE, "0xbase")
            self.assertTrue(any("CORE ALPHA" in h for h in alerter.sent), alerter.sent)
        finally:
            store.close()

    async def test_empty_alert_chains_allows_all(self):
        store, col, alerter = self._col([])           # standalone: alert everything
        try:
            await self._seed(store, Chain.ROBINHOOD, "0xrh")
            await col._maybe_alert(Chain.ROBINHOOD, "0xrh")
            self.assertTrue(alerter.sent)
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
