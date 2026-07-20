"""Solana (SVM) adapter — the main net-new chain.

Safety enrichment uses RugCheck.xyz — Solana's de-facto rug oracle — which in
one call gives a risk score, mint/freeze authority status, LP status and top
holder concentration. Maps cleanly onto the unified snapshot.

  discover(): pump.fun is the direct analog of flap (bonding-curve launches).
  Earliest catch = subscribe to the pump.fun program via a Solana RPC (Helius
  logsSubscribe) or pump.fun's public API; Raydium for graduated/direct pools.
  Until that's wired, the collector falls back to the DexScreener discovery feed
  (slug "solana"), so Solana still collects from day one.

Market data (price/liq/volume) comes from the shared DexScreener ingest — this
adapter only adds Solana-specific safety.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from ..models import Chain, TokenSnapshot
from .base import ChainAdapter

log = logging.getLogger("memelab.solana")

_RUGCHECK = "https://api.rugcheck.xyz/v1/tokens"


class SolanaAdapter(ChainAdapter):
    def __init__(self, config, session=None):
        super().__init__(config)
        self._session = session

    @property
    def chain(self) -> Chain:
        return Chain.SOLANA

    async def discover(self) -> list[TokenSnapshot]:
        """Newest pump.fun launches — the earliest Solana catch (pre-graduation).

        pump.fun is the Solana analog of flap: tokens launch on a bonding curve
        and only hit DexScreener after graduating to Raydium. Its public API lists
        the freshest mints, so we catch them while still bonding.
        """
        if self._session is None:
            return []
        url = ("https://frontend-api.pump.fun/coins?offset=0&limit=50"
               "&sort=created_timestamp&order=DESC&includeNsfw=false")
        data = await self._get(url)
        if not isinstance(data, list):
            return []
        import time
        out = []
        for c in data:
            mint = c.get("mint")
            if not mint:
                continue
            created = c.get("created_timestamp")
            age = None
            if isinstance(created, (int, float)):
                age = max(0.0, (time.time() - created / 1000.0) / 60.0)
            socials = {}
            for k in ("twitter", "telegram", "website"):
                if c.get(k):
                    socials[k] = c[k]
            out.append(TokenSnapshot(
                chain=Chain.SOLANA, token_address=mint,
                symbol=c.get("symbol") or "", name=c.get("name") or "",
                market_cap_usd=_f(c.get("usd_market_cap")),
                on_curve=not c.get("complete"),
                age_minutes=age, socials=socials, launchpad="pumpfun"))
        return out

    async def enrich_safety(self, snap: TokenSnapshot) -> None:
        if self._session is None or not snap.token_address:
            return
        data = await self._get(f"{_RUGCHECK}/{snap.token_address}/report")
        if not isinstance(data, dict):
            return
        # RugCheck score: lower = safer. Normalise to a 0-100 "higher = safer".
        raw = data.get("score")
        if isinstance(raw, (int, float)):
            snap.external_risk_score = max(0.0, min(100.0, 100.0 - float(raw) / 100.0))
        # Disambiguate revoked-vs-absent: only trust the field when RugCheck
        # actually RETURNED it. A missing key (older/incomplete report) must stay
        # unknown (None), not be read as "revoked" — that would pass a mintable
        # token as safe. null value present = authority genuinely revoked.
        if "mintAuthority" in data:
            snap.mint_authority_revoked = not data.get("mintAuthority")
        # top holders concentration
        holders = data.get("topHolders") or []
        pcts = sorted((_f(h.get("pct")) for h in holders), reverse=True)
        pcts = [p for p in pcts if p is not None]
        if pcts:
            snap.top1_supply_pct = round(pcts[0], 2)
            snap.top10_supply_pct = round(sum(pcts[:10]), 2)
        # LP: RugCheck markets carry lp locked/burned percentages
        markets = data.get("markets") or []
        for m in markets:
            lp = m.get("lp") or {}
            if _f(lp.get("lpLockedPct")) and _f(lp.get("lpLockedPct")) >= 90:
                snap.lp_burned_or_locked = True
                break
        # honeypot-ish: RugCheck flags non-transferable / freeze risks
        risks = {r.get("name", "").lower() for r in (data.get("risks") or [])}
        if "honeypot" in risks or "cannot sell" in risks:
            snap.is_honeypot = True

        # Token-2022 danger extensions: permanent-delegate / transfer-hook /
        # non-transferable / default-frozen / settable transfer-fee. These pass
        # mint & freeze checks yet still rug — the biggest active Solana vector.
        try:
            from ..token2022 import scan_rugcheck
            t22 = scan_rugcheck(data)
            # An optional direct getAccountInfo(jsonParsed) is higher-fidelity;
            # merge it in when a Solana RPC is configured.
            acct = await self._token2022_via_rpc(snap.token_address)
            if acct["flags"]:
                for f in acct["flags"]:
                    if f not in t22["flags"]:
                        t22["flags"].append(f)
                t22["honeypot"] = t22["honeypot"] or acct["honeypot"]
                t22["settable_fee"] = t22["settable_fee"] or acct["settable_fee"]
                if acct["fee_pct"] is not None:
                    t22["fee_pct"] = max(t22.get("fee_pct") or 0.0, acct["fee_pct"])
            if t22["flags"]:
                snap.token2022_flags = t22["flags"]
            if t22["honeypot"]:
                snap.is_honeypot = True          # can be blocked or drained
            if t22["settable_fee"]:
                fee = t22.get("fee_pct")
                # surface as sell-tax so the existing tax gate/alert reacts;
                # a live fee ≥ threshold is a trap, an unknown fee is a caution.
                snap.sell_tax_pct = max(snap.sell_tax_pct or 0.0, fee if fee else 0.0)
        except Exception:  # noqa: BLE001
            log.debug("token-2022 scan failed", exc_info=True)

    async def _token2022_via_rpc(self, mint: str) -> dict:
        """getAccountInfo(jsonParsed) → extension list, if a Solana RPC is set.

        Empty result (no RPC / not token-2022 / error) is safe — the RugCheck
        parse already ran; this only *adds* fidelity when an endpoint exists.
        """
        from ..token2022 import scan_extensions
        import os
        rpc = os.environ.get("MEMELAB_SOLANA_RPC") or getattr(self.config, "rpc_url", "")
        if not rpc or self._session is None:
            return {"flags": [], "honeypot": False, "settable_fee": False, "fee_pct": None}
        payload = {"jsonrpc": "2.0", "id": 1, "method": "getAccountInfo",
                   "params": [mint, {"encoding": "jsonParsed"}]}
        try:
            async with self._session.post(rpc, json=payload) as r:
                if r.status != 200:
                    return {"flags": [], "honeypot": False, "settable_fee": False, "fee_pct": None}
                data = await r.json()
        except Exception:  # noqa: BLE001
            return {"flags": [], "honeypot": False, "settable_fee": False, "fee_pct": None}
        info = (((data or {}).get("result") or {}).get("value") or {}).get("data") or {}
        parsed = info.get("parsed") if isinstance(info, dict) else None
        exts = ((parsed or {}).get("info") or {}).get("extensions") if parsed else None
        return scan_extensions(exts)

    async def _get(self, url: str):
        for attempt in range(4):
            try:
                async with self._session.get(url) as r:
                    if r.status in (429, 502, 503, 504):
                        await asyncio.sleep(min(2.0, 0.4 * (2 ** attempt)))
                        continue
                    return await r.json() if r.status == 200 else None
            except Exception:  # noqa: BLE001
                if attempt < 3:
                    await asyncio.sleep(min(2.0, 0.4 * (2 ** attempt)))
                    continue
                return None
        return None


def _f(x) -> Optional[float]:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None
