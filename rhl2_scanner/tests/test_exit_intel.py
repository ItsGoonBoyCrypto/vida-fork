"""Learned take-profit / top-zone exit intelligence."""

from __future__ import annotations

import unittest

from rhl2_scanner.exit_intel import learn_exit_model, top_zone


def _rows(peaks, settled=True):
    return [{"max_mult": p, "settled": 1 if settled else 0} for p in peaks]


class TestExitModel(unittest.TestCase):
    def test_dormant_until_enough_winners(self):
        self.assertIsNone(learn_exit_model(_rows([3, 4, 5]), min_winners=8))

    def test_learns_peak_percentiles(self):
        peaks = [2, 2, 3, 3, 4, 5, 6, 8, 10, 12]      # 10 winners
        m = learn_exit_model(_rows(peaks), win_multiple=2.0, min_winners=8)
        self.assertIsNotNone(m)
        self.assertEqual(m["n"], 10)
        self.assertGreater(m["p75"], m["p50"])

    def test_ignores_non_winners_and_unsettled(self):
        rows = _rows([3, 4, 5, 6, 7, 8, 9, 10]) + _rows([1.1, 1.2], ) \
            + _rows([20], settled=False)
        m = learn_exit_model(rows, win_multiple=2.0, min_winners=8)
        self.assertEqual(m["n"], 8)      # only the 8 settled winners


class TestTopZone(unittest.TestCase):
    def setUp(self):
        self.model = {"p50": 4.0, "p75": 7.0, "n": 20}

    def test_fires_in_zone_and_turning(self):
        # at 5x (past p50), fell 20% from a 6.25x peak → fire
        fire, reason = top_zone(current_mult=5.0, peak_mult=6.25, model=self.model,
                                early_giveback_pct=15.0)
        self.assertTrue(fire)
        self.assertIn("top zone", reason)

    def test_no_fire_below_zone(self):
        fire, _ = top_zone(current_mult=3.0, peak_mult=3.5, model=self.model)
        self.assertFalse(fire)

    def test_no_fire_still_climbing(self):
        # at 5x but that IS the peak (no give-back) → not turning yet
        fire, _ = top_zone(current_mult=5.0, peak_mult=5.0, model=self.model,
                           early_giveback_pct=15.0)
        self.assertFalse(fire)

    def test_dormant_without_model(self):
        self.assertEqual(top_zone(5.0, 6.0, None), (False, ""))


if __name__ == "__main__":
    unittest.main()
