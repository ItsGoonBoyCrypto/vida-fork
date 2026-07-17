"""Unified market-data ingest — DexScreener across all four chains.

DexScreener uses the SAME schema for every chain (price, liquidity, fdv,
volume/txns per 5m/1h/6h/24h window, socials), keyed by a chain slug. That makes
it the platform's common spine: one client fills market fields on any
TokenSnapshot regardless of chain.

API (public, no key): https://api.dexscreener.com
  · /latest/dex/tokens/{addr}            → all pairs for a token
  · /latest/dex/pairs/{slug}/{pair}      → one pair
  · /token-profiles, /token-boosts       → discovery/hype signals
Rate-limited — reuse the retry/backoff pattern from rhl2_scanner.
"""

from __future__ import annotations

from typing import Optional

from ..models import Chain, TokenSnapshot


class DexScreenerFeed:
    BASE = "https://api.dexscreener.com"

    def __init__(self, session=None):
        self._session = session

    async def market_for(self, chain: Chain, token_address: str) -> Optional[TokenSnapshot]:
        """Best (highest-liquidity) pair for a token on `chain`, as a snapshot.

        Returns None if DexScreener doesn't index it (e.g. still on a bonding
        curve pre-graduation — that's expected; the chain adapter covers those).
        """
        # TODO: GET /latest/dex/tokens/{token_address}, filter items to
        # chainId == chain.value, pick max-liquidity pair, map to TokenSnapshot.
        raise NotImplementedError

    async def new_pairs(self, chain: Chain) -> list[TokenSnapshot]:
        """DexScreener-visible fresh pairs for a chain (indexed-discovery path).

        Complements adapter.discover() (source-level, earliest). Useful for
        chains/launchpads we don't yet have a source listener for.
        """
        # TODO: token-profiles / boosts / search filtered to chain slug.
        raise NotImplementedError

    @staticmethod
    def _map(item: dict, chain: Chain) -> TokenSnapshot:
        """Map a DexScreener pair object → TokenSnapshot (shared for all chains)."""
        # TODO: vol.m5/h1/h24, txns.*.buys/sells, priceChange.*, liquidity.usd,
        # marketCap/fdv, pairCreatedAt, info.socials, boosts.
        raise NotImplementedError
