"""Demand-quality signal: wash/distribution demotes, real holder growth boosts."""

from __future__ import annotations

import unittest

from rhl2_scanner.demand import demand_signal


class TestDemand(unittest.TestCase):
    def test_wash_distribution_demoted(self):
        # 2x mcap traded in 1h but only 20 holders on a 40-min token → wash
        delta, note = demand_signal(holder_count=20, volume_1h=200_000,
                                    mcap=100_000, age_minutes=40)
        self.assertLess(delta, 0)
        self.assertIn("wash", note.lower())

    def test_flat_holder_growth_with_volume_demoted(self):
        delta, note = demand_signal(holder_count=80, volume_1h=200_000, mcap=100_000,
                                    age_minutes=40, holder_growth_1h=0)
        self.assertLess(delta, 0)

    def test_real_demand_boosted(self):
        delta, note = demand_signal(holder_count=300, volume_1h=50_000, mcap=100_000,
                                    age_minutes=90, holder_growth_1h=60)
        self.assertGreater(delta, 0)
        self.assertIn("demand", note.lower())

    def test_fresh_token_not_penalised(self):
        # 5-min-old token with few holders is normal, not wash
        delta, note = demand_signal(holder_count=10, volume_1h=200_000,
                                    mcap=100_000, age_minutes=5)
        self.assertEqual((delta, note), (0.0, ""))

    def test_thin_inputs_neutral(self):
        self.assertEqual(demand_signal(None, None, None, None), (0.0, ""))
        self.assertEqual(demand_signal(50, 1000, None, 30), (0.0, ""))


if __name__ == "__main__":
    unittest.main()
