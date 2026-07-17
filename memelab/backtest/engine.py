"""Backtest engine — derive & validate the winner signature.

Given labeled feature vectors, find which early metrics (and in what ranges)
separated big pumpers from duds, and emit a `Signature` the screener applies to
live tokens. Two complementary, dependency-free forms:

  RULES   Per feature, compare the winner vs dud distributions with a
          standardized mean difference (Cohen's d). Features that separate the
          classes become soft gates ("buy_ratio_5m >= midpoint", weighted by
          |d|). Human-readable and easy to sanity-check.
  MODEL   Those same signed effect sizes become linear weights over standardized
          features → a nearest-centroid / diagonal-LDA classifier. Deterministic
          (no iterative fit, no numpy), captures the combined signal.

Guardrails against fooling ourselves:
  · TIME-SPLIT validation (train older, test newer) — memecoin metas drift, so
    a random shuffle would leak the future.
  · Report out-of-sample precision / recall / F1 AND lift (winner-rate in the
    top-scored decile ÷ base rate) — lift is what says "worth trading".
  · Features come from metrics.extract (early-window only) so no post-pump leak.
"""

from __future__ import annotations

import math
import time

from ..models import (
    Chain,
    FeatureVector,
    Outcome,
    Signature,
    signature_to_json,
)

_EPS = 1e-9


def _mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def _std(xs, mean=None):
    if len(xs) < 2:
        return 0.0
    m = _mean(xs) if mean is None else mean
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def _sigmoid(z):
    if z < -60:
        return 0.0
    if z > 60:
        return 1.0
    return 1.0 / (1.0 + math.exp(-z))


def _is_winner(v: FeatureVector) -> bool:
    return v.label == Outcome.WINNER


def derive(vectors: list, win_multiple: float = 3.0, chains: list = None,
           min_effect: float = 0.25, min_coverage: float = 0.4,
           max_rules: int = 12) -> Signature:
    """Fit a Signature from labeled feature vectors (train split only).

    min_effect   keep features whose |Cohen's d| clears this (weak signal cut)
    min_coverage require a feature be present in >= this fraction of samples
    """
    winners = [v for v in vectors if _is_winner(v)]
    duds = [v for v in vectors if v.label in (Outcome.NEUTRAL, Outcome.RUG)]
    sig = Signature(chains=chains or [], win_multiple=win_multiple,
                    trained_on=len(winners) + len(duds), created_ts=0.0)
    if len(winners) < 3 or len(duds) < 3:
        sig.notes = "insufficient labeled samples to derive a signature"
        return sig

    # candidate features = any seen in either class
    names = set()
    for v in vectors:
        names.update(v.features.keys())

    stats: dict = {}
    rules: list = []
    model: dict = {}
    n = len(vectors)
    for name in sorted(names):
        wv = [v.get(name) for v in winners if name in v.features]
        dv = [v.get(name) for v in duds if name in v.features]
        present = sum(1 for v in vectors if name in v.features)
        if present / n < min_coverage or len(wv) < 3 or len(dv) < 3:
            continue
        mw, md = _mean(wv), _mean(dv)
        allv = wv + dv
        pooled = _std(allv, _mean(allv)) or _EPS
        d = (mw - md) / pooled              # signed effect size
        if abs(d) < min_effect:
            continue
        stats[name] = {"mean": _mean(allv), "std": pooled, "mw": mw, "md": md}
        model[name] = d
        rules.append({
            "feature": name,
            "op": ">=" if d > 0 else "<=",
            "value": round((mw + md) / 2.0, 6),   # class midpoint
            "weight": round(abs(d), 4),
        })

    rules.sort(key=lambda r: r["weight"], reverse=True)
    sig.rules = rules[:max_rules]
    sig.model = {"weights": model, "stats": stats, "bias": 0.0}
    # bias: center the decision at the midpoint of class-mean model scores
    sw = _model_raw(_class_center(winners, model, stats), model, stats)
    sd = _model_raw(_class_center(duds, model, stats), model, stats)
    sig.model["bias"] = -((sw + sd) / 2.0)
    return sig


