"""Graduation-imminent boost: a near-graduation curve token reaches the band."""

from __future__ import annotations

import unittest

from rhl2_scanner.config import Config
from rhl2_scanner.models import AlertLevel
from rhl2_scanner.models import TokenSnapshot
from rhl2_scanner.scoring import _graduation_bonus, score_token


class TestGraduationBoost(unittest.TestCase):
    def _cfg(self):
        return Config.load("rhl2_scanner/config/robinhood.example.yaml")

    def test_bonus_shape(self):
        th = self._cfg().thresholds
        base = dict(chain="robinhood", pair_address="", token_address="0xt")
        # off-curve → no bonus
        self.assertEqual(_graduation_bonus(TokenSnapshot(**base), th), 0.0)
        # below sweet spot → no bonus
        self.assertEqual(
            _graduation_bonus(TokenSnapshot(curve_progress_pct=40.0, **base), th), 0.0)
        # in sweet spot → full points
        self.assertEqual(
            _graduation_bonus(TokenSnapshot(curve_progress_pct=70.0, **base), th),
            th.grad_boost_points)
        # essentially graduating → half
        self.assertEqual(
            _graduation_bonus(TokenSnapshot(curve_progress_pct=97.0, **base), th),
            th.grad_boost_points * 0.5)

    def test_imminent_token_lifted_into_band(self):
        cfg = self._cfg()
        th = cfg.thresholds
        # A safe pre-graduation flap token in the sweet spot with thin momentum
        # data: the boost should push its composite above the same token at 30%.
        common = dict(chain="robinhood", pair_address="", token_address="0xt",
                      launchpad="flap", market_cap_usd=25000.0, liquidity_usd=9000.0,
                      holder_count=80, price_usd=3e-5, price_native=9e-9)
        early = score_token(TokenSnapshot(curve_progress_pct=30.0, **common), cfg,
                            strict_safety=False, pragmatic=True)
        imminent = score_token(TokenSnapshot(curve_progress_pct=75.0, **common), cfg,
                               strict_safety=False, pragmatic=True)
        self.assertGreater(imminent.composite, early.composite)
        # the boost is at least the configured points minus the discovery delta noise
        self.assertGreaterEqual(imminent.composite - early.composite,
                                th.grad_boost_points - 1)


if __name__ == "__main__":
    unittest.main()
