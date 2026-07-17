"""Point-in-time snapshot store — the dataset that makes the platform smarter.

Every poll, we append a TokenSnapshot per tracked token. Over time this becomes
a labeled time-series: features at discovery + how the token actually did. That
history is the training set for the backtest engine — so this store IS the moat.

SQLite to start (mirrors rhl2_scanner, zero-ops); the schema is chain-agnostic
and ports directly to DuckDB/Postgres/Timescale when row counts grow.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import asdict, fields
from typing import Optional

from .models import Chain, Outcome, TokenSnapshot, TokenTimeSeries

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tokens (
    chain           TEXT NOT NULL,
    token_address   TEXT NOT NULL,
    pair_address    TEXT,
    symbol          TEXT,
    first_seen_ts   REAL NOT NULL,
    entry_price     REAL,
    launchpad       TEXT,
    peak_multiple   REAL DEFAULT 1.0,
    trough_multiple REAL DEFAULT 1.0,
    outcome         TEXT DEFAULT 'pending',
    last_snapshot_ts REAL,
    PRIMARY KEY (chain, token_address)
);
CREATE TABLE IF NOT EXISTS snapshots (
    chain          TEXT NOT NULL,
    token_address  TEXT NOT NULL,
    ts             REAL NOT NULL,
    price_usd      REAL,
    market_cap_usd REAL,
    liquidity_usd  REAL,
    json           TEXT NOT NULL,      -- full TokenSnapshot for feature extraction
    PRIMARY KEY (chain, token_address, ts)
);
CREATE INDEX IF NOT EXISTS idx_snap_token ON snapshots(chain, token_address, ts);
CREATE TABLE IF NOT EXISTS signatures (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_ts REAL NOT NULL,
    json       TEXT NOT NULL           -- serialised Signature (active = latest)
);
CREATE TABLE IF NOT EXISTS screen_alerts (
    chain         TEXT NOT NULL,
    token_address TEXT NOT NULL,
    ts            REAL NOT NULL,
    score         REAL,
    PRIMARY KEY (chain, token_address)  -- one alert per token
);
"""


def _snap_to_json(snap: TokenSnapshot) -> str:
    d = asdict(snap)
    d["chain"] = snap.chain.value
    return json.dumps(d)


def _snap_from_json(raw: str) -> TokenSnapshot:
    d = json.loads(raw)
    d["chain"] = Chain(d["chain"])
    valid = {f.name for f in fields(TokenSnapshot)}
    return TokenSnapshot(**{k: v for k, v in d.items() if k in valid})


