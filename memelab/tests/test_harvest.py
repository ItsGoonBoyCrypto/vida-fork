"""Manual winner harvest (memelab counterpart to the RH scanner's /harvest)."""

from __future__ import annotations

import unittest

import memelab.ingest.dexscreener as dex_mod
import memelab.smartmoney as sm_mod
from memelab.harvest import harvest_manual_winner
from memelab.models import Chain, TokenSnapshot
from memelab.storage import Store

TOKEN = "So11111111111111111111111111111111111111112"


class _FakeFeed:
    snap = None

    def __init__(self, session=None):
        pass

    async def market_for(self, chain, token):
        return _FakeFeed.snap


class _FakeSmart:
    n = 3

    def __init__(self, store, session=None):
        self.store = store

    async def harvest_winner(self, chain, token):
        for i in range(_FakeSmart.n):
            self.store.add_smart_wallet(chain, f"0x{i:040x}", source=f"winner:{token}")
        return _FakeSmart.n


class TestHarvest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._feed, self._smart = dex_mod.DexScreenerFeed, sm_mod.SmartMoney
        dex_mod.DexScreenerFeed = _FakeFeed
        sm_mod.SmartMoney = _FakeSmart
        _FakeFeed.snap = TokenSnapshot(
            chain=Chain.SOLANA, token_address=TOKEN, symbol="GEM",
            market_cap_usd=90000, liquidity_usd=12000, bundle_supply_pct=8.0)
        _FakeSmart.n = 3

    def tearDown(self):
        dex_mod.DexScreenerFeed = self._feed
        sm_mod.SmartMoney = self._smart

    async def test_harvests_and_records_exemplar(self):
        s = Store(":memory:")
        try:
            r = await harvest_manual_winner(s, Chain.SOLANA, TOKEN.upper(), session=object())
            self.assertTrue(r["ok"])
            self.assertEqual(r["symbol"], "GEM")
            self.assertEqual(r["added"], 3)
            self.assertFalse(r["bundled_out"])
            self.assertEqual(s.smart_wallet_count(Chain.SOLANA), 3)
            self.assertTrue(s.has_manual_winner(Chain.SOLANA, TOKEN))
            rows = s.manual_winners()
            self.assertEqual(rows[0]["symbol"], "GEM")
            self.assertEqual(rows[0]["buyers_added"], 3)
        finally:
            s.close()

    async def test_bundled_winner_skips_buyer_harvest(self):
        _FakeFeed.snap.bundle_supply_pct = 71.2      # heavily bundled → sybil risk
        s = Store(":memory:")
        try:
            r = await harvest_manual_winner(s, Chain.SOLANA, TOKEN, session=object())
            self.assertTrue(r["bundled_out"])
            self.assertEqual(r["added"], 0)
            self.assertEqual(s.smart_wallet_count(Chain.SOLANA), 0)  # no sybils added
            self.assertTrue(s.has_manual_winner(Chain.SOLANA, TOKEN))  # exemplar still kept
        finally:
            s.close()

    async def test_not_found_off_dex(self):
        _FakeFeed.snap = None                        # DexScreener has nothing
        s = Store(":memory:")
        try:
            r = await harvest_manual_winner(s, Chain.BASE, "0x" + "d" * 40, session=object())
            self.assertFalse(r["found"])
            self.assertEqual(r["symbol"], "?")
            # still attempts the buyer harvest (buyer source is independent of DEX)
            self.assertEqual(r["added"], 3)
        finally:
            s.close()

    async def test_chain_scoped_smart_set(self):
        s = Store(":memory:")
        try:
            await harvest_manual_winner(s, Chain.SOLANA, TOKEN, session=object())
            # wallets land under SOLANA, not other chains
            self.assertEqual(s.smart_wallet_count(Chain.SOLANA), 3)
            self.assertEqual(s.smart_wallet_count(Chain.BASE), 0)
        finally:
            s.close()


if __name__ == "__main__":
    unittest.main()
