"""Live screener — score live tokens against the derived Signature.

The production output. For each live token (its snapshots so far), compute the
same early features and score them against the active Signature → a 0-100
pump-likelihood. High scorers become alerts (reuse rhl2_scanner alerting).

This is the mirror of backtest.validate, but on live, unlabeled tokens — and it
MUST use metrics.features.extract_live (identical math to training) to avoid
train/serve skew.
"""

from __future__ import annotations

from ..models import FeatureVector, Screen, Signature, TokenSnapshot


def score(fv: FeatureVector, sig: Signature) -> Screen:
    """Apply a Signature to one live feature vector → a Screen(0-100 + reasons)."""
    # TODO:
    #   rule score: sum(weight for rule where feature op value holds), normalised
    #   model score: sigmoid(sum(w_i * f_i) + bias) * 100
    #   combine (e.g. average), collect matched rules → reasons
    raise NotImplementedError


class Screener:
    def __init__(self, store):
        self.store = store
        self._sig: Signature = None      # loaded from store.active_signature()

    def reload_signature(self) -> None:
        """Pick up the latest signature the backtest engine produced."""
        raise NotImplementedError

    def screen(self, snapshots: list) -> Screen:
        """Feature-extract a live token's snapshots and score it."""
        raise NotImplementedError

    def min_confident_score(self) -> float:
        """Alert threshold, tied to the signature's validated precision so we
        don't alert from a weak/undertrained signature."""
        raise NotImplementedError
