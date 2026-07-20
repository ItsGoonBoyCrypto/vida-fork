"""CompositeSafetySource — merge every safety signal conservatively.

Three independent providers, most to least coverage:
  * GoPlus token-security API (RugCheck-equivalent) — rich, but may not cover
    a brand-new L2.
  * On-chain EVM client — verification, authorities, LP-burn, holders.
  * Honeypot simulator — sell-simulation + taxes.

The merge is deliberately pessimistic: a token is only credited as safe on a
fact when a source *confirms* it, and any source flagging danger wins. This
keeps the "confluence > single metric, safety first" philosophy: unknowns stay
unknown (=> gated), and a single credible red flag sinks the token.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import aiohttp

from ..config import Config
from ..models import SafetyReport, TokenSnapshot
from ..simulator import HoneypotSimulator
from .chain import EvmChainClient
from .goplus import GoPlusClient

log = logging.getLogger("rhl2.safety")


def _and_safe(*vals: Optional[bool]) -> Optional[bool]:
    """Safe-when-True fields: any False -> False; else any True -> True; else None."""
    if any(v is False for v in vals):
        return False
    if any(v is True for v in vals):
        return True
    return None


def _or_danger(*vals: Optional[bool]) -> Optional[bool]:
    """Danger-when-True fields (honeypot): any True -> True; else any False -> False; else None."""
    if any(v is True for v in vals):
        return True
    if any(v is False for v in vals):
        return False
    return None


def _max_opt(*vals: Optional[float]) -> Optional[float]:
    present = [v for v in vals if v is not None]
    return max(present) if present else None


def _min_opt(*vals: Optional[float]) -> Optional[float]:
    present = [v for v in vals if v is not None]
    return min(present) if present else None


def merge_reports(*reports: SafetyReport) -> SafetyReport:
    """Conservatively combine multiple SafetyReports into one."""
    out = SafetyReport()

    # Safe-when-True facts: require confirmation, danger wins.
    out.contract_verified = _and_safe(*[r.contract_verified for r in reports])
    out.mint_authority_revoked = _and_safe(*[r.mint_authority_revoked for r in reports])
    out.freeze_authority_revoked = _and_safe(*[r.freeze_authority_revoked for r in reports])
    out.lp_burned = _and_safe(*[r.lp_burned for r in reports])
    out.lp_locked = _and_safe(*[r.lp_locked for r in reports])
    out.lp_lock_seconds = _max_opt(*[r.lp_lock_seconds for r in reports])

    # Danger-when-True facts.
    out.is_honeypot = _or_danger(*[r.is_honeypot for r in reports])
    out.dev_recent_sell = _or_danger(*[r.dev_recent_sell for r in reports])
    out.owner_can_rug = _or_danger(*[r.owner_can_rug for r in reports])
    out.owner_active = _or_danger(*[r.owner_active for r in reports])

    # Union of owner-capability hooks (for display).
    hooks: list[str] = []
    for r in reports:
        for h in r.owner_hooks:
            if h not in hooks:
                hooks.append(h)
    out.owner_hooks = hooks

    # Worst-case numeric facts.
    out.buy_tax_pct = _max_opt(*[r.buy_tax_pct for r in reports])
    out.sell_tax_pct = _max_opt(*[r.sell_tax_pct for r in reports])
    out.dev_holdings_pct = _max_opt(*[r.dev_holdings_pct for r in reports])
    out.bundle_supply_pct = _max_opt(*[r.bundle_supply_pct for r in reports])
    out.sniper_cluster_pct = _max_opt(*[r.sniper_cluster_pct for r in reports])
    out.external_risk_score = _min_opt(*[r.external_risk_score for r in reports])

    # Union of all flags.
    flags: list[str] = []
    for r in reports:
        for f in r.high_risk_flags:
            if f not in flags:
                flags.append(f)
    out.high_risk_flags = flags
    return out


class CompositeSafetySource:
    def __init__(self, cfg: Config, session: Optional[aiohttp.ClientSession] = None):
        self.cfg = cfg
        self._session = session
        self._owns_session = session is None

    async def __aenter__(self) -> "CompositeSafetySource":
        if self._session is None:
            timeout = aiohttp.ClientTimeout(total=self.cfg.runtime.request_timeout_seconds)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self

    async def __aexit__(self, *exc) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()

    async def assess(self, snap: TokenSnapshot) -> SafetyReport:
        goplus = GoPlusClient(self.cfg, session=self._session)
        chain = EvmChainClient(self.cfg, session=self._session)
        sim = HoneypotSimulator(self.cfg, session=self._session)

        async def _safe(coro, label):
            try:
                return await coro
            except Exception as exc:
                log.debug("safety source %s failed: %s", label, exc)
                return SafetyReport()

        gp, oc, hp = await asyncio.gather(
            _safe(goplus.assess(snap), "goplus"),
            _safe(chain.assess(snap), "onchain"),
            _safe(sim.check(snap), "simulator"),
        )
        return merge_reports(gp, oc, hp)
