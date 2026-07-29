"""First-buyer wallet-age signal.

The strongest published predictor of a pump.fun-style graduation is the AGE of
the earliest buyers: a launch bought almost entirely by BRAND-NEW wallets
(created minutes ago, no history) is a bot/sniper bundle dressed up as organic
demand — it dumps. A launch where aged, established wallets are early is real
interest.

Pure classification over (wallet -> first_seen_ts) data the caller fetches from
the explorer. Returns the fresh-wallet fraction and a verdict the scorer uses to
demote bot launches / lightly boost organic ones.
"""

from __future__ import annotations

from typing import Optional

_FRESH_MAX_AGE_H = 24.0     # a wallet younger than this is "fresh"
_AGED_MIN_AGE_D = 14.0      # older than this counts as "established"


def fresh_buyer_ratio(first_seen: dict, now: float) -> Optional[dict]:
    """first_seen: {wallet: earliest_tx_unixts}. Returns None if too little data,
    else {fresh_pct, aged_pct, n}. fresh_pct = fraction of buyers younger than a
    day; aged_pct = fraction older than two weeks."""
    ages_h = [(now - ts) / 3600.0 for ts in first_seen.values() if ts and ts > 0]
    n = len(ages_h)
    if n < 3:
        return None                       # not enough resolvable ages to judge
    fresh = sum(1 for a in ages_h if a <= _FRESH_MAX_AGE_H)
    aged = sum(1 for a in ages_h if a >= _AGED_MIN_AGE_D * 24.0)
    return {"fresh_pct": round(100.0 * fresh / n, 1),
            "aged_pct": round(100.0 * aged / n, 1), "n": n}


def age_verdict(ratio: Optional[dict]) -> tuple[float, str]:
    """(conviction_delta, note) from a fresh_buyer_ratio result.

    Heavily-fresh early buyers => bot/sniper launch => demote. A meaningful
    share of aged wallets => real hands => small boost. Neutral otherwise.
    """
    if not ratio:
        return 0.0, ""
    fp, ap = ratio["fresh_pct"], ratio["aged_pct"]
    if fp >= 80.0:
        return -14.0, f"⚠️ {fp:.0f}% of early buyers are fresh wallets (bot-launch tell)"
    if fp >= 60.0:
        return -7.0, f"⚠️ {fp:.0f}% fresh early buyers"
    if ap >= 40.0:
        return 6.0, f"🧓 {ap:.0f}% of early buyers are aged wallets (real hands)"
    return 0.0, ""
