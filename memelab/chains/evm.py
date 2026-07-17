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
    # Standard DEX-factory creation-event topics (keccak of the signatures).
    _PAIR_CREATED = "0x0d3648bd0f6ba80134a33ba9275ac585d9d315f0ad8355cddefde31afa28d0e9"
    _POOL_CREATED = "0x783cca1c0412dd0d695e784568c96da2e9c22ff989357a2e8b1d9b2b4e6b7118"

    def __init__(self, config, session=None):
        super().__init__(config)
        self._session = session
        self._last_block = None

    @property
    def chain(self) -> Chain:
        return self.config.chain

    async def discover(self) -> list[TokenSnapshot]:
        """New pools from the DEX factory via eth_getLogs — earliest EVM catch.

        Works when the chain has rpc_url + dex_factory_address configured (RH does
        out of the box; add an RPC for ETH/Base to enable). Otherwise returns []
        and the collector falls back to the DexScreener discovery feed.
        """
        rpc = self.config.rpc_url
        factory = self.config.dex_factory_address
        if not (rpc and factory) or self._session is None:
            return []
        head = await self._block_number(rpc)
        if head is None:
            return []
        if self._last_block is None:
            self._last_block = head - 500          # small backfill on first poll
        frm = self._last_block + 1
        if frm > head:
            return []
        topic = (self._POOL_CREATED if self.config.dex_factory_kind == "univ3"
                 else self._PAIR_CREATED)
        logs = await self._get_logs(rpc, factory, frm, head, topic)
        self._last_block = head
        weth = (self.config.weth_address or "").lower()
        out, seen = [], set()
        for lg in logs:
            token = self._token_from_log(lg, weth)
            if token and token not in seen:
                seen.add(token)
                out.append(TokenSnapshot(chain=self.chain, token_address=token,
                                         age_minutes=0.0))
        return out

    def _token_from_log(self, lg: dict, weth: str):
        """The non-WETH token from a Pair/PoolCreated log (token0/token1 indexed)."""
        topics = lg.get("topics") or []
        if len(topics) < 3:
            return None
        t0 = "0x" + topics[1][-40:]
        t1 = "0x" + topics[2][-40:]
        if weth and t0.lower() == weth:
            return t1.lower()
        if weth and t1.lower() == weth:
            return t0.lower()
        return t0.lower()

    async def _rpc(self, rpc, method, params):
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        for attempt in range(4):
            try:
                async with self._session.post(rpc, json=payload) as r:
                    if r.status in (429, 502, 503, 504):
                        await asyncio.sleep(min(2.0, 0.4 * (2 ** attempt)))
                        continue
                    if r.status != 200:
                        return None
                    d = await r.json()
                    return None if d.get("error") else d.get("result")
            except Exception:  # noqa: BLE001
                if attempt < 3:
                    await asyncio.sleep(min(2.0, 0.4 * (2 ** attempt)))
                    continue
                return None
        return None

    async def _block_number(self, rpc):
        res = await self._rpc(rpc, "eth_blockNumber", [])
        try:
            return int(res, 16) if res else None
        except (ValueError, TypeError):
            return None

    async def _get_logs(self, rpc, factory, frm, to, topic):
        res = await self._rpc(rpc, "eth_getLogs", [{
            "fromBlock": hex(frm), "toBlock": hex(to),
            "address": factory.lower(), "topics": [topic]}])
        return res if isinstance(res, list) else []

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
