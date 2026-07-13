"""Smart-money source: match a curated wallet list against token buyers.

Maintain a curated list of high-win-rate wallets (config
``smart_money_wallets`` or a newline file). This source checks which of them
appear among a token's recent buyers/holders.

The real check needs token-transfer history (explorer `tokentx` for the
token, filtered to the curated set, incoming transfers = holders/buyers).
That call is implemented here against the Etherscan-style explorer; if the
explorer isn't configured it returns an empty list (no false positives).
"""

from __future__ import annotations

from typing import Any, Optional

import aiohttp

from ..config import Config
from ..models import TokenSnapshot


class SmartMoneyClient:
    def __init__(self, cfg: Config, session: Optional[aiohttp.ClientSession] = None):
        self.cfg = cfg
        self.wallets = {w.lower() for w in cfg.smart_money_wallets}
        self._session = session
        self._owns_session = session is None

    async def __aenter__(self) -> "SmartMoneyClient":
        if self._session is None:
            timeout = aiohttp.ClientTimeout(total=self.cfg.runtime.request_timeout_seconds)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self

    async def __aexit__(self, *exc) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()

    async def active_wallets(self, snap: TokenSnapshot) -> list[str]:
        if not self.wallets:
            return []
        explorer = self.cfg.chain.explorer_api_url
        if not explorer or self._session is None:
            return []

        params: dict[str, Any] = {
            "module": "account",
            "action": "tokentx",
            "contractaddress": snap.token_address,
            "page": 1,
            "offset": 1000,
            "sort": "asc",
        }
        if self.cfg.chain.explorer_api_key:
            params["apikey"] = self.cfg.chain.explorer_api_key

        try:
            async with self._session.get(explorer, params=params) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
        except (aiohttp.ClientError, TimeoutError):
            return []

        result = data.get("result")
        if not isinstance(result, list):
            return []

        seen: set[str] = set()
        for tx in result:
            # Incoming transfer to a curated wallet = it acquired the token.
            to_addr = (tx.get("to") or "").lower()
            if to_addr in self.wallets:
                seen.add(to_addr)
        snap.smart_money_wallets = sorted(seen)
        return snap.smart_money_wallets
