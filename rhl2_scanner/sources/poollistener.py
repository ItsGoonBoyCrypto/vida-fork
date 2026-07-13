"""New-pool factory listener — earliest possible discovery.

DexScreener indexes a pair only after it has some activity. To catch a token
the moment liquidity is added, we subscribe to the DEX factory's creation
event via ``eth_getLogs`` polling:

  * UniswapV2-style: ``PairCreated(address indexed token0, address indexed
    token1, address pair, uint)``
  * UniswapV3-style: ``PoolCreated(address indexed token0, address indexed
    token1, uint24 indexed fee, int24 tickSpacing, address pool)``

The listener tracks the last scanned block and yields ``TokenSnapshot`` stubs
(pair + token address, chain) for the non-native side of each new pair. Those
stubs flow into the same enrichment/scoring pipeline as DexScreener discovery.

Needs only ``chain.rpc_url`` + ``chain.dex_factory_address`` (+ ``weth_address``
to pick the memecoin side). Yields nothing until configured, so it composes
safely with DexScreener discovery.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import aiohttp

from ..config import Config
from ..keccak import event_topic
from ..models import TokenSnapshot

log = logging.getLogger("rhl2.poollistener")

PAIR_CREATED_TOPIC = event_topic("PairCreated(address,address,address,uint256)")
POOL_CREATED_TOPIC = event_topic("PoolCreated(address,address,uint24,int24,address)")


def _topic_to_addr(topic: str) -> str:
    """Last 20 bytes of a 32-byte topic -> checksum-less address."""
    return "0x" + topic[-40:]


def _word(data_hex: str, index: int) -> str:
    """Return the 32-byte word at position ``index`` from hex data (no 0x)."""
    d = data_hex[2:] if data_hex.startswith("0x") else data_hex
    start = index * 64
    return d[start:start + 64]


def decode_pair_created(log_entry: dict, kind: str) -> Optional[tuple[str, str, str]]:
    """Return (token0, token1, pair_or_pool) from a factory log, or None."""
    topics = log_entry.get("topics") or []
    data = log_entry.get("data") or "0x"
    if kind == "univ2":
        if len(topics) < 3:
            return None
        token0 = _topic_to_addr(topics[1])
        token1 = _topic_to_addr(topics[2])
        pair = "0x" + _word(data, 0)[-40:]
        return token0, token1, pair
    if kind == "univ3":
        if len(topics) < 4:
            return None
        token0 = _topic_to_addr(topics[1])
        token1 = _topic_to_addr(topics[2])
        pool = "0x" + _word(data, 1)[-40:]   # word0 = tickSpacing, word1 = pool
        return token0, token1, pool
    return None


class PoolListener:
    def __init__(self, cfg: Config, session: Optional[aiohttp.ClientSession] = None):
        self.cfg = cfg
        self.chain = cfg.chain
        self._session = session
        self._owns_session = session is None
        self._last_block: Optional[int] = None
        self._rpc_id = 0

    async def __aenter__(self) -> "PoolListener":
        if self._session is None:
            timeout = aiohttp.ClientTimeout(total=self.cfg.runtime.request_timeout_seconds)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self

    async def __aexit__(self, *exc) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()

    def enabled(self) -> bool:
        return bool(self.chain.rpc_url and self.chain.dex_factory_address)

    async def poll_new_pairs(self) -> list[TokenSnapshot]:
        """Return TokenSnapshot stubs for pairs created since the last poll."""
        if not self.enabled() or self._session is None:
            return []

        head = await self._block_number()
        if head is None:
            return []

        if self._last_block is None:
            self._last_block = max(0, head - self.chain.pool_scan_block_lookback)

        from_block = self._last_block + 1
        if from_block > head:
            return []

        topic = PAIR_CREATED_TOPIC if self.chain.dex_factory_kind == "univ2" else POOL_CREATED_TOPIC
        snaps: dict[str, TokenSnapshot] = {}

        # Chunk the range so a single eth_getLogs never spans too many blocks.
        start = from_block
        while start <= head:
            end = min(start + self.chain.pool_scan_max_range - 1, head)
            logs = await self._get_logs(start, end, topic)
            for entry in logs:
                snap = self._log_to_snapshot(entry)
                if snap:
                    snaps[snap.pair_address.lower()] = snap
            start = end + 1

        self._last_block = head
        if snaps:
            log.info("pool listener found %d new pairs (blocks %d-%d)", len(snaps), from_block, head)
        return list(snaps.values())

    def _log_to_snapshot(self, entry: dict) -> Optional[TokenSnapshot]:
        decoded = decode_pair_created(entry, self.chain.dex_factory_kind)
        if not decoded:
            return None
        token0, token1, pair = decoded
        weth = (self.chain.weth_address or "").lower()

        # Pick the memecoin side (the non-native token).
        if weth and token0.lower() == weth:
            token = token1
        elif weth and token1.lower() == weth:
            token = token0
        else:
            token = token0  # no native match; default to token0, enrichment will sort it out

        return TokenSnapshot(
            chain=self.chain.dexscreener_chain,
            pair_address=pair,
            token_address=token,
            age_minutes=0.0,   # brand new by construction
        )

    # -- rpc -------------------------------------------------------------

    async def _rpc(self, method: str, params: list) -> Any:
        assert self._session is not None
        self._rpc_id += 1
        payload = {"jsonrpc": "2.0", "id": self._rpc_id, "method": method, "params": params}
        try:
            async with self._session.post(self.chain.rpc_url, json=payload) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                return data.get("result")
        except (aiohttp.ClientError, TimeoutError, ValueError):
            return None

    async def _block_number(self) -> Optional[int]:
        res = await self._rpc("eth_blockNumber", [])
        try:
            return int(res, 16) if res else None
        except (ValueError, TypeError):
            return None

    async def _get_logs(self, from_block: int, to_block: int, topic0: str) -> list[dict]:
        params = [{
            "fromBlock": hex(from_block),
            "toBlock": hex(to_block),
            "address": self.chain.dex_factory_address,
            "topics": [topic0],
        }]
        res = await self._rpc("eth_getLogs", params)
        return res if isinstance(res, list) else []
