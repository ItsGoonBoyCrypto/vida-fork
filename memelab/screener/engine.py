"""Live screener — score live tokens against the active Signature.

The production output. For each live token (its snapshots so far) compute the
SAME early features used in training and score them → a 0-100 pump-likelihood
plus the human-readable rules it matched. High scorers become alerts.

Uses metrics.extract_live (identical math to training) → no train/serve skew,
and backtest.model_probability for the model score, so screening and validation
agree by construction.
"""

from __future__ import annotations

from ..backtest.engine import model_probability
from ..metrics.features import extract_live
from ..models import (
    Chain,
    FeatureVector,
    Screen,
    Signature,
    TokenSnapshot,
    signature_from_json,
)


def _rule_holds(rule: dict, fv: FeatureVector) -> bool:
    if rule["feature"] not in fv.features:
        return False
    val = fv.get(rule["feature"])
    return val >= rule["value"] if rule["op"] == ">=" else val <= rule["value"]


def score_vector(fv: FeatureVector, sig: Signature):
    """Return (score_0_100, matched_rules, reasons) for a feature vector."""
    matched, reasons = [], []
    total_w = sum(r["weight"] for r in sig.rules) or 0.0
    hit_w = 0.0
    for r in sig.rules:
        if _rule_holds(r, fv):
            hit_w += r["weight"]
            matched.append(r["feature"])
            arrow = "≥" if r["op"] == ">=" else "≤"
            reasons.append(f"{r['feature']} {arrow} {r['value']:g}")
    rule_score = (hit_w / total_w) if total_w else None
    model_score = model_probability(fv, sig) if sig.model.get("weights") else None

    parts = [s for s in (rule_score, model_score) if s is not None]
    score01 = sum(parts) / len(parts) if parts else 0.0
    return round(score01 * 100, 1), matched, reasons


class Screener:
    def __init__(self, store):
        self.store = store
        self._sig: Signature = None

    def reload_signature(self) -> bool:
        """Pick up the latest signature the backtest engine produced."""
        raw = self.store.active_signature()
        self._sig = signature_from_json(raw) if raw else None
        return self._sig is not None

    def ready(self) -> bool:
        return self._sig is not None and bool(self._sig.rules or self._sig.model.get("weights"))

    def screen(self, chain: Chain, token_address: str, snapshots: list) -> Screen:
        if self._sig is None:
            self.reload_signature()
        fv = extract_live(chain, token_address, snapshots)
        last = snapshots[-1] if snapshots else TokenSnapshot(chain=chain, token_address=token_address)
        if not self.ready():
            return Screen(snapshot=last, score=0.0, reasons=["no signature yet — still collecting"])
        sc, matched, reasons = score_vector(fv, self._sig)
        return Screen(snapshot=last, score=sc, matched_rules=matched, reasons=reasons)

    def min_confident_score(self) -> float:
        """Alert threshold, scaled by the signature's validated precision so a
        weak/undertrained signature doesn't spray alerts."""
        if self._sig is None or self._sig.precision is None:
            return 101.0                       # never alert without a validated signature
        # higher precision → we trust lower scores; floor at 55
        return max(55.0, 80.0 - 30.0 * self._sig.precision)


def rank_live(store, screener: "Screener", chain=None, min_score: float = 0.0,
              limit: int = 50, max_age_hours: float = 24.0) -> list:
    """Score every live tracked token vs the signature, return top-ranked rows.

    Backs the dashboard /screen endpoint. Chain-agnostic; reuses screen()."""
    from ..models import Chain
    if not screener.ready():
        return []
    out = []
    for row in store.tracked_tokens(chain, max_age_hours=max_age_hours):
        ts = store.time_series(Chain(row["chain"]), row["token_address"])
        if not ts.snapshots:
            continue
        res = screener.screen(ts.chain, row["token_address"], ts.snapshots)
        if res.score < min_score:
            continue
        s = res.snapshot
        out.append({
            "chain": s.chain.value, "symbol": s.symbol, "token": s.token_address,
            "score": res.score, "market_cap": s.market_cap_usd,
            "liquidity": s.liquidity_usd, "age_minutes": s.age_minutes,
            "reasons": res.reasons, "pair": s.pair_address})
    out.sort(key=lambda x: x["score"], reverse=True)
    return out[:limit]
