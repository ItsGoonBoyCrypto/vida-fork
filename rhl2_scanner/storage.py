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
    best_score     REAL DEFAULT 0,
    best_alert_rank INTEGER DEFAULT -1
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

CREATE TABLE IF NOT EXISTS paper_trades (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    pair_address     TEXT NOT NULL,
    token_address    TEXT,
    symbol           TEXT,
    chain            TEXT,
    entry_ts         REAL NOT NULL,
    entry_price      REAL,
    entry_mcap       REAL,
    entry_liq        REAL,
    score            REAL,
    level            TEXT,
    safety_passed    INTEGER,
    breakdown_json   TEXT,
    checkpoints_json TEXT DEFAULT '{}',
    max_mult         REAL DEFAULT 1.0,
    min_mult         REAL DEFAULT 1.0,
    last_price       REAL,
    last_checked_ts  REAL,
    settled          INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_paper_open ON paper_trades(settled);
CREATE INDEX IF NOT EXISTS idx_paper_pair ON paper_trades(pair_address);

CREATE TABLE IF NOT EXISTS wallet_seen (
    tx_key  TEXT PRIMARY KEY,   -- wallet|txhash|token
    ts      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS muted_tokens (
    token   TEXT PRIMARY KEY,   -- lowercased contract address, alerts suppressed
    ts      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS kv (
    k  TEXT PRIMARY KEY,
    v  TEXT
);

CREATE TABLE IF NOT EXISTS smart_wallets (
    wallet  TEXT PRIMARY KEY,   -- lowercased address; a curated "smart money" wallet
    ts      REAL NOT NULL,
    source  TEXT,               -- "manual" | "auto:<token>" (harvested from a winner)
    note    TEXT
);

CREATE TABLE IF NOT EXISTS blocked_symbols (
    symbol  TEXT PRIMARY KEY,   -- normalised (uppercase, alnum) blocked ticker
    ts      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS smart_buys (
    token   TEXT NOT NULL,      -- lowercased token address
    wallet  TEXT NOT NULL,      -- lowercased smart-money wallet that bought it
    ts      REAL NOT NULL,
    PRIMARY KEY (token, wallet)  -- one row per (token, wallet); ts = first buy
);
CREATE INDEX IF NOT EXISTS idx_smart_buys_token ON smart_buys(token);

CREATE TABLE IF NOT EXISTS cluster_alerts (
    token   TEXT PRIMARY KEY,   -- token we already fired a cluster alert for
    ts      REAL NOT NULL,
    n       INTEGER             -- distinct buyers at alert time
);
"""


class Storage:
    def __init__(self, path: str):
        self.path = path
        # Ensure the parent dir exists (e.g. a freshly-mounted Railway Volume at
        # /app/data) so opening the DB file doesn't fail on first boot.
        if path and path != ":memory:":
            import os
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """Additive migrations for DBs created before a column existed."""
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(seen_tokens)")}
        if "best_alert_rank" not in cols:
            self._conn.execute(
                "ALTER TABLE seen_tokens ADD COLUMN best_alert_rank INTEGER DEFAULT -1")

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

    def alert_rank(self, pair_address: str) -> int:
        """Highest alert tier this token has been alerted at (-1 = never).

        Tiers: 0 = early launch, 1 = watch, 2 = strong. A later cycle reaching a
        higher tier is an *escalation* — worth an immediate upgrade re-alert.
        """
        cur = self._conn.execute(
            "SELECT best_alert_rank FROM seen_tokens WHERE pair_address = ?",
            (pair_address.lower(),),
        )
        row = cur.fetchone()
        if row is None or row["best_alert_rank"] is None:
            return -1
        return int(row["best_alert_rank"])

    # -- recording ------------------------------------------------------

    def record_score(self, snap: TokenSnapshot, result: ScoreResult) -> None:
        self._conn.execute(
            """UPDATE seen_tokens
               SET last_scored = ?, best_score = MAX(best_score, ?)
               WHERE pair_address = ?""",
            (time.time(), result.composite, snap.pair_address.lower()),
        )
        self._conn.commit()

    def record_alert(self, snap: TokenSnapshot, result: ScoreResult,
                     rank: Optional[int] = None) -> None:
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
        if rank is None:
            self._conn.execute(
                "UPDATE seen_tokens SET last_alerted = ? WHERE pair_address = ?",
                (now, snap.pair_address.lower()),
            )
        else:
            self._conn.execute(
                """UPDATE seen_tokens
                   SET last_alerted = ?, best_alert_rank = MAX(best_alert_rank, ?)
                   WHERE pair_address = ?""",
                (now, rank, snap.pair_address.lower()),
            )
        self._conn.commit()

    # -- paper trading / calibration ------------------------------------

    def has_open_paper_trade(self, pair_address: str) -> bool:
        cur = self._conn.execute(
            "SELECT 1 FROM paper_trades WHERE pair_address = ? AND settled = 0",
            (pair_address.lower(),),
        )
        return cur.fetchone() is not None

    def open_paper_trade(self, snap: TokenSnapshot, result: ScoreResult) -> Optional[int]:
        """Record a would-be entry. One open trade per pair; returns row id."""
        if self.has_open_paper_trade(snap.pair_address):
            return None
        now = time.time()
        breakdown = {c.name: round(c.raw, 1) for c in result.categories}
        cur = self._conn.execute(
            """INSERT INTO paper_trades
               (pair_address, token_address, symbol, chain, entry_ts, entry_price,
                entry_mcap, entry_liq, score, level, safety_passed, breakdown_json,
                last_price, last_checked_ts)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                snap.pair_address.lower(),
                snap.token_address.lower(),
                snap.symbol,
                snap.chain,
                now,
                snap.price_usd,
                snap.market_cap_usd,
                snap.liquidity_usd,
                result.composite,
                result.level.value,
                1 if result.safety_passed else 0,
                json.dumps(breakdown),
                snap.price_usd,
                now,
            ),
        )
        self._conn.commit()
        return cur.lastrowid

    def open_paper_trades(self) -> list[sqlite3.Row]:
        cur = self._conn.execute("SELECT * FROM paper_trades WHERE settled = 0")
        return cur.fetchall()

    def update_paper_trade(
        self,
        trade_id: int,
        last_price: float,
        checkpoints: dict,
        max_mult: float,
        min_mult: float,
        settled: bool,
    ) -> None:
        self._conn.execute(
            """UPDATE paper_trades
               SET last_price = ?, checkpoints_json = ?, max_mult = ?, min_mult = ?,
                   last_checked_ts = ?, settled = ?
               WHERE id = ?""",
            (last_price, json.dumps(checkpoints), max_mult, min_mult,
             time.time(), 1 if settled else 0, trade_id),
        )
        self._conn.commit()

    def all_paper_trades(self) -> list[sqlite3.Row]:
        cur = self._conn.execute("SELECT * FROM paper_trades ORDER BY entry_ts")
        return cur.fetchall()

    # -- wallet-activity dedup ------------------------------------------

    def wallet_event_is_new(self, tx_key: str) -> bool:
        cur = self._conn.execute("SELECT 1 FROM wallet_seen WHERE tx_key = ?", (tx_key,))
        return cur.fetchone() is None

    def mark_wallet_event(self, tx_key: str) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO wallet_seen (tx_key, ts) VALUES (?, ?)",
            (tx_key, time.time()),
        )
        self._conn.commit()

    # -- muted tokens (/zero) + key-value ------------------------------

    def mute_token(self, token: str) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO muted_tokens (token, ts) VALUES (?, ?)",
            (token.lower(), time.time()),
        )
        self._conn.commit()

    def unmute_token(self, token: str) -> None:
        self._conn.execute("DELETE FROM muted_tokens WHERE token = ?", (token.lower(),))
        self._conn.commit()

    def is_muted(self, token: str) -> bool:
        if not token:
            return False
        cur = self._conn.execute("SELECT 1 FROM muted_tokens WHERE token = ?", (token.lower(),))
        return cur.fetchone() is not None

    def muted_tokens(self) -> list[str]:
        cur = self._conn.execute("SELECT token FROM muted_tokens ORDER BY ts DESC")
        return [r["token"] for r in cur.fetchall()]

    # -- smart-money wallet set (seeding) -------------------------------

    def add_smart_wallet(self, wallet: str, source: str = "manual",
                         note: str = "") -> bool:
        """Add a curated smart-money wallet. Returns True if newly added."""
        w = wallet.lower()
        cur = self._conn.execute("SELECT 1 FROM smart_wallets WHERE wallet = ?", (w,))
        existed = cur.fetchone() is not None
        self._conn.execute(
            "INSERT OR IGNORE INTO smart_wallets (wallet, ts, source, note) VALUES (?, ?, ?, ?)",
            (w, time.time(), source, note),
        )
        self._conn.commit()
        return not existed

    def remove_smart_wallet(self, wallet: str) -> None:
        self._conn.execute("DELETE FROM smart_wallets WHERE wallet = ?", (wallet.lower(),))
        self._conn.commit()

    def smart_wallets(self) -> list[str]:
        cur = self._conn.execute("SELECT wallet FROM smart_wallets ORDER BY ts DESC")
        return [r["wallet"] for r in cur.fetchall()]

    def smart_wallets_detailed(self) -> list[sqlite3.Row]:
        cur = self._conn.execute(
            "SELECT wallet, source, note, ts FROM smart_wallets ORDER BY ts DESC")
        return cur.fetchall()

    # -- blocked scam symbols (/block) ----------------------------------

    def block_symbol(self, symbol: str) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO blocked_symbols (symbol, ts) VALUES (?, ?)",
            (symbol.upper(), time.time()),
        )
        self._conn.commit()

    def unblock_symbol(self, symbol: str) -> None:
        self._conn.execute("DELETE FROM blocked_symbols WHERE symbol = ?", (symbol.upper(),))
        self._conn.commit()

    def blocked_symbols(self) -> list[str]:
        cur = self._conn.execute("SELECT symbol FROM blocked_symbols ORDER BY symbol")
        return [r["symbol"] for r in cur.fetchall()]

    # -- smart-money cluster tracking -----------------------------------

    def record_smart_buy(self, token: str, wallet: str) -> None:
        """Record that a smart wallet bought a token (first buy timestamp kept)."""
        self._conn.execute(
            "INSERT OR IGNORE INTO smart_buys (token, wallet, ts) VALUES (?, ?, ?)",
            (token.lower(), wallet.lower(), time.time()),
        )
        self._conn.commit()

    def distinct_smart_buyers(self, token: str, since_ts: float) -> list[str]:
        """Distinct smart wallets that bought ``token`` at/after ``since_ts``."""
        cur = self._conn.execute(
            "SELECT wallet FROM smart_buys WHERE token = ? AND ts >= ? ORDER BY ts",
            (token.lower(), since_ts),
        )
        return [r["wallet"] for r in cur.fetchall()]

    def cluster_already_alerted(self, token: str) -> bool:
        cur = self._conn.execute(
            "SELECT 1 FROM cluster_alerts WHERE token = ?", (token.lower(),))
        return cur.fetchone() is not None

    def mark_cluster_alert(self, token: str, n: int) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO cluster_alerts (token, ts, n) VALUES (?, ?, ?)",
            (token.lower(), time.time(), n),
        )
        self._conn.commit()

    def kv_get(self, key: str) -> Optional[str]:
        cur = self._conn.execute("SELECT v FROM kv WHERE k = ?", (key,))
        row = cur.fetchone()
        return row["v"] if row else None

    def kv_set(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO kv (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v",
            (key, value),
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
