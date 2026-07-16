"""Composite scoring engine (0-100) implementing spec §3.

Weights (default): Safety 38% | Distribution 20% | Momentum 25% | Discovery 17%.
Each category is scored 0-100 on its own, then weighted into the composite.
The safety gate (filters.safety_gate) runs first; a hard fail forces SKIP and
zeroes the composite regardless of momentum ("confluence > single metric").

Every graded points award appends a human-readable reason so alerts can show
*why* — matching the spec's "Reasons:" line.
"""

from __future__ import annotations

from .config import Config, Thresholds
from .filters import safety_gate
from .models import (
    AlertLevel,
    CategoryScore,
    ScoreResult,
    TokenSnapshot,
)


def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def _lerp_down(value: float, good: float, bad: float) -> float:
    """Return 100 when value<=good, 0 when value>=bad, linear between.

    Used for "lower is better" metrics (top10 %, dev %, bundle %).
    """
    if value <= good:
        return 100.0
    if value >= bad:
        return 0.0
    return 100.0 * (bad - value) / (bad - good)


def _lerp_up(value: float, bad: float, good: float) -> float:
    """Return 0 at/below bad, 100 at/above good, linear between.

    Used for "higher is better" metrics (holders, liquidity, volume).
    """
    if value <= bad:
        return 0.0
    if value >= good:
        return 100.0
    return 100.0 * (value - bad) / (good - bad)


# ---------------------------------------------------------------------------
# Category scorers
# ---------------------------------------------------------------------------
def score_safety(t: TokenSnapshot, th: Thresholds, weight: float) -> CategoryScore:
    """Graded safety score (0-100) for tokens that already PASSED the gate.

    The gate is pass/fail; this rewards *extra* safety confluence (burned vs
    merely locked, revoked authorities, low dev holdings, clean external score).
    """
    s = t.safety
    cs = CategoryScore(name="safety", raw=0.0, weight=weight)
    pts = 0.0

    if s.contract_verified:
        pts += 15
        cs.reasons.append("verified contract")
    if s.mint_authority_revoked:
        pts += 15
    if s.freeze_authority_revoked:
        pts += 10
    if s.mint_authority_revoked and s.freeze_authority_revoked:
        cs.reasons.append("authorities revoked")

    if s.lp_burned:
        pts += 25
        cs.reasons.append("LP burned")
    elif s.lp_locked:
        pts += 18
        cs.reasons.append("LP locked")

    # Low/transparent taxes
    tax = max(s.buy_tax_pct or 0.0, s.sell_tax_pct or 0.0)
    if s.buy_tax_pct is not None or s.sell_tax_pct is not None:
        pts += 10 * (_lerp_down(tax, 0.0, th.max_tax_pct) / 100.0)
        if tax <= 1.0:
            cs.reasons.append("~0 tax")

    # Low dev holdings
    if s.dev_holdings_pct is not None:
        pts += 15 * (_lerp_down(s.dev_holdings_pct, 1.0, th.max_dev_holdings_pct) / 100.0)
        if s.dev_holdings_pct <= th.max_dev_holdings_pct:
            cs.reasons.append(f"dev {s.dev_holdings_pct:.1f}%")

    # External risk score contribution
    if s.external_risk_score is not None:
        pts += 10 * (s.external_risk_score / 100.0)

    cs.raw = _clamp(pts)
    return cs


def score_distribution(t: TokenSnapshot, th: Thresholds, weight: float) -> CategoryScore:
    cs = CategoryScore(name="distribution", raw=0.0, weight=weight)
    parts: list[float] = []

    if t.top10_supply_pct is not None:
        v = _lerp_down(t.top10_supply_pct, 15.0, th.skip_top10_pct)
        parts.append(v)
        if t.top10_supply_pct <= th.max_top10_pct:
            cs.reasons.append(f"top10 {t.top10_supply_pct:.0f}%")
        else:
            cs.penalties.append(f"top10 {t.top10_supply_pct:.0f}% high")

    if t.top1_supply_pct is not None:
        parts.append(_lerp_down(t.top1_supply_pct, 3.0, 20.0))

    if t.holder_count is not None:
        v = _lerp_up(t.holder_count, th.min_holders * 0.4, th.ideal_holders)
        parts.append(v)
        if t.holder_count >= th.min_holders:
            cs.reasons.append(f"{t.holder_count} holders")

    if t.holder_growth_1h is not None and t.holder_growth_1h > 0:
        parts.append(min(100.0, t.holder_growth_1h * 2.0))
        cs.reasons.append("holders growing")

    # Bundle penalty folds into distribution as a strong drag.
    if t.safety.bundle_supply_pct is not None:
        v = _lerp_down(t.safety.bundle_supply_pct, 5.0, th.skip_bundle_pct)
        parts.append(v)
        if t.safety.bundle_supply_pct <= th.max_bundle_pct:
            cs.reasons.append("low bundle")
        else:
            cs.penalties.append(f"bundled {t.safety.bundle_supply_pct:.0f}%")

    cs.raw = _clamp(sum(parts) / len(parts)) if parts else 0.0
    return cs


