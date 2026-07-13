"""GoPlus Security token-security client — RugCheck-equivalent for EVM.

GoPlus is the de-facto on-chain risk API for EVM chains (the analog of
RugCheck on Solana). One request returns authorities, taxes, honeypot status,
LP lock/burn holders, holder distribution, and dozens of risk flags — exactly
the facts our SafetyReport needs.

    GET {goplus_api_url}/{chain_id}?contract_addresses=0x...

Fields come back as *stringified* booleans ("0"/"1") and decimal-fraction
taxes ("0.05" = 5%); this client normalizes them into a SafetyReport and
derives a 0-100 risk score from the flag set.

IMPORTANT: GoPlus must support the chain (by decimal chain id). Robinhood L2
may not be covered at launch — in that case leave ``goplus_chain_id`` empty and
rely on the on-chain SafetySource + simulator. This client returns an empty
report (all None) when unconfigured or the token is unknown, so nothing is
silently assumed safe.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import aiohttp

from ..config import Config
from ..models import SafetyReport, TokenSnapshot

log = logging.getLogger("rhl2.goplus")

BURN_ADDRESSES = {
    "0x000000000000000000000000000000000000dead",
    "0x0000000000000000000000000000000000000000",
}

# Boolean risk flags: "1" => raise this flag (higher weight ones penalize more).
_RISK_FLAGS = {
    "is_honeypot": 40,
    "cannot_sell_all": 40,
    "transfer_pausable": 25,
    "is_blacklisted": 20,
    "hidden_owner": 20,
    "can_take_back_ownership": 25,
    "selfdestruct": 30,
    "is_mintable": 15,
    "slippage_modifiable": 15,
    "trading_cooldown": 10,
    "is_anti_whale": 3,
    "external_call": 10,
    "owner_change_balance": 30,
    "is_proxy": 8,
}


def _b(v: Any) -> Optional[bool]:
    """GoPlus '0'/'1' string -> bool; anything else -> None (unknown)."""
    if v in ("1", 1, True):
        return True
    if v in ("0", 0, False):
        return False
    return None


def _pct(v: Any) -> Optional[float]:
    """GoPlus decimal-fraction string ('0.05') -> percent (5.0)."""
    try:
        return float(v) * 100.0
    except (TypeError, ValueError):
        return None


def _f(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class GoPlusClient:
    """SafetySource backed by the GoPlus token-security API."""

    def __init__(self, cfg: Config, session: Optional[aiohttp.ClientSession] = None):
        self.cfg = cfg
        self._session = session
        self._owns_session = session is None

    async def __aenter__(self) -> "GoPlusClient":
        if self._session is None:
            timeout = aiohttp.ClientTimeout(total=self.cfg.runtime.request_timeout_seconds)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self

    async def __aexit__(self, *exc) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()

    async def assess(self, snap: TokenSnapshot) -> SafetyReport:
        chain_id = self.cfg.chain.goplus_chain_id
        if not chain_id or self._session is None:
            return SafetyReport()

        url = f"{self.cfg.chain.goplus_api_url}/{chain_id}"
        params = {"contract_addresses": snap.token_address}
        try:
            async with self._session.get(url, params=params) as resp:
                if resp.status != 200:
                    return SafetyReport()
                data = await resp.json()
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            log.debug("goplus fetch failed: %s", exc)
            return SafetyReport()

        result = (data or {}).get("result") or {}
        entry = result.get(snap.token_address.lower())
        if not isinstance(entry, dict):
            return SafetyReport()
        return self.parse(entry, snap)

    # -- pure parsing (unit-testable without network) --------------------

    @staticmethod
    def parse(entry: dict, snap: Optional[TokenSnapshot] = None) -> SafetyReport:
        r = SafetyReport()

        r.contract_verified = _b(entry.get("is_open_source"))
        r.is_honeypot = _b(entry.get("is_honeypot"))
        r.buy_tax_pct = _pct(entry.get("buy_tax"))
        r.sell_tax_pct = _pct(entry.get("sell_tax"))

        # Authorities: not-mintable => mint disabled; not-pausable => no freeze.
        mintable = _b(entry.get("is_mintable"))
        r.mint_authority_revoked = (not mintable) if mintable is not None else None
        pausable = _b(entry.get("transfer_pausable"))
        r.freeze_authority_revoked = (not pausable) if pausable is not None else None

        # Dev / creator concentration.
        r.dev_holdings_pct = _f_max(_pct(entry.get("owner_percent")), _pct(entry.get("creator_percent")))

        # Holder count.
        hc = entry.get("holder_count")
        if hc not in (None, ""):
            try:
                if snap is not None:
                    snap.holder_count = int(hc)
            except (ValueError, TypeError):
                pass

        # LP status from lp_holders: burned if a burn addr holds ~all LP;
        # locked if any locker/tag marks it locked.
        lp_holders = entry.get("lp_holders") or []
        burned_pct = locked_pct = 0.0
        for h in lp_holders:
            addr = (h.get("address") or "").lower()
            pct = _f(h.get("percent")) or 0.0
            pct *= 100.0 if pct <= 1.0 else 1.0   # some fields are fractions
            if addr in BURN_ADDRESSES:
                burned_pct += pct
            elif _b(h.get("is_locked")) or h.get("tag"):
                locked_pct += pct
        if lp_holders:
            r.lp_burned = burned_pct >= 90.0
            r.lp_locked = (not r.lp_burned) and (locked_pct + burned_pct) >= 90.0

        # Top-holder concentration (exclude LP / locked / burn).
        holders = entry.get("holders") or []
        top = 0.0
        first = None
        for h in holders[:10]:
            if _b(h.get("is_locked")) or (h.get("address") or "").lower() in BURN_ADDRESSES:
                continue
            pct = _f(h.get("percent")) or 0.0
            pct *= 100.0 if pct <= 1.0 else 1.0
            top += pct
            if first is None:
                first = pct
        if holders and snap is not None:
            snap.top10_supply_pct = round(top, 2)
            if first is not None:
                snap.top1_supply_pct = round(first, 2)

        # Flags + derived 0-100 risk score.
        score = 100.0
        for flag, weight in _RISK_FLAGS.items():
            if _b(entry.get(flag)):
                r.high_risk_flags.append(flag)
                score -= weight
        # Only report a score when GoPlus actually returned data for the token.
        if entry:
            r.external_risk_score = max(0.0, score)
        return r


def _f_max(a: Optional[float], b: Optional[float]) -> Optional[float]:
    vals = [x for x in (a, b) if x is not None]
    return max(vals) if vals else None
