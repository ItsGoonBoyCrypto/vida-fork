"""Tests for the out-of-the-gate enhancements: bootstrap prior, richer features,
social links, source-level discovery."""

from __future__ import annotations

import time
import unittest

from memelab.models import Chain, Outcome, Screen, TokenSnapshot, TokenTimeSeries
from memelab.storage import Store
from memelab.bootstrap import default_signature, is_prior
from memelab.metrics.features import extract
from memelab.screener.engine import score_vector
from memelab.alerting import format_screen_html
from memelab.backtest.engine import run_backtest
from memelab.collector import Collector, CollectorConfig
from memelab.chains.evm import EvmAdapter
from memelab.chains.registry import REGISTRY


class TestBootstrapPrior(unittest.TestCase):
    def test_prior_scores_good_over_bad(self):
        sig = default_signature([Chain.SOLANA])
        self.assertTrue(is_prior(sig))
        good = _fv(buy_ratio_5m=0.8, smart_money_count=2, vol5m_to_vol1h=2.0,
                   buy_pressure_trend=0.2, holder_velocity=3.0, liq_growth=1.4,
                   liq_to_mcap=0.2, top10_pct=25, dev_holdings_pct=3,
                   is_sellable=1, lp_safe=1, launchpad_flag=1, has_socials=1)
        bad = _fv(buy_ratio_5m=0.35, smart_money_count=0, vol5m_to_vol1h=0.5,
                  buy_pressure_trend=-0.1, holder_velocity=0.1, liq_growth=0.7,
                  liq_to_mcap=0.02, top10_pct=70, dev_holdings_pct=30,
                  is_sellable=0, lp_safe=0, launchpad_flag=0, has_socials=0)
        gs, _, reasons = score_vector(good, sig)
        bs, _, _ = score_vector(bad, sig)
        self.assertGreater(gs, 70)
        self.assertLess(bs, 25)
        self.assertTrue(reasons)

    def test_collector_seeds_prior_from_day_one(self):
        store = Store(":memory:")
        try:
            col = Collector(CollectorConfig(chains=[Chain.BASE]), store, {}, None)
            self.assertIsNotNone(store.active_signature())   # prior seeded on init
            self.assertTrue(col.screener.ready())            # can screen immediately
        finally:
            store.close()

    def test_backtest_keeps_prior_when_cold(self):
        store = Store(":memory:")
        try:
            Collector(CollectorConfig(chains=[Chain.BASE]), store, {}, None)  # seeds prior
            before = store.active_signature()
            run_backtest(store, chains=[Chain.BASE])          # no data → must not overwrite
            self.assertEqual(store.active_signature(), before)
            self.assertIn("bootstrap prior", before)
        finally:
            store.close()


class TestRicherFeatures(unittest.TestCase):
    def test_new_features_computed(self):
        first = 1_000_000.0
        snaps = [
            TokenSnapshot(chain=Chain.BASE, token_address="0xt", ts=first,
                          price_usd=1.0, liquidity_usd=5000, holder_count=10),
            TokenSnapshot(chain=Chain.BASE, token_address="0xt", ts=first + 600,
                          price_usd=1.4, liquidity_usd=8000, holder_count=40,
                          buys_5m=40, sells_5m=10, buys_1h=200, sells_1h=100),
        ]
        ts = TokenTimeSeries(chain=Chain.BASE, token_address="0xt",
                             first_seen_ts=first, entry_price=1.0, snapshots=snaps)
        fv = extract(ts, early_window_min=30)
        self.assertAlmostEqual(fv.get("liq_growth"), 1.6)              # 8000/5000
        self.assertAlmostEqual(fv.get("holder_velocity"), 3.0)         # 30 holders / 10 min
        self.assertAlmostEqual(fv.get("buy_pressure_trend"), 0.8 - 0.667, places=2)


class TestSocialLinks(unittest.TestCase):
    def test_x_and_tg_in_alert(self):
        snap = TokenSnapshot(chain=Chain.SOLANA, token_address="MINT", symbol="WOW",
                             pair_address="P", socials={"twitter": "https://x.com/wow",
                                                        "telegram": "https://t.me/wow"})
        html = format_screen_html(Screen(snapshot=snap, score=80.0))
        self.assertIn("https://x.com/wow", html)
        self.assertIn("https://t.me/wow", html)
        self.assertIn("TG", html)


class TestEvmDiscoveryDecode(unittest.TestCase):
    def test_token_from_log_picks_non_weth(self):
        cfg = REGISTRY[Chain.ROBINHOOD]
        ad = EvmAdapter(cfg)
        weth = cfg.weth_address.lower()
        tok = "0x" + "a" * 40
        log = {"topics": ["0xtopic",
                          "0x" + "0" * 24 + weth[2:],
                          "0x" + "0" * 24 + tok[2:]]}
        self.assertEqual(ad._token_from_log(log, weth), tok)


class TestPumpfunDiscover(unittest.IsolatedAsyncioTestCase):
    async def test_maps_coins(self):
        from memelab.chains.solana import SolanaAdapter
        ad = SolanaAdapter(REGISTRY[Chain.SOLANA], session=object())

        async def fake_get(url):
            return [{"mint": "MINT1", "symbol": "AAA", "name": "Alpha",
                     "usd_market_cap": 42000, "complete": False,
                     "created_timestamp": int(time.time() * 1000) - 120000,
                     "twitter": "https://x.com/a", "telegram": "https://t.me/a"}]
        ad._get = fake_get  # type: ignore
        out = await ad.discover()
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].token_address, "MINT1")
        self.assertTrue(out[0].on_curve)
        self.assertEqual(out[0].launchpad, "pumpfun")
        self.assertEqual(out[0].socials.get("telegram"), "https://t.me/a")


def _fv(**feats):
    from memelab.models import FeatureVector
    return FeatureVector(chain=Chain.SOLANA, token_address="0x", features=feats)


if __name__ == "__main__":
    unittest.main()
