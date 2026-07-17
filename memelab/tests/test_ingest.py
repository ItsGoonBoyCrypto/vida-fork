"""Verify the DexScreener pair→TokenSnapshot mapping against the documented schema."""

from __future__ import annotations

import unittest

from memelab.models import Chain
from memelab.ingest.dexscreener import DexScreenerFeed, _pairs

# A representative DexScreener pair object (shape per their public API).
SAMPLE = {
    "chainId": "solana",
    "pairAddress": "PAIR1111111111111111111111111111111111111",
    "baseToken": {"address": "MINT2222222222222222222222222222222222222",
                  "name": "Wow Coin", "symbol": "WOW"},
    "priceUsd": "0.00042",
    "txns": {"m5": {"buys": 45, "sells": 5}, "h1": {"buys": 300, "sells": 120}},
    "volume": {"m5": 2000, "h1": 18000, "h24": 90000},
    "priceChange": {"m5": 12, "h1": 40, "h24": 250},
    "liquidity": {"usd": 15000},
    "fdv": 60000, "marketCap": 48000,
    "pairCreatedAt": 1_700_000_000_000,
    "info": {"socials": [{"type": "telegram", "url": "https://t.me/wow"},
                          {"type": "twitter", "url": "https://x.com/wow"}]},
    "boosts": {"active": 2},
}


class TestMapping(unittest.TestCase):
    def test_maps_all_windows(self):
        snap = DexScreenerFeed._map(SAMPLE, Chain.SOLANA)
        self.assertEqual(snap.chain, Chain.SOLANA)
        self.assertEqual(snap.symbol, "WOW")
        self.assertEqual(snap.token_address, "mint2222222222222222222222222222222222222")
        self.assertAlmostEqual(snap.price_usd, 0.00042)
        self.assertEqual(snap.market_cap_usd, 48000)
        self.assertEqual(snap.liquidity_usd, 15000)
        self.assertEqual(snap.volume_5m, 2000)
        self.assertEqual(snap.buys_5m, 45)
        self.assertEqual(snap.sells_5m, 5)
        self.assertEqual(snap.price_change_1h, 40)
        self.assertTrue(snap.dex_boosted)
        self.assertEqual(snap.socials.get("telegram"), "https://t.me/wow")
        self.assertIsNotNone(snap.age_minutes)      # derived from pairCreatedAt

    def test_missing_fields_are_none_not_crash(self):
        snap = DexScreenerFeed._map({"baseToken": {"address": "X", "symbol": "Y"}}, Chain.BASE)
        self.assertEqual(snap.symbol, "Y")
        self.assertIsNone(snap.price_usd)
        self.assertIsNone(snap.buys_5m)
        self.assertEqual(snap.socials, {})

    def test_pairs_unwrap(self):
        self.assertEqual(_pairs({"pairs": [SAMPLE]}), [SAMPLE])
        self.assertEqual(_pairs([SAMPLE]), [SAMPLE])
        self.assertEqual(_pairs(None), [])


if __name__ == "__main__":
    unittest.main()
