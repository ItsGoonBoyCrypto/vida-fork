"""Bootstrap prior — a hand-crafted signature so memelab is useful from day one.

The platform is cold until enough tokens age ~48h and a data-derived signature
validates. Rather than sit silent, we seed a PRIOR signature built from the same
meme-pumper heuristics proven in the RH scanner: early buy pressure, smart-money
presence, accelerating volume, healthy distribution, real/growing liquidity, and
clean safety. It alerts conservatively (modest precision → higher score floor).

Once the backtest engine derives a signature with real rules from live data, that
replaces the prior automatically (engine.run_backtest only overwrites when it has
learned something; otherwise the prior stays active).
"""

from __future__ import annotations

from .models import Chain, Signature

# (feature, op, value, weight) — one-sided soft gates over metrics.features.
_PRIOR_RULES = [
    ("buy_ratio_5m",       ">=", 0.62, 1.0),   # fresh buy dominance
    ("smart_money_count",  ">=", 1.0,  1.0),   # a known sharp wallet is in
    ("vol5m_to_vol1h",     ">=", 1.3,  0.8),   # volume accelerating right now
    ("buy_pressure_trend", ">=", 0.0,  0.6),   # 5m buying >= 1h baseline
    ("holder_velocity",    ">=", 1.0,  0.6),   # holders/min growing
    ("liq_growth",         ">=", 1.0,  0.5),   # liquidity deepening, not draining
    ("liq_to_mcap",        ">=", 0.06, 0.5),   # real float vs a thin trap
    ("top10_pct",          "<=", 45.0, 0.8),   # not overly concentrated
    ("dev_holdings_pct",   "<=", 15.0, 0.6),   # dev not holding the bag
    ("is_sellable",        ">=", 1.0,  0.7),   # not a confirmed honeypot
    ("lp_safe",            ">=", 1.0,  0.4),   # LP burned/locked
    ("launchpad_flag",     ">=", 1.0,  0.5),   # from a recognised launchpad
    ("has_socials",        ">=", 1.0,  0.3),   # has a community presence
]


def default_signature(chains: list = None) -> Signature:
    rules = [{"feature": f, "op": op, "value": v, "weight": w}
             for (f, op, v, w) in _PRIOR_RULES]
    return Signature(
        chains=chains or list(Chain),
        rules=rules,
        model={},                       # rules-only until data trains a model
        win_multiple=3.0,
        trained_on=0,
        precision=0.5,                  # conservative — sets a higher alert floor
        recall=None,
        created_ts=0.0,
        notes="bootstrap prior (heuristic) — replaced once a data-derived "
              "signature validates",
    )


def is_prior(sig: Signature) -> bool:
    return bool(sig and sig.trained_on == 0 and "bootstrap prior" in (sig.notes or ""))
