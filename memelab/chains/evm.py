"""EVM adapter — Robinhood, Ethereum, Base (all Uniswap-style).

Thin wrapper over the battle-tested rhl2_scanner sources: the DEX-factory pool
listener, bonding-curve launchpad listener, honeypot sim, bundle/sniper and
Blockscout distribution enrichment already work — this generalises them across
EVM chains via ChainConfig.

Implementation plan (mostly wiring, since the RH code exists):
  discover():
    · poll dex_factory_address for PairCreated/PoolCreated (poollistener)
    · poll each launchpad manager for new tokens (launchpad_curve)
  enrich_safety():
    · honeypot sell-sim (simulator) + tax
    · authorities / LP burned-or-locked (chain.EvmChainClient)
    · holders / top10 / dev / bundle / sniper (Blockscout v2 + bundle.py)
    · GoPlus token-security cross-check (goplus_chain_id)
"""

from __future__ import annotations

from ..models import Chain, TokenSnapshot
from .base import ChainAdapter


class EvmAdapter(ChainAdapter):
    @property
    def chain(self) -> Chain:
        return self.config.chain

    async def discover(self) -> list[TokenSnapshot]:
        # TODO: reuse rhl2_scanner.sources.poollistener + launchpad_curve,
        # parameterised by self.config. Return TokenSnapshot(chain=self.chain, …).
        raise NotImplementedError

    async def enrich_safety(self, snap: TokenSnapshot) -> None:
        # TODO: reuse rhl2_scanner.sources.chain / simulator / bundle / safety,
        # writing into the unified snapshot fields.
        raise NotImplementedError
