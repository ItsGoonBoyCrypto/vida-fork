"""Domain models shared across the scanner.

These are plain dataclasses that decouple the rest of the pipeline from
the shape of any single data provider (DexScreener, explorer, RugCheck).
Each data source is responsible for populating a ``TokenSnapshot`` (and
optionally a ``SafetyReport``); the scorer/filters/formatter only ever
touch these models.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class RiskTier(str, Enum):
    """Configurable risk profile selected per run / per TG command."""

    SNIPER = "sniper"        # <1-2h, higher risk tolerance
    MOMENTUM = "momentum"    # 2-24h, stricter filters


class AlertLevel(str, Enum):
    STRONG = "strong"        # score >= strong threshold + safety passes
    WATCH = "watch"          # moderate score band
    SKIP = "skip"            # below floor or safety fail


@dataclass
class SafetyReport:
    """On-chain / tooling safety facts for a token.

    ``None`` means "unknown / not yet checked" — treated as a *risk* by the
    gatekeepers, never as a pass. Only explicit ``True``/``False`` are trusted.
    """

    contract_verified: Optional[bool] = None
    mint_authority_revoked: Optional[bool] = None
    freeze_authority_revoked: Optional[bool] = None
    lp_locked: Optional[bool] = None
    lp_burned: Optional[bool] = None
    lp_lock_seconds: Optional[int] = None          # remaining lock duration
    buy_tax_pct: Optional[float] = None
    sell_tax_pct: Optional[float] = None
    is_honeypot: Optional[bool] = None
    dev_holdings_pct: Optional[float] = None        # creator wallet % of supply
    dev_recent_sell: Optional[bool] = None
    external_risk_score: Optional[float] = None     # 0-100 from RugCheck-style tool
    high_risk_flags: list[str] = field(default_factory=list)
    # Bundle / clustered-wallet detection
    bundle_supply_pct: Optional[float] = None       # % of supply in clustered wallets
    sniper_cluster_pct: Optional[float] = None      # % bought in same/near launch blocks

    @property
    def lp_safe(self) -> bool:
        return bool(self.lp_burned) or bool(self.lp_locked)


@dataclass
class TokenSnapshot:
    """Everything known about a token/pair at one point in time."""

    # Identity
    chain: str
    pair_address: str
    token_address: str
    symbol: str = ""
    name: str = ""

    # Market
    price_usd: Optional[float] = None
    price_native: Optional[float] = None      # price in the chain's native token (ETH on RH)
    market_cap_usd: Optional[float] = None
    fdv_usd: Optional[float] = None
    liquidity_usd: Optional[float] = None

    # Age
    pair_created_at: Optional[float] = None          # unix seconds
    age_minutes: Optional[float] = None

    # Volume (USD) across windows
    volume_5m: Optional[float] = None
    volume_1h: Optional[float] = None
    volume_6h: Optional[float] = None
    volume_24h: Optional[float] = None

    # Transaction counts across windows (buys / sells)
    buys_5m: Optional[int] = None
    sells_5m: Optional[int] = None
    buys_1h: Optional[int] = None
    sells_1h: Optional[int] = None
    buys_24h: Optional[int] = None
    sells_24h: Optional[int] = None

    # Price change %
    price_change_5m: Optional[float] = None
    price_change_1h: Optional[float] = None
    price_change_24h: Optional[float] = None

    # Distribution
    holder_count: Optional[int] = None
    top10_supply_pct: Optional[float] = None
    top1_supply_pct: Optional[float] = None
    holder_growth_1h: Optional[int] = None           # net new holders last hour

    # Discovery / hype
    dex_boosted: bool = False
    dex_trending: bool = False
    socials: dict[str, str] = field(default_factory=dict)   # {"telegram": url, ...}
    smart_money_wallets: list[str] = field(default_factory=list)
    smart_money_labels: dict[str, str] = field(default_factory=dict)  # wallet -> label/group
    # Wallet-reputation signals (populated from the reputation ledger at enrich).
    smart_money_quality_bonus: Optional[float] = None  # quality-weighted discovery pts
    core_alpha_wallets: list[str] = field(default_factory=list)  # proven sharps among buyers
    toxic_buyer: bool = False       # a known rug/dumper wallet is among the buyers
    curve_matched: bool = False     # matched the learned pre-migration winning setup this cycle
    launchpad: str = ""             # origin launchpad name if known (e.g. "flap")
    curve_progress_pct: Optional[float] = None   # bonding-curve fill % (pre-graduation)

    # Links
    dexscreener_url: str = ""
    chart_url: str = ""
    explorer_url: str = ""      # Blockscout token page (holders/transfers/contract)
    trade_url: str = ""         # launchpad trade page (flap/bags) — works pre-graduation

    # Nested safety facts
    safety: SafetyReport = field(default_factory=SafetyReport)

    # --- Derived convenience metrics -------------------------------------

    @property
    def buy_ratio_1h(self) -> Optional[float]:
        """Fraction of transactions in the last hour that were buys (0-1)."""
        if self.buys_1h is None or self.sells_1h is None:
            return None
        total = self.buys_1h + self.sells_1h
        return (self.buys_1h / total) if total else None

    @property
    def vol_to_mcap_24h(self) -> Optional[float]:
        if not self.volume_24h or not self.market_cap_usd:
            return None
        return self.volume_24h / self.market_cap_usd

    @property
    def volume_accelerating(self) -> Optional[bool]:
        """True if hourly-rate volume is rising vs the 24h average hourly rate.

        Compares the last hour's volume against the mean hourly volume over
        the trailing 24h. A rising short-window rate is the momentum signal.
        """
        if self.volume_1h is None or self.volume_24h is None:
            return None
        avg_hourly = self.volume_24h / 24.0
        if avg_hourly <= 0:
            return self.volume_1h > 0
        return self.volume_1h > avg_hourly

    @property
    def buy_ratio_5m(self) -> Optional[float]:
        """Fraction of last-5-min transactions that were buys (0-1).

        The freshest read on buy pressure — for a token minutes old this
        reacts long before the 1h bucket has meaningful data.
        """
        if self.buys_5m is None or self.sells_5m is None:
            return None
        total = self.buys_5m + self.sells_5m
        return (self.buys_5m / total) if total else None

    @property
    def volume_velocity(self) -> Optional[float]:
        """Ratio of the last-5-min hourly-equivalent rate to the trailing 1h.

        ``volume_5m * 12`` projects the last 5 minutes to an hourly rate; we
        divide by ``volume_1h`` to see whether *right now* is running hotter
        than the trailing hour. >1 = accelerating, <1 = cooling. This is the
        earliest momentum tell — it moves within minutes of a launch, well
        before the 1h-vs-24h signal has any 24h history to compare against.
        """
        if self.volume_5m is None or not self.volume_1h:
            return None
        return (self.volume_5m * 12.0) / self.volume_1h

    @property
    def short_term_accelerating(self) -> Optional[bool]:
        """True when the 5-min rate is meaningfully above the 1h rate."""
        v = self.volume_velocity
        if v is None:
            return None
        return v >= 1.2

    @property
    def price_velocity(self) -> Optional[float]:
        """Last-5-min hourly-equivalent price move vs the trailing 1h move.

        Signed ratio: >1 means the last 5 minutes are climbing faster than
        the prior hour (a fresh leg up), negative means a reversal.
        """
        if self.price_change_5m is None or not self.price_change_1h:
            return None
        return (self.price_change_5m * 12.0) / self.price_change_1h


@dataclass
class CategoryScore:
    name: str
    raw: float          # 0-100 within the category
    weight: float       # 0-1 contribution of category to composite
    reasons: list[str] = field(default_factory=list)
    penalties: list[str] = field(default_factory=list)

    @property
    def weighted(self) -> float:
        return self.raw * self.weight


@dataclass
class ScoreResult:
    composite: float                       # 0-100
    level: AlertLevel
    categories: list[CategoryScore]
    safety_passed: bool
    gate_failures: list[str] = field(default_factory=list)

    def category(self, name: str) -> Optional[CategoryScore]:
        return next((c for c in self.categories if c.name == name), None)

    @property
    def reasons(self) -> list[str]:
        out: list[str] = []
        for c in self.categories:
            out.extend(c.reasons)
        return out
