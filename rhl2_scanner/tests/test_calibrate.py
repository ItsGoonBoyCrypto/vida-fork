"""Tests for the calibration harness helpers + report/suggestions."""

from __future__ import annotations

import unittest

from rhl2_scanner.config import Config
from rhl2_scanner.scanner import Scanner, _blocker_key, _fmt, _round_nice


class TestHelpers(unittest.TestCase):
    def test_blocker_key_collapses_numeric(self):
        self.assertEqual(_blocker_key("liquidity $3,200 < $5,000"), "liquidity")
        self.assertEqual(_blocker_key("top10 62.0% > skip 60%"), "top10")
        self.assertEqual(_blocker_key("buy tax 3.0% > 1%"), "buy tax")
        self.assertEqual(_blocker_key("mint authority not revoked"),
                         "mint authority not revoked")
        self.assertEqual(_blocker_key("honeypot status unconfirmed"),
                         "honeypot status unconfirmed")

    def test_round_nice(self):
        self.assertEqual(_round_nice(3200), 3200)
        self.assertEqual(_round_nice(41), 40)
        self.assertEqual(_round_nice(7.3), 7.3)
        self.assertEqual(_round_nice(0), 0.0)

    def test_fmt(self):
        self.assertEqual(_fmt(1_500_000, "$"), "$1.50M")
        self.assertEqual(_fmt(3200, "$"), "$3.2k")
        self.assertEqual(_fmt(62, "%"), "62%")
        self.assertEqual(_fmt(45, "m"), "45m")


class TestCalibrateReport(unittest.IsolatedAsyncioTestCase):
    async def test_suggests_lower_floor_and_raise_ceiling(self):
        cfg = Config()
        cfg.runtime.db_path = ":memory:"
        # Tight thresholds so a winner is blocked both ways.
        th = cfg.thresholds
        th.min_liquidity_usd = 20_000
        th.thin_liquidity_usd = 20_000
        th.skip_top10_pct = 40
        sc = Scanner(cfg)

        # Stub _calibrate_one to avoid network: one winner with low liq + high top10.
        async def fake_one(ca):
            return {
                "ca": ca, "symbol": "WIN", "found": True,
                "metrics": {"liquidity_usd": 6000, "market_cap_usd": 80000,
                            "age_minutes": 20, "holder_count": 150,
                            "top10_supply_pct": 55, "top1_supply_pct": 8,
                            "bundle_supply_pct": 4, "sniper_cluster_pct": None,
                            "dev_holdings_pct": 3, "max_tax_pct": 0},
                "quick_fails": ["liquidity $6,000 < $20,000"],
                "safety_fails": ["top10 55.0% > skip 40%"],
                "composite": 30, "would_gem": False, "would_early": False,
                "would_alert": False,
            }
        sc._calibrate_one = fake_one  # type: ignore

        report = await sc.calibrate(["0x" + "a" * 40, "0x" + "b" * 40])
        try:
            self.assertIn("CALIBRATION", report)
            self.assertIn("would alert now: 0/2", report)
            # floor lowered for liquidity, ceiling raised for top10
            self.assertIn("min_liquidity_usd", report)
            self.assertIn("skip_top10_pct", report)
            self.assertIn("liquidity", report)  # blocker aggregation
        finally:
            sc.storage.close()

    async def test_no_change_when_winners_pass(self):
        cfg = Config()
        cfg.runtime.db_path = ":memory:"
        sc = Scanner(cfg)

        async def fake_one(ca):
            return {
                "ca": ca, "symbol": "OK", "found": True,
                "metrics": {"liquidity_usd": 50000, "market_cap_usd": 120000,
                            "age_minutes": 10, "holder_count": 400,
                            "top10_supply_pct": 12, "top1_supply_pct": 3,
                            "bundle_supply_pct": 2, "sniper_cluster_pct": 1,
                            "dev_holdings_pct": 1, "max_tax_pct": 0},
                "quick_fails": [], "safety_fails": [],
                "composite": 82, "would_gem": True, "would_early": False,
                "would_alert": True,
            }
        sc._calibrate_one = fake_one  # type: ignore
        report = await sc.calibrate(["0x" + "c" * 40])
        try:
            self.assertIn("would alert now: 1/1", report)
            self.assertIn("No numeric threshold change needed", report)
        finally:
            sc.storage.close()

    async def test_empty_input(self):
        cfg = Config()
        cfg.runtime.db_path = ":memory:"
        sc = Scanner(cfg)
        try:
            out = await sc.calibrate(["notaca"])
            self.assertIn("give one or more token addresses", out)
        finally:
            sc.storage.close()


if __name__ == "__main__":
    unittest.main()
