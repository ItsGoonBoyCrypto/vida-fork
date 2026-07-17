"""Solana (SVM) adapter — the main net-new chain.

Solana differs from EVM in every mechanic, so this is where most new work is:

  discover():
    · pump.fun new-token feed — tokens launch on a bonding curve (direct analog
      of flap). Subscribe to the pump.fun program via a Solana RPC (Helius
      logsSubscribe / getSignaturesForAddress) or pump.fun's public API.
    · Raydium new-pool feed for graduated / direct launches.
  enrich_safety():
    · SPL mint & freeze authority (should be revoked) via getAccountInfo /
      getMint — the Solana analog of EVM authorities.
    · top holders + concentration via getTokenLargestAccounts.
    · RugCheck.xyz API score (rugcheck_enabled) — Solana's de-facto rug oracle.
    · LP status (burned / locked) for the Raydium pool.

Market data (price/liq/volume/txns) still comes from the shared DexScreener
ingest (slug "solana") — no need to reinvent it here.

Addresses/programs live in registry.ChainConfig (TODO: fill pump.fun + Raydium
program ids, pick a Solana RPC such as Helius).
"""

from __future__ import annotations

from ..models import Chain, TokenSnapshot
from .base import ChainAdapter


class SolanaAdapter(ChainAdapter):
    @property
    def chain(self) -> Chain:
        return Chain.SOLANA

    async def discover(self) -> list[TokenSnapshot]:
        # TODO: pump.fun + Raydium new-token/new-pool feeds → TokenSnapshot stubs
        # (on_curve=True for pump.fun pre-graduation).
        raise NotImplementedError

    async def enrich_safety(self, snap: TokenSnapshot) -> None:
        # TODO: SPL authorities, getTokenLargestAccounts concentration, RugCheck.
        raise NotImplementedError
