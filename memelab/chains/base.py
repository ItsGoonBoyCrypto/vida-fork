"""ChainAdapter — the one interface every chain implements.

An adapter does two jobs the unified market feed (DexScreener) can't:
  1. DISCOVER new tokens at the source (launchpad / DEX factory / pump.fun),
     earlier than DexScreener indexes them.
  2. ENRICH safety + distribution from that chain's on-chain data / rug APIs.

Everything else (market data, storage, metrics, backtest, screening) is shared
and chain-agnostic. Add a chain = implement this class; nothing downstream
changes.
"""

from __future__ import annotations

import abc
from typing import Optional

from ..models import Chain, TokenSnapshot


class ChainAdapter(abc.ABC):
    def __init__(self, config: "ChainConfig"):
        self.config = config

    @property
    @abc.abstractmethod
    def chain(self) -> Chain: ...

    @abc.abstractmethod
    async def discover(self) -> list[TokenSnapshot]:
        """Return stubs for tokens newly created since the last call.

        EVM: poll DEX-factory + launchpad logs (eth_getLogs). Solana: pump.fun /
        Raydium new-pool feed. Stubs carry chain + token/pair address; the market
        feed + enrich() fill the rest.
        """

    @abc.abstractmethod
    async def enrich_safety(self, snap: TokenSnapshot) -> None:
        """Populate safety + distribution fields in place (best-effort).

        EVM: honeypot sim, authorities, holders, bundle/sniper, GoPlus.
        Solana: SPL mint/freeze authority, RugCheck score, top holders.
        Leave unknown facts as None — never guess.
        """

    async def close(self) -> None:  # optional cleanup hook
        return None