def _class_center(vs, model, stats) -> dict:
    return {name: _mean([v.get(name) for v in vs if name in v.features])
            for name in model if any(name in v.features for v in vs)}


def _model_raw(features: dict, weights: dict, stats: dict) -> float:
    z = 0.0
    for name, w in weights.items():
        st = stats.get(name)
        if not st or name not in features:
            continue
        z += w * ((features[name] - st["mean"]) / (st["std"] or _EPS))
    return z


def model_probability(fv: FeatureVector, sig: Signature) -> float:
    m = sig.model or {}
    z = _model_raw(fv.features, m.get("weights", {}), m.get("stats", {})) + m.get("bias", 0.0)
    return _sigmoid(z)


def validate(signature: Signature, holdout: list) -> dict:
    """Score the holdout, sweep the threshold, return P/R/F1 + lift + base rate."""
    labeled = [v for v in holdout if v.label in (Outcome.WINNER, Outcome.NEUTRAL, Outcome.RUG)]
    if not labeled:
        return {"n": 0}
    scored = [(model_probability(v, signature), _is_winner(v)) for v in labeled]
    n = len(scored)
    base = sum(1 for _, w in scored if w) / n

    best = {"f1": -1.0, "precision": 0.0, "recall": 0.0, "threshold": 0.5}
    for thr in [i / 20 for i in range(1, 20)]:
        tp = sum(1 for p, w in scored if p >= thr and w)
        fp = sum(1 for p, w in scored if p >= thr and not w)
        fn = sum(1 for p, w in scored if p < thr and w)
        prec = tp / (tp + fp) if (tp + fp) else 0.0
        rec = tp / (tp + fn) if (tp + fn) else 0.0
        f1 = (2 * prec * rec / (prec + rec)) if (prec + rec) else 0.0
        if f1 > best["f1"]:
            best = {"f1": round(f1, 3), "precision": round(prec, 3),
                    "recall": round(rec, 3), "threshold": thr}

    # lift: winner rate among the top-scored decile vs base rate
    scored.sort(key=lambda t: t[0], reverse=True)
    top = scored[: max(1, n // 10)]
    top_rate = sum(1 for _, w in top if w) / len(top)
    lift = (top_rate / base) if base else 0.0
    return {"n": n, "base_rate": round(base, 3), "lift": round(lift, 2), **best}


def run_backtest(store, win_multiple: float = 3.0, chains: list = None,
                 train_frac: float = 0.7, now: float = None) -> Signature:
    """Full loop: labeled vectors → time-split → derive → validate → persist.

    Returns the validated Signature (also saved). Run on a schedule so the
    signature tracks the current meta.
    """
    from ..metrics.features import extract
    now = time.time() if now is None else now
    tss = store.labeled_tokens()
    if chains:
        wanted = {c.value for c in chains}
        tss = [t for t in tss if t.chain.value in wanted]
    settled = [t for t in tss if t.outcome in (Outcome.WINNER, Outcome.NEUTRAL, Outcome.RUG)]
    settled.sort(key=lambda t: t.first_seen_ts)          # TIME order for the split
    vectors = [extract(t) for t in settled]
    vectors = [v for v in vectors if v.features]
    if len(vectors) < 10:
        sig = Signature(chains=chains or [], win_multiple=win_multiple, created_ts=now,
                        notes=f"only {len(vectors)} usable samples — keep collecting")
        store.save_signature(signature_to_json(sig))
        return sig

    cut = int(len(vectors) * train_frac)
    train, holdout = vectors[:cut], vectors[cut:]
    sig = derive(train, win_multiple=win_multiple, chains=chains)
    metrics = validate(sig, holdout)
    sig.precision = metrics.get("precision")
    sig.recall = metrics.get("recall")
    sig.created_ts = now
    sig.notes = (f"train={len(train)} holdout={len(holdout)} "
                 f"lift={metrics.get('lift')} base={metrics.get('base_rate')} "
                 f"thr={metrics.get('threshold')}")
    store.save_signature(signature_to_json(sig))
    return sig
