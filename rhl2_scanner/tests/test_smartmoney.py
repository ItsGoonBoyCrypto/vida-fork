"""Tests for smart-money seeding: storage, commands, harvest, buyer parsing."""

from __future__ import annotations

import unittest

from rhl2_scanner.config import Config
from rhl2_scanner.models import TokenSnapshot
from rhl2_scanner.scanner import Scanner
from rhl2_scanner.storage import Storage
from rhl2_scanner.sources.smartmoney import SmartMoneyClient, _infra_addresses

W1 = "0x1111111111111111111111111111111111111111"
W2 = "0x2222222222222222222222222222222222222222"
TOKEN = "0x3333333333333333333333333333333333333333"


class TestSmartWalletStorage(unittest.TestCase):
    def test_add_remove_list(self):
        s = Storage(":memory:")
        try:
            self.assertTrue(s.add_smart_wallet(W1, source="manual"))
            self.assertFalse(s.add_smart_wallet(W1))          # dupe -> False
            s.add_smart_wallet(W2, source="auto:0xabc", note="$WIN")
            self.assertEqual(set(s.smart_wallets()), {W1, W2})
            rows = {r["wallet"]: r["source"] for r in s.smart_wallets_detailed()}
            self.assertEqual(rows[W2], "auto:0xabc")
            s.remove_smart_wallet(W1)
            self.assertEqual(s.smart_wallets(), [W2])
        finally:
            s.close()


class TestInfraExclusion(unittest.TestCase):
    def test_infra_includes_flap_and_router(self):
        cfg = Config()
        cfg.chain.dex_router_address = "0xR"
        cfg._launchpad("flap")["manager"] = W1
        infra = _infra_addresses(cfg)
        self.assertIn(W1.lower(), infra)                       # flap manager
        self.assertIn("0x000000000000000000000000000000000000dead", infra)
        self.assertNotIn("", infra)


class TestBuyerParsing(unittest.IsolatedAsyncioTestCase):
    async def test_v2_receivers_and_early_buyers(self):
        cfg = Config()
        client = SmartMoneyClient(cfg)

        # newest-first v2 feed: [W2 (newest), infra-router, W1 (oldest)]
        payload = {"items": [
            {"to": {"hash": W2}},
            {"to": {"hash": "0x000000000000000000000000000000000000dEaD"}},
            {"to": {"hash": W1}},
        ], "next_page_params": None}

        async def fake_get(url, params=None):
            return payload if "/transfers" in url else None
        client._get = fake_get  # type: ignore

        recv = await client._v2_receivers(TOKEN)
        self.assertEqual([r for r in recv], [W2.lower(),
                         "0x000000000000000000000000000000000000dead", W1.lower()])

        # early_buyers reverses (earliest first) and drops the dead/infra addr
        early = await client.early_buyers(TOKEN, n=5)
        self.assertEqual(early, [W1.lower(), W2.lower()])

    async def test_active_wallets_matches_curated(self):
        cfg = Config()
        cfg.smart_money_wallets = [W1]
        client = SmartMoneyClient(cfg)

        async def fake_get(url, params=None):
            return {"items": [{"to": {"hash": W1}}, {"to": {"hash": W2}}],
                    "next_page_params": None}
        client._get = fake_get  # type: ignore

        snap = TokenSnapshot(chain="robinhood", pair_address="0xp", token_address=TOKEN)
        found = await client.active_wallets(snap)
        self.assertEqual(found, [W1.lower()])
        self.assertEqual(snap.smart_money_wallets, [W1.lower()])


class TestSmartCommands(unittest.IsolatedAsyncioTestCase):
    def _scanner(self) -> Scanner:
        cfg = Config()
        cfg.runtime.db_path = ":memory:"
        return Scanner(cfg)

    async def test_smart_add_remove(self):
        sc = self._scanner()
        try:
            await sc._handle_command(f"/smart {W1}")
            self.assertIn(W1.lower(), sc.storage.smart_wallets())
            self.assertIn(W1.lower(), sc.cfg.smart_money_wallets)
            await sc._handle_command(f"/unsmart {W1}")
            self.assertNotIn(W1.lower(), sc.storage.smart_wallets())
            self.assertNotIn(W1.lower(), sc.cfg.smart_money_wallets)
            await sc._handle_command("/smart notawallet")  # no crash, no add
            self.assertEqual(sc.storage.smart_wallets(), [])
        finally:
            sc.storage.close()

    async def test_persisted_merge_on_construct(self):
        # Seed a shared DB, then a new Scanner should pick them up.
        import tempfile, os
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            s = Storage(path)
            s.add_smart_wallet(W2, source="manual")
            s.close()
            cfg = Config()
            cfg.runtime.db_path = path
            sc = Scanner(cfg)
            try:
                self.assertIn(W2.lower(), sc.cfg.smart_money_wallets)
            finally:
                sc.storage.close()
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
