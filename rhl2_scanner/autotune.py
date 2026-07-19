"""Bounded, reversible auto-tune of the composite scoring weights.

The scanner ships with hand-picked category weights (safety / distribution /
momentum / discovery). Once enough alerts have *settled* (we know whether they
2x'd or rugged), the ``by_signal`` split in the paper report tells us which
signal actually discriminated winners from rugs on this chain. This module
nudges the live weights toward the signals that earned their keep — but on a
tight leash:

  * DORMANT until ``min_settled`` alerts have settled (no acting on noise).
  * Each category needs a real high AND low bucket (``min_bucket`` each) before
    its edge counts — otherwise its weight is left alone.
  * Every step is small (``step``) and every weight is hard-clamped to
    ``baseline ± max_drift``, so the model can drift but never blow up.
  * REVERSIBLE: the override is a single kv row; ``reset`` deletes it and the
    scanner falls straight back to the config baseline. History is logged.

The core (``propose_weights``) is a pure function — no I/O — so it's trivially
testable. ``apply`` / ``load_override`` / ``reset`` handle persistence.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Optional

log = logging.getLogger("rhl2.autotune")

_KV_KEY = "autotune:weights"
_CATS = ("safety", "distribution", "momentum", "discovery")


def _edge(cat_report: dict, min_bucket: int) -> Optional[float]:
    """How well being HIGH on this signal separated winners from rugs.

    edge = (hit_high - hit_low) - (rug_high - rug_low).  Positive → high values
    of this signal precede more wins and fewer rugs (worth more weight). None
    when either bucket is too thin to trust.
    """
    hi = cat_report.get("high") or {}
    lo = cat_report.get("low") or {}
    if (hi.get("n") or 0) < min_bucket or (lo.get("n") or 0) < min_bucket:
        return None
    hit_gap = (hi.get("hit_rate") or 0.0) - (lo.get("hit_rate") or 0.0)
    rug_gap = (hi.get("rug_rate") or 0.0) - (lo.get("rug_rate") or 0.0)
    return hit_gap - rug_gap


def propose_weights(
    report: dict,
    baseline: dict,
    current: dict,
    *,
    min_settled: int = 30,
    min_bucket: int = 5,
    step: float = 0.02,
    max_drift: float = 0.08,
    min_weight: float = 0.05,
) -> dict:
    """Compute a nudged weight set from settled-alert performance.

    Returns a dict: ``{"weights": {...}|None, "changed": bool, "reason": str,
    "edges": {...}}``. ``weights`` is None (and ``changed`` False) when gated or
    when nothing moved meaningfully.
    """
    settled = int(report.get("settled") or 0)
    if settled < min_settled:
        return {"weights": None, "changed": False,
                "reason": f"dormant — {settled}/{min_settled} settled", "edges": {}}

    by_signal = report.get("by_signal") or {}
    edges: dict = {}
    for cat in _CATS:
        e = _edge(by_signal.get(cat) or {}, min_bucket)
        if e is not None:
            edges[cat] = e
    if len(edges) < 2:
        return {"weights": None, "changed": False,
                "reason": "not enough discriminating signals yet", "edges": edges}

    # Centre the edges so the nudges are (near) zero-sum: reward above-average
    # signals, tax below-average ones, leave under-sampled categories put.
    mean_edge = sum(edges.values()) / len(edges)
    span = max((max(edges.values()) - min(edges.values())), 1e-6)

    proposed = dict(current)
    for cat, e in edges.items():
        delta = step * ((e - mean_edge) / span)          # within ±step
        proposed[cat] = current.get(cat, 0.0) + delta

    # Normalise for readability, then HARD-clamp to the leash as the final word.
    # (The scorer re-normalises at score time, so the clamp — not the sum — is the
    # guarantee that matters: no weight may ever leave baseline ± max_drift.)
    total = sum(proposed.values())
    if total <= 0:
        return {"weights": None, "changed": False, "reason": "degenerate", "edges": edges}
    proposed = {k: v / total for k, v in proposed.items()}
    for k in list(proposed):
        base = baseline.get(k, proposed[k])
        proposed[k] = max(min_weight, min(base + max_drift, max(base - max_drift, proposed[k])))

    moved = max(abs(proposed[c] - current.get(c, 0.0)) for c in proposed)
    if moved < 1e-4:
        return {"weights": None, "changed": False,
                "reason": "converged — no meaningful change", "edges": edges}

    return {"weights": proposed, "changed": True,
            "reason": f"tuned off {settled} settled alerts", "edges": edges}


# -- persistence -------------------------------------------------------------

def load_override(storage) -> Optional[dict]:
    """Return the persisted tuned weights, or None if never tuned / cleared."""
    try:
        raw = storage.kv_get(_KV_KEY)
    except Exception:  # noqa: BLE001
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
        w = data.get("weights")
        return w if isinstance(w, dict) and w else None
    except (ValueError, TypeError):
        return None


def apply(storage, result: dict) -> bool:
    """Persist a proposal (from ``propose_weights``). Returns True if written."""
    if not result.get("changed") or not result.get("weights"):
        return False
    payload = {
        "weights": result["weights"],
        "reason": result.get("reason", ""),
        "edges": result.get("edges", {}),
        "ts": time.time(),
    }
    try:
        storage.kv_set(_KV_KEY, json.dumps(payload))
    except Exception:  # noqa: BLE001
        log.exception("autotune persist failed")
        return False
    log.info("autotune applied: %s | %s", result["weights"], result.get("reason"))
    return True


def reset(storage) -> None:
    """Drop the override — the scanner reverts to its config baseline."""
    try:
        storage.kv_set(_KV_KEY, "")
    except Exception:  # noqa: BLE001
        log.exception("autotune reset failed")


def describe(storage, baseline: dict) -> str:
    """Human-readable current state for the /autotune command."""
    try:
        raw = storage.kv_get(_KV_KEY)
    except Exception:  # noqa: BLE001
        raw = None
    if not raw:
        return ("baseline (untuned): " +
                " ".join(f"{k} {baseline.get(k, 0):.2f}" for k in _CATS))
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return "baseline (override unreadable)"
    w = data.get("weights") or {}
    parts = []
    for k in _CATS:
        cur = w.get(k)
        base = baseline.get(k, 0.0)
        if cur is None:
            continue
        arrow = "▲" if cur > base + 1e-3 else "▼" if cur < base - 1e-3 else "="
        parts.append(f"{k} {cur:.2f}{arrow}")
    return " ".join(parts) + f"  ({data.get('reason', '')})"
