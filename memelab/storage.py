"""Point-in-time snapshot store — the dataset that makes the platform smarter.

Every poll, we append a TokenSnapshot per tracked token. Over time this becomes
a labeled time-series: features at discovery + how the token actually did. That
history is the training set for the backtest engine — so this store IS the moat.

Start with SQLite (mirrors rhl2_scanner); swap for Postgres/Timescale/DuckDB
when the row count grows. Schema is chain-agnostic.
"""

from __future__ import annotations

from typing import Optional

from .models import Chain, Outcome, TokenSnapshot, TokenTimeSeries

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tokens (
    chain         TEXT NOT NULL,
    token_address TEXT NOT NULL,
    pair_address  TEXT,
    symbol        TEXT,
    first_seen_ts REAL NOT NULL,
    entry_price   REAL,
    launchpad     TEXT,
    peak_multiple REAL DEFAULT 1.0,
    trough_multiple REAL DEFAULT 1.0,
    outcome       TEXT DEFAULT 'pending',
    PRIMARY KEY (chain, token_address)
);
CREATE TABLE IF NOT EXISTS snapshots (
    chain         TEXT NOT NULL,
    token_address TEXT NOT NULL,
    ts            REAL NOT NULL,
    price_usd     REAL,
    market_cap_usd REAL,
    liquidity_usd REAL,
    json          TEXT,               -- full TokenSnapshot for feature extraction
    PRIMARY KEY (chain, token_address, ts)
);
CREATE INDEX IF NOT EXISTS idx_snap_token ON snapshots(chain, token_address, ts);
CREATE TABLE IF NOT EXISTS signatures (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_ts REAL NOT NULL,
    json       TEXT NOT NULL          -- serialised Signature (active = latest)
);
"""


class Store:
    def __init__(self, path: str = "memelab.db"):
        self.path = path
        # TODO: sqlite3.connect(path); executescript(_SCHEMA)

    # -- ingest ---------------------------------------------------------
    def record_snapshot(self, snap: TokenSnapshot) -> None:
        """Upsert the token row (first-seen/entry) + append one snapshot row."""
        raise NotImplementedError

    # -- backtest reads -------------------------------------------------
    def time_series(self, chain: Chain, token_address: str) -> TokenTimeSeries:
        """All snapshots for a token + its anchor/outcome."""
        raise NotImplementedError

    def labeled_tokens(self, chain: Optional[Chain] = None,
                       min_age_hours: float = 48) -> list[TokenTimeSeries]:
        """Tokens old enough to have a settled outcome — the training set."""
        raise NotImplementedError

    def set_outcome(self, chain: Chain, token_address: str, outcome: Outcome,
                    peak: float, trough: float) -> None:
        raise NotImplementedError

    # -- signatures -----------------------------------------------------
    def save_signature(self, signature_json: str) -> None:
        raise NotImplementedError

    def active_signature(self) -> Optional[str]:
        raise NotImplementedError
