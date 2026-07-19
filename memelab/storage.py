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
CREATE TABLE IF NOT EXISTS smart_wallets (
    chain   TEXT NOT NULL,
    wallet  TEXT NOT NULL,             -- lowercased; a curated/harvested sharp wallet
    source  TEXT,                      -- "seed" | "winner:<token>"
    ts      REAL NOT NULL,
    PRIMARY KEY (chain, wallet)
);
CREATE TABLE IF NOT EXISTS harvested (
    chain   TEXT NOT NULL,
    token   TEXT NOT NULL,             -- winner tokens already harvested (dedup)
    ts      REAL NOT NULL,
    PRIMARY KEY (chain, token)
);
CREATE TABLE IF NOT EXISTS manual_winners (
    chain         TEXT NOT NULL,
    token         TEXT NOT NULL,       -- lowercased; a winner the operator fed in
    symbol        TEXT,
    metrics_json  TEXT,                -- enrichment snapshot at harvest time
    buyers_added  INTEGER DEFAULT 0,
    ts            REAL NOT NULL,
    PRIMARY KEY (chain, token)
);

-- Wallet reputation ledger (per chain) ------------------------------------
-- Every (wallet, winner-token) it was an early buyer of; DISTINCT count per
-- wallet = its winner-overlap, the primary quality signal.
CREATE TABLE IF NOT EXISTS wallet_winners (
    chain   TEXT NOT NULL,
    wallet  TEXT NOT NULL,
    token   TEXT NOT NULL,
    mult    REAL,
    ts      REAL NOT NULL,
    PRIMARY KEY (chain, wallet, token)
);
CREATE INDEX IF NOT EXISTS idx_ml_ww ON wallet_winners(chain, wallet);

-- Forward picks: a smart wallet was seen among a token's buyers. Joined with
-- tokens.outcome to score whether that wallet's later picks actually pumped.
CREATE TABLE IF NOT EXISTS smart_buys (
    chain   TEXT NOT NULL,
    wallet  TEXT NOT NULL,
    token   TEXT NOT NULL,
    ts      REAL NOT NULL,
    PRIMARY KEY (chain, wallet, token)
);
CREATE INDEX IF NOT EXISTS idx_ml_sb ON smart_buys(chain, wallet);

-- Every (wallet, rug-token) it was early in; DISTINCT count = toxicity.
CREATE TABLE IF NOT EXISTS wallet_rugs (
    chain   TEXT NOT NULL,
    wallet  TEXT NOT NULL,
    token   TEXT NOT NULL,
    ts      REAL NOT NULL,
    PRIMARY KEY (chain, wallet, token)
);
CREATE INDEX IF NOT EXISTS idx_ml_wr ON wallet_rugs(chain, wallet);

-- Deployers of confirmed winners/rugs → per-creator win-rate (EVM only; Solana
-- has no cheap creator lookup). Known launchpad managers are excluded upstream.
CREATE TABLE IF NOT EXISTS deployer_tokens (
    chain     TEXT NOT NULL,
    deployer  TEXT NOT NULL,
    token     TEXT NOT NULL,
    outcome   TEXT,
    ts        REAL NOT NULL,
    PRIMARY KEY (chain, deployer, token)
);
CREATE INDEX IF NOT EXISTS idx_ml_dt ON deployer_tokens(chain, deployer);

