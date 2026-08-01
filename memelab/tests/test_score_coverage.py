"""Screener scores over EVALUABLE rules, not all rules — fresh tokens aren't
penalised for time-series features that can't exist yet."""

from __future__ import annotations

import unittest

from memelab.bootstrap import default_signature
from memelab.models import Chain, FeatureVector
from memelab.screener.engine import score_vector


def _fv(feats):
    return FeatureVector(chain=Chain.SOLANA, token_address="0xt", features=feats)


class TestCoverageScoring(unittest.TestCase):
    def setUp(self):
        self.sig = default_signature([Chain.SOLANA])

    def test_static_token_scores_moderate_not_certain(self):
        # A fresh token: only the static rules are measurable (no velocity yet).
        # It matches the ones it CAN, so it's PROMISING (well above the old ~40
        # all-rules cap) but NOT a confident 100 — confidence scaling reserves the
        # top scores for tokens with real coverage (time-series confirmation).
        feats = {
            "buy_ratio_5m": 0.7,        # ≥0.62 ✓
            "smart_money_count": 0,     # <1 ✗ (real miss — always present)
            "liq_to_mcap": 0.1,         # ≥0.06 ✓
            "top10_pct": 30,            # ≤45 ✓
            "dev_holdings_pct": 5,      # ≤15 ✓
            "is_sellable": 1,           # ✓
            # velocity/social features absent → skipped, NOT penalised
        }
        score, matched, _ = score_vector(_fv(feats), self.sig)
        self.assertGreater(score, 45)                  # well above the old ~40 cap
        self.assertLess(score, 80)                     # but NOT an inflated 100
        self.assertNotIn("holder_velocity", matched)   # absent → never counted

    def test_evaluable_ratio_beats_total_weight_ratio(self):
        # Under the OLD all-rules denominator this token was ~40. The evaluable
        # denominator (fresh tokens not penalised for missing time-series) keeps
        # it clearly higher, even after confidence scaling.
        feats = {"buy_ratio_5m": 0.7, "smart_money_count": 0, "liq_to_mcap": 0.1,
                 "top10_pct": 30, "dev_holdings_pct": 5, "is_sellable": 1}
        score, _, _ = score_vector(_fv(feats), self.sig)
        self.assertGreater(score, 45)                  # > the old ~40 total-weight ratio

    def test_thin_all_match_is_not_a_perfect_score(self):
        # The reported bug: a token matching its FEW measurable rules used to hit
        # ~100. Confidence scaling caps it — a near-full match on partial coverage
        # is not certainty.
        feats = {"buy_ratio_5m": 0.9, "liq_to_mcap": 0.2, "is_sellable": 1}
        score, _, _ = score_vector(_fv(feats), self.sig)
        self.assertLess(score, 75)                     # no more flood of 100/100s

    def test_thin_data_guarded_by_coverage_floor(self):
        # Only two low-coverage features present → below the coverage floor → 0,
        # so a single lucky data point can't fluke a high score.
        score, _, _ = score_vector(_fv({"buy_ratio_5m": 0.9}), self.sig)
        self.assertEqual(score, 0.0)

    def test_failing_evaluable_rules_still_drag_score(self):
        # Token where the measurable rules FAIL → low score (real signal preserved).
        feats = {"buy_ratio_5m": 0.2, "smart_money_count": 0, "liq_to_mcap": 0.01,
                 "top10_pct": 80, "dev_holdings_pct": 40, "is_sellable": 0}
        score, _, _ = score_vector(_fv(feats), self.sig)
        self.assertLess(score, 20)


if __name__ == "__main__":
    unittest.main()
