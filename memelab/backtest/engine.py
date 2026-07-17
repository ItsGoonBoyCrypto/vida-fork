"""Backtest engine — derive & validate the winner signature.

Takes the labeled feature vectors and answers: *which early metrics, in what
ranges/combinations, separated the big pumpers from the duds?* Output is a
`Signature` the screener applies to live tokens.

Two complementary approaches (start with rules for interpretability, add the
model for lift):

  A. RULE MINING  For each feature, compare the winner vs dud distributions
     (e.g. percentiles, KS / mutual information). Emit soft gates like
     "buy_ratio_5m >= p40(winners)" with a weight ∝ separation strength. Human-
     readable; easy to sanity-check against intuition.

  B. FITTED MODEL Logistic regression / gradient boosting on the feature matrix
     (label = WINNER). Store normalised weights in Signature.model. Captures
     interactions rules miss.

CRITICAL — avoid fooling ourselves:
  · TIME-SPLIT validation: train on older tokens, test on newer (never random
    shuffle — memecoin regimes drift). Report out-of-sample precision/recall.
  · Class imbalance: winners are rare; use balanced sampling / precision-recall,
    not accuracy.
  · Per-chain vs pooled: train per chain AND pooled; keep whichever validates
    better. Record `Signature.chains`.
  · Leakage: features must use only the early window (features.extract enforces
    the window, but re-check no post-pump field sneaks in).
"""

from __future__ import annotations

from ..models import Chain, FeatureVector, Signature


def derive(vectors: list, win_multiple: float = 3.0,
           chains: list = None) -> Signature:
    """Fit a Signature from labeled feature vectors (train split only)."""
    # TODO:
    #   winners = [v for v in vectors if v.label == Outcome.WINNER]
    #   duds    = [v for v in vectors if v.label in (NEUTRAL, RUG)]
    #   per feature: separation = distribution_gap(winners, duds)
    #   rules = soft gates for the top-separating features, weighted
    #   (optionally) model = fit_logistic(matrix, labels)
    raise NotImplementedError


def validate(signature: Signature, holdout: list) -> dict:
    """Score the holdout, sweep the decision threshold, return P/R/F1 + lift.

    Lift = winner-rate among the top-scored decile ÷ base winner-rate. That's the
    number that actually says "is this signature worth trading."
    """
    raise NotImplementedError


def run_backtest(store, win_multiple: float = 3.0, chains: list = None) -> Signature:
    """Full loop: pull labeled vectors → time-split → derive → validate → persist.

    Returns the validated Signature (also saved via store.save_signature). Run on
    a schedule (e.g. daily) so signatures track the current meta.
    """
    raise NotImplementedError