-- One-shot markers for rug-harvest dedup + core-alpha alerts.
CREATE TABLE IF NOT EXISTS ml_markers (
    key  TEXT PRIMARY KEY,
    ts   REAL NOT NULL
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
        # check_same_thread=False: the API serves read-only routes on a threadpool,
        # so the connection is touched from worker threads. Python's sqlite3 is
        # serialized (threadsafety=3), so one shared connection is safe. The
        # collector is single-threaded async and unaffected.
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def backup(self, dest_path: str) -> None:
        """Online SQLite backup to dest_path (safe while the DB is in use)."""
        dst = sqlite3.connect(dest_path)
        try:
            self._conn.backup(dst)
        finally:
            dst.close()

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

    def winner_tokens(self) -> list:
        """(Chain, token) for every token currently labeled WINNER."""
        cur = self._conn.execute(
            "SELECT chain, token_address FROM tokens WHERE outcome = 'winner'")
        return [(Chain(r["chain"]), r["token_address"]) for r in cur.fetchall()]

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

    # -- smart-money set ------------------------------------------------

    def add_smart_wallet(self, chain: Chain, wallet: str, source: str = "seed") -> bool:
        cur = self._conn.execute(
            "SELECT 1 FROM smart_wallets WHERE chain = ? AND wallet = ?",
            (chain.value, wallet.lower()))
        existed = cur.fetchone() is not None
        self._conn.execute(
            "INSERT OR IGNORE INTO smart_wallets (chain, wallet, source, ts) VALUES (?, ?, ?, ?)",
            (chain.value, wallet.lower(), source, time.time()))
        self._conn.commit()
        return not existed

    def is_smart_wallet(self, chain: Chain, wallet: str) -> bool:
        cur = self._conn.execute(
            "SELECT 1 FROM smart_wallets WHERE chain = ? AND wallet = ?",
            (chain.value, wallet.lower()))
        return cur.fetchone() is not None

    def smart_wallets(self, chain: Chain) -> set:
        cur = self._conn.execute(
            "SELECT wallet FROM smart_wallets WHERE chain = ?", (chain.value,))
        return {r["wallet"] for r in cur.fetchall()}

    def smart_wallet_count(self, chain: Optional[Chain] = None) -> int:
        if chain:
            return self._conn.execute(
                "SELECT COUNT(*) FROM smart_wallets WHERE chain = ?", (chain.value,)).fetchone()[0]
        return self._conn.execute("SELECT COUNT(*) FROM smart_wallets").fetchone()[0]

    def smart_wallets_detailed(self) -> list:
        """All smart wallets across chains with source + added-time (newest first)."""
        cur = self._conn.execute(
            "SELECT chain, wallet, source, ts FROM smart_wallets ORDER BY ts DESC")
        return [{"chain": r["chain"], "wallet": r["wallet"],
                 "source": r["source"], "ts": r["ts"]} for r in cur.fetchall()]

    def remove_smart_wallet(self, chain: Chain, wallet: str) -> bool:
        cur = self._conn.execute(
            "DELETE FROM smart_wallets WHERE chain = ? AND wallet = ?",
            (chain.value, wallet.lower()))
        self._conn.commit()
        return cur.rowcount > 0

    def mark_harvested(self, chain: Chain, token: str) -> bool:
        """True if not yet harvested (and records it)."""
        cur = self._conn.execute(
            "SELECT 1 FROM harvested WHERE chain = ? AND token = ?", (chain.value, token.lower()))
        if cur.fetchone() is not None:
            return False
        self._conn.execute("INSERT INTO harvested (chain, token, ts) VALUES (?, ?, ?)",
                           (chain.value, token.lower(), time.time()))
        self._conn.commit()
        return True

    # -- manually-fed winners (harvest) ---------------------------------

    def save_manual_winner(self, chain: Chain, token: str, symbol: str,
                           metrics: dict, buyers_added: int) -> None:
        import json
        self._conn.execute(
            "INSERT OR REPLACE INTO manual_winners "
            "(chain, token, symbol, metrics_json, buyers_added, ts) VALUES (?,?,?,?,?,?)",
            (chain.value, token.lower(), symbol or "", json.dumps(metrics),
             int(buyers_added), time.time()))
        self._conn.commit()

    def has_manual_winner(self, chain: Chain, token: str) -> bool:
        cur = self._conn.execute(
            "SELECT 1 FROM manual_winners WHERE chain = ? AND token = ?",
            (chain.value, token.lower()))
        return cur.fetchone() is not None

    def manual_winners(self) -> list:
        import json
        out = []
        for r in self._conn.execute(
                "SELECT * FROM manual_winners ORDER BY ts DESC").fetchall():
            try:
                m = json.loads(r["metrics_json"] or "{}")
            except Exception:  # noqa: BLE001
                m = {}
            out.append({"chain": r["chain"], "token": r["token"], "symbol": r["symbol"],
                        "metrics": m, "buyers_added": r["buyers_added"], "ts": r["ts"]})
        return out

    # -- wallet reputation ledger ---------------------------------------

    def record_wallet_winner(self, chain: Chain, wallet: str, token: str,
                             mult: float = 0.0) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO wallet_winners (chain, wallet, token, mult, ts) "
            "VALUES (?,?,?,?,?)",
            (chain.value, wallet.lower(), token.lower(), float(mult or 0.0), time.time()))
        self._conn.commit()

    def record_smart_buy(self, chain: Chain, wallet: str, token: str) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO smart_buys (chain, wallet, token, ts) VALUES (?,?,?,?)",
            (chain.value, wallet.lower(), token.lower(), time.time()))
        self._conn.commit()

    def wallet_reputation_rows(self, chain: Chain, wallets: list) -> dict:
        """{wallet: {winner_overlap, pick_wins, pick_total}} for a chain's wallets.

        Forward picks join smart_buys with the tokens table's realized outcome
        (peak_multiple >= 2 counts as a win), so a wallet's later picks are graded
        by what actually happened."""
        out: dict = {}
        if not wallets:
            return out
        ws = [w.lower() for w in wallets]
        for w in ws:
            out[w] = {"winner_overlap": 0, "pick_wins": 0, "pick_total": 0}
        qs = ",".join("?" for _ in ws)
        args = [chain.value, *ws]
        for r in self._conn.execute(
                f"SELECT wallet, COUNT(*) n FROM wallet_winners "
                f"WHERE chain = ? AND wallet IN ({qs}) GROUP BY wallet", args):
            out[r["wallet"]]["winner_overlap"] = r["n"]
        for r in self._conn.execute(
                f"SELECT sb.wallet AS wallet, "
                "  SUM(CASE WHEN t.peak_multiple >= 2.0 THEN 1 ELSE 0 END) AS wins, "
                "  SUM(CASE WHEN t.outcome IN ('winner','rug','neutral') THEN 1 ELSE 0 END) AS total "
                "FROM smart_buys sb JOIN tokens t "
                "  ON t.chain = sb.chain AND t.token_address = sb.token "
                f"WHERE sb.chain = ? AND sb.wallet IN ({qs}) GROUP BY sb.wallet", args):
            out[r["wallet"]]["pick_wins"] = r["wins"] or 0
            out[r["wallet"]]["pick_total"] = r["total"] or 0
        return out

    def record_wallet_rug(self, chain: Chain, wallet: str, token: str) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO wallet_rugs (chain, wallet, token, ts) VALUES (?,?,?,?)",
            (chain.value, wallet.lower(), token.lower(), time.time()))
        self._conn.commit()

    def toxic_wallets(self, chain: Chain, min_rugs: int = 2) -> set:
        """Wallets in >= min_rugs rugs whose rug count outweighs their winner
        overlap (a genuine sharp who once aped a rug is not blacklisted)."""
        rows = self._conn.execute(
            "SELECT r.wallet AS wallet, COUNT(DISTINCT r.token) rugs, "
            "  (SELECT COUNT(DISTINCT w.token) FROM wallet_winners w "
            "   WHERE w.chain = r.chain AND w.wallet = r.wallet) wins "
            "FROM wallet_rugs r WHERE r.chain = ? GROUP BY r.wallet",
            (chain.value,)).fetchall()
        return {r["wallet"] for r in rows
                if r["rugs"] >= min_rugs and (r["rugs"] - (r["wins"] or 0)) > 0}

    def rug_tokens(self) -> list:
        """(Chain, token) for every token currently labeled RUG."""
        cur = self._conn.execute(
            "SELECT chain, token_address FROM tokens WHERE outcome = 'rug'")
        return [(Chain(r["chain"]), r["token_address"]) for r in cur.fetchall()]

    def core_alpha_wallets(self, chain: Chain, min_overlap: int) -> set:
        cur = self._conn.execute(
            "SELECT wallet FROM wallet_winners WHERE chain = ? "
            "GROUP BY wallet HAVING COUNT(*) >= ?", (chain.value, min_overlap))
        return {r["wallet"] for r in cur.fetchall()}

    def record_deployer_token(self, chain: Chain, deployer: str, token: str,
                              outcome: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO deployer_tokens (chain, deployer, token, outcome, ts) "
            "VALUES (?,?,?,?,?)",
            (chain.value, deployer.lower(), token.lower(), outcome, time.time()))
        self._conn.commit()

    def top_deployers(self, limit: int = 15) -> list:
        rows = self._conn.execute(
            "SELECT chain, deployer, "
            "SUM(CASE WHEN outcome='winner' THEN 1 ELSE 0 END) wins, "
            "SUM(CASE WHEN outcome='rug' THEN 1 ELSE 0 END) rugs, COUNT(*) total "
            "FROM deployer_tokens GROUP BY chain, deployer "
            "ORDER BY wins DESC, total DESC LIMIT ?", (limit,)).fetchall()
        return [{"chain": r["chain"], "deployer": r["deployer"], "wins": r["wins"],
                 "rugs": r["rugs"], "total": r["total"]} for r in rows]

    def marker_new(self, key: str) -> bool:
        """True (and records) if this one-shot key hasn't been seen."""
        if self._conn.execute("SELECT 1 FROM ml_markers WHERE key = ?", (key,)).fetchone():
            return False
        self._conn.execute("INSERT INTO ml_markers (key, ts) VALUES (?, ?)",
                           (key, time.time()))
        self._conn.commit()
        return True

    def top_reputation_wallets(self, chain: Optional[Chain] = None,
                               limit: int = 15) -> list:
        """Wallets ranked by winner-overlap (+ forward-pick record) — the data
        behind the smart_money_quality feature, for the dashboard panel."""
        q = ("SELECT chain, wallet, COUNT(*) n, MAX(mult) best "
             "FROM wallet_winners")
        args: list = []
        if chain:
            q += " WHERE chain = ?"
            args.append(chain.value)
        q += " GROUP BY chain, wallet ORDER BY n DESC, best DESC LIMIT ?"
        args.append(limit)
        out = []
        for r in self._conn.execute(q, args).fetchall():
            fwd = self._conn.execute(
                "SELECT SUM(CASE WHEN t.peak_multiple >= 2.0 THEN 1 ELSE 0 END) wins, "
                "SUM(CASE WHEN t.outcome IN ('winner','rug','neutral') THEN 1 ELSE 0 END) total "
                "FROM smart_buys sb JOIN tokens t "
                "  ON t.chain = sb.chain AND t.token_address = sb.token "
                "WHERE sb.chain = ? AND sb.wallet = ?",
                (r["chain"], r["wallet"])).fetchone()
            out.append({"chain": r["chain"], "wallet": r["wallet"],
                        "overlap": r["n"], "best_mult": r["best"] or 0,
                        "pick_wins": (fwd["wins"] or 0) if fwd else 0,
                        "pick_total": (fwd["total"] or 0) if fwd else 0})
        return out

    def reputation_summary(self) -> dict:
        """Counts for the coverage tiles: scored wallets + winner-overlap links."""
        scored = self._conn.execute(
            "SELECT COUNT(DISTINCT wallet) n FROM wallet_winners").fetchone()["n"]
        links = self._conn.execute(
            "SELECT COUNT(*) n FROM wallet_winners").fetchone()["n"]
        picks = self._conn.execute("SELECT COUNT(*) n FROM smart_buys").fetchone()["n"]
        rugs = self._conn.execute(
            "SELECT COUNT(DISTINCT wallet) n FROM wallet_rugs").fetchone()["n"]
        return {"scored_wallets": scored, "overlap_links": links,
                "forward_picks": picks, "rug_flagged": rugs}

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
