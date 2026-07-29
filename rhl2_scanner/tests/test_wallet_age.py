"""First-buyer wallet-age: bot launches (all-fresh buyers) demote; aged boost."""

from __future__ import annotations

import unittest

from rhl2_scanner.wallet_age import age_verdict, fresh_buyer_ratio

NOW = 1_000_000_000.0
HOUR = 3600.0
DAY = 86400.0


class TestRatio(unittest.TestCase):
    def test_needs_min_samples(self):
        self.assertIsNone(fresh_buyer_ratio({"a": NOW - HOUR, "b": NOW - HOUR}, NOW))

    def test_fresh_and_aged_fractions(self):
        first = {
            "a": NOW - 2 * HOUR,    # fresh
            "b": NOW - 5 * HOUR,    # fresh
            "c": NOW - 30 * DAY,    # aged
            "d": NOW - 20 * DAY,    # aged
        }
        r = fresh_buyer_ratio(first, NOW)
        self.assertEqual(r["n"], 4)
        self.assertEqual(r["fresh_pct"], 50.0)
        self.assertEqual(r["aged_pct"], 50.0)


class TestVerdict(unittest.TestCase):
    def test_all_fresh_is_demoted(self):
        r = {"fresh_pct": 100.0, "aged_pct": 0.0, "n": 5}
        delta, note = age_verdict(r)
        self.assertLess(delta, 0)
        self.assertIn("fresh", note.lower())

    def test_aged_is_boosted(self):
        r = {"fresh_pct": 10.0, "aged_pct": 60.0, "n": 5}
        delta, note = age_verdict(r)
        self.assertGreater(delta, 0)

    def test_mixed_is_neutral(self):
        r = {"fresh_pct": 40.0, "aged_pct": 20.0, "n": 5}
        self.assertEqual(age_verdict(r), (0.0, ""))

    def test_none_is_neutral(self):
        self.assertEqual(age_verdict(None), (0.0, ""))


if __name__ == "__main__":
    unittest.main()
