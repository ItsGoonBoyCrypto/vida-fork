"""Abstract data-source interfaces.

Splitting sources by role keeps the scanner independent of any single
vendor. Implementations live alongside this file; the scanner wires them
together via these protocols.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..models import SafetyReport, TokenSnapshot


@runtime_checkable
class PairSource(Protocol):
    """Discovers new pairs and fills market/volume/tx snapshot fields."""

    async def fetch_new_pairs(self) -> list[TokenSnapshot]:
        """Return recently-created pairs on the configured chain."""
        ...

    async def refresh(self, snap: TokenSnapshot) -> TokenSnapshot:
        """Re-poll a single pair to update volume/price/tx counts."""
        ...


@runtime_checkable
class SafetySource(Protocol):
    """Populates on-chain / tooling safety facts (authorities, LP, taxes)."""

    async def assess(self, snap: TokenSnapshot) -> SafetyReport:
        ...


@runtime_checkable
class DistributionSource(Protocol):
    """Populates holder count / top-holder concentration / bundle metrics."""

    async def enrich_distribution(self, snap: TokenSnapshot) -> TokenSnapshot:
        ...


@runtime_checkable
class SmartMoneySource(Protocol):
    """Returns which curated smart-money wallets are active in a token."""

    async def active_wallets(self, snap: TokenSnapshot) -> list[str]:
        ...
