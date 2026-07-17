"""EVM adapter — Robinhood, Ethereum, Base (all Uniswap-style).

Safety enrichment uses GoPlus token-security — one API, uniform across EVM
chains (keyed by decimal chain id in ChainConfig.goplus_chain_id). It returns
honeypot, buy/sell tax, mintability, LP lock/burn and holder concentration in a
single call, which maps cleanly onto the unified snapshot.

  discover(): source-level (DEX-factory + launchpad logs) is the earliest catch
  but needs a per-chain RPC + factory addresses. Only RH has those wired today
  (rhl2_scanner), so discover() returns [] here and the collector falls back to
  the DexScreener discovery feed. Wire per-chain listeners as an enhancement
  (RH: reuse rhl2_scanner.sources.poollistener + launchpad_curve).

GoPlus doesn't cover every chain (RH likely unsupported) — enrich is best-effort
and leaves fields None when the oracle has nothing, exactly as the model wants.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from ..models import Chain, TokenSnapshot
from .base import ChainAdapter

log = logging.getLogger("memelab.evm")

_GOPLUS = "https://api.gopluslabs.io/api/v1/token_security"


def _pct(x) -> Optional[float]:
    try:
        return float(x) * 100.0
    except (TypeError, ValueError):
        return None


class EvmAdapter(ChainAdapter):
    def __init__(self, config, session=None):
        super().__init__(config)
        self._session = session

    @property
    def chain(self) -> Chain:
        return self.config.chain

    async def discover(self) -> list[TokenSnapshot]:
        # Source-level discovery needs a per-chain RPC + factory addresses; only
        # RH has them wired (rhl2_scanner). Collector falls back to feed.new_pairs.
        return []

    async def enrich_safety(self, snap: TokenSnapshot) -> None:
        gid = self.config.goplus_chain_id
        if not gid or self._session is None or not snap.token_address:
            return
        data = await self._get(f"{_GOPLUS}/{gid}?contract_addresses={snap.token_address}")
        result = (data or {}).get("result") or {}
        row = result.get(snap.token_address.lower()) or (
            next(iter(result.values())) if result else None)
        if not isinstance(row, dict):
            return
        snap.is_honeypot = row.get("is_honeypot") == "1" if "is_honeypot" in row else snap.is_honeypot
        snap.buy_tax_pct = _pct(row.get("buy_tax")) if row.get("buy_tax") not in (None, "") else snap.buy_tax_pct
        snap.sell_tax_pct = _pct(row.get("sell_tax")) if row.get("sell_tax") not in (None, "") else snap.sell_tax_pct
        if "is_mintable" in row:
            snap.mint_authority_revoked = row.get("is_mintable") == "0"
        # LP burned or locked: GoPlus lp_holders carry is_locked / burn tags
        lp = row.get("lp_holders") or []
        if lp:
            locked = any(str(h.get("is_locked")) == "1" for h in lp)
            burned = any((h.get("address") or "").lower() in
                         ("0x000000000000000000000000000000000000dead",
                          "0x0000000000000000000000000000000000000000") for h in lp)
            snap.lp_burned_or_locked = bool(locked or burned)
        # holder concentration → top10 / top1
        holders = row.get("holders") or []
        if holders:
            pcts = sorted((_frac(h.get("percent")) for h in holders), reverse=True)
            pcts = [p for p in pcts if p is not None]
            if pcts:
                snap.top1_supply_pct = round(pcts[0] * 100, 2)
                snap.top10_supply_pct = round(sum(pcts[:10]) * 100, 2)
        if row.get("creator_percent") not in (None, ""):
            snap.dev_holdings_pct = _frac(row.get("creator_percent"))
            if snap.dev_holdings_pct is not None:
                snap.dev_holdings_pct = round(snap.dev_holdings_pct * 100, 2)

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


def _frac(x) -> Optional[float]:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None
