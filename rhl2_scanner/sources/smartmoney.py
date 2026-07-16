"""Smart-money source: match a curated wallet set against a token's buyers,
and harvest the earliest buyers of confirmed winners back into that set.

Two jobs:
  * ``active_wallets(snap)`` — which curated wallets appear among the token's
    buyers (incoming transfers). Populates ``snap.smart_money_wallets`` which
    the discovery scorer rewards.
  * ``early_buyers(token, n)`` — the first ``n`` distinct non-infra receivers of
    a token, used to auto-seed the smart set from a token that became a runner
    ("wallets that bought previous bangers early").

Both read the explorer. Blockscout (RH Chain's explorer) does NOT serve the
Etherscan ``account/tokentx`` action reliably, so we prefer the Blockscout v2
``/tokens/{addr}/transfers`` feed and fall back to the Etherscan-style call for
other explorers. No explorer configured => empty (no false positives).
"""

from __future__ import annotations

from typing import Any, Optional

import aiohttp

from ..config import Config
from ..models import TokenSnapshot


def _infra_addresses(cfg: Config) -> set[str]:
    ch = cfg.chain
    infra = {
        (ch.weth_address or "").lower(),
        (ch.dex_factory_address or "").lower(),
        (ch.dex_router_address or "").lower(),
        (ch.nft_position_manager or "").lower(),
        "0x0000000000000000000000000000000000000000",
        "0x000000000000000000000000000000000000dead",
    }
    for lp in (ch.launchpads or []):
        if isinstance(lp, dict) and lp.get("manager"):
            infra.add(str(lp["manager"]).lower())
    for a in (ch.launchpad_factory_addresses or []):
        if a:
            infra.add(str(a).lower())
    infra.discard("")
    return infra


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

    # -- explorer helpers -----------------------------------------------

    def _v2_base(self) -> str:
        base = (self.cfg.chain.explorer_api_url or "").rstrip("/")
        if not base:
            return ""
        return base + "/v2" if base.endswith("/api") else base

    async def _get(self, url: str, params: Optional[dict] = None):
        if self._session is None:
            return None
        try:
            async with self._session.get(url, params=params or {}) as resp:
                if resp.status != 200:
                    return None
                return await resp.json()
        except (aiohttp.ClientError, TimeoutError, ValueError):
            return None

    async def _v2_receivers(self, token: str, max_pages: int = 3) -> list[str]:
        """Receiver addresses from Blockscout v2 token transfers (newest-first).

        Returns them oldest-last (as the feed gives them); callers that want the
        earliest reverse the list. Follows next_page_params up to ``max_pages``.
        """
        base = self._v2_base()
        if not base:
            return []
        out: list[str] = []
        params: dict[str, Any] = {}
        for _ in range(max_pages):
            data = await self._get(f"{base}/tokens/{token}/transfers", params)
            if not isinstance(data, dict):
                break
            items = data.get("items")
            if not isinstance(items, list):
                break
            for tx in items:
                to = tx.get("to")
                addr = (to.get("hash") if isinstance(to, dict) else to) or ""
                if addr:
                    out.append(addr.lower())
            nxt = data.get("next_page_params")
            if not nxt:
                break
            params = nxt
        return out

    async def _etherscan_receivers(self, token: str, sort: str = "asc") -> list[str]:
        """Etherscan-style tokentx fallback (non-Blockscout explorers)."""
        explorer = self.cfg.chain.explorer_api_url
        if not explorer:
            return []
        params: dict[str, Any] = {
            "module": "account", "action": "tokentx",
            "contractaddress": token, "page": 1, "offset": 1000, "sort": sort,
        }
        if self.cfg.chain.explorer_api_key:
            params["apikey"] = self.cfg.chain.explorer_api_key
        data = await self._get(explorer, params)
        result = data.get("result") if isinstance(data, dict) else None
        if not isinstance(result, list):
            return []
        return [(tx.get("to") or "").lower() for tx in result if tx.get("to")]

    # -- public API -----------------------------------------------------

    async def active_wallets(self, snap: TokenSnapshot) -> list[str]:
        """Curated wallets found among this token's buyers -> snap.smart_money."""
        if not self.wallets:
            return []
        receivers = await self._v2_receivers(snap.token_address)
        if not receivers:
            receivers = await self._etherscan_receivers(snap.token_address, sort="desc")
        seen = {r for r in receivers if r in self.wallets}
        if seen:
            snap.smart_money_wallets = sorted(seen)
        return snap.smart_money_wallets

    async def wallet_acquisitions(self, wallet: str, limit: int = 40) -> list[dict]:
        """Recent tokens a wallet ACQUIRED (incoming ERC-20 transfers).

        Returns newest-first dicts {token, symbol, name, ts} deduped to the
        wallet's *first* acquisition of each token in the window. Used by the
        /wallet research command to see what a proven-early wallet got into.
        Blockscout v2 only (RH Chain's explorer); empty elsewhere.
        """
        base = self._v2_base()
        if not base:
            return []
        w = wallet.lower()
        params: dict[str, Any] = {"type": "ERC-20", "filter": "to"}
        acquisitions: dict[str, dict] = {}
        for _ in range(3):  # up to 3 pages
            data = await self._get(f"{base}/addresses/{wallet}/token-transfers", params)
            if not isinstance(data, dict):
                break
            items = data.get("items")
            if not isinstance(items, list):
                break
            for tx in items:
                to = tx.get("to")
                to_addr = (to.get("hash") if isinstance(to, dict) else to) or ""
                if to_addr.lower() != w:
                    continue
                tok = tx.get("token") or {}
                addr = (tok.get("address") or tok.get("address_hash") or "").lower()
                if not addr:
                    continue
                # keep the earliest seen (feed is newest-first, so overwrite)
                acquisitions[addr] = {
                    "token": addr,
                    "symbol": tok.get("symbol") or "?",
                    "name": tok.get("name") or "",
                    "ts": tx.get("timestamp") or "",
                }
            nxt = data.get("next_page_params")
            if not nxt or len(acquisitions) >= limit:
                break
            params = {**params, **nxt}
        return list(acquisitions.values())[:limit]

    async def early_buyers(self, token: str, n: int) -> list[str]:
        """First ``n`` distinct non-infra receivers of ``token`` (earliest first).

        Used to auto-seed the smart set from a confirmed winner. Prefers the
        Etherscan asc feed (true earliest) then Blockscout v2 (reversed to
        approximate earliest). Infra/dead/router addresses are excluded.
        """
        infra = _infra_addresses(self.cfg)
        ordered = await self._etherscan_receivers(token, sort="asc")
        if not ordered:
            # v2 is newest-first; reverse so earliest come first.
            ordered = list(reversed(await self._v2_receivers(token, max_pages=5)))
        out: list[str] = []
        for addr in ordered:
            if addr and addr not in infra and addr not in out:
                out.append(addr)
                if len(out) >= n:
                    break
        return out
