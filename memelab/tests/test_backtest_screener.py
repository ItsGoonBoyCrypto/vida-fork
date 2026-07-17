"""End-to-end: synthetic winners with a known signal → derive → validate → screen.

We plant a real separation (winners have high buy_ratio_5m + smart money + fast
holder growth; duds don't) and assert the engine recovers it, validates with
lift > 1, and the screener scores a winner-shaped token above a dud.
"""

from __future__ import annotations

import unittest

from memelab.models import Chain, FeatureVector, Outcome, Signature
from memelab.backtest.engine import derive, validate, model_probability
from memelab.screener.engine import score_vector


def _fv(winner: bool, i: int) -> FeatureVector:
    # Winners: strong early buy pressure, smart money, fast holder growth, low top10.
    # Duds: the opposite. A little deterministic jitter so stds are non-zero.
    j = (i % 5) * 0.01
    if winner:
        feats = {"buy_ratio_5m": 0.78 + j, "smart_money_count": 2 + (i % 2),
                 "holder_growth": 40 + i % 7, "top10_pct": 25 + j * 10,
                 "vol5m_to_vol1h": 1.8 + j, "liq_to_mcap": 0.25 + j}
        label = Outcome.WINNER
    else:
        feats = {"buy_ratio_5m": 0.45 + j, "smart_money_count": 0,
                 "holder_growth": 5 + i % 4, "top10_pct": 55 + j * 10,
                 "vol5m_to_vol1h": 0.7 + j, "liq_to_mcap": 0.6 + j}
        label = Outcome.NEUTRAL if i % 3 else Outcome.RUG
    return FeatureVector(chain=Chain.SOLANA, token_address=f"0x{i:040x}",
                         features=feats, label=label)


def _dataset(nw=30):
    # Interleave 1 winner : 2 duds so any time-split carries both classes.
    out = []
    for i in range(nw):
        out.append(_fv(True, i))
        out.append(_fv(False, 2 * i))
        out.append(_fv(False, 2 * i + 1))
    return out


class TestDeriveValidate(unittest.TestCase):
    def test_recovers_signal(self):
        sig = derive(_dataset(), win_multiple=3.0, chains=[Chain.SOLANA])
        rule_feats = {r["feature"] for r in sig.rules}
        # the planted discriminators should surface as rules
        self.assertIn("buy_ratio_5m", rule_feats)
        self.assertIn("smart_money_count", rule_feats)
        # directions correct: buy_ratio high-good (>=), top10 low-good (<=)
        by = {r["feature"]: r["op"] for r in sig.rules}
        self.assertEqual(by["buy_ratio_5m"], ">=")
        if "top10_pct" in by:
            self.assertEqual(by["top10_pct"], "<=")

    def test_validate_has_lift(self):
        data = _dataset()                              # already interleaved
        cut = int(len(data) * 0.7)
        sig = derive(data[:cut], chains=[Chain.SOLANA])
        m = validate(sig, data[cut:])
        self.assertGreater(m["n"], 0)
        self.assertGreaterEqual(m["lift"], 1.0)       # top decile richer in winners
        self.assertGreaterEqual(m["precision"], 0.5)

    def test_insufficient_samples_is_safe(self):
        sig = derive([_fv(True, 0), _fv(False, 1)], chains=[Chain.SOLANA])
        self.assertEqual(sig.rules, [])
        self.assertIn("insufficient", sig.notes)


class TestScorer(unittest.TestCase):
    def test_winner_scores_above_dud(self):
        sig = derive(_dataset(), win_multiple=3.0, chains=[Chain.SOLANA])
        win_fv = _fv(True, 1)
        dud_fv = _fv(False, 1)
        win_score, matched, reasons = score_vector(win_fv, sig)
        dud_score, _, _ = score_vector(dud_fv, sig)
        self.assertGreater(win_score, dud_score)
        self.assertTrue(matched)                       # winner matched some rules
        self.assertGreater(model_probability(win_fv, sig), model_probability(dud_fv, sig))


if __name__ == "__main__":
    unittest.main()
