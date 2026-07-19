"""Cross-chain smart-money — activate the strongest meme predictor.

A token bought by wallets that got into past winners early is one of the best
signals there is. This module:
  · holds a per-chain smart-wallet set (seeded + auto-grown),
  · annotates each snapshot with which smart wallets are among its buyers
    (populates snap.smart_money_wallets → the smart_money_count feature), and
  · harvests a confirmed winner's earliest buyers back into the set — the RH
    scanner's winner-harvest, generalised to every chain.

Buyer data per chain:
  EVM      Blockscout v2 /tokens/{addr}/transfers (receivers = buyers)
  Solana   RugCheck topHolders as a pragmatic proxy (holders of a fresh token
           are overwhelmingly its early buyers)

Seeds: the RH scanner's 14 proven-early wallets seed `robinhood`; other chains
grow purely from their own winners. Add more via MEMELAB_SEED_WALLETS
("chain:addr,chain:addr").
"""

from __future__ import annotations

import logging
from typing import Optional

from .models import Chain, TokenSnapshot

log = logging.getLogger("memelab.smartmoney")

# RH scanner's proven early wallets (robinhood chain).
_RH_SEED = [
    "0xae6ad7c09668c8c6b2838e0c92b28fb2db891ff7",
    "0xbde8bbc336b548357c841b5a5290f553e4ff8809",
    "0x0a01b82508f877b8923bae8427a3d60fec5bd707",
    "0xc1a3e23049e2f016d61d2a2e4fbb241df20b10d7",
    "0xe781b28e02ed5fc4b989905cb6848d318f4735fd",
    "0x5638484ba2d2f1d1d35020572b0aa439a9869192",
    "0x82608cb7ba84322a7a0d4cc0df8f1d2d065877ef",
    "0x39069add37ea21d3db98e01e8ad81bacef739168",
    "0x6099ff02f63c69162175249c2700c84c0a94dafa",
    "0x8e65cbcdc822968131b1e37e5cee02c9ce82ab21",
    "0xf70da97812cb96acdf810712aa562db8dfa3dbef",
    "0x8f47a238c7701247ee8469ddc37ac1df121cfcce",
    "0x3b1f57028e5bc1f28c7201b1f6bd913e46b64e62",
    "0x16f5ef133d0d15b196a778d22bf1ec56f8f37c05",
]

_INFRA = {"0x0000000000000000000000000000000000000000",
          "0x000000000000000000000000000000000000dead"}


def default_seeds() -> list:
    """[(Chain, wallet), …] seed set. Extend via env MEMELAB_SEED_WALLETS."""
    import os
    seeds = [(Chain.ROBINHOOD, w) for w in _RH_SEED]
    for pair in os.environ.get("MEMELAB_SEED_WALLETS", "").split(","):
        pair = pair.strip()
        if ":" in pair:
            ch, w = pair.split(":", 1)
            try:
                seeds.append((Chain(ch.strip()), w.strip()))
            except ValueError:
                pass
    return seeds


