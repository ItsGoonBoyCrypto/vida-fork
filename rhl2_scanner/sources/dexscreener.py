"""DexScreener client — real, functional against the public API.

Docs: https://docs.dexscreener.com/api/reference

This is the primary discovery + market/volume/tx source. It works today for
any chain DexScreener indexes. For Robinhood L2 specifically, set
``chain.dexscreener_chain`` to RH L2's slug once it is indexed; until then
point it at an example EVM L2 (e.g. "base") to exercise the full pipeline.

Only standard-library + aiohttp are used. All network access is defensive:
timeouts, status checks, and shape-tolerant parsing (missing keys -> None).
"""

from __future__ import annotations

import time
from typing import Any, Optional

import aiohttp

from ..config import Config
from ..models import TokenSnapshot

_BASE = "https://api.dexscreener.com"


def _f(x: Any) -> Optional[float]:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _i(x: Any) -> Optional[int]:
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


class DexScreenerClient:
    """Async DexScreener pair source."""

    def __init__(self, cfg: Config, session: Optional[aiohttp.ClientSession] = None):
        self.cfg = cfg
        self.chain = cfg.chain.dexscreener_chain
        self._session = session
        self._owns_session = session is None

    async def __aenter__(self) -> "DexScreenerClient":
        if self._session is None:
            timeout = aiohttp.ClientTimeout(total=self.cfg.runtime.request_timeout_seconds)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self

    async def __aexit__(self, *exc) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()

    async def _get(self, path: str) -> Optional[dict]:
        assert self._session is not None, "use as async context manager"
        try:
            async with self._session.get(f"{_BASE}{path}") as resp:
                if resp.status != 200:
                    return None
                return await resp.json()
        except (aiohttp.ClientError, TimeoutError):
            return None

    # -- Discovery -------------------------------------------------------

    async def fetch_new_pairs(self) -> list[TokenSnapshot]:
        """Discover recent pairs on the configured chain.

        DexScreener's public API does not expose a pure "newest pairs on
        chain X" firehose, so we combine two lawful entry points:
          * token-profiles / boosted feeds (fresh, promoted tokens), and
          * the search endpoint scoped to the chain.
        Both are de-duplicated by pair address upstream in the scanner.

        NOTE: for production you'll likely also want a chain-native new-pool
        listener (see sources/chain.py::iter_new_pools) to catch pairs the
        moment liquidity is added, before DexScreener indexes them.
        """
        snaps: dict[str, TokenSnapshot] = {}

        # 1) Latest boosted tokens (promoted => visibility signal too).
        boosted = await self._get("/token-boosts/latest/v1")
        for entry in _as_list(boosted):
            if entry.get("chainId") != self.chain:
                continue
            addr = entry.get("tokenAddress")
            if not addr:
                continue
            for snap in await self.pairs_for_token(addr):
                snap.dex_boosted = True
                snaps[snap.pair_address.lower()] = snap

        # 2) Broad search scoped to chain (captures organically new pairs).
        search = await self._get(f"/latest/dex/search?q={self.chain}")
        for pair in _as_list((search or {}).get("pairs")):
            if pair.get("chainId") != self.chain:
                continue
            snap = self._pair_to_snapshot(pair)
            if snap:
                snaps.setdefault(snap.pair_address.lower(), snap)

        return list(snaps.values())

    async def pairs_for_token(self, token_address: str) -> list[TokenSnapshot]:
        data = await self._get(f"/latest/dex/tokens/{token_address}")
        out = []
        for pair in _as_list((data or {}).get("pairs")):
            if pair.get("chainId") != self.chain:
                continue
            snap = self._pair_to_snapshot(pair)
            if snap:
                out.append(snap)
        return out

    async def refresh(self, snap: TokenSnapshot) -> TokenSnapshot:
        data = await self._get(f"/latest/dex/pairs/{self.chain}/{snap.pair_address}")
        pairs = _as_list((data or {}).get("pairs")) or _as_list((data or {}).get("pair"))
        if pairs:
            fresh = self._pair_to_snapshot(pairs[0])
            if fresh:
                # preserve enrichment already attached (safety, holders, smart money)
                fresh.safety = snap.safety
                fresh.holder_count = snap.holder_count or fresh.holder_count
                fresh.top10_supply_pct = snap.top10_supply_pct
                fresh.top1_supply_pct = snap.top1_supply_pct
                fresh.smart_money_wallets = snap.smart_money_wallets
                return fresh
        return snap

    # -- Parsing ---------------------------------------------------------

    def _pair_to_snapshot(self, p: dict) -> Optional[TokenSnapshot]:
        base = p.get("baseToken") or {}
        token_addr = base.get("address")
        pair_addr = p.get("pairAddress")
        if not token_addr or not pair_addr:
            return None

        created_ms = p.get("pairCreatedAt")
        created_at = (created_ms / 1000.0) if created_ms else None
        age_min = ((time.time() - created_at) / 60.0) if created_at else None

        vol = p.get("volume") or {}
        txns = p.get("txns") or {}
        chg = p.get("priceChange") or {}
        liq = p.get("liquidity") or {}
        info = p.get("info") or {}

        socials: dict[str, str] = {}
        for s in _as_list(info.get("socials")):
            typ, url = s.get("type"), s.get("url")
            if typ and url:
                socials[typ] = url
        for w in _as_list(info.get("websites")):
            if w.get("url"):
                socials["website"] = w["url"]

        snap = TokenSnapshot(
            chain=self.chain,
            pair_address=pair_addr,
            token_address=token_addr,
            symbol=base.get("symbol", ""),
            name=base.get("name", ""),
            price_usd=_f(p.get("priceUsd")),
            market_cap_usd=_f(p.get("marketCap")),
            fdv_usd=_f(p.get("fdv")),
            liquidity_usd=_f(liq.get("usd")),
            pair_created_at=created_at,
            age_minutes=age_min,
            volume_5m=_f(vol.get("m5")),
            volume_1h=_f(vol.get("h1")),
            volume_6h=_f(vol.get("h6")),
            volume_24h=_f(vol.get("h24")),
            buys_5m=_txn(txns, "m5", "buys"),
            sells_5m=_txn(txns, "m5", "sells"),
            buys_1h=_txn(txns, "h1", "buys"),
            sells_1h=_txn(txns, "h1", "sells"),
            buys_24h=_txn(txns, "h24", "buys"),
            sells_24h=_txn(txns, "h24", "sells"),
            price_change_5m=_f(chg.get("m5")),
            price_change_1h=_f(chg.get("h1")),
            price_change_24h=_f(chg.get("h24")),
            socials=socials,
            dex_boosted=bool(p.get("boosts", {}).get("active")) if p.get("boosts") else False,
            dexscreener_url=p.get("url", ""),
            chart_url=p.get("url", ""),
        )
        return snap


def _txn(txns: dict, window: str, side: str) -> Optional[int]:
    w = txns.get(window) or {}
    return _i(w.get(side))


def _as_list(x: Any) -> list[dict]:
    if isinstance(x, list):
        return [e for e in x if isinstance(e, dict)]
    return []
