"""Manual winner harvest (/harvest): feed a pumped token we missed."""

from __future__ import annotations

import unittest

from rhl2_scanner.config import Config
from rhl2_scanner.scanner import Scanner


def _sc():
    cfg = Config()
    cfg.runtime.db_path = ":memory:"
    cfg.runtime.smart_money_autoseed_buyers = 5
    cfg.runtime.smart_money_max_set = 500
    return Scanner(cfg)


_CA = "0x3450598e419abb5609f60e4b2fda127ff0897777"


def _fake_info(ca):
    return {
        "ca": ca, "symbol": "GEM", "found": True,
        "metrics": {"liquidity_usd": 12000, "market_cap_usd": 80000,
                    "age_minutes": 22, "holder_count": 140,
                    "top10_supply_pct": 38, "top1_supply_pct": 7,
                    "bundle_supply_pct": 5, "sniper_cluster_pct": None,
                    "dev_holdings_pct": 4, "max_tax_pct": 0},
        "quick_fails": ["liquidity $12,000 < $15,000"],
        "safety_fails": [], "composite": 41,
        "would_gem": False, "would_early": False, "would_alert": False,
    }


class TestManualHarvest(unittest.IsolatedAsyncioTestCase):
    async def test_harvests_buyers_and_records_exemplar(self):
        sc = _sc()

        async def fake_one(ca):
            return _fake_info(ca)

        async def fake_buyers(token, symbol, source_prefix, mult=0.0):
            # simulate harvesting 3 early buyers into the smart set
            for i in range(3):
                sc._add_smart_wallet(f"0x{i:040x}", source=f"{source_prefix}:{token}",
                                     note=f"${symbol}")
            return 3

        sc._calibrate_one = fake_one          # type: ignore
        sc._harvest_buyers = fake_buyers      # type: ignore
        try:
            html = await sc._harvest_manual_winner(_CA)
            self.assertIn("Harvested winner", html)
            self.assertIn("GEM", html)
            self.assertIn("3", html)                      # buyers added
            self.assertIn("What blocked it", html)        # missed → shows blockers
            # exemplar persisted + the 3 harvested wallets are in the smart set
            # (alongside the curated alpha seed).
            self.assertTrue(sc.storage.has_manual_winner(_CA))
            smart = set(sc.storage.smart_wallets())
            for i in range(3):
                self.assertIn(f"0x{i:040x}", smart)
            rows = sc.storage.manual_winners()
            self.assertEqual(rows[0]["symbol"], "GEM")
            self.assertEqual(rows[0]["buyers_added"], 3)
        finally:
            sc.storage.close()

    async def test_command_dispatch_and_dedup(self):
        sc = _sc()
        sent = []

        async def fake_send(html, reply_to=None):
            sent.append(html)

        async def fake_one(ca):
            return _fake_info(ca)

        async def fake_buyers(token, symbol, source_prefix, mult=0.0):
            return 0

        sc._send_html = fake_send             # type: ignore
        sc._calibrate_one = fake_one          # type: ignore
        sc._harvest_buyers = fake_buyers      # type: ignore
        try:
            await sc._handle_command(f"/harvest {_CA}")
            self.assertTrue(any("Harvested winner" in h for h in sent))
            # second time → same token, flagged as refreshed
            await sc._handle_command(f"/winner {_CA}")
            self.assertTrue(any("refreshed" in h for h in sent))
        finally:
            sc.storage.close()

    async def test_usage_when_no_ca(self):
        sc = _sc()
        sent = []

        async def fake_send(html, reply_to=None):
            sent.append(html)
        sc._send_html = fake_send             # type: ignore
        try:
            await sc._handle_command("/harvest")
            self.assertTrue(any("Usage" in h for h in sent))
        finally:
            sc.storage.close()

    async def test_solana_ca_flagged_not_dropped(self):
        sc = _sc()
        sent = []

        async def fake_send(html, reply_to=None):
            sent.append(html)
        sc._send_html = fake_send             # type: ignore
        try:
            # a base58 Solana mint — not 0x; must be flagged, not silently ignored
            await sc._handle_command("/harvest 7EYnhQoR9YM3N7UoaKRoA44Uy8JeaZV3qyouov87awMs")
            self.assertTrue(any("memelab" in h for h in sent), sent)
            self.assertTrue(any("non-Robinhood" in h for h in sent), sent)
        finally:
            sc.storage.close()

    async def test_not_found_on_rh_points_to_memelab(self):
        sc = _sc()

        async def fake_one(ca):
            info = _fake_info(ca)
            info["found"] = False           # e.g. a Base token, not on RH DexScreener
            return info
        sc._calibrate_one = fake_one          # type: ignore
        try:
            html = await sc._harvest_manual_winner("0x" + "d" * 40)
            self.assertIn("memelab", html)
            self.assertIn("Couldn't find", html)
            self.assertFalse(sc.storage.has_manual_winner("0x" + "d" * 40))
        finally:
            sc.storage.close()

    async def test_bundled_winner_skips_buyer_harvest(self):
        sc = _sc()
        harvested = {"called": False}

        async def fake_one(ca):
            info = _fake_info(ca)
            info["metrics"]["bundle_supply_pct"] = 71.2   # heavily bundled (like $FLETCH)
            return info

        async def fake_buyers(token, symbol, source_prefix, mult=0.0):
            harvested["called"] = True
            return 9
        sc._calibrate_one = fake_one          # type: ignore
        sc._harvest_buyers = fake_buyers      # type: ignore
        try:
            html = await sc._harvest_manual_winner(_CA)
            self.assertIn("Skipped buyer harvest", html)
            self.assertFalse(harvested["called"])          # never harvested sybils
            # exemplar still recorded, with 0 buyers added
            self.assertTrue(sc.storage.has_manual_winner(_CA))
            self.assertEqual(sc.storage.manual_winners()[0]["buyers_added"], 0)
        finally:
            sc.storage.close()


if __name__ == "__main__":
    unittest.main()
