"""Tests for the dashboard data path (rank_live) + the page exists."""

from __future__ import annotations

import os
import time
import unittest

from memelab.models import Chain, TokenSnapshot
from memelab.storage import Store
from memelab.screener.engine import Screener, rank_live
from memelab.collector import Collector, CollectorConfig


class TestRankLive(unittest.TestCase):
    def test_ranks_by_score(self):
        store = Store(":memory:")
        try:
            Collector(CollectorConfig(chains=[Chain.BASE]), store, {}, None)  # seeds prior
            sc = Screener(store); sc.reload_signature()
            now = time.time()
            # a strong token and a weak one, both freshly tracked
            store.record_snapshot(TokenSnapshot(chain=Chain.BASE, token_address="0xWIN",
                ts=now, price_usd=1.0, symbol="WIN", buys_5m=45, sells_5m=5,
                buys_1h=200, sells_1h=100, market_cap_usd=60000, liquidity_usd=15000,
                top10_supply_pct=25, dev_holdings_pct=3, is_honeypot=False,
                smart_money_wallets=["0xa"]))
            store.record_snapshot(TokenSnapshot(chain=Chain.BASE, token_address="0xDUD",
                ts=now, price_usd=1.0, symbol="DUD", buys_5m=5, sells_5m=25,
                buys_1h=50, sells_1h=200, market_cap_usd=60000, liquidity_usd=15000,
                top10_supply_pct=70, dev_holdings_pct=30))
            ranked = rank_live(store, sc, min_score=0, limit=10)
            self.assertGreaterEqual(len(ranked), 2)
            self.assertEqual(ranked[0]["symbol"], "WIN")             # strongest first
            self.assertGreater(ranked[0]["score"], ranked[-1]["score"])
        finally:
            store.close()

    def test_empty_when_no_signature(self):
        store = Store(":memory:")
        try:
            sc = Screener(store)                    # no signature loaded
            self.assertEqual(rank_live(store, sc), [])
        finally:
            store.close()


class TestDashboardAsset(unittest.TestCase):
    def test_dashboard_html_present(self):
        here = os.path.join(os.path.dirname(__file__), "..", "api", "dashboard.html")
        self.assertTrue(os.path.exists(here))
        with open(here, encoding="utf-8") as fh:
            html = fh.read()
        self.assertIn("memelab", html)
        self.assertIn("j('screen", html)          # same-origin (relative) fetch
        self.assertIn("j('stats')", html)

    def test_dashboard_has_harvest_ui(self):
        here = os.path.join(os.path.dirname(__file__), "..", "api", "dashboard.html")
        with open(here, encoding="utf-8") as fh:
            html = fh.read()
        self.assertIn("Harvest a winner", html)
        self.assertIn("harvestWinner", html)
        self.assertIn("fetch('harvest'", html)     # posts to the /harvest endpoint


class TestApiRoutes(unittest.TestCase):
    def test_harvest_route_registered(self):
        try:
            from memelab.api.app import create_app
        except Exception as exc:  # pragma: no cover - fastapi always present in CI
            self.skipTest(f"fastapi unavailable: {exc}")
        app = create_app(":memory:")
        paths = {getattr(r, "path", None) for r in app.routes}
        self.assertIn("/harvest", paths)
        self.assertIn("/smart-wallets", paths)


if __name__ == "__main__":
    unittest.main()
