"""Bundle / insider clustering detection.

Two independent heuristics from the spec:

  1. Same/near-block sniping: wallets that acquired the token within the
     first N blocks of the pair's life (coordinated launch snipers).
  2. Common-funder clustering: fresh wallets funded from the same source
     address shortly before buying (Bubblemaps-style cluster).

``analyze_bundles`` is a *pure* function over a normalized transfer list so
it is fully unit-testable without network. ``BundleAnalyzer`` wraps it with
an explorer fetch for live use.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Optional

import aiohttp

from .config import Config
from .models import TokenSnapshot


@dataclass
class Transfer:
    block: int
    from_addr: str
    to_addr: str
    value: float


@dataclass
class BundleResult:
    bundle_supply_pct: Optional[float]      # % supply in common-funder clusters
    sniper_cluster_pct: Optional[float]     # % supply bought in launch window
    largest_cluster_wallets: int


def analyze_bundles(
    transfers: list[Transfer],
    total_supply: float,
    launch_block: Optional[int] = None,
    launch_window_blocks: int = 3,
    min_cluster_size: int = 3,
    infra: Optional[set] = None,
) -> BundleResult:
    """Detect coordinated accumulation from raw transfers.

    ``transfers`` should be the token's transfer log (ascending block order).
    ``total_supply`` is used to express clusters as a % of supply.
    ``infra`` = pool/router/factory/launchpad/burn/WETH addresses. On a launchpad
    every buyer is funded BY the pool/launchpad, which would otherwise flag every
    holder as one giant "bundle" (false positive) — so infra addresses are
    excluded both as cluster funders and as sniper/holder participants.
    """
    if total_supply <= 0 or not transfers:
        return BundleResult(None, None, 0)
    infra = {a.lower() for a in (infra or set())}

    # Net holdings per wallet (incoming - outgoing), plus first-seen block.
    holdings: dict[str, float] = defaultdict(float)
    first_block: dict[str, int] = {}
    funder: dict[str, str] = {}   # first non-zero inbound counterparty
    for tx in transfers:
        holdings[tx.to_addr] += tx.value
        holdings[tx.from_addr] -= tx.value
        if tx.to_addr not in first_block:
            first_block[tx.to_addr] = tx.block
            funder[tx.to_addr] = tx.from_addr

    if launch_block is None:
        launch_block = min(tx.block for tx in transfers)

    # 1) Snipers: wallets first acquiring within the launch window (excl. infra).
    sniper_supply = sum(
        max(0.0, holdings[w])
        for w, b in first_block.items()
        if b <= launch_block + launch_window_blocks and w not in infra
    )
    sniper_pct = 100.0 * sniper_supply / total_supply

    # 2) Common-funder clusters: fresh wallets seeded by the SAME NON-infra
    #    address. Buyers funded by the pool/router/launchpad are normal, not a
    #    bundle, so those funders are skipped.
    by_funder: dict[str, list[str]] = defaultdict(list)
    for wallet, src in funder.items():
        if src in infra or wallet in infra:
            continue
        by_funder[src].append(wallet)

    largest = 0
    clustered_supply = 0.0
    for src, wallets in by_funder.items():
        if len(wallets) >= min_cluster_size:
            largest = max(largest, len(wallets))
            clustered_supply += sum(max(0.0, holdings[w]) for w in wallets)
    bundle_pct = 100.0 * clustered_supply / total_supply

    return BundleResult(
        bundle_supply_pct=round(bundle_pct, 2),
        sniper_cluster_pct=round(sniper_pct, 2),
        largest_cluster_wallets=largest,
    )


class BundleAnalyzer:
    """Fetches transfer history and runs ``analyze_bundles``."""

    def __init__(self, cfg: Config, session: Optional[aiohttp.ClientSession] = None):
        self.cfg = cfg
        self._session = session
        self._owns_session = session is None

    async def __aenter__(self) -> "BundleAnalyzer":
        if self._session is None:
            timeout = aiohttp.ClientTimeout(total=self.cfg.runtime.request_timeout_seconds)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self

    async def __aexit__(self, *exc) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()

    async def enrich(self, snap: TokenSnapshot) -> TokenSnapshot:
        transfers, supply = await self._fetch_transfers(snap.token_address)
        if not transfers or supply <= 0:
            return snap
        result = analyze_bundles(transfers, supply, infra=self._infra(snap))
        snap.safety.bundle_supply_pct = result.bundle_supply_pct
        snap.safety.sniper_cluster_pct = result.sniper_cluster_pct
        return snap

    def _infra(self, snap: TokenSnapshot) -> set:
        """Pool/router/factory/launchpad/NPM/burn/WETH — not real 'bundlers'."""
        c = self.cfg.chain
        addrs = {
            snap.pair_address, c.dex_router_address, c.dex_factory_address,
            c.weth_address, c.launchpad_factory_address, c.nft_position_manager,
        }
        addrs |= set(c.launchpad_factory_addresses or [])
        addrs |= set(c.excluded_holder_addresses)
        infra = {a.lower() for a in addrs if a}
        infra |= {"0x000000000000000000000000000000000000dead",
                  "0x0000000000000000000000000000000000000000"}
        return infra

    async def _fetch_transfers(self, token: str) -> tuple[list[Transfer], float]:
        explorer = self.cfg.chain.explorer_api_url
        if not explorer or self._session is None:
            return [], 0.0
        params: dict[str, Any] = {
            "module": "account",
            "action": "tokentx",
            "contractaddress": token,
            "page": 1,
            "offset": 2000,
            "sort": "asc",
        }
        if self.cfg.chain.explorer_api_key:
            params["apikey"] = self.cfg.chain.explorer_api_key
        try:
            async with self._session.get(explorer, params=params) as resp:
                if resp.status != 200:
                    return [], 0.0
                data = await resp.json()
        except (aiohttp.ClientError, TimeoutError):
            return [], 0.0

        rows = data.get("result")
        if not isinstance(rows, list) or not rows:
            return [], 0.0

        decimals = _to_int(rows[0].get("tokenDecimal")) or 18
        scale = 10 ** decimals
        transfers: list[Transfer] = []
        for r in rows:
            block = _to_int(r.get("blockNumber"))
            value = _to_int(r.get("value"))
            if block is None or value is None:
                continue
            transfers.append(
                Transfer(
                    block=block,
                    from_addr=(r.get("from") or "").lower(),
                    to_addr=(r.get("to") or "").lower(),
                    value=value / scale,
                )
            )
        # Supply approximated from circulating movement is unreliable; callers
        # should prefer on-chain totalSupply. Use max cumulative inflow as a
        # floor when supply is otherwise unknown.
        supply = sum(t.value for t in transfers if t.from_addr in {
            "0x0000000000000000000000000000000000000000"
        }) or sum(t.value for t in transfers) / 2
        return transfers, supply


def _to_int(x: Any) -> Optional[int]:
    try:
        return int(x)
    except (TypeError, ValueError):
        return None
