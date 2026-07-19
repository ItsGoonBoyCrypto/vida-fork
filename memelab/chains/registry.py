"""Per-chain configuration + adapter factory.

One place to declare each chain's endpoints and knobs. `get_adapter(chain)`
returns the right adapter instance. Addresses/URLs are placeholders where not
yet known — fill per chain (RH values are already proven in rhl2_scanner).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..models import Chain


@dataclass
class ChainConfig:
    chain: Chain
    dexscreener_slug: str                 # DexScreener's chain id
    rpc_url: str = ""
    explorer_api_url: str = ""            # Blockscout /api (EVM buyer/creator fetch)
    # Etherscan V2 unified-API chain id (1=ETH, 8453=Base, 56=BSC). Used as the
    # buyer-fetch fallback when there's no Blockscout — one ETHERSCAN_API_KEY
    # covers every chain here.
    etherscan_chain_id: str = ""
    # EVM discovery
    dex_factory_address: str = ""
    dex_factory_kind: str = "univ3"       # univ2 | univ3
    weth_address: str = ""
    launchpads: list = field(default_factory=list)   # [{name, manager, kind, …}]
    # Safety oracles
    goplus_chain_id: str = ""             # EVM: GoPlus decimal chain id
    rugcheck_enabled: bool = False        # Solana
    # Solana discovery
    pumpfun_program: str = ""
    raydium_program: str = ""


# DexScreener slugs are the unifying key. RH values are the ones proven in
# rhl2_scanner; others are the well-known public addresses / slugs.
REGISTRY: dict = {
    Chain.ROBINHOOD: ChainConfig(
        chain=Chain.ROBINHOOD, dexscreener_slug="robinhood",
        rpc_url="https://rpc.mainnet.chain.robinhood.com",
        explorer_api_url="https://robinhoodchain.blockscout.com/api",
        dex_factory_address="0x1f7d7550B1b028f7571E69A784071F0205FD2EfA",
        dex_factory_kind="univ3",
        weth_address="0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73",
        launchpads=[{"name": "flap", "manager": "0x26605f322f7fF986f381bB9A6e3f5DAb0bEaEb09",
                     "kind": "curve", "token_suffixes": ["8888", "7777"]}],
    ),
    Chain.BASE: ChainConfig(
        chain=Chain.BASE, dexscreener_slug="base",
        rpc_url="",                        # TODO: a Base RPC
        explorer_api_url="https://base.blockscout.com/api",
        etherscan_chain_id="8453",         # Etherscan V2 fallback
        goplus_chain_id="8453",
        # TODO: Uniswap V3 / Aerodrome factory, WETH (0x4200...0006), launchpads
    ),
    Chain.ETHEREUM: ChainConfig(
        chain=Chain.ETHEREUM, dexscreener_slug="ethereum",
        rpc_url="",                        # TODO: an ETH RPC
        explorer_api_url="https://eth.blockscout.com/api",
        etherscan_chain_id="1",            # Etherscan V2 fallback
        goplus_chain_id="1",
        # TODO: Uniswap V2/V3 factory, WETH
    ),
    Chain.BNB: ChainConfig(
        chain=Chain.BNB, dexscreener_slug="bsc",
        rpc_url="",                        # TODO: a BSC RPC
        # No canonical Blockscout-v2 for BSC, so buyer/creator fetch uses the
        # Etherscan V2 unified API (chain 56) with ETHERSCAN_API_KEY. Discovery
        # (DexScreener) + safety (GoPlus 56) work with no key.
        explorer_api_url="",
        etherscan_chain_id="56",
        goplus_chain_id="56",
    ),
    Chain.SOLANA: ChainConfig(
        chain=Chain.SOLANA, dexscreener_slug="solana",
        rpc_url="",                        # TODO: Helius / Solana RPC
        rugcheck_enabled=True,
        # TODO: pump.fun + Raydium program ids
    ),
}


def get_adapter(chain: Chain, session=None):
    """Construct the adapter for a chain (lazy import to avoid heavy deps)."""
    cfg = REGISTRY[chain]
    if chain.is_evm:
        from .evm import EvmAdapter
        return EvmAdapter(cfg, session=session)
    if chain is Chain.SOLANA:
        from .solana import SolanaAdapter
        return SolanaAdapter(cfg, session=session)
    raise ValueError(f"no adapter for {chain}")
