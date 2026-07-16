"""Tests for address-env validation + wallet research parsing."""

from __future__ import annotations

import os
import unittest

from rhl2_scanner.config import Config, _looks_like_address
from rhl2_scanner.models import TokenSnapshot
from rhl2_scanner.sources.smartmoney import SmartMoneyClient

GOOD = "0x1f7d7550B1b028f7571E69A784071F0205FD2EfA"


class TestLooksLikeAddress(unittest.TestCase):
    def test_valid_and_invalid(self):
        self.assertTrue(_looks_like_address(GOOD))
        self.assertTrue(_looks_like_address(GOOD.lower()))
        self.assertFalse(_looks_like_address("Uniswap V3 Factory"))  # the real bug
        self.assertFalse(_looks_like_address("0x1234"))              # too short
        self.assertFalse(_looks_like_address("1f7d7550B1b028f7571E69A784071F0205FD2EfA"))
        self.assertFalse(_looks_like_address(""))
        self.assertFalse(_looks_like_address("0x" + "z" * 40))       # non-hex


class TestEnvRejectsLabel(unittest.TestCase):
    def test_label_ignored_keeps_yaml(self):
        os.environ["RHL2_DEX_FACTORY"] = "Uniswap V3 Factory"
        try:
            cfg = Config.load("rhl2_scanner/config/robinhood.example.yaml")
            # bad env ignored -> YAML's real 0x factory preserved
            self.assertTrue(_looks_like_address(cfg.chain.dex_factory_address))
        finally:
            del os.environ["RHL2_DEX_FACTORY"]

    def test_good_env_applied(self):
        os.environ["RHL2_DEX_FACTORY"] = GOOD
        try:
            cfg = Config.load(None)
            self.assertEqual(cfg.chain.dex_factory_address, GOOD)
        finally:
            del os.environ["RHL2_DEX_FACTORY"]


class TestSeededWallets(unittest.TestCase):
    def test_early_wallets_seeded(self):
        cfg = Config.load("rhl2_scanner/config/robinhood.example.yaml")
        # All seeded wallets are valid, unique, and mirrored into the watch list.
        sm = cfg.smart_money_wallets
        self.assertGreaterEqual(len(sm), 14)
        self.assertEqual(len(sm), len(set(sm)))               # no dupes
        self.assertTrue(all(_looks_like_address(a) for a in sm))
        self.assertIn("0xae6ad7c09668c8c6b2838e0c92b28fb2db891ff7", sm)
        self.assertIn("0x16f5ef133d0d15b196a778d22bf1ec56f8f37c05", sm)
        self.assertEqual(set(sm), set(cfg.wallet_watch.wallets))


class TestWalletAcquisitions(unittest.IsolatedAsyncioTestCase):
    async def test_parses_incoming_only(self):
        cfg = Config()
        client = SmartMoneyClient(cfg)
        wallet = "0xaaaa000000000000000000000000000000000001"
        other = "0xbbbb000000000000000000000000000000000002"
        payload = {"items": [
            {"to": {"hash": wallet}, "token": {"address": "0xTok1", "symbol": "CAT"},
             "timestamp": "2026-07-01T00:00:00Z"},
            {"to": {"hash": other}, "token": {"address": "0xTok2", "symbol": "NOPE"},
             "timestamp": "2026-07-01T00:00:00Z"},   # not to our wallet -> excluded
        ], "next_page_params": None}

        async def fake_get(url, params=None):
            return payload if "token-transfers" in url else None
        client._get = fake_get  # type: ignore

        acqs = await client.wallet_acquisitions(wallet)
        syms = {a["symbol"] for a in acqs}
        self.assertEqual(syms, {"CAT"})


if __name__ == "__main__":
    unittest.main()
