"""SQLite persistence: seen tokens, scores, alert history, performance.

Keeps the scanner idempotent across restarts (dedupe by pair address) and
enables the re-alert cooldown plus later performance backtesting/attribution.
Uses only the stdlib ``sqlite3`` module; writes are small and synchronous,
run off the event loop via ``asyncio.to_thread`` from the scanner.
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Optional

from .models import ScoreResult, TokenSnapshot

_SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_tokens (
    pair_address   TEXT PRIMARY KEY,
    token_address  TEXT NOT NULL,
    symbol         TEXT,
    first_seen     REAL NOT NULL,
    last_scored    REAL,
    last_alerted   REAL,
    best_score     REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS alerts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    pair_address   TEXT NOT NULL,
    ts             REAL NOT NULL,
    level          TEXT NOT NULL,
    score          REAL NOT NULL,
    snapshot_json  TEXT,
    breakdown_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_alerts_pair ON alerts(pair_address);
"""


class Storage:
    def __init__(self, path: str):
        self.path = path
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # -- dedupe / cooldown ----------------------------------------------

    def is_new(self, pair_address: str) -> bool:
        cur = self._conn.execute(
            "SELECT 1 FROM seen_tokens WHERE pair_address = ?", (pair_address.lower(),)
        )
        return cur.fetchone() is None

    def mark_seen(self, snap: TokenSnapshot) -> None:
        self._conn.execute(
            """INSERT OR IGNORE INTO seen_tokens
               (pair_address, token_address, symbol, first_seen)
               VALUES (?, ?, ?, ?)""",
            (snap.pair_address.lower(), snap.token_address.lower(), snap.symbol, time.time()),
        )
        self._conn.commit()

    def last_alerted(self, pair_address: str) -> Optional[float]:
        cur = self._conn.execute(
            "SELECT last_alerted FROM seen_tokens WHERE pair_address = ?",
            (pair_address.lower(),),
        )
        row = cur.fetchone()
        return row["last_alerted"] if row else None

    def in_cooldown(self, pair_address: str, cooldown_seconds: float) -> bool:
        last = self.last_alerted(pair_address)
        return last is not None and (time.time() - last) < cooldown_seconds

    # -- recording ------------------------------------------------------

    def record_score(self, snap: TokenSnapshot, result: ScoreResult) -> None:
        self._conn.execute(
            """UPDATE seen_tokens
               SET last_scored = ?, best_score = MAX(best_score, ?)
               WHERE pair_address = ?""",
            (time.time(), result.composite, snap.pair_address.lower()),
        )
        self._conn.commit()

    def record_alert(self, snap: TokenSnapshot, result: ScoreResult) -> None:
        now = time.time()
        breakdown = {
            c.name: {"raw": round(c.raw, 1), "reasons": c.reasons, "penalties": c.penalties}
            for c in result.categories
        }
        self._conn.execute(
            """INSERT INTO alerts
               (pair_address, ts, level, score, snapshot_json, breakdown_json)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                snap.pair_address.lower(),
                now,
                result.level.value,
                result.composite,
                json.dumps(_snap_summary(snap)),
                json.dumps(breakdown),
            ),
        )
        self._conn.execute(
            "UPDATE seen_tokens SET last_alerted = ? WHERE pair_address = ?",
            (now, snap.pair_address.lower()),
        )
        self._conn.commit()


def _snap_summary(snap: TokenSnapshot) -> dict:
    return {
        "symbol": snap.symbol,
        "token": snap.token_address,
        "mcap": snap.market_cap_usd,
        "liq": snap.liquidity_usd,
        "age_min": snap.age_minutes,
        "holders": snap.holder_count,
        "top10": snap.top10_supply_pct,
        "vol_1h": snap.volume_1h,
        "buy_ratio_1h": snap.buy_ratio_1h,
        "smart_money": len(snap.smart_money_wallets),
    }
