"""Unified market-data ingest — DexScreener across all four chains.

DexScreener uses the SAME schema for every chain (price, liquidity, fdv,
volume/txns per 5m/1h/6h/24h window, socials), keyed by a chain slug — so one
client fills market fields on any TokenSnapshot regardless of chain.

Public API (no key): https://api.dexscreener.com
  · /latest/dex/tokens/{addr}                → all pairs for a token
  · /token-profiles/latest/v1                → recently listed/profiled tokens
  · /token-boosts/latest/v1                  → recently boosted (hype) tokens
  · /latest/dex/search?q=                     → search
Rate-limited; the shared retry/backoff below rides through 429s.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from ..models import Chain, TokenSnapshot

log = logging.getLogger("memelab.dexscreener")

_BASE = "https://api.dexscreener.com"
_TRANSIENT = (429, 502, 503, 504)


def _f(x) -> Optional[float]:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


class DexScreenerFeed:
    def __init__(self, session=None):
        self._session = session
        self._owns = session is None

    async def __aenter__(self):
        if self._session is None:
            import aiohttp
            self._session = aiohttp.ClientSession(
                timeout=__import__("aiohttp").ClientTimeout(total=15))
        return self

    async def __aexit__(self, *exc):
        if self._owns and self._session is not None:
            await self._session.close()

    async def _get(self, path: str):
        assert self._session is not None
        url = _BASE + path
        for attempt in range(4):
            try:
                async with self._session.get(url) as r:
                    if r.status in _TRANSIENT:
                        await asyncio.sleep(min(2.0, 0.4 * (2 ** attempt)))
                        continue
                    if r.status != 200:
                        return None
                    return await r.json()
            except Exception:  # noqa: BLE001 — network/parse; retry then give up
                if attempt < 3:
                    await asyncio.sleep(min(2.0, 0.4 * (2 ** attempt)))
                    continue
                return None
        return None

    # -- market data ----------------------------------------------------

    async def market_for(self, chain: Chain, token_address: str) -> Optional[TokenSnapshot]:
        """Best (highest-liquidity) pair for a token on `chain`, as a snapshot.

        None if DexScreener doesn't index it (e.g. still on a bonding curve pre-
        graduation — expected; the chain adapter covers those).
        """
        data = await self._get(f"/latest/dex/tokens/{token_address}")
        pairs = _pairs(data)
        pairs = [p for p in pairs if p.get("chainId") == chain.value]
        if not pairs:
            return None
        best = max(pairs, key=lambda p: _f((p.get("liquidity") or {}).get("usd")) or 0.0)
        return self._map(best, chain)

    async def new_pairs(self, chain: Chain, limit: int = 60) -> list[TokenSnapshot]:
        """Recently profiled/boosted tokens on a chain — DexScreener-indexed
        discovery. Complements adapter.discover() (source-level, earliest); this
        is the cross-chain fallback that works with no per-chain RPC."""
        out: dict = {}
        for path in ("/token-profiles/latest/v1", "/token-boosts/latest/v1"):
            items = await self._get(path)
            if not isinstance(items, list):
                continue
            for it in items:
                if it.get("chainId") != chain.value:
                    continue
                addr = it.get("tokenAddress")
                if not addr:
                    continue
                snap = await self.market_for(chain, addr)
                if snap is not None:
                    snap.dex_boosted = "boost" in path or snap.dex_boosted
                    out[addr.lower()] = snap
                if len(out) >= limit:
                    break
        return list(out.values())

    # -- mapping (shared for all chains) --------------------------------

    @staticmethod
    def _map(p: dict, chain: Chain) -> TokenSnapshot:
        vol = p.get("volume") or {}
        txns = p.get("txns") or {}
        chg = p.get("priceChange") or {}
        liq = p.get("liquidity") or {}
        info = p.get("info") or {}
        base = p.get("baseToken") or {}

        def _tx(win, side):
            w = txns.get(win) or {}
            v = w.get(side)
            return int(v) if isinstance(v, (int, float)) else None

        created_ms = p.get("pairCreatedAt")
        created_s = (created_ms / 1000.0) if isinstance(created_ms, (int, float)) else None
        age_min = None
        if created_s:
            import time
            age_min = max(0.0, (time.time() - created_s) / 60.0)

        socials = {}
        for s in (info.get("socials") or []):
            if s.get("type") and s.get("url"):
                socials[s["type"]] = s["url"]

        return TokenSnapshot(
            chain=chain,
            token_address=(base.get("address") or "").lower(),
            pair_address=(p.get("pairAddress") or "").lower(),
            symbol=base.get("symbol") or "",
            name=base.get("name") or "",
            price_usd=_f(p.get("priceUsd")),
            market_cap_usd=_f(p.get("marketCap")),
            fdv_usd=_f(p.get("fdv")),
            liquidity_usd=_f(liq.get("usd")),
            pair_created_at=created_s,
            age_minutes=age_min,
            volume_5m=_f(vol.get("m5")), volume_1h=_f(vol.get("h1")),
            volume_24h=_f(vol.get("h24")),
            buys_5m=_tx("m5", "buys"), sells_5m=_tx("m5", "sells"),
            buys_1h=_tx("h1", "buys"), sells_1h=_tx("h1", "sells"),
            price_change_5m=_f(chg.get("m5")), price_change_1h=_f(chg.get("h1")),
            price_change_24h=_f(chg.get("h24")),
            socials=socials,
            dex_boosted=bool(p.get("boosts")),
        )


def _pairs(data) -> list:
    if isinstance(data, dict):
        return data.get("pairs") or []
    if isinstance(data, list):
        return data
    return []
