"""Bags launchpad client — registry discovery + on-curve pricing (Robinhood Chain).

Bags exposes exactly what we need through two contracts:

* **BagsLens.getTokenState(token)** — one eth_call returning the whole state:
  exists / migrated flags, spot price, bonding progress, and live ETH reserves.
  This prices a Bags token while it's still on the curve (pre-graduation), just
  like flap's getTokenV2.
* **BagsFactory** — an append-only registry (``allTokensLength`` + ``getTokens``)
  whose tail is the newest launches, so discovery needs no log-scanning or event
  topic to pin — we just read the new entries each cycle.

Addresses are host-configured (RHL2_BAGS_MANAGER = the Lens, RHL2_BAGS_FACTORY =
the Factory); the client is inert until both are set. Dependency-free decoding
(mirrors keccak.py / the flap reader).
"""

from __future__ import annotations

import logging
from typing import Optional

import aiohttp

from ..keccak import keccak256

log = logging.getLogger("rhl2.bags")

# getTokenState returns this struct, in this word order (all 32-byte static slots).
_STATE_FIELDS = [
    "exists", "migrated", "curve", "feeShare", "poolId", "thresholdQuote",
    "realQuoteReserves", "realTokenReserves", "virtualTokenReserves",
    "virtualQuoteReserves", "priceQuotePerToken", "bondingProgressPct", "totalRaised",
]
_ADDR_FIELDS = {"curve", "feeShare"}
_BOOL_FIELDS = {"exists", "migrated"}
_RAW_FIELDS = {"poolId"}   # keep as hex

_SEL_STATE = keccak256(b"getTokenState(address)").hex()[:8]
_SEL_LEN = keccak256(b"allTokensLength()").hex()[:8]
_SEL_TOKENS = keccak256(b"getTokens(uint256,uint256)").hex()[:8]


def _words(hexstr: str) -> list[str]:
    b = hexstr[2:] if hexstr.startswith("0x") else hexstr
    return [b[i:i + 64] for i in range(0, len(b), 64)]


def decode_token_state(result: str) -> Optional[dict]:
    """Decode a getTokenState return into a dict, or None if it can't be read."""
    if not result or result == "0x":
        return None
    words = _words(result)
    if len(words) < len(_STATE_FIELDS):
        return None
    out: dict = {}
    for i, name in enumerate(_STATE_FIELDS):
        w = words[i]
        if name in _BOOL_FIELDS:
            out[name] = int(w, 16) != 0
        elif name in _ADDR_FIELDS:
            out[name] = "0x" + w[-40:]
        elif name in _RAW_FIELDS:
            out[name] = "0x" + w
        else:
            out[name] = int(w, 16)
    return out


def decode_address_array(result: str) -> list[str]:
    """Decode an ABI-encoded address[] return (offset, length, elements)."""
    if not result or result == "0x":
        return []
    words = _words(result)
    if len(words) < 2:
        return []
    # words[0] = offset (0x20); words[1] = length; then the elements.
    try:
        length = int(words[1], 16)
    except ValueError:
        return []
    out = []
    for i in range(length):
        idx = 2 + i
        if idx >= len(words):
            break
        out.append("0x" + words[idx][-40:])
    return out


class BagsClient:
    def __init__(self, cfg, session: aiohttp.ClientSession,
                 lens: str = "", factory: str = ""):
        self.cfg = cfg
        self._session = session
        self.lens = (lens or "").strip()
        self.factory = (factory or "").strip()

    def can_price(self) -> bool:
        return bool(self.lens and self.cfg.chain.rpc_url)

    def can_discover(self) -> bool:
        return bool(self.factory and self.cfg.chain.rpc_url)

    async def _call(self, to: str, data: str, delay: float = 1.0) -> Optional[str]:
        """eth_call with light retry on the rate-limited public RPC."""
        import asyncio
        payload = {"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                   "params": [{"to": to, "data": data}, "latest"]}
        for attempt in range(3):
            try:
                async with self._session.post(self.cfg.chain.rpc_url, json=payload) as r:
                    if r.status in (429, 503, 504):
                        if delay:
                            await asyncio.sleep(1.0 + attempt)
                        continue
                    if r.status != 200:
                        return None
                    d = await r.json()
                    if isinstance(d, dict) and d.get("error"):
                        return None
                    return d.get("result")
            except Exception:  # noqa: BLE001
                if attempt < 2 and delay:
                    await asyncio.sleep(1.0 + attempt)
        return None

    async def token_state(self, token: str, delay: float = 1.0) -> Optional[dict]:
        """getTokenState(token) → decoded state dict (None if unreadable)."""
        if not self.can_price():
            return None
        arg = token.lower().replace("0x", "").rjust(64, "0")
        res = await self._call(self.lens, "0x" + _SEL_STATE + arg, delay=delay)
        return decode_token_state(res)

    async def total_tokens(self, delay: float = 1.0) -> Optional[int]:
        if not self.can_discover():
            return None
        res = await self._call(self.factory, "0x" + _SEL_LEN, delay=delay)
        if not res or res == "0x":
            return None
        try:
            return int(res, 16)
        except ValueError:
            return None

    async def tokens_slice(self, offset: int, count: int, delay: float = 1.0) -> list[str]:
        """getTokens(offset, count) → address[]."""
        if not self.can_discover() or count <= 0:
            return []
        data = ("0x" + _SEL_TOKENS
                + offset.to_bytes(32, "big").hex()
                + count.to_bytes(32, "big").hex())
        res = await self._call(self.factory, data, delay=delay)
        return decode_address_array(res or "")

    async def newest_tokens(self, limit: int, known_total: Optional[int] = None,
                            delay: float = 1.0) -> tuple[list[str], int]:
        """Newest ``limit`` launches from the registry tail + the current total.

        Pass ``known_total`` (the last total we saw) to fetch only what's new:
        the returned list covers the tail up to ``limit`` newest addresses.
        """
        total = await self.total_tokens(delay=delay)
        if total is None or total == 0:
            return [], total or 0
        want = min(limit, total)
        if known_total is not None and total > known_total:
            want = min(limit, total - known_total)   # only the genuinely new tail
        offset = max(0, total - want)
        toks = await self.tokens_slice(offset, total - offset, delay=delay)
        toks.reverse()   # newest first
        return toks, total
