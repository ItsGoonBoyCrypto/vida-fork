"""Solana (SVM) adapter — the main net-new chain.

Safety enrichment uses RugCheck.xyz — Solana's de-facto rug oracle — which in
one call gives a risk score, mint/freeze authority status, LP status and top
holder concentration. Maps cleanly onto the unified snapshot.

  discover(): pump.fun is the direct analog of flap (bonding-curve launches).
  Earliest catch = subscribe to the pump.fun program via a Solana RPC (Helius
  logsSubscribe) or pump.fun's public API; Raydium for graduated/direct pools.
  Until that's wired, the collector falls back to the DexScreener discovery feed
  (slug "solana"), so Solana still collects from day one.

Market data (price/liq/volume) comes from the shared DexScreener ingest — this
adapter only adds Solana-specific safety.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from ..models import Chain, TokenSnapshot
from .base import ChainAdapter

log = logging.getLogger("memelab.solana")

_RUGCHECK = "https://api.rugcheck.xyz/v1/tokens"


class SolanaAdapter(ChainAdapter):
    def __init__(self, config, session=None):
        super().__init__(config)
        self._session = session

    @property
    def chain(self) -> Chain:
        return Chain.SOLANA

    async def discover(self) -> list[TokenSnapshot]:
        # TODO: pump.fun (bonding-curve) + Raydium new-pool feeds → stubs with
        # on_curve=True pre-graduation. Collector falls back to feed.new_pairs.
        return []

    async def enrich_safety(self, snap: TokenSnapshot) -> None:
        if self._session is None or not snap.token_address:
            return
        data = await self._get(f"{_RUGCHECK}/{snap.token_address}/report")
        if not isinstance(data, dict):
            return
        # RugCheck score: lower = safer. Normalise to a 0-100 "higher = safer".
        raw = data.get("score")
        if isinstance(raw, (int, float)):
            snap.external_risk_score = max(0.0, min(100.0, 100.0 - float(raw) / 100.0))
        mint = data.get("mintAuthority")
        freeze = data.get("freezeAuthority")
        if mint is not None:
            snap.mint_authority_revoked = not mint      # null authority = revoked
        # top holders concentration
        holders = data.get("topHolders") or []
        pcts = sorted((_f(h.get("pct")) for h in holders), reverse=True)
        pcts = [p for p in pcts if p is not None]
        if pcts:
            snap.top1_supply_pct = round(pcts[0], 2)
            snap.top10_supply_pct = round(sum(pcts[:10]), 2)
        # LP: RugCheck markets carry lp locked/burned percentages
        markets = data.get("markets") or []
        for m in markets:
            lp = m.get("lp") or {}
            if _f(lp.get("lpLockedPct")) and _f(lp.get("lpLockedPct")) >= 90:
                snap.lp_burned_or_locked = True
                break
        # honeypot-ish: RugCheck flags non-transferable / freeze risks
        risks = {r.get("name", "").lower() for r in (data.get("risks") or [])}
        if "honeypot" in risks or "cannot sell" in risks:
            snap.is_honeypot = True

    async def _get(self, url: str):
        for attempt in range(4):
            try:
                async with self._session.get(url) as r:
                    if r.status in (429, 502, 503, 504):
                        await asyncio.sleep(min(2.0, 0.4 * (2 ** attempt)))
                        continue
                    return await r.json() if r.status == 200 else None
            except Exception:  # noqa: BLE001
                if attempt < 3:
                    await asyncio.sleep(min(2.0, 0.4 * (2 ** attempt)))
                    continue
                return None
        return None


def _f(x) -> Optional[float]:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None
