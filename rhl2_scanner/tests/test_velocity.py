"""Tests for short-term velocity signals (5m-vs-1h acceleration).

These are the earliest-catch momentum tells: for a token minutes old the 24h
bucket is empty, so we lean on the 5m rate vs the trailing hour.
"""

from __future__ import annotations

import unittest

from rhl2_scanner.config import Config
from rhl2_scanner.models import SafetyReport, TokenSnapshot
from rhl2_scanner.scoring import score_momentum


def _fresh(**over) -> TokenSnapshot:
    snap = TokenSnapshot(
        chain="robinhood",
        pair_address="0xpair",
        token_address="0xtoken",
        symbol="NEW",
        market_cap_usd=60_000,
        liquidity_usd=20_000,
        age_minutes=8,
        volume_5m=6_000,
        volume_1h=12_000,
        volume_24h=12_000,   # basically all volume is in the last hour (brand new)
        buys_5m=45,
        sells_5m=5,
        buys_1h=120,
        sells_1h=40,
        price_change_5m=18,
        price_change_1h=30,
    )
    snap.safety = SafetyReport(contract_verified=True)
    for k, v in over.items():
        setattr(snap, k, v)
    return snap


class TestVelocityProps(unittest.TestCase):
    def test_volume_velocity_accelerating(self):
        # 5m=6000 -> hourly-equiv 72000; 1h=12000 -> 6.0x
        t = _fresh()
        self.assertAlmostEqual(t.volume_velocity, 6.0, places=3)
        self.assertTrue(t.short_term_accelerating)

    def test_volume_velocity_cooling(self):
        t = _fresh(volume_5m=100, volume_1h=12_000)  # 1200/12000 = 0.1x
        self.assertLess(t.volume_velocity, 1.0)
        self.assertFalse(t.short_term_accelerating)

    def test_velocity_none_when_missing(self):
        self.assertIsNone(_fresh(volume_5m=None).volume_velocity)
        self.assertIsNone(_fresh(volume_1h=0).volume_velocity)  # no divide-by-zero

    def test_buy_ratio_5m(self):
        self.assertAlmostEqual(_fresh().buy_ratio_5m, 45 / 50, places=3)
        self.assertIsNone(_fresh(buys_5m=None).buy_ratio_5m)
        self.assertIsNone(_fresh(buys_5m=0, sells_5m=0).buy_ratio_5m)  # no txns yet

    def test_price_velocity(self):
        # 5m=18 -> hourly-equiv 216; 1h=30 -> 7.2x (fresh leg up)
        self.assertAlmostEqual(_fresh().price_velocity, 7.2, places=3)


class TestVelocityScoring(unittest.TestCase):
    def test_surging_beats_flat(self):
        cfg = Config()
        w = cfg.weights.normalized().momentum
        hot = score_momentum(_fresh(), cfg.thresholds, w)
        flat = score_momentum(
            _fresh(volume_5m=800, buys_5m=10, sells_5m=10, price_change_5m=0),
            cfg.thresholds, w)
        self.assertGreater(hot.raw, flat.raw)
        self.assertTrue(any("surging" in r or "rising" in r for r in hot.reasons))

    def test_cooling_penalty(self):
        cfg = Config()
        w = cfg.weights.normalized().momentum
        cs = score_momentum(_fresh(volume_5m=50, volume_1h=12_000),
                            cfg.thresholds, w)
        self.assertTrue(any("cooling" in p for p in cs.penalties))


if __name__ == "__main__":
    unittest.main()
