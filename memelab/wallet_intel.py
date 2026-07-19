"""Wallet reputation for memelab — the cross-chain twin of the RH scanner's
wallet_intel. Turns the per-chain smart set from a flat headcount into a
quality-weighted signal.

Each wallet is scored from its reputation-ledger counters:
  · winner_overlap — DISTINCT winners it was an early buyer of (primary signal);
  · forward picks   — of tokens it later bought, how many settled as winners
                      (Bayesian-shrunk so 1/1 doesn't outrank 7/10);
  · recency         — a wallet gone cold decays toward neutral.

quality() → 0..1 per wallet. The collector sums the qualities of a token's
smart buyers into snap.smart_money_quality, which becomes a feature the
signature can learn from — so a token bought by two proven sharps reads far
stronger than one bought by five unproven harvests.

Pure + dependency-free (mirrors rhl2_scanner.wallet_intel so both services stay
independently deployable).
"""

from __future__ import annotations

import math

_PICK_PRIOR_WIN = 1.0
_PICK_PRIOR_LOSS = 2.0
_OVERLAP_SATURATE = 4.0
_RECENCY_HALFLIFE_DAYS = 30.0


def _pick_rate(wins: int, total: int) -> float:
    return (wins + _PICK_PRIOR_WIN) / (total + _PICK_PRIOR_WIN + _PICK_PRIOR_LOSS)


def _recency_factor(last_ts: float, now: float) -> float:
    if not last_ts or now <= last_ts:
        return 1.0
    days = (now - last_ts) / 86400.0
    return 0.5 + 0.5 * math.pow(0.5, days / _RECENCY_HALFLIFE_DAYS)


def quality(rep: dict, last_ts: float = 0.0, now: float = 0.0) -> float:
    """0..1 reputation from {winner_overlap, pick_wins, pick_total} (+ recency)."""
    overlap = float(rep.get("winner_overlap") or 0)
    wins = int(rep.get("pick_wins") or 0)
    total = int(rep.get("pick_total") or 0)

    base = min(1.0, overlap / _OVERLAP_SATURATE)
    pick = _pick_rate(wins, total)
    q = 0.65 * base + 0.35 * pick
    if overlap >= 1:
        q = max(q, 0.35)          # a harvested wallet is a modest positive, not ~0
    if now:
        q *= _recency_factor(last_ts, now)
    return max(0.0, min(1.0, q))


def quality_sum(qualities: list) -> float:
    """Total reputation weight of a token's smart buyers (the feature value)."""
    return round(sum(max(0.0, min(1.0, q)) for q in qualities), 4)
