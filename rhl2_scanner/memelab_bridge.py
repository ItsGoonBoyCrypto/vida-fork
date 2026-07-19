"""Cross-pollinate wallet reputation from memelab into the RH scanner.

memelab tracks the robinhood chain too (seeded with this scanner's wallets) and
learns which wallets keep landing winners across ITS view of the chain. This
module reads memelab's robinhood reputation (read-only, from the shared volume)
and merges its proven wallets into the scanner's own smart set + reputation
ledger — so a wallet memelab has confirmed on several robinhood winners is
immediately trusted here too (counts toward core-alpha, quality-weights scoring).

Mirror of signature_bonus.py: best-effort, dormant when the DB is absent, and it
only ever ADDS reputation (never removes), so it can't degrade the local view.
The reverse direction (scanner → memelab) lives in memelab/bridge.py.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Optional

log = logging.getLogger("rhl2.memelab_bridge")


def import_memelab_reputation(storage, db_path: str, min_overlap: int = 2) -> int:
    """Merge memelab's proven robinhood wallets into the scanner. Returns the
    number of wallets imported.

    A wallet is imported when memelab has it as an early buyer of >= min_overlap
    DISTINCT robinhood winners. Its (wallet, token) winner rows are copied into
    the scanner's ledger (PK-deduped, idempotent) and it's added to the smart set.
    """
    if not db_path:
        return 0
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except Exception:  # noqa: BLE001 — memelab DB not present yet → dormant
        return 0
    try:
        rows = conn.execute(
            "SELECT wallet, token, mult FROM wallet_winners WHERE chain = 'robinhood'"
        ).fetchall()
    except Exception:  # noqa: BLE001 — schema not there yet
        return 0
    finally:
        conn.close()

    # Group by wallet; only import wallets proven on enough distinct winners.
    by_wallet: dict = {}
    for wallet, token, mult in rows:
        by_wallet.setdefault(wallet.lower(), []).append((token.lower(), mult or 0.0))
    imported = 0
    for wallet, picks in by_wallet.items():
        if len({t for t, _ in picks}) < min_overlap:
            continue
        for token, mult in picks:
            try:
                storage.record_wallet_winner(wallet, token, mult)
            except Exception:  # noqa: BLE001
                pass
        try:
            storage.add_smart_wallet(wallet, source="memelab", note="cross-chain proven")
        except Exception:  # noqa: BLE001
            pass
        imported += 1
    if imported:
        log.info("memelab bridge: imported %d proven robinhood wallet(s)", imported)
    return imported
