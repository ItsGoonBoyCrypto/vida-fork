"""Cross-pollinate wallet reputation from the RH scanner into memelab.

The RH scanner harvests proven robinhood wallets (via /harvest + its winner
sweep) into its own reputation ledger. This module reads that ledger (read-only,
from the shared volume) and seeds memelab's robinhood reputation with the
wallets the scanner has confirmed — so a wallet proven on the scanner is
immediately trusted in memelab's robinhood view (quality-weights the
smart_money feature, can fire core-alpha).

Mirror of rhl2_scanner/memelab_bridge.py (the reverse direction). Best-effort,
dormant when scanner.db is absent, and additive only.
"""

from __future__ import annotations

import logging
import sqlite3

from .models import Chain

log = logging.getLogger("memelab.bridge")


def import_scanner_reputation(store, db_path: str, min_overlap: int = 2) -> int:
    """Merge the RH scanner's proven wallets into memelab's robinhood reputation.
    Returns the number of wallets imported.

    The scanner is robinhood-only, so its wallet_winners rows have no chain
    column; every row is credited to memelab's robinhood chain. Idempotent
    (PK-deduped)."""
    if not db_path:
        return 0
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except Exception:  # noqa: BLE001 — scanner DB not present yet → dormant
        return 0
    try:
        rows = conn.execute("SELECT wallet, token, mult FROM wallet_winners").fetchall()
    except Exception:  # noqa: BLE001 — schema not there yet
        return 0
    finally:
        conn.close()

    by_wallet: dict = {}
    for wallet, token, mult in rows:
        by_wallet.setdefault(wallet.lower(), []).append((token.lower(), mult or 0.0))
    imported = 0
    for wallet, picks in by_wallet.items():
        if len({t for t, _ in picks}) < min_overlap:
            continue
        for token, mult in picks:
            try:
                store.record_wallet_winner(Chain.ROBINHOOD, wallet, token, mult)
            except Exception:  # noqa: BLE001
                pass
        try:
            store.add_smart_wallet(Chain.ROBINHOOD, wallet, source="scanner")
        except Exception:  # noqa: BLE001
            pass
        imported += 1
    if imported:
        log.info("scanner bridge: imported %d proven robinhood wallet(s)", imported)
    return imported
