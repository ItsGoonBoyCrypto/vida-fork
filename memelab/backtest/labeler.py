"""Outcome labeling — decide, in hindsight, which tokens were WINNERS.

Runs over the snapshot time-series once tokens are old enough to judge. This is
the ground truth the whole platform learns from.

  peak_multiple   = max(price) / entry_price
  trough_multiple = min(price) / entry_price

  WINNER  peak >= win_multiple AND not a rug
  RUG     liquidity collapsed (>= rug_drawdown drop from its max) or price ~0
  NEUTRAL settled, neither
  PENDING younger than min_age_hours — don't judge yet

RUG stays separate from NEUTRAL: a pump-then-rug is still useful *entry* signal
(it pumped) but the engine may weight it down.
"""

from __future__ import annotations

import time

from ..models import Outcome, TokenTimeSeries


def label(ts: TokenTimeSeries, win_multiple: float = 3.0,
          min_age_hours: float = 48.0, rug_drawdown: float = 0.8,
          now: float = None) -> Outcome:
    now = time.time() if now is None else now
    if not ts.snapshots or not ts.entry_price:
        return Outcome.PENDING
    age_h = (now - ts.first_seen_ts) / 3600.0
    if age_h < min_age_hours:
        return Outcome.PENDING

    prices = [s.price_usd for s in ts.snapshots if s.price_usd]
    liqs = [s.liquidity_usd for s in ts.snapshots if s.liquidity_usd is not None]
    if not prices:
        return Outcome.PENDING
    peak = max(prices) / ts.entry_price
    trough = min(prices) / ts.entry_price

    liq_collapsed = bool(liqs) and liqs[-1] <= (1.0 - rug_drawdown) * max(liqs)
    if liq_collapsed or trough <= 0.1:
        return Outcome.RUG
    if peak >= win_multiple:
        return Outcome.WINNER
    return Outcome.NEUTRAL


def relabel_all(store, win_multiple: float = 3.0, min_age_hours: float = 48.0,
                now: float = None) -> dict:
    """Recompute outcomes for every settled token; return counts by Outcome.

    Run periodically so the training set stays current as tokens age.
    """
    counts: dict = {o.value: 0 for o in Outcome}
    for ts in store.labeled_tokens(min_age_hours=min_age_hours):
        outcome = label(ts, win_multiple=win_multiple, min_age_hours=min_age_hours, now=now)
        prices = [s.price_usd for s in ts.snapshots if s.price_usd]
        if ts.entry_price and prices:
            peak = max(prices) / ts.entry_price
            trough = min(prices) / ts.entry_price
        else:
            peak, trough = ts.peak_multiple, ts.trough_multiple
        store.set_outcome(ts.chain, ts.token_address, outcome, peak, trough)
        counts[outcome.value] += 1
    return counts
