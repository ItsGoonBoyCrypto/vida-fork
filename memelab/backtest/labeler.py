"""Outcome labeling — decide, in hindsight, which tokens were WINNERS.

Runs over the snapshot time-series once tokens are old enough to judge. This is
the "ground truth" the whole platform learns from, so the definitions matter:

  peak_multiple   = max(price) / entry_price   over the observation window
  trough_multiple = min(price) / entry_price

  WINNER  peak_multiple >= win_multiple            (e.g. 3x)   AND not a rug
  RUG     liquidity collapsed (>~80% drop) or price → ~0 early
  NEUTRAL settled, neither of the above
  PENDING < min_age_hours old — don't judge yet

Keeping RUG separate from NEUTRAL matters: a token that pumped then rugged is
still useful signal for the *entry* (it pumped), but we may weight it down.
"""

from __future__ import annotations

from ..models import Outcome, TokenTimeSeries


def label(ts: TokenTimeSeries, win_multiple: float = 3.0,
          min_age_hours: float = 48.0, rug_drawdown: float = 0.8) -> Outcome:
    """Assign an Outcome from the token's realised price path."""
    # TODO:
    #   if age < min_age_hours: return PENDING
    #   peak = max snapshot price / entry; trough = min / entry
    #   liquidity_collapsed = last_liq <= (1-rug_drawdown) * max_liq
    #   if liquidity_collapsed or trough <= 0.1: return RUG
    #   if peak >= win_multiple: return WINNER
    #   return NEUTRAL
    raise NotImplementedError


def relabel_all(store, **kwargs) -> dict:
    """Recompute outcomes for every settled token; return counts by Outcome.

    Run periodically (e.g. hourly) so the training set stays current as tokens
    age. Writes back via store.set_outcome().
    """
    raise NotImplementedError