class Store:
    def __init__(self, path: str = "memelab.db"):
        self.path = path
        if path != ":memory:":
            import os
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # -- ingest ---------------------------------------------------------

    def record_snapshot(self, snap: TokenSnapshot) -> None:
        """Upsert the token row (first-seen/entry) + append one snapshot row.

        The first snapshot for a token sets its entry price/anchor; later ones
        extend the series and bump peak/trough as price moves.
        """
        ch, addr = snap.chain.value, snap.token_address.lower()
        ts = snap.ts or time.time()
        cur = self._conn.execute(
            "SELECT entry_price, peak_multiple, trough_multiple FROM tokens "
            "WHERE chain = ? AND token_address = ?", (ch, addr))
        row = cur.fetchone()
        if row is None:
            self._conn.execute(
                "INSERT INTO tokens (chain, token_address, pair_address, symbol, "
                "first_seen_ts, entry_price, launchpad, last_snapshot_ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (ch, addr, snap.pair_address, snap.symbol, ts, snap.price_usd,
                 snap.launchpad, ts))
            entry = snap.price_usd
        else:
            entry = row["entry_price"]
            if entry and snap.price_usd:
                mult = snap.price_usd / entry
                self._conn.execute(
                    "UPDATE tokens SET peak_multiple = MAX(peak_multiple, ?), "
                    "trough_multiple = MIN(trough_multiple, ?), last_snapshot_ts = ? "
                    "WHERE chain = ? AND token_address = ?",
                    (mult, mult, ts, ch, addr))
            else:
                self._conn.execute(
                    "UPDATE tokens SET last_snapshot_ts = ? WHERE chain = ? AND token_address = ?",
                    (ts, ch, addr))
        self._conn.execute(
            "INSERT OR REPLACE INTO snapshots (chain, token_address, ts, price_usd, "
            "market_cap_usd, liquidity_usd, json) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ch, addr, ts, snap.price_usd, snap.market_cap_usd, snap.liquidity_usd,
             _snap_to_json(snap)))
        self._conn.commit()

    def tracked_tokens(self, chain: Optional[Chain] = None,
                       max_age_hours: float = 72.0) -> list[sqlite3.Row]:
        """Tokens still inside the tracking window — the ones to keep snapshotting."""
        cutoff = time.time() - max_age_hours * 3600.0
        if chain:
            cur = self._conn.execute(
                "SELECT * FROM tokens WHERE chain = ? AND first_seen_ts >= ?",
                (chain.value, cutoff))
        else:
            cur = self._conn.execute(
                "SELECT * FROM tokens WHERE first_seen_ts >= ?", (cutoff,))
        return cur.fetchall()

    # -- backtest reads -------------------------------------------------

    def time_series(self, chain: Chain, token_address: str) -> TokenTimeSeries:
        addr = token_address.lower()
        trow = self._conn.execute(
            "SELECT * FROM tokens WHERE chain = ? AND token_address = ?",
            (chain.value, addr)).fetchone()
        if trow is None:
            return TokenTimeSeries(chain=chain, token_address=addr, first_seen_ts=0.0)
        snaps = [
            _snap_from_json(r["json"]) for r in self._conn.execute(
                "SELECT json FROM snapshots WHERE chain = ? AND token_address = ? ORDER BY ts",
                (chain.value, addr))]
        return TokenTimeSeries(
            chain=chain, token_address=addr, first_seen_ts=trow["first_seen_ts"],
            entry_price=trow["entry_price"], snapshots=snaps,
            peak_multiple=trow["peak_multiple"] or 1.0,
            trough_multiple=trow["trough_multiple"] or 1.0,
            outcome=Outcome(trow["outcome"] or "pending"))

    def labeled_tokens(self, chain: Optional[Chain] = None,
                       min_age_hours: float = 48.0) -> list[TokenTimeSeries]:
        """Tokens old enough to have a settled outcome — the training set."""
        cutoff = time.time() - min_age_hours * 3600.0
        q = "SELECT chain, token_address FROM tokens WHERE first_seen_ts <= ?"
        args: list = [cutoff]
        if chain:
            q += " AND chain = ?"
            args.append(chain.value)
        rows = self._conn.execute(q, args).fetchall()
        return [self.time_series(Chain(r["chain"]), r["token_address"]) for r in rows]

    def set_outcome(self, chain: Chain, token_address: str, outcome: Outcome,
                    peak: float, trough: float) -> None:
        self._conn.execute(
            "UPDATE tokens SET outcome = ?, peak_multiple = ?, trough_multiple = ? "
            "WHERE chain = ? AND token_address = ?",
            (outcome.value, peak, trough, chain.value, token_address.lower()))
        self._conn.commit()

    # -- signatures -----------------------------------------------------

    def save_signature(self, signature_json: str) -> None:
        self._conn.execute("INSERT INTO signatures (created_ts, json) VALUES (?, ?)",
                           (time.time(), signature_json))
        self._conn.commit()

    def active_signature(self) -> Optional[str]:
        row = self._conn.execute(
            "SELECT json FROM signatures ORDER BY created_ts DESC LIMIT 1").fetchone()
        return row["json"] if row else None

    # -- screen-alert dedup ---------------------------------------------

    def screen_alert_is_new(self, chain: Chain, token_address: str, score: float) -> bool:
        """True (and records it) if this token hasn't been alerted before."""
        cur = self._conn.execute(
            "SELECT 1 FROM screen_alerts WHERE chain = ? AND token_address = ?",
            (chain.value, token_address.lower()))
        if cur.fetchone() is not None:
            return False
        self._conn.execute(
            "INSERT INTO screen_alerts (chain, token_address, ts, score) VALUES (?, ?, ?, ?)",
            (chain.value, token_address.lower(), time.time(), score))
        self._conn.commit()
        return True

    # -- coverage stats -------------------------------------------------

    def coverage(self) -> dict:
        out = {"by_chain": {}, "total_tokens": 0, "total_snapshots": 0}
        for r in self._conn.execute(
                "SELECT chain, COUNT(*) n, SUM(CASE WHEN outcome='winner' THEN 1 ELSE 0 END) w "
                "FROM tokens GROUP BY chain"):
            out["by_chain"][r["chain"]] = {"tokens": r["n"], "winners": r["w"]}
            out["total_tokens"] += r["n"]
        out["total_snapshots"] = self._conn.execute(
            "SELECT COUNT(*) FROM snapshots").fetchone()[0]
        return out