def score_momentum(t: TokenSnapshot, th: Thresholds, weight: float) -> CategoryScore:
    cs = CategoryScore(name="momentum", raw=0.0, weight=weight)
    parts: list[float] = []

    # Absolute volume
    if t.volume_24h is not None:
        parts.append(_lerp_up(t.volume_24h, th.min_volume_24h_usd * 0.3, th.min_volume_24h_usd * 5))

    # Acceleration: 1h rate vs 24h average hourly rate
    if t.volume_accelerating is True:
        parts.append(100.0)
        cs.reasons.append("accelerating volume")
    elif t.volume_accelerating is False:
        parts.append(35.0)

    # Short-term velocity: last-5-min rate vs the trailing 1h rate. This is the
    # earliest tell — it moves within minutes of launch, before the 24h window
    # has any history. Weighted heavily so fresh runners surface fast.
    vv = t.volume_velocity
    if vv is not None:
        parts.append(_lerp_up(vv, th.min_volume_velocity - 0.4, th.strong_volume_velocity))
        if vv >= th.strong_volume_velocity:
            cs.reasons.append(f"volume surging ({vv:.1f}x)")
        elif vv >= th.min_volume_velocity:
            cs.reasons.append(f"volume rising ({vv:.1f}x)")
        elif vv < 0.6:
            cs.penalties.append("volume cooling")

    # Fresh buy pressure from the last 5 minutes (reacts before the 1h ratio).
    br5 = t.buy_ratio_5m
    if br5 is not None:
        parts.append(_lerp_up(br5 * 100, th.min_buy_ratio_1h * 100 - 10,
                              th.strong_buy_ratio_1h * 100 + 15))
        if br5 >= th.strong_buy_ratio_1h:
            cs.reasons.append(f"5m buys {br5*100:.0f}%")

    # Volume vs mcap turnover
    if t.vol_to_mcap_24h is not None:
        parts.append(_lerp_up(t.vol_to_mcap_24h, 0.1, 1.0))
        if t.vol_to_mcap_24h >= 1.0:
            cs.reasons.append("24h vol >= mcap")

    # Buy pressure
    br = t.buy_ratio_1h
    if br is not None:
        parts.append(_lerp_up(br * 100, th.min_buy_ratio_1h * 100 - 10, th.strong_buy_ratio_1h * 100 + 15))
        if br >= th.strong_buy_ratio_1h:
            cs.reasons.append(f"buy ratio {br*100:.0f}%")
        elif br < th.min_buy_ratio_1h:
            cs.penalties.append(f"weak buys {br*100:.0f}%")

    # Healthy price action — reward positive but not single-spike-then-dead.
    if t.price_change_1h is not None and t.price_change_24h is not None:
        if t.price_change_1h > 0 and t.price_change_24h > 0:
            parts.append(80.0)
        elif t.price_change_24h > 0 >= t.price_change_1h:
            parts.append(50.0)  # cooled off but net up (shakeout ok)
        else:
            parts.append(25.0)

    cs.raw = _clamp(sum(parts) / len(parts)) if parts else 0.0
    return cs


def score_discovery(t: TokenSnapshot, th: Thresholds, weight: float) -> CategoryScore:
    cs = CategoryScore(name="discovery", raw=0.0, weight=weight)
    pts = 0.0

    # Freshness bonus (earlier = higher within the allowed window)
    if t.age_minutes is not None:
        pts += 25 * (_lerp_down(t.age_minutes, 0.0, th.max_age_minutes) / 100.0)

    # Known launchpad origin (e.g. flap.sh) — a recognised launchpad is a mild
    # positive (curated deploy, standard LP handling) and lets alerts label it.
    if t.launchpad:
        pts += 15
        cs.reasons.append(f"via {t.launchpad}")

    # Bonding-curve tokens caught pre-graduation are the earliest possible entry.
    # Reward the sweet spot: enough traction to be real, not yet graduated.
    if t.curve_progress_pct is not None:
        if 20.0 <= t.curve_progress_pct < 100.0:
            pts += 12
            cs.reasons.append(f"curve {t.curve_progress_pct:.0f}% (pre-grad)")
        elif t.curve_progress_pct < 20.0:
            pts += 4  # very fresh, unproven

    # Smart money
    n_smart = len(t.smart_money_wallets)
    if n_smart:
        pts += min(35.0, 15.0 * n_smart)
        if n_smart >= th.smart_money_min_wallets:
            cs.reasons.append(f"{n_smart} smart wallets")

    # Socials present
    if t.socials:
        pts += min(20.0, 7.0 * len(t.socials))
        cs.reasons.append("socials present")

    # DexScreener visibility (boost/trending is a later-stage signal — mild)
    if t.dex_trending:
        pts += 12
        cs.reasons.append("trending")
    if t.dex_boosted:
        pts += 8
        cs.reasons.append("boosted")

    cs.raw = _clamp(pts)
    return cs


# ---------------------------------------------------------------------------
# Composite
# ---------------------------------------------------------------------------
def score_token(t: TokenSnapshot, cfg: Config, strict_safety: bool = True,
                pragmatic: bool = False) -> ScoreResult:
    th = cfg.thresholds
    w = cfg.weights.normalized()

    gate = safety_gate(t, th, strict=strict_safety, pragmatic=pragmatic)

    categories = [
        score_safety(t, th, w.safety),
        score_distribution(t, th, w.distribution),
        score_momentum(t, th, w.momentum),
        score_discovery(t, th, w.discovery),
    ]

    if not gate.passed:
        # Confluence rule: safety fail => not tradeable. Composite floored.
        return ScoreResult(
            composite=0.0,
            level=AlertLevel.SKIP,
            categories=categories,
            safety_passed=False,
            gate_failures=gate.failures,
        )

    composite = _clamp(sum(c.weighted for c in categories))

    if composite >= th.strong_alert_score:
        level = AlertLevel.STRONG
    elif composite >= th.watch_alert_score:
        level = AlertLevel.WATCH
    else:
        level = AlertLevel.SKIP

    return ScoreResult(
        composite=composite,
        level=level,
        categories=categories,
        safety_passed=True,
        gate_failures=[],
    )
