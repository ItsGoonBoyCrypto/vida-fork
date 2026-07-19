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

import logging as _logging

_log = _logging.getLogger("rhl2.config")


def _looks_like_address(s: str) -> bool:
    """True if ``s`` is a 0x-prefixed 20-byte hex address.

    Guards env overrides: a label like 'Uniswap V3 Factory' pasted into
    RHL2_DEX_FACTORY must NOT be accepted — the RPC rejects it and it silently
    disables log-based discovery.
    """
    s = (s or "").strip()
    if not s.startswith("0x") or len(s) != 42:
        return False
    try:
        int(s, 16)
        return True
    except ValueError:
        return False


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

    # Defaults are the REAL Robinhood Chain mainnet values (live since 2026-07-01,
    # Arbitrum-Orbit EVM L2, chain id 4663). DexScreener indexes it as "robinhood".
    name: str = "robinhood-l2"
    chain_id: int = 4663
    rpc_url: str = "https://rpc.mainnet.chain.robinhood.com"
    # Robinhood Chain's explorer is Blockscout (Etherscan-compatible /api).
    explorer_api_url: str = "https://robinhoodchain.blockscout.com/api"
    explorer_api_key: str = ""                         # Blockscout works without a key
    dexscreener_chain: str = "robinhood"               # confirmed live slug
    native_symbol: str = "ETH"
    # RH Chain mixes tokenized STOCKS (MU/TSLA/SPCX/… named "• Robinhood Token")
    # with memecoins. Skip the equities — they're not gems.
    exclude_stock_tokens: bool = True
    # Scam-impersonator symbols to drop from ALL alerts, matched on symbol/name
    # (case-insensitive, non-alphanumerics ignored). RH Chain is flooded with
    # fake "$ROBINHOOD" tokens. Add more with /block <SYMBOL> at runtime or via
    # RHL2_BLOCKED_SYMBOLS=ROBINHOOD,FOO. Compared after stripping a leading $.
    blocked_symbols: list[str] = field(default_factory=lambda: ["ROBINHOOD"])
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
    # Non-vanity flap detection: confirm this many non-suffix candidates per cycle
    # via Portal.getTokenV2 (catches flap tokens without an 8888/7777 address).
    # 0 disables (suffix-only). Each address is cached so it's checked once.
    curve_confirm_budget: int = 10

    # --- Swap simulation (honeypot / tax) ---
    dex_router_address: str = ""                       # router for the sell simulation
    dex_router_kind: str = "univ2"                     # "univ2" | "univ3" (SwapRouter02)
    dex_v3_fee_tiers: list = field(default_factory=lambda: [10000, 3000, 500])  # V3 pool fees to try
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

    # --- V3 launchpad LP-lock detection (getLaunchedToken pattern) ---
    # Launchers exposing getLaunchedToken(token) -> (..., positionManager,
    # positionId, deployer, ...) let us check ownerOf(positionId): burned or
    # protocol-held => safe; deployer-held => removable (rug risk). Launchpads
    # churn on RH Chain (NOXA rugged), so this is a LIST — add each launchpad's
    # factory as they emerge; each is tried until one reports the token. LP-lock
    # simply stays "unknown" (tolerated) for tokens from unlisted launchpads;
    # the honeypot sell-sim + top10 + dev checks carry rug protection regardless.
    launchpad_factory_addresses: list = field(
        default_factory=lambda: ["0xD9eC2db5f3D1b236843925949fe5bd8a3836FCcB"]  # NOXA (may be dead)
    )
    launchpad_factory_address: str = ""       # legacy single (still honored if set)
    nft_position_manager: str = "0x73991a25C818Bf1f1128dEAaB1492D45638DE0D3"        # Uniswap V3 NPM

    # --- Bonding-curve launchpads (flap.sh etc.) ---
    # Unlike NOXA (single-sided V3 at block 1), flap.sh launches tokens onto a
    # constant-product BONDING CURVE. A token trades on the curve until ~80% of
    # supply is bought, then "graduates" — liquidity is moved to a DEX pool.
    # That's why flap tokens only appear on DexScreener AFTER graduation (late).
    # To catch them EARLY we watch the flap manager contract's logs directly.
    #
    # Each entry: {name, manager, kind, create_topic?, graduate_topic?, boost?}
    #   manager       — the curve-manager/factory contract that emits creations
    #   kind          — "curve" (bonding curve) | "v3" (single-sided, uses the
    #                   getLaunchedToken list above instead)
    #   create_topic  — topic0 of the token-creation event (leave "" to run the
    #                   listener in DISCOVERY mode: it logs every distinct topic0
    #                   the manager emits so the real signature can be confirmed
    #                   from the running host's logs, then set here)
    #   graduate_topic— topic0 of the graduation/DEX-listing event (optional)
    #   boost         — discovery-score bump for tokens from this launchpad
    # Address left blank by default: set RHL2_FLAP_MANAGER on the host (grab it
    # from the flap docs "Deployed Contracts" page) to light it up.
    #   confirm_fn    — a manager read fn (e.g. "getTokenV2(address)") that returns
    #                   a non-zero first word for one of ITS tokens; lets the
    #                   listener confirm non-vanity candidates without a topic/suffix
    # flap is fully wired (confirm_fn set). RobinFun/Bags are here so they can be
    # lit up with just a host Variable (RHL2_ROBINFUN_MANAGER / RHL2_BAGS_MANAGER)
    # once you've verified the address on Blockscout; they run in discovery mode
    # (log event shapes) until a create_topic/confirm_fn is pinned. Graduated
    # tokens from ALL launchpads are already caught via DexScreener regardless —
    # these entries add the pre-graduation edge + origin labeling.
    launchpads: list = field(default_factory=lambda: [
        {"name": "flap", "manager": "", "kind": "curve",
         "create_topic": "", "graduate_topic": "", "boost": 15,
         "confirm_fn": "getTokenV2(address)"},
        {"name": "robinfun", "manager": "", "kind": "curve",
         "create_topic": "", "graduate_topic": "", "boost": 12, "confirm_fn": ""},
        {"name": "bags", "manager": "", "kind": "curve",
         "create_topic": "", "graduate_topic": "", "boost": 12,
         "confirm_fn": "getTokenState(address)", "factory": ""},
    ])

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
    # The sniper-cluster % is only meaningful once a token has aged PAST its
    # launch window — on a token that's minutes old, essentially everyone bought
    # "at launch", so the metric pins near 100% and would wrongly skip every fresh
    # gem. Only enforce the ceiling once the token is at least this old.
    sniper_cluster_min_age_minutes: float = 45.0
    # Safety-score floor for a token that PASSED the gate (no confirmable danger).
    # On data-poor chains (RH Chain: no verification, no GoPlus, V3 LP unreadable)
    # every safety-positive signal is UNKNOWN, so score_safety would give 0 —
    # which, at safety's 0.38 weight, caps the composite below the alert band and
    # makes the maturity tier unreachable. A moderate floor says "clean gate pass,
    # extras unconfirmed" so quality tokens can still score into the band. 0 keeps
    # the old behaviour (chains where these signals ARE available).
    safety_baseline: float = 0.0

    # Safety
    max_dev_holdings_pct: float = 5.0
    max_tax_pct: float = 10.0                  # buy or sell tax ceiling
    min_external_risk_score: float = 60.0      # RugCheck-style 0-100
    min_lp_lock_seconds: int = 30 * 24 * 3600  # 30 days considered "meaningful"
    # Require a Blockscout-verified contract to pass the safety gate. OFF on RH
    # Chain: almost nothing is verified there, so requiring it zeroes legit
    # tokens. Verification stays a SCORING bonus (+15); real safety comes from
    # the honeypot sim + distribution + LP checks. Turn on for chains where
    # verification is the norm.
    require_contract_verified: bool = False

    # Momentum
    min_volume_24h_usd: float = 10_000
    min_buy_ratio_1h: float = 0.60             # 60% buys by count
    strong_buy_ratio_1h: float = 0.70
    # Short-term velocity (5m rate vs 1h rate) — the earliest momentum tell.
    # >= this ratio means the last 5 minutes are running hotter than the hour.
    min_volume_velocity: float = 1.2
    strong_volume_velocity: float = 2.0

    # Smart money
    smart_money_min_wallets: int = 2

    # Alert bands (composite 0-100)
    strong_alert_score: float = 75.0
    watch_alert_score: float = 60.0

    # Graduation-imminent boost: a bonding-curve token this far along is at the
    # sweet spot where the run often starts, but has thin DEX momentum data (no
    # pair yet) so it can't otherwise reach the alert band. Add composite points
    # for the sweet spot; half that once it's essentially graduating.
    grad_sweet_low: float = 55.0
    grad_sweet_high: float = 92.0
    grad_boost_points: float = 10.0


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
    # Tiered re-alerting: when a token ESCALATES to a higher tier than it was
    # last alerted at (early 🌱 -> watch 👀 -> strong 🚨), send an "upgrade" ping
    # immediately, bypassing the cooldown. This is the "get in early, then
    # confirm to size up" flow — catch it fresh, re-ping when confluence lands.
    realert_on_escalation: bool = True

    # --- Smart-money auto-seeding ---
    # When a token escalates to a STRONG alert (a confirmed runner), harvest its
    # earliest N distinct buyers into the smart-money set, so future launches
    # those wallets buy score higher. Off by default — turn on once the RH
    # explorer's transfer feed is confirmed working (RHL2_SMART_AUTOSEED=1).
    smart_money_autoseed: bool = False
    smart_money_autoseed_buyers: int = 12      # earliest buyers to harvest per winner
    smart_money_max_set: int = 500             # cap the auto-grown set

    # --- Retroactive winner harvest ---
    # Record every discovered token, then a periodic sweep re-prices those aged
    # winner_harvest_min..max_hours and, for any that ran >= win_mult from our
    # discovery price, harvests its earliest buyers into the smart set. Unlike
    # real-time autoseed this ALSO learns from winners we never alerted on — the
    # 24-36h delay confirms a real runner, not a fakeout. Opt-in: RHL2_WINNER_HARVEST=1
    winner_harvest_enabled: bool = False
    winner_harvest_min_age_hours: float = 24.0
    winner_harvest_max_age_hours: float = 36.0
    winner_harvest_win_mult: float = 3.0       # peak from discovery to count as a winner
    winner_harvest_batch: int = 20             # tokens re-priced per sweep (rate-limit guard)
    winner_harvest_interval_hours: float = 1.0

    # --- Smart-money CLUSTER alert ---
    # When this many DISTINCT smart-money wallets buy the same token within the
    # window, fire a high-priority "cluster" alert — convergence of proven early
    # wallets is the strongest early-runner signal we have. Bypasses the score
    # gate (it IS the signal), but still respects mutes + blocked symbols.
    smart_cluster_enabled: bool = True
    smart_cluster_min_wallets: int = 2
    smart_cluster_window_hours: float = 6.0

    # --- Post-alert position monitoring (protect the position) ---
    # Smart-money EXIT alert: a smart wallet sells a token smart money had bought.
    smart_exit_enabled: bool = True
    # Milestone pings: an alerted token reaching these multiples from entry.
    position_monitor_enabled: bool = True
    milestone_multiples: list = field(default_factory=lambda: [2.0, 5.0, 10.0])
    # Dump/rug guard: warn once when an alerted token that ran up (>= this peak)
    # falls back by this % from its peak.
    dump_min_peak_mult: float = 1.5
    dump_drawdown_pct: float = 55.0

    # --- Early-launch alerts (catch runners pre/just-after graduation) ---
    # Fresh tokens have few holders + concentrated supply + little volume, so
    # they can't reach the maturity-based score. This path alerts on a SAFE,
    # brand-new launch regardless of that score — the "get in early" signal.
    early_launch_enabled: bool = True
    early_launch_max_age_minutes: float = 60      # only tokens this new
    early_launch_min_liquidity_usd: float = 3000  # confirm a real (graduated) pool
    early_launch_require_safety: bool = True       # still require the safety gate to pass
    # Quality gates so the 🌱 early path isn't just "safe + has a pool" (that
    # floods). 0 = disabled; when a metric is known it must clear the bar.
    early_launch_min_holders: int = 0             # require real distribution, not a dead launch
    early_launch_min_buy_ratio_1h: float = 0.0    # require net buying (0-1), when txn data exists
    early_launch_min_volume_1h_usd: float = 0.0   # require some real 1h volume, when known

    # --- Pre-migration curve pattern (learned 'winning setup' matcher) ---
    # Track on-curve flap tokens, learn the profile of ones that graduated AND
    # pumped >= winner_harvest_win_mult, and fire a 🧬 curve-match alert on fresh
    # tokens matching that profile while actively climbing.
    curve_pattern_enabled: bool = True
    curve_match_alert: bool = True                # send 🧬 alerts (off => track/learn only)
    curve_pattern_min_winners: int = 5            # winners needed before the LEARNED profile is used
    curve_obs_interval_seconds: float = 600       # persist an observation at most this often per token
    curve_obs_retention_hours: float = 168        # prune observations older than this (7 days)

    # --- Paper trading / calibration ---
    paper_mode: bool = False                   # record would-be entries + realized outcomes
    # Record any safety-passing candidate at/above this composite (below the
    # watch band too) so calibration can see where the alert band *should* be.
    paper_record_floor: float = 45.0
    # Hours after entry to snapshot realized price. Last value = when settled.
    paper_checkpoint_hours: list = field(default_factory=lambda: [1, 6, 24])
    paper_settle_batch: int = 25               # max open trades to re-price per cycle
    # Calibration digest auto-posted to the alert channel.
    paper_digest_enabled: bool = False
    paper_digest_interval_hours: float = 24.0  # how often to post (e.g. 1 = hourly)
    paper_digest_win_multiple: float = 2.0
    # --- Bounded auto-tune of scoring weights ---
    # Once enough alerts have settled, nudge the category weights toward the
    # signals that actually separated winners from rugs (see autotune.py). Tight
    # leash: dormant until min_settled, small steps, hard-clamped to
    # baseline ± max_drift, and fully reversible (/autotune reset). Opt-in.
    autotune_enabled: bool = False
    autotune_min_settled: int = 30             # dormant until this many alerts have settled
    autotune_min_bucket: int = 5               # min samples in a high/low bucket for its edge to count
    autotune_step: float = 0.02                # max weight move per run (pre-clamp)
    autotune_max_drift: float = 0.08           # a weight can never leave baseline ± this
    autotune_interval_hours: float = 24.0      # how often to re-tune

    # /harvest bundle guard: skip harvesting 'early buyers' from a manually-fed
    # winner whose launch was bundled above this % of supply — those wallets are
    # likely sybils and would poison the smart-money cluster signal.
    harvest_max_bundle_pct: float = 50.0

    # --- Wallet reputation (self-curating smart set) ---
    # A wallet that was an early buyer of >= this many DISTINCT winners is
    # "core alpha" — proven enough to fire a single-wallet alert on its own.
    core_alpha_min_overlap: int = 3
    core_alpha_alert: bool = True          # fire 💎 alerts on a core-alpha buy
    # A wallet in >= this many DISTINCT rugs (and net-negative vs its winners) is
    # toxic: tokens it buys get demoted.
    toxic_min_rugs: int = 2
    # Retroactive winner-harvest also labels each harvested wallet against the
    # winner (overlap) and records the token's realized outcome (forward-pick
    # validation). A token whose peak fell to/below this is counted a rug.
    rug_peak_mult_ceiling: float = 0.5

    # Post a "scanner online" message on startup (also serves as a wiring test).
    send_startup_message: bool = True
    # Live alerts on a chain without a tax oracle (no GoPlus coverage, no DEX
    # router wired): enforce every confirmable safety metric strictly, but allow
    # an UNCONFIRMED honeypot/tax (a CONFIRMED-bad still fails). See safety_gate.
    live_pragmatic_safety: bool = False


