"""Unified, chain-agnostic data model for the analytics platform.

Everything downstream (metrics, backtest, screener) speaks these types so the
same pipeline runs across Robinhood, Solana, Ethereum and Base. Chain-specific
detail is confined to the adapters in ``chains/``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Chain(str, Enum):
    ROBINHOOD = "robinhood"
    SOLANA = "solana"
    ETHEREUM = "ethereum"
    BASE = "base"

    @property
    def is_evm(self) -> bool:
        return self in (Chain.ROBINHOOD, Chain.ETHEREUM, Chain.BASE)


class Outcome(str, Enum):
    """Realised fate of a token, assigned by the backtest labeler."""
    WINNER = "winner"       # peak >= win_multiple from first-seen
    NEUTRAL = "neutral"     # went nowhere
    RUG = "rug"             # liquidity pulled / price → ~0
    PENDING = "pending"     # not enough time elapsed to judge yet


@dataclass
class TokenSnapshot:
    """A token's state at ONE point in time (one row in the time-series)."""
    chain: Chain
    token_address: str
    pair_address: str = ""
    symbol: str = ""
    name: str = ""
    ts: float = 0.0                       # unix seconds this snapshot was taken

    # Market
    price_usd: Optional[float] = None
    market_cap_usd: Optional[float] = None
    liquidity_usd: Optional[float] = None
    fdv_usd: Optional[float] = None

    # Age / lifecycle
    pair_created_at: Optional[float] = None
    age_minutes: Optional[float] = None
    on_curve: bool = False                # pre-graduation (pump.fun / flap)
    curve_progress_pct: Optional[float] = None

    # Volume / txns across windows (5m/1h/6h/24h)
    volume_5m: Optional[float] = None
    volume_1h: Optional[float] = None
    volume_24h: Optional[float] = None
    buys_5m: Optional[int] = None
    sells_5m: Optional[int] = None
    buys_1h: Optional[int] = None
    sells_1h: Optional[int] = None
    price_change_5m: Optional[float] = None
    price_change_1h: Optional[float] = None
    price_change_24h: Optional[float] = None

    # Distribution
    holder_count: Optional[int] = None
    top10_supply_pct: Optional[float] = None
    top1_supply_pct: Optional[float] = None
    dev_holdings_pct: Optional[float] = None
    bundle_supply_pct: Optional[float] = None
    sniper_cluster_pct: Optional[float] = None

    # Safety (chain adapter fills what it can; None = unknown)
    is_honeypot: Optional[bool] = None
    buy_tax_pct: Optional[float] = None
    sell_tax_pct: Optional[float] = None
    lp_burned_or_locked: Optional[bool] = None
    mint_authority_revoked: Optional[bool] = None    # SPL / mintable
    external_risk_score: Optional[float] = None      # GoPlus / RugCheck 0-100

    # Discovery / social / alpha
    launchpad: str = ""                   # "flap" | "pumpfun" | "" …
    socials: dict = field(default_factory=dict)
    smart_money_wallets: list = field(default_factory=list)
    # Sum of the reputation qualities of the smart wallets among this token's
    # buyers (0..N). Quality-weights the flat smart_money_count so proven sharps
    # count for more than unproven harvests. Populated at annotate.
    smart_money_quality: Optional[float] = None
    dex_boosted: bool = False

    # Social sentiment (LunarCrush; None = not covered / not fetched)
    social_volume: Optional[float] = None      # interactions / mentions
    social_sentiment: Optional[float] = None   # 0-100 (higher = more bullish)
    social_score: Optional[float] = None       # LunarCrush galaxy-style 0-100


@dataclass
class TokenTimeSeries:
    """All snapshots for one token, plus its first-seen anchor + outcome."""
    chain: Chain
    token_address: str
    first_seen_ts: float
    entry_price: Optional[float] = None   # price at first snapshot
    snapshots: list = field(default_factory=list)     # list[TokenSnapshot], time-ordered
    peak_multiple: float = 1.0
    trough_multiple: float = 1.0
    outcome: Outcome = Outcome.PENDING


@dataclass
class FeatureVector:
    """Numeric features extracted at (or near) discovery — the backtest inputs.

    Deliberately chain-agnostic and normalised so a signature learned on one
    chain's winners is at least comparable on another.
    """
    chain: Chain
    token_address: str
    features: dict = field(default_factory=dict)      # name -> float
    label: Outcome = Outcome.PENDING
    peak_multiple: float = 1.0

    def get(self, name: str, default: float = 0.0) -> float:
        v = self.features.get(name)
        return default if v is None else float(v)


@dataclass
class Signature:
    """A derived 'winner DNA' — how to score a live token's pump likelihood.

    Two supported forms (an implementation may use either or both):
      * rules:  list of (feature, op, value) hard/soft gates with weights
      * model:  serialised weights of a fitted classifier (e.g. logistic)
    Carries its own measured quality so screening can gate on confidence.
    """
    chains: list = field(default_factory=list)        # chains it was trained on
    rules: list = field(default_factory=list)         # [{feature, op, value, weight}]
    model: dict = field(default_factory=dict)         # {feature: weight, "_bias": b}
    win_multiple: float = 3.0                         # what "winner" meant in training
    trained_on: int = 0                               # sample count
    precision: Optional[float] = None                 # out-of-sample
    recall: Optional[float] = None
    created_ts: float = 0.0
    notes: str = ""


@dataclass
class Screen:
    """A live token scored against a Signature — the platform's output row."""
    snapshot: TokenSnapshot
    score: float                          # 0-100 pump-likelihood
    matched_rules: list = field(default_factory=list)
    reasons: list = field(default_factory=list)


# --- Signature serialization (persisted as JSON in the store) ---------------

def signature_to_json(sig: Signature) -> str:
    import json
    from dataclasses import asdict
    d = asdict(sig)
    d["chains"] = [c.value if isinstance(c, Chain) else c for c in sig.chains]
    return json.dumps(d)


def signature_from_json(raw: str) -> Signature:
    import json
    from dataclasses import fields as _fields
    d = json.loads(raw)
    d["chains"] = [Chain(c) for c in d.get("chains", [])]
    valid = {f.name for f in _fields(Signature)}
    return Signature(**{k: v for k, v in d.items() if k in valid})