class SmartMoney:
    def __init__(self, store, session=None, seeds: list = None,
                 harvest_buyers: int = 12, core_alpha_min_overlap: int = 3,
                 toxic_min_rugs: int = 2):
        self.store = store
        self._session = session
        self.harvest_buyers = harvest_buyers
        self.core_alpha_min_overlap = core_alpha_min_overlap
        self.toxic_min_rugs = toxic_min_rugs
        for chain, wallet in (seeds if seeds is not None else default_seeds()):
            store.add_smart_wallet(chain, wallet, source="seed")

    def is_smart(self, chain: Chain, wallet: str) -> bool:
        return self.store.is_smart_wallet(chain, wallet)

    async def annotate(self, snap: TokenSnapshot) -> None:
        """Set snap.smart_money_wallets = smart wallets among this token's buyers,
        plus snap.smart_money_quality = the summed reputation of those wallets."""
        smart = self.store.smart_wallets(snap.chain)
        if not smart:
            return
        buyers = await self._buyers(snap.chain, snap.token_address)
        hits = sorted({b for b in buyers if b in smart})
        if not hits:
            return
        snap.smart_money_wallets = hits
        # Forward-pick ledger: record that these smart wallets bought this token,
        # so their later track record can be graded once it settles.
        for w in hits:
            try:
                self.store.record_smart_buy(snap.chain, w, snap.token_address)
            except Exception:  # noqa: BLE001
                pass
        # Quality-weight: sum each buyer's 0..1 reputation → the learnable feature.
        try:
            from . import wallet_intel as wi
            rep = self.store.wallet_reputation_rows(snap.chain, hits)
            snap.smart_money_quality = wi.quality_sum(
                [wi.quality(rep.get(w, {})) for w in hits])
        except Exception:  # noqa: BLE001
            snap.smart_money_quality = None
        # Toxic flag (repeat-rugger among buyers) + core-alpha flag (proven sharp).
        try:
            toxic = self.store.toxic_wallets(snap.chain, self.toxic_min_rugs)
            snap.toxic_buyer = any(w in toxic for w in hits)
            core = self.store.core_alpha_wallets(snap.chain, self.core_alpha_min_overlap)
            hit_core = next((w for w in hits if w in core), "")
            snap.core_alpha_buyer = f"{hit_core[:8]}…{hit_core[-4:]}" if hit_core else ""
            kols = self.store.kol_wallets(snap.chain)
            snap.kol_buyer = next((kols[w] for w in hits if w in kols), "")
        except Exception:  # noqa: BLE001
            pass

    async def harvest_winner(self, chain: Chain, token: str, mult: float = 0.0) -> int:
        """Add a confirmed winner's earliest buyers to the smart set. Returns count.

        Also credits every early buyer in the reputation ledger against this
        winner (wallet_winners) — DISTINCT winners per wallet = its overlap, the
        primary quality signal — even for a wallet already in the set."""
        if not self.store.mark_harvested(chain, token):
            return 0
        buyers = await self._early_buyers(chain, token, self.harvest_buyers)
        added = 0
        for w in buyers:
            try:
                self.store.record_wallet_winner(chain, w, token, mult)
            except Exception:  # noqa: BLE001
                pass
            if self.store.add_smart_wallet(chain, w, source=f"winner:{token.lower()}"):
                added += 1
        if added:
            log.info("smart-money: harvested %d early buyers of %s %s", added, chain.value, token)
        return added

    async def harvest_rug(self, chain: Chain, token: str) -> int:
        """Credit a confirmed rug's early buyers toward toxicity (wallet_rugs).
        A wallet in >= toxic_min_rugs rugs (net-negative vs winners) → tokens it
        buys get demoted (alerts suppressed)."""
        if not self.store.marker_new(f"rugharvest|{chain.value}|{token.lower()}"):
            return 0
        buyers = await self._early_buyers(chain, token, self.harvest_buyers)
        for w in buyers:
            try:
                self.store.record_wallet_rug(chain, w, token)
            except Exception:  # noqa: BLE001
                pass
        return len(buyers)

    async def _creator_of(self, chain: Chain, token: str) -> str:
        """Raw creator of an EVM token via Blockscout (empty for Solana/unknown)."""
        if not chain.is_evm or self._session is None:
            return ""
        from .chains.registry import REGISTRY
        base = (REGISTRY[chain].explorer_api_url or "").rstrip("/")
        if not base:
            return ""
        base = base + "/v2" if base.endswith("/api") else base
        data = await self._get(f"{base}/addresses/{token}", None)
        if not isinstance(data, dict):
            return ""
        return (data.get("creator_address_hash") or data.get("creator_address") or "").lower()

    async def label_deployer(self, chain: Chain, token: str, outcome: str,
                             launchpad_managers: set = None) -> None:
        """Record a token's deployer + outcome (skips shared launchpad managers)."""
        creator = await self._creator_of(chain, token)
        if not creator or (launchpad_managers and creator in launchpad_managers):
            return
        self.store.record_deployer_token(chain, creator, token, outcome)

    # -- buyer sources --------------------------------------------------

    async def _buyers(self, chain: Chain, token: str) -> list:
        if chain.is_evm:
            return await self._evm_receivers(chain, token, newest_first=True)
        return await self._solana_holders(token)

    async def _early_buyers(self, chain: Chain, token: str, n: int) -> list:
        if chain.is_evm:
            recv = await self._evm_receivers(chain, token, newest_first=False)
            return recv[:n]
        return (await self._solana_holders(token))[:n]

    async def _evm_receivers(self, chain: Chain, token: str, newest_first: bool) -> list:
        from .chains.registry import REGISTRY
        base = (REGISTRY[chain].explorer_api_url or "").rstrip("/")
        if not base or self._session is None:
            return []
        base = base + "/v2" if base.endswith("/api") else base
        out: list = []
        params: dict = {}
        for _ in range(3):
            data = await self._get(f"{base}/tokens/{token}/transfers", params)
            items = data.get("items") if isinstance(data, dict) else None
            if not items:
                break
            for tx in items:
                to = tx.get("to")
                addr = (to.get("hash") if isinstance(to, dict) else to) or ""
                addr = addr.lower()
                if addr and addr not in _INFRA and addr not in out:
                    out.append(addr)
            nxt = data.get("next_page_params")
            if not nxt or len(out) >= 200:
                break
            params = nxt
        return out if newest_first else list(reversed(out))

    async def _solana_holders(self, mint: str) -> list:
        if self._session is None:
            return []
        data = await self._get(f"https://api.rugcheck.xyz/v1/tokens/{mint}/report", None)
        holders = (data or {}).get("topHolders") or []
        return [h.get("address", "").lower() for h in holders if h.get("address")]

    async def _get(self, url: str, params: Optional[dict]):
        import asyncio
        for attempt in range(3):
            try:
                async with self._session.get(url, params=params or {}) as r:
                    if r.status in (429, 502, 503, 504):
                        await asyncio.sleep(min(2.0, 0.4 * (2 ** attempt)))
                        continue
                    return await r.json() if r.status == 200 else None
            except Exception:  # noqa: BLE001
                if attempt < 2:
                    await asyncio.sleep(0.4 * (2 ** attempt))
                    continue
                return None
        return None
