"""Configuration: chain params, thresholds, weights, risk tiers.

Loaded from a YAML file (see ``config/config.example.yaml``) with
environment-variable overrides for secrets. Everything the operator would
reasonably want to tune lives here so the code stays generic across RH L2,
Base, and other EVM L2s.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from typing import Any, Optional

try:
    import yaml
except ImportError:  # pragma: no cover - yaml is a listed dependency
    yaml = None

from .models import RiskTier


# ---------------------------------------------------------------------------
# Chain / data-source connection
# ---------------------------------------------------------------------------
@dataclass
class ChainConfig:
    """EVM L2 chain parameters.

    Defaults are placeholders for Robinhood Chain (an Arbitrum-Orbit EVM L2
    announced 2025). Confirm the live chain id / RPC / explorer / DexScreener
    slug before production use — set them here or via env.

    To retarget at Base instead, drop in Base's values (chain_id 8453,
    dexscreener_chain "base"); no code changes required.
    """

    name: str = "robinhood-l2"
    chain_id: int = 0                                  # TODO: set real RH L2 chain id
    rpc_url: str = ""                                  # e.g. https://rpc.<rhl2>...
    explorer_api_url: str = ""                         # Etherscan-style API base
    explorer_api_key: str = ""
    # DexScreener chain slug. RH L2 slug TBD; "base" works as an adaptable example.
    dexscreener_chain: str = "robinhood"
    native_symbol: str = "ETH"
    weth_address: str = ""                             # wrapped-native for pair/router math
    # Addresses excluded from holder-distribution math (LP pools, burn, locker)
    excluded_holder_addresses: list[str] = field(default_factory=list)

    # --- New-pool factory listener (earliest discovery) ---
    # UniswapV2-style factory emitting PairCreated, and/or a V3 factory emitting
    # PoolCreated. Set the RH L2 DEX's factory address to catch pairs the moment
    # liquidity is added, before DexScreener indexes them.
    dex_factory_address: str = ""
    dex_factory_kind: str = "univ2"                    # "univ2" | "univ3"
    pool_scan_block_lookback: int = 3000               # blocks to backfill on first poll
    pool_scan_max_range: int = 5000                    # cap per eth_getLogs call

    # --- Swap simulation (honeypot / tax) ---
    dex_router_address: str = ""                       # UniV2-style router for sell sim
    # Optional buy+sell simulator contract for EXACT tax %. Supply the *runtime*
    # bytecode (solc --bin-runtime) of contracts/HoneypotSimulator.sol; it is
    # injected at honeypot_simulator_address via eth_call stateOverride `code`,
    # so nothing is actually deployed on-chain. Empty => fall back to the
    # boolean sell-simulation.
    honeypot_simulator_bytecode: str = ""
    honeypot_simulator_address: str = "0x00000000000000000000000000000000515a1000"
    honeypot_sim_amount_wei: int = 10 ** 16            # 0.01 native token test buy

    # --- LP lock detection ---
    # Simple form: addresses that, if they hold the LP, count the LP as locked.
    lp_locker_addresses: list[str] = field(default_factory=list)
    # Rich form for reading remaining lock DURATION. Each entry:
    #   {address, unlock_selector?, arg?}  where arg is "lp" (pass pair address)
    #   or "none". unlock_selector is the 4-byte hex of a getter returning a
    #   unix unlock timestamp (uint). See sources/lplock.py for known lockers.
    lp_lockers: list = field(default_factory=list)
    lp_lock_min_fraction: float = 0.5                  # LP fraction at a locker => "locked"

    # --- Third-party safety API (RugCheck-equivalent for EVM) ---
    # GoPlus Security token-security API is the de-facto EVM analog to RugCheck.
    # It keys chains by decimal chain id as a string (e.g. "8453" for Base).
    goplus_chain_id: str = ""
    goplus_api_url: str = "https://api.gopluslabs.io/api/v1/token_security"
    # honeypot.is-compatible endpoint (optional cross-check / RH L2 fallback).
    honeypot_api_url: str = ""


# ---------------------------------------------------------------------------
# Composite scoring weights (must sum to ~1.0)
# ---------------------------------------------------------------------------
@dataclass
class Weights:
    safety: float = 0.38
    distribution: float = 0.20
    momentum: float = 0.25
    discovery: float = 0.17

    def normalized(self) -> "Weights":
        total = self.safety + self.distribution + self.momentum + self.discovery
        if total <= 0:
            raise ValueError("scoring weights sum to zero")
        return Weights(
            safety=self.safety / total,
            distribution=self.distribution / total,
            momentum=self.momentum / total,
            discovery=self.discovery / total,
        )


# ---------------------------------------------------------------------------
# Thresholds — one set per risk tier
# ---------------------------------------------------------------------------
@dataclass
class Thresholds:
    """Filter/scoring thresholds. Tune per tier via config.

    ``skip_*`` values are used by the safety gatekeepers (hard filters);
    the rest feed the graded category scorers.
    """

    # Age window (minutes)
    max_age_minutes: float = 24 * 60
    min_age_minutes: float = 0.0

    # Liquidity (USD)
    min_liquidity_usd: float = 15_000
    thin_liquidity_usd: float = 5_000          # below this = hard skip

    # Market cap sweet spot (USD)
    min_market_cap_usd: float = 30_000
    max_market_cap_usd: float = 2_000_000
    ideal_mcap_low: float = 50_000
    ideal_mcap_high: float = 500_000

    # Distribution
    max_top10_pct: float = 25.0                # hard-ish ceiling
    skip_top10_pct: float = 35.0               # above this = skip
    max_top1_pct: float = 15.0
    min_holders: int = 100
    ideal_holders: int = 300

    # Bundle / sniper clustering (% of supply)
    max_bundle_pct: float = 20.0
    skip_bundle_pct: float = 35.0
    max_sniper_cluster_pct: float = 25.0

    # Safety
    max_dev_holdings_pct: float = 5.0
    max_tax_pct: float = 10.0                  # buy or sell tax ceiling
    min_external_risk_score: float = 60.0      # RugCheck-style 0-100
    min_lp_lock_seconds: int = 30 * 24 * 3600  # 30 days considered "meaningful"

    # Momentum
    min_volume_24h_usd: float = 10_000
    min_buy_ratio_1h: float = 0.60             # 60% buys by count
    strong_buy_ratio_1h: float = 0.70

    # Smart money
    smart_money_min_wallets: int = 2

    # Alert bands (composite 0-100)
    strong_alert_score: float = 75.0
    watch_alert_score: float = 60.0


@dataclass
class TierConfig:
    tier: RiskTier
    thresholds: Thresholds = field(default_factory=Thresholds)


def _sniper_defaults() -> TierConfig:
    t = Thresholds(
        max_age_minutes=120,
        min_liquidity_usd=8_000,
        thin_liquidity_usd=4_000,
        min_holders=40,
        ideal_holders=150,
        max_top10_pct=30.0,
        skip_top10_pct=40.0,
        min_volume_24h_usd=3_000,
        min_buy_ratio_1h=0.55,
        strong_alert_score=72.0,
        watch_alert_score=58.0,
    )
    return TierConfig(tier=RiskTier.SNIPER, thresholds=t)


def _momentum_defaults() -> TierConfig:
    return TierConfig(tier=RiskTier.MOMENTUM, thresholds=Thresholds())


# ---------------------------------------------------------------------------
# Telegram / runtime
# ---------------------------------------------------------------------------
@dataclass
class TelegramConfig:
    bot_token: str = ""
    alert_chat_id: str = ""
    # Restrict interactive commands to these user ids (empty = allow all).
    admin_user_ids: list[int] = field(default_factory=list)


@dataclass
class RuntimeConfig:
    poll_interval_seconds: float = 20.0
    request_timeout_seconds: float = 15.0
    max_concurrent_enrichments: int = 8
    db_path: str = "scanner.db"
    dry_run: bool = False                      # log alerts instead of sending
    log_level: str = "INFO"
    # Re-alert suppression: don't re-alert the same token within this window.
    realert_cooldown_seconds: float = 6 * 3600

    # --- Paper trading / calibration ---
    paper_mode: bool = False                   # record would-be entries + realized outcomes
    # Record any safety-passing candidate at/above this composite (below the
    # watch band too) so calibration can see where the alert band *should* be.
    paper_record_floor: float = 45.0
    # Hours after entry to snapshot realized price. Last value = when settled.
    paper_checkpoint_hours: list = field(default_factory=lambda: [1, 6, 24])
    paper_settle_batch: int = 25               # max open trades to re-price per cycle


@dataclass
class Config:
    chain: ChainConfig = field(default_factory=ChainConfig)
    weights: Weights = field(default_factory=Weights)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    active_tier: RiskTier = RiskTier.MOMENTUM
    tiers: dict[RiskTier, TierConfig] = field(
        default_factory=lambda: {
            RiskTier.SNIPER: _sniper_defaults(),
            RiskTier.MOMENTUM: _momentum_defaults(),
        }
    )
    smart_money_wallets: list[str] = field(default_factory=list)

    @property
    def thresholds(self) -> Thresholds:
        return self.tiers[self.active_tier].thresholds

    # -- Loading ---------------------------------------------------------

    @classmethod
    def load(cls, path: Optional[str] = None) -> "Config":
        cfg = cls()
        if path and yaml is not None and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
            cfg._apply_dict(data)
        cfg._apply_env()
        # Validate weights early.
        cfg.weights.normalized()
        return cfg

    def _apply_dict(self, data: dict[str, Any]) -> None:
        _fill(self.chain, data.get("chain", {}))
        _fill(self.weights, data.get("weights", {}))
        _fill(self.telegram, data.get("telegram", {}))
        _fill(self.runtime, data.get("runtime", {}))
        if "active_tier" in data:
            self.active_tier = RiskTier(data["active_tier"])
        for tier_name, tvals in (data.get("tiers", {}) or {}).items():
            tier = RiskTier(tier_name)
            tc = self.tiers.setdefault(tier, TierConfig(tier=tier))
            _fill(tc.thresholds, tvals.get("thresholds", tvals))
        wallets = data.get("smart_money_wallets")
        if wallets:
            self.smart_money_wallets = [w.lower() for w in wallets]

    def _apply_env(self) -> None:
        """Secrets and overrides from env take precedence over file.

        A ``.env`` file in the working directory is loaded first (without
        overriding already-set process env), so secrets never need to live in
        the YAML config or the repo.
        """
        _load_dotenv()
        env = os.environ
        if v := env.get("RHL2_RPC_URL"):
            self.chain.rpc_url = v
        if v := env.get("RHL2_EXPLORER_API_KEY"):
            self.chain.explorer_api_key = v
        if v := env.get("RHL2_EXPLORER_API_URL"):
            self.chain.explorer_api_url = v
        if v := env.get("RHL2_SIMULATOR_BYTECODE"):
            self.chain.honeypot_simulator_bytecode = v
        if v := env.get("TELEGRAM_BOT_TOKEN"):
            self.telegram.bot_token = v
        if v := env.get("TELEGRAM_ALERT_CHAT_ID"):
            self.telegram.alert_chat_id = v
        if v := env.get("RHL2_DRY_RUN"):
            self.runtime.dry_run = v.lower() in ("1", "true", "yes")
        if v := env.get("RHL2_DB_DIR"):
            self.runtime.db_path = os.path.join(v, os.path.basename(self.runtime.db_path))


def _load_dotenv(path: str = ".env") -> None:
    """Minimal, dependency-free .env loader (KEY=VALUE lines; # comments)."""
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key, value = key.strip(), value.strip().strip('"').strip("'")
                os.environ.setdefault(key, value)
    except OSError:
        pass


def _fill(obj: Any, data: dict[str, Any]) -> None:
    """Assign known dataclass fields from a dict, ignoring extras."""
    if not data:
        return
    valid = {f.name for f in fields(obj)}
    for key, value in data.items():
        if key in valid:
            setattr(obj, key, value)
