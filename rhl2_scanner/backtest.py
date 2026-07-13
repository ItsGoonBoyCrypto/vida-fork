"""Backtesting / historical replay harness.

Replays historical token snapshots through the exact same scoring pipeline
used live, so you can tune thresholds against realized outcomes (win rate,
false-positive rate) before risking capital.

Input: a JSON lines file where each line is a serialized snapshot plus an
optional realized outcome (e.g. max multiple in the following 24h). This
module scores each and reports how the current config would have alerted.

Collecting the historical dataset (DexScreener has limited history; you may
need to archive live snapshots or use a chain-indexer) is left to the
operator — this harness consumes whatever you archive via ``Storage`` alerts
or your own dumps.
"""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from typing import Iterable, Optional

from .config import Config
from .models import AlertLevel, SafetyReport, TokenSnapshot
from .scoring import score_token


def snapshot_from_dict(d: dict) -> TokenSnapshot:
    safety = SafetyReport(**(d.pop("safety", {}) or {}))
    known = {k: v for k, v in d.items() if k in TokenSnapshot.__annotations__}
    snap = TokenSnapshot(**known)
    snap.safety = safety
    return snap


def replay(lines: Iterable[str], cfg: Config) -> dict:
    """Score each JSONL record; return aggregate stats + per-token results."""
    total = alerts = strong = 0
    hits = 0            # alerted AND realized_multiple >= win_threshold
    win_threshold = 2.0
    per_token = []

    for line in lines:
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        realized = rec.pop("realized_multiple", None)
        snap = snapshot_from_dict(rec)
        result = score_token(snap, cfg, strict_safety=False)
        total += 1
        alerted = result.level in (AlertLevel.STRONG, AlertLevel.WATCH)
        if alerted:
            alerts += 1
        if result.level is AlertLevel.STRONG:
            strong += 1
        if alerted and realized is not None and realized >= win_threshold:
            hits += 1
        per_token.append(
            {
                "symbol": snap.symbol,
                "score": round(result.composite, 1),
                "level": result.level.value,
                "realized_multiple": realized,
            }
        )

    precision = (hits / alerts) if alerts else 0.0
    return {
        "scored": total,
        "alerts": alerts,
        "strong": strong,
        "win_threshold": win_threshold,
        "alert_precision": round(precision, 3),
        "results": per_token,
    }


def replay_file(path: str, cfg: Optional[Config] = None) -> dict:
    cfg = cfg or Config.load()
    with open(path, "r", encoding="utf-8") as fh:
        return replay(fh, cfg)


def _ensure_serializable(obj):  # helper for archiving snapshots to JSONL
    if is_dataclass(obj):
        return asdict(obj)
    raise TypeError(type(obj))
