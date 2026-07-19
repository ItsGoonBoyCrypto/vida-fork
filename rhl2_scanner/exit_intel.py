"""Exit intelligence — a LEARNED take-profit signal.

Catching entries is half the game; getting out is the other half. The dump guard
only fires AFTER a big give-back. This model learns, from your own settled
winners, the multiple range where tokens *historically topped*, then fires a
proactive "⏏️ top zone" heads-up on a live position the moment it enters that
range and starts to turn — before the full dump.

learn_exit_model() reads settled winners' peak multiples and takes percentiles
(p50/p75). top_zone() flags a live position that has (a) run up past the learned
zone AND (b) already ticked down modestly from its own peak (the turn starting),
so it's an early, actionable take-profit — not a lagging dump alert.

Pure + dependency-free; dormant until enough winners exist to learn from.
"""

from __future__ import annotations

from typing import Optional


def _pct(sorted_vals: list, q: float) -> float:
    if not sorted_vals:
        return 0.0
    i = min(len(sorted_vals) - 1, max(0, int(round(q * (len(sorted_vals) - 1)))))
    return sorted_vals[i]


def learn_exit_model(rows, win_multiple: float = 2.0, min_winners: int = 8) -> Optional[dict]:
    """Learn the typical-peak zone from settled winners. None until enough exist.

    Returns {p50, p75, n} — the multiple percentiles at which past winners
    peaked. p50 = half of winners had already topped by this multiple.
    """
    peaks = sorted(
        (r["max_mult"] or 1.0) for r in rows
        if r["settled"] and (r["max_mult"] or 1.0) >= win_multiple)
    if len(peaks) < min_winners:
        return None
    return {"p50": round(_pct(peaks, 0.50), 2),
            "p75": round(_pct(peaks, 0.75), 2),
            "n": len(peaks)}


def top_zone(current_mult: float, peak_mult: float, model: Optional[dict],
             early_giveback_pct: float = 15.0) -> tuple[bool, str]:
    """True if a live position is in the learned top zone AND starting to turn.

    Conditions: (a) it has run up to at least the median winner-peak (p50), so
    most past winners had topped by here; (b) it's ticked down at least
    early_giveback_pct from its OWN peak — the reversal beginning — but not so
    far it's already a full dump. Returns (fire, reason).
    """
    if not model or not peak_mult or peak_mult <= 1.0:
        return False, ""
    if current_mult < model["p50"]:
        return False, ""
    giveback = (1.0 - (current_mult / peak_mult)) * 100.0
    if giveback < early_giveback_pct:
        return False, ""            # still climbing / at peak — not turning yet
    reason = (f"in the learned top zone (winners median ~{model['p50']:g}x) and "
              f"−{giveback:.0f}% off its {peak_mult:.1f}x peak")
    return True, reason
