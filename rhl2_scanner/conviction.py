"""Conviction — fuse every independent signal into one 0-100 confidence number.

The composite score is one model (hand-weighted safety/distribution/momentum/
discovery). Conviction is a CONFLUENCE meter on top of it: how many *independent*
brains agree this token is a runner. Independent confirmations — each a different
model looking at different data — are what separate a genuine setup from one
model's noise:

  · the composite itself (the base read);
  · memelab's learned cross-chain signature matching;
  · the pre-migration curve-pattern matcher;
  · a proven core-alpha wallet (multi-winner) buying;
  · quality-weighted smart-money confluence;
  · social sentiment.
  · a toxic (repeat-rugger) buyer is a hard negative.

The raw number matters less than its CALIBRATION: conviction is tracked per
bucket against realized outcomes (paper.py), so an alert can say "78 conviction —
that bucket has returned +2.4x avg". Pure + dependency-free.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Conviction:
    score: float                       # 0-100
    factors: list = field(default_factory=list)   # [(label, points)] for the alert

    def summary(self) -> str:
        top = sorted(self.factors, key=lambda f: -abs(f[1]))[:4]
        return " · ".join(f"{lbl} {pts:+.0f}" for lbl, pts in top if pts)


def fuse(signals: dict) -> Conviction:
    """Fuse signal values into a 0-100 conviction + a factor breakdown.

    signals keys (all optional; missing = that model didn't weigh in):
      composite       0-100 hand-model score (the base)
      safety_passed   bool — a hard gate; conviction floored low if False
      signature_match 0-1 fraction matched of memelab's learned signature
      curve_match     bool — matches the learned pre-migration winning setup
      core_alpha      int — proven multi-winner wallets among buyers
      smart_quality   0-45 quality-weighted smart-money discovery points
      social          0-100 social sentiment (LunarCrush), or None
      toxic           bool — a repeat-rugger wallet is among the buyers
    """
    factors: list = []
    # Base: the composite is the anchor (0..50 of the conviction budget).
    composite = float(signals.get("composite") or 0.0)
    base = (composite / 100.0) * 50.0
    factors.append(("base", round(base, 1)))
    score = base

    if signals.get("safety_passed") is False:
        return Conviction(score=min(score, 15.0), factors=factors + [("unsafe", -99)])

    # Independent confirmations — confluence is the whole point.
    sm = signals.get("signature_match")
    if sm is not None and sm >= 0.6:
        pts = 8.0 + 12.0 * min(1.0, (sm - 0.6) / 0.4)      # up to +20
        factors.append(("memelab sig", round(pts, 1)))
        score += pts

    if signals.get("curve_match"):
        factors.append(("curve match", 12.0))
        score += 12.0

    core = int(signals.get("core_alpha") or 0)
    if core:
        pts = min(18.0, 12.0 + 6.0 * (core - 1))
        factors.append(("core-alpha", pts))
        score += pts

    sq = float(signals.get("smart_quality") or 0.0)
    if sq > 0:
        pts = min(12.0, sq * 0.35)                          # 45 pts → ~16, capped 12
        factors.append(("smart money", round(pts, 1)))
        score += pts

    social = signals.get("social")
    if social is not None and social >= 60:
        pts = min(6.0, (social - 60) / 40.0 * 6.0)
        factors.append(("social", round(pts, 1)))
        score += pts

    if signals.get("toxic"):
        factors.append(("toxic buyer", -35.0))
        score -= 35.0

    return Conviction(score=max(0.0, min(100.0, score)), factors=factors)
