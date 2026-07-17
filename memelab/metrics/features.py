"""Feature extraction — turn a token's early history into the backtest inputs.

These are the candidate "metrics we back-test against past big pumpers." The
backtest engine decides which actually predict pumps and with what thresholds;
this module's job is to compute them consistently for both historical (training)
and live (screening) tokens, so there's no train/serve skew.

A feature is computed from the snapshots within an EARLY WINDOW after first-seen
(e.g. first 30 min) — because the whole point is predicting from what was
knowable EARLY, not with hindsight.
"""

from __future__ import annotations

from ..models import Chain, FeatureVector, TokenTimeSeries

# The candidate feature set. Grouped by hypothesis about what drives a runner.
FEATURE_NAMES = [
    # Liquidity / size at entry
    "entry_liquidity_usd", "entry_market_cap_usd", "liq_to_mcap",
    # Momentum / velocity (the earliest tells)
    "vol5m_to_vol1h", "buy_ratio_5m", "buy_ratio_1h", "price_change_5m",
    "unique_buyer_growth", "vol_to_mcap",
    # Distribution / holder health
    "holder_count", "holder_growth_rate", "top10_pct", "top1_pct",
    "dev_holdings_pct", "bundle_pct", "sniper_pct",
    # Alpha / social
    "smart_money_count", "has_socials", "launchpad_flag", "dex_boosted",
    # Safety (as numeric flags)
    "is_sellable", "authorities_ok", "lp_safe", "risk_score",
    # Shape of the first minutes
    "minutes_to_first_1k_vol", "accel_slope",
]


def extract(ts: TokenTimeSeries, early_window_min: float = 30.0) -> FeatureVector:
    """Compute the feature vector from snapshots within the early window.

    Uses only data timestamped <= first_seen + early_window_min, so training
    features match what the live screener would have seen at that age.
    """
    fv = FeatureVector(chain=ts.chain, token_address=ts.token_address,
                       label=ts.outcome, peak_multiple=ts.peak_multiple)
    # TODO: select early snapshots; compute each FEATURE_NAMES entry into
    # fv.features. Prefer robust/normalised forms (ratios over absolutes) so a
    # signature transfers across chains. Missing data → leave the key absent
    # (the engine imputes / treats as unknown).
    raise NotImplementedError
    return fv


def extract_live(snapshots: list) -> FeatureVector:
    """Same feature computation for a LIVE token from its snapshots so far.

    Identical math to extract() — shared code path prevents train/serve skew.
    """
    raise NotImplementedError
