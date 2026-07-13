"""LP-lock reader — detects locked LP and reads remaining lock DURATION.

Two levels of signal:
  * **locked?** — an LP amount at or above ``lp_lock_min_fraction`` of the pair's
    LP supply sitting in a known locker contract counts as locked. This works
    for *any* locker (UNCX/Unicrypt, Team.Finance, PinkLock, Mudra, ...) because
    they all custody the LP tokens.
  * **for how long?** — remaining seconds until unlock. Lockers expose this via
    different getters, so each locker is configured with the 4-byte selector of
    a getter returning a unix timestamp, and how to pass the pair address.

Config (``chain.lp_lockers``), each entry:
    {address: "0x..", unlock_selector: "0x..", arg: "lp" | "none"}

``lp_locker_addresses`` (plain string list) is also honored for locked-detection
only (no duration). Remaining time is computed against the chain's latest block
timestamp, not local wall-clock, to stay accurate on an L2.

Known selectors (verify against the specific locker before trusting duration):
  * ``getLockedTokenAtIndex``/``tokenLocks`` shapes vary — prefer a thin custom
    getter or the locker's subgraph if exact duration matters.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import aiohttp

from ..config import Config

log = logging.getLogger("rhl2.lplock")

_SEL_TOTAL_SUPPLY = "0x18160ddd"
_SEL_BALANCEOF = "0x70a08231"


def _addr32(a: str) -> str:
    return a.lower().replace("0x", "").rjust(64, "0")


def _norm_lockers(cfg: Config) -> list[dict]:
    """Normalize both config shapes into a list of locker dicts."""
    out: list[dict] = []
    for entry in cfg.chain.lp_lockers or []:
        if isinstance(entry, dict) and entry.get("address"):
            out.append(entry)
        elif isinstance(entry, str):
            out.append({"address": entry})
    for addr in cfg.chain.lp_locker_addresses or []:
        if addr and not any(l["address"].lower() == addr.lower() for l in out):
            out.append({"address": addr})
    return out


class LpLockReader:
    def __init__(self, cfg: Config, session: Optional[aiohttp.ClientSession] = None):
        self.cfg = cfg
        self.chain = cfg.chain
        self._session = session
        self._owns_session = session is None
        self._rpc_id = 0

    async def __aenter__(self) -> "LpLockReader":
        if self._session is None:
            timeout = aiohttp.ClientTimeout(total=self.cfg.runtime.request_timeout_seconds)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self

    async def __aexit__(self, *exc) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()

    async def read(self, pair_address: str) -> tuple[Optional[bool], Optional[int]]:
        """Return (lp_locked, lp_lock_seconds_remaining)."""
        lockers = _norm_lockers(self.cfg)
        if not lockers or not self.chain.rpc_url or self._session is None:
            return None, None

        total = await self._call_int(pair_address, _SEL_TOTAL_SUPPLY)
        if not total:
            return None, None

        locked = False
        best_remaining: Optional[int] = None
        now_ts = await self._block_timestamp()

        for locker in lockers:
            addr = locker["address"]
            bal = await self._call_int(pair_address, _SEL_BALANCEOF + _addr32(addr))
            if bal is None:
                continue
            if bal / total >= self.chain.lp_lock_min_fraction:
                locked = True
                selector = locker.get("unlock_selector")
                if selector and now_ts is not None:
                    unlock_ts = await self._read_unlock(locker, pair_address)
                    if unlock_ts is not None:
                        remaining = unlock_ts - now_ts
                        if remaining > 0:
                            best_remaining = max(best_remaining or 0, remaining)

        if not locked:
            return None, None
        return True, best_remaining

    async def _read_unlock(self, locker: dict, pair_address: str) -> Optional[int]:
        selector = locker["unlock_selector"]
        selector = selector if selector.startswith("0x") else "0x" + selector
        data = selector
        if locker.get("arg", "lp") == "lp":
            data += _addr32(pair_address)
        ts = await self._call_int(locker["address"], data)
        # Guard against absurd values (some lockers return 0 or ms). Accept only
        # plausible unix-seconds in a sane window (2020..2100).
        if ts is None or ts < 1_500_000_000 or ts > 4_000_000_000:
            return None
        return ts

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

    async def _call_int(self, to: str, data: str) -> Optional[int]:
        if not data.startswith("0x"):
            data = "0x" + data
        res = await self._rpc("eth_call", [{"to": to, "data": data}, "latest"])
        if not res or res == "0x":
            return None
        try:
            return int(res, 16)
        except (ValueError, TypeError):
            return None

    async def _block_timestamp(self) -> Optional[int]:
        block = await self._rpc("eth_getBlockByNumber", ["latest", False])
        if not isinstance(block, dict):
            return None
        ts = block.get("timestamp")
        try:
            return int(ts, 16) if ts else None
        except (ValueError, TypeError):
            return None
