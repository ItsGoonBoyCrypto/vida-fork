"""Fresh-token fixes: sniper-cluster age gate + safety baseline on data-poor chains."""

from __future__ import annotations

import unittest

from rhl2_scanner.config import Config
from rhl2_scanner.filters import safety_gate
from rhl2_scanner.models import SafetyReport, TokenSnapshot
from rhl2_scanner.scoring import score_safety


def _snap(age, sniper=None):
    s = TokenSnapshot(chain="robinhood", pair_address="0xp", token_address="0xt",
                      age_minutes=age)
    s.safety = SafetyReport(sniper_cluster_pct=sniper)
    return s


class TestSniperAgeGate(unittest.TestCase):
    def setUp(self):
        self.th = Config.load("rhl2_scanner/config/robinhood.example.yaml").thresholds

    def test_fresh_token_not_skipped_on_sniper(self):
        # 5-min-old token pinned at 100% sniper → must NOT fail (metric meaningless)
        g = safety_gate(_snap(5, sniper=100.0), self.th, strict=False, pragmatic=True)
        self.assertNotIn("sniper", " ".join(g.failures).lower())

    def test_matured_token_still_gated_on_sniper(self):
        # 120-min-old token at 100% sniper → real red flag, still fails
        g = safety_gate(_snap(120, sniper=100.0), self.th, strict=False, pragmatic=True)
        self.assertTrue(any("sniper cluster" in f for f in g.failures))

    def test_unknown_age_not_gated_on_sniper(self):
        g = safety_gate(_snap(None, sniper=100.0), self.th, strict=False, pragmatic=True)
        self.assertNotIn("sniper", " ".join(g.failures).lower())


class TestSafetyBaseline(unittest.TestCase):
    def test_baseline_lifts_dataless_token_off_zero(self):
        cfg = Config.load("rhl2_scanner/config/robinhood.example.yaml")
        th = cfg.thresholds
        w = cfg.weights.normalized().safety
        # a token with NO confirmable safety facts (typical RH) — used to score 0
        snap = TokenSnapshot(chain="robinhood", pair_address="0xp", token_address="0xt")
        cs = score_safety(snap, th, w)
        self.assertGreaterEqual(cs.raw, th.safety_baseline)
        self.assertGreater(cs.raw, 0.0)   # no longer structurally zero

    def test_zero_baseline_preserves_old_behaviour(self):
        cfg = Config()               # defaults: safety_baseline = 0
        th = cfg.thresholds
        w = cfg.weights.normalized().safety
        snap = TokenSnapshot(chain="x", pair_address="0xp", token_address="0xt")
        self.assertEqual(score_safety(snap, th, w).raw, 0.0)


if __name__ == "__main__":
    unittest.main()
