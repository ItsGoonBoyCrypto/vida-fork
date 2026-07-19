"""Manually feed a confirmed winner into memelab — the cross-chain counterpart
to the RH scanner's /harvest.

You spotted a token that pumped on Solana / Base / Ethereum that memelab never
caught. The soundest thing to learn from it is *who bought it early*: those
wallets are early buyers no matter when you notice the pump, and they feed the
``smart_money_count`` feature the signature already trains on. (Its current,
post-pump price metrics are NOT fed as a training vector — that would teach the
classifier backwards, since the features that predict winners are the ones at
discovery, when the token was still small.)

So a manual harvest, per chain:
  • harvests the token's earliest buyers into that chain's smart-money set;
  • records the winner + its live metrics as an exemplar (a reviewable dataset);
  • is sybil-guarded: a heavily-bundled launch's "early buyers" are mostly the
    bundler, so the wallet harvest is skipped (the exemplar is still kept).

Pure orchestration over Store + SmartMoney + the DexScreener feed, so both the
API endpoint and the CLI call the same code.
"""

from __future__ import annotations

import logging
from typing import Optional

from .models import Chain

log = logging.getLogger("memelab.harvest")

# Above this bundled-supply %, skip the buyer harvest (likely sybils).
MAX_BUNDLE_PCT = 50.0


def _metrics(snap) -> dict:
    if snap is None:
        return {}
    return {
        "symbol": snap.symbol, "price_usd": snap.price_usd,
        "market_cap_usd": snap.market_cap_usd, "liquidity_usd": snap.liquidity_usd,
        "age_minutes": snap.age_minutes, "holder_count": snap.holder_count,
        "top10_supply_pct": snap.top10_supply_pct, "dev_holdings_pct": snap.dev_holdings_pct,
        "bundle_supply_pct": snap.bundle_supply_pct,
    }


async def harvest_manual_winner(store, chain: Chain, token: str, session,
                                max_bundle_pct: float = MAX_BUNDLE_PCT) -> dict:
    """Harvest a hand-picked winner on ``chain``. Returns a result dict:

    {ok, chain, token, symbol, found, added, smart_count, bundled_out,
     bundle_pct, metrics, already}
    """
    from .ingest.dexscreener import DexScreenerFeed
    from .smartmoney import SmartMoney

    token = token.lower()
    already = store.has_manual_winner(chain, token)

    # Enrich for display + the sybil guard (best-effort; may be None off-DEX).
    snap = None
    try:
        snap = await DexScreenerFeed(session=session).market_for(chain, token)
    except Exception:  # noqa: BLE001
        log.debug("market_for failed", exc_info=True)
    metrics = _metrics(snap)
    symbol = (snap.symbol if snap else "") or "?"

    bundle = metrics.get("bundle_supply_pct")
    bundled_out = bundle is not None and bundle > max_bundle_pct

    added = 0
    if not bundled_out:
        try:
            smart = SmartMoney(store, session=session)
            added = await smart.harvest_winner(chain, token)
        except Exception:  # noqa: BLE001
            log.debug("harvest_winner failed", exc_info=True)

    try:
        store.save_manual_winner(chain, token, symbol, metrics, added)
    except Exception:  # noqa: BLE001
        log.debug("save_manual_winner failed", exc_info=True)

    return {
        "ok": True, "chain": chain.value, "token": token, "symbol": symbol,
        "found": snap is not None, "added": added,
        "smart_count": store.smart_wallet_count(chain),
        "bundled_out": bundled_out, "bundle_pct": bundle,
        "metrics": metrics, "already": already,
    }
