"""Closed-loop conviction learning: factors are up/down-weighted by real lift."""

from __future__ import annotations

import unittest

from rhl2_scanner.conviction import fuse
from rhl2_scanner.conviction_learn import apply_multiplier, learn_multipliers


def _rows(good_factor, bad_factor, n=30):
    """good_factor precedes winners; bad_factor precedes rugs."""
    rows = []
    for i in range(n):
        rows.append(([good_factor], 5.0, True))     # active + won
        rows.append(([bad_factor], 1.0, True))       # active + flat/rug
    # base rate ~50% so lifts are clearly separable
    return rows


class TestLearn(unittest.TestCase):
    def test_amplifies_winners_damps_losers(self):
        m = learn_multipliers(_rows("core-alpha", "social"))
        self.assertGreater(m["core-alpha"], 1.0)     # preceded winners → amplified
        self.assertLess(m["social"], 1.0)            # preceded non-winners → damped

    def test_insufficient_data_returns_empty(self):
        self.assertEqual(learn_multipliers([([("core-alpha")], 5.0, True)]), {})

    def test_unsettled_ignored(self):
        rows = [(["core-alpha"], 9.0, False)] * 40   # none settled
        self.assertEqual(learn_multipliers(rows), {})


class TestApply(unittest.TestCase):
    def test_multiplier_scales_points(self):
        self.assertEqual(apply_multiplier("core-alpha", 10.0, {"core-alpha": 1.5}), 15.0)
        self.assertEqual(apply_multiplier("social", 10.0, None), 10.0)
        self.assertEqual(apply_multiplier("social", 10.0, {"core-alpha": 1.5}), 10.0)

    def test_fuse_uses_learned_weights(self):
        signals = {"composite": 60, "safety_passed": True, "core_alpha": 1}
        base = fuse(signals).score
        damped = fuse(signals, learned={"core-alpha": 0.0}).score   # kill the factor
        boosted = fuse(signals, learned={"core-alpha": 1.8}).score
        self.assertLess(damped, base)
        self.assertGreater(boosted, base)


if __name__ == "__main__":
    unittest.main()
