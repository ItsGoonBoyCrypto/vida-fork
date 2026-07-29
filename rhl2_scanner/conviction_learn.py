"""Closed-loop conviction learning.

Conviction fuses independent signals with HAND-picked weights. /perf proved
hand-weights can be flat wrong (momentum/discovery were inverted), so this
learns, from settled outcomes, how much each conviction factor ACTUALLY lifts
the odds of a winner — and feeds a per-factor multiplier back into fuse().

Method (robust on small samples, not a black box):
  for each factor, compare the win-rate of alerts WHERE IT WAS ACTIVE against
  the overall base win-rate, using the Wilson lower bound so a 2/2 fluke doesn't
  swing the weight. multiplier = clamp(lift), shrunk toward 1.0 when n is small.

A factor that historically preceded winners gets amplified; one that didn't
(or preceded rugs) gets damped toward zero. Pure + dependency-free.
"""

from __future__ import annotations

from typing import Optional

# factor labels produced by conviction.fuse() (must match)
FACTORS = ("memelab sig", "curve match", "core-alpha", "smart money",
           "social", "toxic buyer")

_MIN_ACTIVE = 5          # need at least this many alerts with a factor to learn it
_MULT_LO, _MULT_HI = 0.0, 1.8   # clamp learned multipliers


def _wilson_lb(succ: int, n: int, z: float = 1.96) -> float:
    if n <= 0:
        return 0.0
    p = succ / n
    z2 = z * z
    return max(0.0, (p + z2 / (2 * n) - z * ((p * (1 - p) / n + z2 / (4 * n * n)) ** 0.5))
               / (1 + z2 / n))


def learn_multipliers(rows, win_multiple: float = 2.0) -> dict:
    """rows: iterable of (factor_labels: list[str], max_mult: float, settled: bool).

    Returns {factor_label: multiplier}. Absent factors default to 1.0 at apply
    time. A negative-lift factor (esp. a hard-negative like 'toxic buyer') is
    damped below 1.0; a strong-lift factor is amplified up to _MULT_HI.
    """
    rows = [r for r in rows if r[2]]        # settled only
    n_all = len(rows)
    if n_all < _MIN_ACTIVE:
        return {}                            # not enough data — keep hand-weights
    base = sum(1 for r in rows if (r[1] or 1.0) >= win_multiple) / n_all
    base = max(base, 1e-6)
    out: dict = {}
    for f in FACTORS:
        active = [r for r in rows if f in (r[0] or [])]
        if len(active) < _MIN_ACTIVE:
            continue
        wins = sum(1 for r in active if (r[1] or 1.0) >= win_multiple)
        wr = _wilson_lb(wins, len(active))
        lift = wr / base
        # shrink toward 1.0 by sample size (more data -> trust the lift more)
        trust = min(1.0, len(active) / 30.0)
        mult = 1.0 + trust * (lift - 1.0)
        out[f] = round(min(_MULT_HI, max(_MULT_LO, mult)), 3)
    return out


def apply_multiplier(label: str, points: float, mults: Optional[dict]) -> float:
    """Scale a factor's points by its learned multiplier (1.0 if unlearned)."""
    if not mults or label not in mults:
        return points
    return points * mults[label]
