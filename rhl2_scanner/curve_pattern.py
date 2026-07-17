"""Pre-migration curve pattern — learn the 'winning setup' and match lookalikes.

flap tokens trade on a bonding curve before they graduate to a DEX. We now price
that phase (Portal.getTokenV2), so we can also *characterise* it: how fast the
curve fills, how holders and reserve climb, how concentrated it is. This module
turns those observations into a feature vector, holds a prime 'winning setup'
profile (a bootstrap prior, replaced by one LEARNED from tokens that graduated
and pumped >=3x), and scores a live on-curve token against it.

Dependency-free (no numpy) — same house style as scoring/keccak.
"""

from __future__ import annotations

from typing import Optional

# The features that describe a pre-migration setup. Velocity terms (per hour)
# are the strongest tell — a curve that is actively filling with growing holders
# behaves very differently from a dead one parked at the same progress.
FEATURE_KEYS = [
    "progress",            # graduation fill %  (0-100)
    "progress_velocity",   # Δprogress per hour
    "reserve_eth",         # ETH backing the curve
    "reserve_velocity",    # Δreserve (ETH) per hour
    "mcap_usd",
    "holders",
    "holder_velocity",     # Δholders per hour
    "smart_count",         # distinct smart-money wallets holding
    "age_min",
]


def extract_features(snap, prev_obs, now_ts: float) -> dict:
    """Build a feature vector from a live on-curve snapshot + its last observation.

    ``prev_obs`` is the previous stored observation row (or None on first sight);
    velocities are computed against it, defaulting to 0 when there's no prior.
    """
    progress = snap.curve_progress_pct
    reserve_eth = (snap.liquidity_usd or 0) / _eth_usd_from(snap) if _eth_usd_from(snap) else None
    holders = snap.holder_count
    smart_count = len(snap.smart_money_wallets or [])
    age_min = snap.age_minutes

    prog_v = res_v = hold_v = 0.0
    if prev_obs is not None:
        dt_h = max((now_ts - float(prev_obs["ts"])) / 3600.0, 1e-6)
        if progress is not None and prev_obs["progress"] is not None:
            prog_v = (progress - float(prev_obs["progress"])) / dt_h
        if reserve_eth is not None and prev_obs["reserve_eth"] is not None:
            res_v = (reserve_eth - float(prev_obs["reserve_eth"])) / dt_h
        if holders is not None and prev_obs["holders"] is not None:
            hold_v = (holders - int(prev_obs["holders"])) / dt_h

    return {
        "progress": progress,
        "progress_velocity": prog_v,
        "reserve_eth": reserve_eth,
        "reserve_velocity": res_v,
        "mcap_usd": snap.market_cap_usd,
        "holders": holders,
        "holder_velocity": hold_v,
        "smart_count": float(smart_count),
        "age_min": age_min,
    }


def _eth_usd_from(snap) -> Optional[float]:
    """Recover the ETH/USD used to price this snap (price_usd / price_native)."""
    if snap.price_usd and snap.price_native and snap.price_native > 0:
        return snap.price_usd / snap.price_native
    return None


def is_confirmed_climb(features: dict) -> bool:
    """Is the curve actively filling? (progress or reserve rising)."""
    return (features.get("progress_velocity") or 0) > 0 \
        or (features.get("reserve_velocity") or 0) > 0


# A band is (lo, hi, weight); None on a bound means open. A feature scores its
# weight when its value falls in [lo, hi]. `match` returns the weighted fraction.
def bootstrap_profile() -> dict:
    """Prior 'prime pre-migration gem' bands — used until enough winners exist."""
    return {
        # climbing, but neither dead nor essentially-graduated
        "progress": (15.0, 92.0, 1.0),
        # actively filling — the core 'confirmed climb' signal
        "progress_velocity": (3.0, None, 2.0),
        "reserve_velocity": (0.03, None, 1.5),
        # real, growing holder base
        "holders": (20.0, None, 1.0),
        "holder_velocity": (1.5, None, 1.5),
        # sub-$100K gem territory (with headroom)
        "mcap_usd": (6000.0, 140000.0, 1.0),
        # fresh-ish (<= 2 days on the curve)
        "age_min": (None, 2880.0, 0.5),
        # smart money present is a bonus, not a gate
        "smart_count": (1.0, None, 0.75),
    }


def match(features: dict, profile: dict) -> tuple[bool, float, list[str], list[str]]:
    """Score a feature vector against a profile.

    Returns (matched, score_0_100, hits, misses). A feature with an unknown
    (None) value is skipped (neither hit nor miss) so thin early data isn't
    penalised. ``matched`` is score >= 70 over the weight that could be judged.
    """
    total_w = judged_w = got_w = 0.0
    hits: list[str] = []
    misses: list[str] = []
    for key, (lo, hi, w) in profile.items():
        total_w += w
        val = features.get(key)
        if val is None:
            continue
        judged_w += w
        ok = (lo is None or val >= lo) and (hi is None or val <= hi)
        if ok:
            got_w += w
            hits.append(key)
        else:
            misses.append(key)
    if judged_w <= 0:
        return False, 0.0, hits, misses
    score = 100.0 * got_w / judged_w
    return score >= 70.0, score, hits, misses


def _percentile(values: list[float], p: float) -> float:
    """Linear-interpolated percentile (p in 0..100) of a non-empty list."""
    xs = sorted(values)
    if len(xs) == 1:
        return xs[0]
    idx = (p / 100.0) * (len(xs) - 1)
    lo = int(idx)
    frac = idx - lo
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * frac


# Features where only a HIGH value is good → learn a lower bound (open top).
_LOWER_BOUND_ONLY = {"progress_velocity", "reserve_velocity", "holder_velocity",
                     "holders", "smart_count", "reserve_eth"}
# Features that are a range → learn both bounds.
_RANGED = {"progress", "mcap_usd"}


def learn_profile(setups: list[dict], min_winners: int = 5) -> Optional[dict]:
    """Derive a winning-setup profile from labeled setups.

    Uses the winners' [p15, p85] range per feature (lower-bound-only for the
    'more-is-better' velocity/holder features). Returns None if there aren't yet
    enough winners — the caller falls back to the bootstrap prior.
    """
    winners = [s for s in setups if s.get("win")]
    if len(winners) < min_winners:
        return None
    profile: dict = {}
    for key in FEATURE_KEYS:
        if key == "age_min":
            continue  # keep age soft; handled by the bootstrap, not learned
        vals = [float(s["features"][key]) for s in winners
                if s.get("features", {}).get(key) is not None]
        if len(vals) < min_winners:
            continue
        lo = _percentile(vals, 15.0)
        if key in _LOWER_BOUND_ONLY:
            profile[key] = (lo, None, 1.5 if "velocity" in key else 1.0)
        elif key in _RANGED:
            hi = _percentile(vals, 85.0)
            profile[key] = (lo, hi, 1.0)
    return profile or None
