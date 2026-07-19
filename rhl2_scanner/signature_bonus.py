"""Feed memelab's LEARNED winner-signature back into the scanner's score.

memelab watches every chain, waits ~48h to see which tokens actually pumped, and
derives a rule-set (the "winner DNA"). This module lets the RH scanner read that
signature (from the shared memelab DB) and give a bonus to live tokens that match
it — so once memelab has learned, the scanner scores off *proven* winners, not
just hand weights.

Safety valve: the bonus is DORMANT until the signature is genuinely trained
(``trained_on >= min_trained``). While memelab is cold (trained_on 0) this returns
0 and never touches live scoring. Dependency-free; the DB read is best-effort.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Optional


def _feature_value(snap, name: str) -> Optional[float]:
    """Map a memelab feature name onto the scanner's snapshot (None if unknown)."""
    s = snap.safety
    if name == "smart_money_count":
        return float(len(snap.smart_money_wallets or []))
    if name == "buy_ratio_5m":
        b, sl = snap.buys_5m, snap.sells_5m
        return (b / (b + sl)) if (b is not None and sl is not None and (b + sl)) else None
    if name == "buy_ratio_1h":
        return snap.buy_ratio_1h
    if name == "vol5m_to_vol1h":
        if snap.volume_5m and snap.volume_1h:
            return snap.volume_5m / (snap.volume_1h / 12.0) if snap.volume_1h else None
        return None
    if name == "holder_velocity":
        return float(snap.holder_growth_1h) if snap.holder_growth_1h is not None else None
    if name == "liq_to_mcap":
        if snap.liquidity_usd and snap.market_cap_usd:
            return snap.liquidity_usd / snap.market_cap_usd
        return None
    if name == "top10_pct":
        return snap.top10_supply_pct
    if name == "dev_holdings_pct":
        return s.dev_holdings_pct
    if name == "is_sellable":
        return 0.0 if s.is_honeypot is True else 1.0 if s.is_honeypot is False else None
    if name == "mcap_usd":
        return snap.market_cap_usd
    if name == "liquidity_usd":
        return snap.liquidity_usd
    return None   # feature the scanner doesn't track → skip this rule


def _passes(value: float, op: str, target: float) -> bool:
    if op in (">=", "ge", "gte"):
        return value >= target
    if op in ("<=", "le", "lte"):
        return value <= target
    if op in (">", "gt"):
        return value > target
    if op in ("<", "lt"):
        return value < target
    if op in ("==", "eq"):
        return value == target
    return True


def load_signature(db_path: str, min_trained: int = 20) -> Optional[dict]:
    """Read memelab's active signature — only if it's genuinely trained.

    Returns a dict with 'rules' + 'precision' + 'trained_on', or None (dormant)
    when the DB is missing/unreadable or the signature is still the bootstrap
    prior (trained_on < min_trained).
    """
    if not db_path:
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT json FROM signatures ORDER BY created_ts DESC LIMIT 1").fetchone()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — DB not there yet / schema mismatch → dormant
        return None
    if not row or not row[0]:
        return None
    try:
        sig = json.loads(row[0])
    except (ValueError, TypeError):
        return None
    if int(sig.get("trained_on") or 0) < min_trained:
        return None   # bootstrap prior — don't touch live scoring
    return sig


def match_fraction(snap, sig: dict) -> tuple[float, int]:
    """Fraction of the signature's evaluable rules this token satisfies (0..1),
    plus the count of rules that were evaluable."""
    rules = sig.get("rules") or []
    ok = judged = 0
    for r in rules:
        val = _feature_value(snap, r.get("feature", ""))
        if val is None:
            continue
        judged += 1
        if _passes(val, str(r.get("op", ">=")), float(r.get("value", 0))):
            ok += 1
    if judged == 0:
        return 0.0, 0
    return ok / judged, judged


def signature_bonus(snap, sig: Optional[dict], max_bonus: float = 12.0) -> float:
    """Composite points to add for matching the learned signature.

    Scales with match fraction × the signature's out-of-sample precision (so a
    weakly-validated signature contributes little). 0 when dormant / no match.
    """
    if not sig:
        return 0.0
    frac, judged = match_fraction(snap, sig)
    if judged < 3 or frac < 0.6:      # need a real, mostly-matching read
        return 0.0
    precision = sig.get("precision")
    scale = float(precision) if isinstance(precision, (int, float)) and precision else 0.6
    return max_bonus * frac * min(1.0, scale / 0.6)
