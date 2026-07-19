"""Wallet reputation — turn the smart-money set from a flat list into a
quality-weighted, self-curating model.

The scanner harvests early buyers of winners into a smart set, but until now
every wallet counted the same and the set only ever grew. This module scores
each wallet from the reputation ledger (storage.wallet_reputation_rows):

  · winner_overlap — how many DISTINCT confirmed winners it was early on. The
    strongest signal: a wallet on 4 of your winners is a proven sharp, not a
    one-time fluke.
  · forward picks — of the tokens it later bought, how many actually pumped
    (Bayesian-shrunk so 1/1 doesn't outrank 7/10).
  · rug involvement — distinct rugs it was early in / dumped; taxes the score
    and, past a threshold, marks it toxic.
  · recency — a wallet that's gone cold decays toward neutral.

quality() returns 0..1. It drives three things downstream:
  1. discovery scoring uses SUM of buyer qualities, not a raw headcount (#1);
  2. wallets with overlap >= core_alpha_min_overlap fire single-wallet alerts (#2);
  3. tokens bought by net-toxic wallets get demoted (#3).

Pure + dependency-free so it's trivially testable; all I/O lives in storage.
"""

from __future__ import annotations

# Bayesian prior for forward-pick hit rate (pseudo-counts): starts skeptical.
_PICK_PRIOR_WIN = 1.0
_PICK_PRIOR_LOSS = 2.0

# Winners-overlap that already implies a top-tier wallet (score saturates here).
_OVERLAP_SATURATE = 4.0

# Half-life for recency decay, in days.
_RECENCY_HALFLIFE_DAYS = 30.0


def _pick_rate(wins: int, total: int) -> float:
    """Shrunk forward hit rate in 0..1 (neutral 0.33 when no picks judged)."""
    return (wins + _PICK_PRIOR_WIN) / (total + _PICK_PRIOR_WIN + _PICK_PRIOR_LOSS)


def _recency_factor(last_ts: float, now: float) -> float:
    """1.0 when fresh, decaying toward a 0.5 floor as a wallet goes cold."""
    if not last_ts or now <= last_ts:
        return 1.0
    days = (now - last_ts) / 86400.0
    import math
    decay = math.pow(0.5, days / _RECENCY_HALFLIFE_DAYS)
    return 0.5 + 0.5 * decay


def quality(rep: dict, last_ts: float = 0.0, now: float = 0.0) -> float:
    """0..1 reputation from a wallet's reputation-ledger counters.

    ``rep`` = {winner_overlap, rug_count, pick_wins, pick_total} (as returned by
    storage.wallet_reputation_rows). ``last_ts``/``now`` apply recency decay.
    """
    overlap = float(rep.get("winner_overlap") or 0)
    rugs = float(rep.get("rug_count") or 0)
    wins = int(rep.get("pick_wins") or 0)
    total = int(rep.get("pick_total") or 0)

    # Base from overlap (0..1, saturating) — the primary signal.
    base = min(1.0, overlap / _OVERLAP_SATURATE)

    # Forward-pick evidence nudges up/down from a neutral 0.33 baseline.
    pick = _pick_rate(wins, total)                 # 0..1, ~0.33 when unjudged
    # Blend: overlap dominates, forward record modulates it.
    q = 0.65 * base + 0.35 * pick

    # A brand-new harvested wallet (overlap>=1, no picks yet) shouldn't read as
    # weak just because it has no forward record — floor it to a modest positive.
    if overlap >= 1:
        q = max(q, 0.35)

    # Rug tax: each distinct rug involvement is a strong negative.
    q -= 0.25 * rugs
    if now:
        q *= _recency_factor(last_ts, now)
    return max(0.0, min(1.0, q))


def is_toxic(rep: dict, min_rugs: int, min_net: float = 0.0) -> bool:
    """A wallet is toxic if it's in >= min_rugs rugs and its rug count outweighs
    its winner overlap (so a genuine sharp who once aped a rug isn't blacklisted)."""
    rugs = float(rep.get("rug_count") or 0)
    overlap = float(rep.get("winner_overlap") or 0)
    return rugs >= min_rugs and (rugs - overlap) > min_net


def smart_money_quality_bonus(qualities: list, per_wallet_cap: float = 20.0,
                              total_cap: float = 45.0) -> float:
    """Discovery-score points from a token's smart buyers, weighted by quality.

    Replaces the old flat 15/wallet headcount: two proven sharps (q≈1) are worth
    far more than five unproven harvests (q≈0.35). Capped so a swarm can't
    dominate the composite.
    """
    pts = sum(per_wallet_cap * max(0.0, min(1.0, q)) for q in qualities)
    return min(total_cap, pts)


def deployer_is_trusted(rep: dict, min_total: int = 2, min_win_rate: float = 0.5) -> bool:
    """A deployer worth a pre-emptive alert: enough launches, mostly winners,
    and not a rugger."""
    total = int(rep.get("total") or 0)
    wins = int(rep.get("wins") or 0)
    rugs = int(rep.get("rugs") or 0)
    if total < min_total or rugs > 0:
        return False
    return (wins / total) >= min_win_rate
