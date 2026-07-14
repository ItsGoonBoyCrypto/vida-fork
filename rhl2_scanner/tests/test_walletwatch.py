"""Tests for the whale-wallet watcher: buy/sell classify, dedup, USD filter."""

from __future__ import annotations

import unittest

from rhl2_scanner.config import Config
from rhl2_scanner.storage import Storage
from rhl2_scanner.walletwatch import WalletWatcher, format_whale_html

WHALE = "0x" + "a" * 40
POOL = "0x" + "b" * 40
MEME = "0x" + "c" * 40
WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"


class FakeResp:
    def __init__(self, payload):
        self._payload = payload
        self.status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        return self._payload


class FakeSession:
    """Routes explorer tokentx (GET) and DexScreener price (GET) by URL."""

    def __init__(self, transfers, price=None):
        self.transfers = transfers
        self.price = price

    def get(self, url, params=None):
        if "dexscreener" in url or "/latest/dex" in url:
            pairs = []
            if self.price is not None:
                pairs = [{"chainId": "robinhood", "pairAddress": "0xpair",
                          "baseToken": {"address": MEME, "symbol": "GEM"},
                          "priceUsd": str(self.price), "url": "https://dexscreener.com/robinhood/x"}]
            return FakeResp({"pairs": pairs})
        return FakeResp({"status": "1", "result": self.transfers})

    def post(self, url, json=None):
        return FakeResp({"ok": True})

    async def close(self):
        pass


def _cfg(**ww):
    cfg = Config()
    cfg.chain.explorer_api_url = "https://robinhoodchain.blockscout.com/api"
    cfg.chain.weth_address = WETH
    cfg.wallet_watch.enabled = True
    cfg.wallet_watch.wallets = [WHALE]
    for k, v in ww.items():
        setattr(cfg.wallet_watch, k, v)
    return cfg


def _buy_tx(h="0xhash1"):
    return {"hash": h, "from": POOL, "to": WHALE, "contractAddress": MEME,
            "value": str(1000 * 10**18), "tokenDecimal": "18", "tokenSymbol": "GEM"}


def _sell_tx(h="0xhash2"):
    return {"hash": h, "from": WHALE, "to": POOL, "contractAddress": MEME,
            "value": str(500 * 10**18), "tokenDecimal": "18", "tokenSymbol": "GEM"}


class TestWalletWatch(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.storage = Storage(":memory:")

    def tearDown(self):
        self.storage.close()

    async def test_buy_detected_and_priced(self):
        cfg = _cfg(min_usd=100)
        w = WalletWatcher(cfg, self.storage, session=FakeSession([_buy_tx()], price=0.5))
        events = await w.poll()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].side, "buy")
        self.assertEqual(events[0].symbol, "GEM")
        self.assertAlmostEqual(events[0].usd, 500.0, places=1)   # 1000 * 0.5

    async def test_dedup_second_poll_empty(self):
        cfg = _cfg()
        w = WalletWatcher(cfg, self.storage, session=FakeSession([_buy_tx()], price=0.5))
        self.assertEqual(len(await w.poll()), 1)
        self.assertEqual(len(await w.poll()), 0)   # already seen

    async def test_sells_ignored_by_default(self):
        cfg = _cfg(alert_on="buys")
        w = WalletWatcher(cfg, self.storage, session=FakeSession([_sell_tx()], price=0.5))
        self.assertEqual(len(await w.poll()), 0)

    async def test_sells_alert_when_enabled(self):
        cfg = _cfg(alert_on="buys_sells")
        w = WalletWatcher(cfg, self.storage, session=FakeSession([_sell_tx()], price=0.5))
        events = await w.poll()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].side, "sell")

    async def test_min_usd_filters_dust(self):
        cfg = _cfg(min_usd=1000)          # 1000 tokens * $0.5 = $500 < $1000 floor
        w = WalletWatcher(cfg, self.storage, session=FakeSession([_buy_tx()], price=0.5))
        self.assertEqual(len(await w.poll()), 0)

    async def test_weth_transfer_ignored(self):
        cfg = _cfg()
        weth_in = {"hash": "0xh", "from": POOL, "to": WHALE, "contractAddress": WETH,
                   "value": str(10**18), "tokenDecimal": "18", "tokenSymbol": "WETH"}
        w = WalletWatcher(cfg, self.storage, session=FakeSession([weth_in], price=0.5))
        self.assertEqual(len(await w.poll()), 0)

    def test_format_html(self):
        from rhl2_scanner.walletwatch import WhaleEvent
        ev = WhaleEvent(wallet=WHALE, label="Whale A", side="buy", token_address=MEME,
                        symbol="GEM", amount=1000, usd=500.0, tx_hash="0xh",
                        chart_url="https://dexscreener.com/robinhood/x")
        html = format_whale_html(ev)
        self.assertIn("WHALE BOUGHT", html)
        self.assertIn("Whale A", html)
        self.assertIn("$500", html)
        self.assertIn("<a href=", html)


if __name__ == "__main__":
    unittest.main()