@dataclass
class WalletWatchConfig:
    """Whale / smart-money wallet activity alerts."""
    enabled: bool = False
    wallets: list = field(default_factory=list)      # 0x addresses to track
    labels: dict = field(default_factory=dict)       # addr(lower) -> display name
    alert_on: str = "buys"                            # "buys" | "buys_sells"
    min_usd: float = 100.0                            # ignore transfers below this USD
    max_transfers_per_wallet: int = 25               # per poll, per wallet
    # Emit individual "🐋 whale bought X" pings. Off => still POLL these wallets
    # and feed their buys into the smart-money cluster signal + gem scoring, just
    # without the per-buy notifications. (Cluster convergence alerts are separate
    # and stay on — that's the high-signal "increase our chances" part.)
    emit_alerts: bool = True
    # Sybil grouping: map wallet address(lower) -> shared entity name. Wallets in
    # the same group count as ONE distinct buyer for the cluster signal, so a
    # person running two wallets can't fake a 2-wallet convergence. Ungrouped
    # wallets are each their own entity.
    wallet_groups: dict = field(default_factory=dict)


@dataclass
class Config:
    chain: ChainConfig = field(default_factory=ChainConfig)
    weights: Weights = field(default_factory=Weights)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    wallet_watch: WalletWatchConfig = field(default_factory=WalletWatchConfig)
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

    # -- Launchpads ------------------------------------------------------

    def _launchpad(self, name: str) -> Optional[dict]:
        """Return the named launchpad config dict (or None)."""
        for lp in (self.chain.launchpads or []):
            if isinstance(lp, dict) and lp.get("name") == name:
                return lp
        return None

    def configured_launchpads(self) -> list[dict]:
        """Launchpad entries that have a manager address set (usable)."""
        return [lp for lp in (self.chain.launchpads or [])
                if isinstance(lp, dict) and lp.get("manager")]

    def known_launchpad_addresses(self) -> dict[str, str]:
        """Map lowercased launchpad manager address -> launchpad name.

        Used to recognise a token's origin (discovery boost + alert label) and
        to treat a curve-manager-held LP as locked.
        """
        out: dict[str, str] = {}
        for lp in self.configured_launchpads():
            out[str(lp["manager"]).lower()] = lp.get("name", "launchpad")
        return out

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
        ww = data.get("wallet_watch")
        if ww:
            _fill(self.wallet_watch, ww)
            self.wallet_watch.wallets = [w.lower() for w in (self.wallet_watch.wallets or [])]
            self.wallet_watch.labels = {k.lower(): v for k, v in (self.wallet_watch.labels or {}).items()}

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
        # DEX addresses — set as host Variables to enable honeypot sim + pool listener
        # without editing the repo. Grab from robinhoodchain.blockscout.com.
        # Validate: reject a non-address value (e.g. a pasted label) so it can't
        # silently break log-based discovery — keep the file/default instead.
        def _addr_env(name: str, current: str) -> str:
            v = env.get(name)
            if not v:
                return current
            if _looks_like_address(v):
                return v.strip()
            _log.warning("%s=%r is not a 0x address — ignoring, keeping %r",
                         name, v, current)
            return current
        self.chain.weth_address = _addr_env("RHL2_WETH_ADDRESS", self.chain.weth_address)
        self.chain.dex_factory_address = _addr_env("RHL2_DEX_FACTORY", self.chain.dex_factory_address)
        self.chain.dex_router_address = _addr_env("RHL2_DEX_ROUTER", self.chain.dex_router_address)
        if v := env.get("RHL2_DEX_FACTORY_KIND"):
            self.chain.dex_factory_kind = v
        if v := env.get("RHL2_DEX_ROUTER_KIND"):
            self.chain.dex_router_kind = v
        # Bonding-curve launchpads (flap, robinfun, bags, …). Enable each with a
        # host Variable RHL2_<NAME>_MANAGER (e.g. RHL2_FLAP_MANAGER,
        # RHL2_ROBINFUN_MANAGER); the _CREATE_TOPIC / _GRADUATE_TOPIC overrides
        # lock in the event signatures once confirmed from discovery logs.
        for lp in (self.chain.launchpads or []):
            nm = str(lp.get("name", "")).strip().upper()
            if not nm:
                continue
            if v := env.get(f"RHL2_{nm}_MANAGER"):
                if _looks_like_address(v):
                    lp["manager"] = v.strip()
                else:
                    _log.warning("RHL2_%s_MANAGER=%r is not a 0x address — ignoring", nm, v)
            if v := env.get(f"RHL2_{nm}_CREATE_TOPIC"):
                lp["create_topic"] = v.strip()
            if v := env.get(f"RHL2_{nm}_GRADUATE_TOPIC"):
                lp["graduate_topic"] = v.strip()
            if v := env.get(f"RHL2_{nm}_CONFIRM_FN"):
                lp["confirm_fn"] = v.strip()
            if v := env.get(f"RHL2_{nm}_FACTORY"):   # Bags-style registry discovery
                if _looks_like_address(v):
                    lp["factory"] = v.strip()
                else:
                    _log.warning("RHL2_%s_FACTORY=%r is not a 0x address — ignoring", nm, v)
        # RHL2_BAGS_LENS is a clearer alias for the Bags launchpad 'manager'.
        bags = self._launchpad("bags")
        if bags is not None and (v := env.get("RHL2_BAGS_LENS")):
            if _looks_like_address(v):
                bags["manager"] = v.strip()
            else:
                _log.warning("RHL2_BAGS_LENS=%r is not a 0x address — ignoring", v)
        # Whale wallets to watch — comma-separated 0x addresses (easy Railway var).
        if v := env.get("RHL2_WATCH_WALLETS"):
            self.wallet_watch.wallets = [w.strip().lower() for w in v.split(",") if w.strip()]
            self.wallet_watch.enabled = True
        if v := env.get("RHL2_WATCH_MIN_USD"):
            try:
                self.wallet_watch.min_usd = float(v)
            except ValueError:
                pass
        # Toggle the individual whale pings without losing the smart-money data.
        if v := env.get("RHL2_WHALE_ALERTS"):
            self.wallet_watch.emit_alerts = v.lower() in ("1", "true", "yes")
        # Smart-money seed set — comma-separated 0x wallets that bought previous
        # bangers early. A token bought by these scores higher (discovery) and,
        # with autoseed on, a token that graduates to a STRONG alert donates its
        # earliest buyers back into the set (self-improving). Durable on Railway
        # (env survives redeploys; the DB does not).
        if v := env.get("RHL2_SMART_WALLETS"):
            seed = [w.strip().lower() for w in v.split(",") if w.strip()]
            merged = list(dict.fromkeys(self.smart_money_wallets + seed))
            self.smart_money_wallets = merged
        if v := env.get("RHL2_SMART_AUTOSEED"):
            self.runtime.smart_money_autoseed = v.lower() in ("1", "true", "yes")
        if v := env.get("RHL2_WINNER_HARVEST"):
            self.runtime.winner_harvest_enabled = v.lower() in ("1", "true", "yes")
        if v := env.get("RHL2_AUTOTUNE"):
            self.runtime.autotune_enabled = v.lower() in ("1", "true", "yes")
        # Extra scam symbols to block, comma-separated (unioned with the default
        # ROBINHOOD so it's always blocked).
        if v := env.get("RHL2_BLOCKED_SYMBOLS"):
            extra = [s.strip().upper() for s in v.split(",") if s.strip()]
            self.chain.blocked_symbols = list(dict.fromkeys(
                [s.upper() for s in self.chain.blocked_symbols] + extra))
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
