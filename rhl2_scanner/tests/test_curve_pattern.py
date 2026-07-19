"""Pre-migration curve pattern: features, bootstrap match, learning, tracking."""

from __future__ import annotations

import unittest

from rhl2_scanner.config import Config
from rhl2_scanner.curve_pattern import (
    bootstrap_profile,
    extract_features,
    is_confirmed_climb,
    learn_profile,
    match as curve_match,
)
from rhl2_scanner.models import TokenSnapshot
from rhl2_scanner.scanner import Scanner


def _snap(**kw):
    base = dict(chain="robinhood", pair_address="", token_address="0xt",
                launchpad="flap", curve_progress_pct=55.0, market_cap_usd=30000.0,
                liquidity_usd=9000.0, price_usd=3.3e-5, price_native=9.4e-9,
                holder_count=60, symbol="GEM")
    base.update(kw)
    return TokenSnapshot(**base)


class _Obs(dict):
    """Row-like mapping for a prior observation."""


class TestCurvePatternUnit(unittest.TestCase):
    def test_velocity_from_prior(self):
        snap = _snap(curve_progress_pct=60.0, holder_count=80)
        prev = _Obs(ts=0.0, progress=40.0, reserve_eth=1.0, holders=50)
        feats = extract_features(snap, prev, now_ts=3600.0)  # 1 hour later
        self.assertAlmostEqual(feats["progress_velocity"], 20.0, places=1)  # +20%/h
        self.assertAlmostEqual(feats["holder_velocity"], 30.0, places=1)    # +30/h
        self.assertTrue(is_confirmed_climb(feats))

    def test_no_prior_zero_velocity(self):
        feats = extract_features(_snap(), None, now_ts=100.0)
        self.assertEqual(feats["progress_velocity"], 0.0)
        self.assertFalse(is_confirmed_climb(feats))

    def test_bootstrap_matches_prime_setup(self):
        snap = _snap(curve_progress_pct=55.0, holder_count=80)
        prev = _Obs(ts=0.0, progress=40.0, reserve_eth=1.0, holders=50)
        feats = extract_features(snap, prev, now_ts=3600.0)
        matched, score, hits, misses = curve_match(feats, bootstrap_profile())
        self.assertTrue(matched, (score, misses))
        self.assertGreaterEqual(score, 70.0)

    def test_bootstrap_rejects_dead_curve(self):
        # parked at same progress, no holder growth → not climbing, misses velocity
        snap = _snap(curve_progress_pct=55.0, holder_count=50)
        prev = _Obs(ts=0.0, progress=55.0, reserve_eth=1.0, holders=50)
        feats = extract_features(snap, prev, now_ts=3600.0)
        matched, score, _h, _m = curve_match(feats, bootstrap_profile())
        self.assertFalse(matched and is_confirmed_climb(feats))

    def test_learn_needs_enough_winners(self):
        setups = [{"win": True, "features": {"progress": 60, "progress_velocity": 10,
                   "reserve_eth": 2, "reserve_velocity": 0.1, "mcap_usd": 30000,
                   "holders": 80, "holder_velocity": 20, "smart_count": 1}}
                  for _ in range(3)]
        self.assertIsNone(learn_profile(setups, min_winners=5))

    def test_learn_derives_bands_from_winners(self):
        setups = []
        for i in range(8):
            setups.append({"win": True, "features": {
                "progress": 50 + i, "progress_velocity": 8 + i, "reserve_eth": 2.0,
                "reserve_velocity": 0.1, "mcap_usd": 25000 + i * 1000,
                "holders": 60 + i, "holder_velocity": 15 + i, "smart_count": 1.0}})
        prof = learn_profile(setups, min_winners=5)
        self.assertIsNotNone(prof)
        # velocity features become lower-bound-only bands
        self.assertIn("progress_velocity", prof)
        lo, hi, _w = prof["progress_velocity"]
        self.assertIsNone(hi)
        self.assertGreater(lo, 0)


class TestCurveTrackingIntegration(unittest.IsolatedAsyncioTestCase):
    def _sc(self):
        cfg = Config.load("rhl2_scanner/config/robinhood.example.yaml")
        cfg.runtime.db_path = ":memory:"
        cfg.runtime.dry_run = False
        cfg.runtime.curve_obs_interval_seconds = 0  # always persist in the test
        return Scanner(cfg)

    async def test_records_observation_and_labels_winner(self):
        sc = self._sc()
        sent = []
        async def _fake_send(html, reply_to=None):
            sent.append(html)
        sc._send_html = _fake_send  # type: ignore
        try:
            # first sighting → observation recorded, no alert (no climb baseline)
            await sc._track_curve(_snap(price_usd=1.0, curve_progress_pct=40.0,
                                        holder_count=50))
            self.assertEqual(sc.storage.curve_observation_count("0xt"), 1)

            # second sighting later, climbing → matches + alerts
            import rhl2_scanner.scanner as scan_mod
            # backdate the first observation so velocity is over ~1h
            sc.storage._conn.execute(
                "UPDATE curve_observations SET ts = ts - 3600 WHERE token = '0xt'")
            sc.storage._conn.commit()
            await sc._track_curve(_snap(price_usd=2.0, curve_progress_pct=70.0,
                                        holder_count=95))
            self.assertTrue(any("CURVE MATCH" in h for h in sent), sent)

            # label it as a >=3x winner (entry 1.0 on curve, peak 4.0 now)
            sc._label_curve_setup("0xt", entry_price=1.0, peak_now=4.0, win_mult=3.0)
            wins, total = sc.storage.curve_setup_counts()
            self.assertEqual((wins, total), (1, 1))
        finally:
            sc.storage.close()

    async def test_non_flap_token_not_tracked(self):
        sc = self._sc()
        try:
            await sc._track_curve(_snap(launchpad="", curve_progress_pct=None))
            self.assertEqual(sc.storage.curve_observation_count("0xt"), 0)
        finally:
            sc.storage.close()

    def test_curvepattern_report(self):
        sc = self._sc()
        try:
            out = sc.curvepattern_report()
            self.assertIn("CURVE PATTERN", out)
            self.assertIn("bootstrap prior", out)  # no winners yet
        finally:
            sc.storage.close()


if __name__ == "__main__":
    unittest.main()
