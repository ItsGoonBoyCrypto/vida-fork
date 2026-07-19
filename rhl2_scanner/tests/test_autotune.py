"""Bounded, reversible weight auto-tune."""

from __future__ import annotations

import unittest

from rhl2_scanner.autotune import (
    apply, describe, load_override, propose_weights, reset,
)
from rhl2_scanner.storage import Storage

_BASE = {"safety": 0.40, "distribution": 0.24, "momentum": 0.21, "discovery": 0.15}


def _report(settled, *, mom_edge=True, thin=True):
    """A report where momentum strongly discriminates (high wins, low rugs)."""
    def _bucket(hit, rug, n=8):
        return {"n": n, "hit_rate": hit, "rug_rate": rug, "avg_peak_mult": 2,
                "median_peak_mult": 2, "best_mult": 5}
    hi_good, lo_bad = _bucket(0.8, 0.1), _bucket(0.1, 0.7)
    flat = {"high": _bucket(0.4, 0.4), "low": _bucket(0.4, 0.4)}
    return {
        "total_recorded": settled, "settled": settled,
        "by_signal": {
            "safety": flat, "distribution": flat,
            "momentum": {"high": hi_good, "low": lo_bad} if mom_edge else flat,
            "discovery": flat,
        },
    }


class TestPropose(unittest.TestCase):
    def test_dormant_until_min_settled(self):
        r = propose_weights(_report(10), _BASE, dict(_BASE), min_settled=30)
        self.assertFalse(r["changed"])
        self.assertIsNone(r["weights"])
        self.assertIn("dormant", r["reason"])

    def test_rewards_discriminating_signal(self):
        r = propose_weights(_report(40), _BASE, dict(_BASE), min_settled=30, min_bucket=5)
        self.assertTrue(r["changed"], r)
        w = r["weights"]
        # momentum separated winners from rugs → its weight should rise
        self.assertGreater(w["momentum"], _BASE["momentum"])
        # every weight positive and near-normalised (scorer re-normalises anyway)
        self.assertTrue(all(v > 0 for v in w.values()))
        self.assertAlmostEqual(sum(w.values()), 1.0, places=1)

    def test_respects_max_drift_clamp(self):
        # feed many rounds; no weight may ever leave baseline ± max_drift
        cur = dict(_BASE)
        for _ in range(50):
            r = propose_weights(_report(40), _BASE, cur, min_settled=30,
                                 min_bucket=5, step=0.05, max_drift=0.08)
            if not r["changed"]:
                break
            cur = r["weights"]
        for k, base in _BASE.items():
            self.assertLessEqual(cur[k], base + 0.08 + 1e-9, k)
            self.assertGreaterEqual(cur[k], base - 0.08 - 1e-9, k)

    def test_gated_when_buckets_thin(self):
        rep = _report(40)
        # collapse every bucket below min_bucket
        for cat in rep["by_signal"].values():
            cat["high"]["n"] = cat["low"]["n"] = 2
        r = propose_weights(rep, _BASE, dict(_BASE), min_settled=30, min_bucket=5)
        self.assertFalse(r["changed"])
        self.assertIn("not enough", r["reason"])


class TestPersistence(unittest.TestCase):
    def setUp(self):
        self.s = Storage(":memory:")

    def tearDown(self):
        self.s.close()

    def test_apply_load_reset_roundtrip(self):
        self.assertIsNone(load_override(self.s))
        r = propose_weights(_report(40), _BASE, dict(_BASE), min_settled=30, min_bucket=5)
        self.assertTrue(apply(self.s, r))
        got = load_override(self.s)
        self.assertIsNotNone(got)
        self.assertAlmostEqual(sum(got.values()), 1.0, places=1)
        reset(self.s)
        self.assertIsNone(load_override(self.s))

    def test_apply_noop_when_unchanged(self):
        self.assertFalse(apply(self.s, {"changed": False, "weights": None}))

    def test_describe_baseline_and_tuned(self):
        self.assertIn("baseline", describe(self.s, _BASE))
        apply(self.s, propose_weights(_report(40), _BASE, dict(_BASE),
                                      min_settled=30, min_bucket=5))
        out = describe(self.s, _BASE)
        self.assertIn("momentum", out)


if __name__ == "__main__":
    unittest.main()
