"""Feature extraction — turn a token's EARLY history into backtest inputs.

The metrics we test against past pumpers. Computed from only the snapshots
within an early window after first-seen (default 30 min), so training features
match what the live screener would have seen at that age — no hindsight, no
train/serve skew (live screening calls the very same `_features_from`).
"""

from __future__ import annotations

from ..models import Chain, FeatureVector, Outcome, TokenSnapshot, TokenTimeSeries

FEATURE_NAMES = [
    "entry_liquidity_usd", "entry_market_cap_usd", "liq_to_mcap",
    "vol5m_to_vol1h", "buy_ratio_5m", "buy_ratio_1h", "buy_pressure_trend",
    "price_change_5m", "vol_to_mcap", "holder_count", "holder_growth",
    "holder_velocity", "liq_growth", "top10_pct", "top1_pct",
    "dev_holdings_pct", "bundle_pct", "sniper_pct", "smart_money_count",
    "smart_money_quality",
    "has_socials", "launchpad_flag", "dex_boosted", "is_sellable",
    "authorities_ok", "lp_safe", "risk_score",
    "social_volume", "social_sentiment", "social_score",
]


def _ratio(a, b):
    if a is None or not b:
        return None
    return a / b


def _features_from(early: list, first: TokenSnapshot, last: TokenSnapshot) -> dict:
    """Shared math for both training and live. `early` = snapshots in the window,
    `first`/`last` = earliest/latest within it."""
    f: dict = {}
    f["entry_liquidity_usd"] = first.liquidity_usd
    f["entry_market_cap_usd"] = first.market_cap_usd
    f["liq_to_mcap"] = _ratio(first.liquidity_usd, first.market_cap_usd)
    f["vol5m_to_vol1h"] = _ratio((last.volume_5m or 0) * 12, last.volume_1h)
    if last.buys_5m is not None and last.sells_5m is not None:
        tot = last.buys_5m + last.sells_5m
        f["buy_ratio_5m"] = (last.buys_5m / tot) if tot else None
    if last.buys_1h is not None and last.sells_1h is not None:
        tot = last.buys_1h + last.sells_1h
        f["buy_ratio_1h"] = (last.buys_1h / tot) if tot else None
    # buy-pressure trend: is 5m buying hotter than the 1h baseline? (accelerating)
    if f.get("buy_ratio_5m") is not None and f.get("buy_ratio_1h") is not None:
        f["buy_pressure_trend"] = f["buy_ratio_5m"] - f["buy_ratio_1h"]
    f["price_change_5m"] = last.price_change_5m
    f["vol_to_mcap"] = _ratio(last.volume_1h, last.market_cap_usd)
    f["holder_count"] = last.holder_count
    # holder growth across the window + velocity (holders per minute elapsed)
    if first.holder_count is not None and last.holder_count is not None:
        f["holder_growth"] = last.holder_count - first.holder_count
        elapsed_min = max(1.0, (last.ts - first.ts) / 60.0)
        f["holder_velocity"] = f["holder_growth"] / elapsed_min
    # liquidity growth across the window (real pools deepen; fakes don't)
    if first.liquidity_usd and last.liquidity_usd:
        f["liq_growth"] = last.liquidity_usd / first.liquidity_usd
    f["top10_pct"] = last.top10_supply_pct
    f["top1_pct"] = last.top1_supply_pct
    f["dev_holdings_pct"] = last.dev_holdings_pct
    f["bundle_pct"] = last.bundle_supply_pct
    f["sniper_pct"] = last.sniper_cluster_pct
    f["smart_money_count"] = len(last.smart_money_wallets or [])
    # Quality-weighted smart money: proven multi-winner wallets count for more
    # than unproven harvests (None until the reputation ledger has data).
    if last.smart_money_quality is not None:
        f["smart_money_quality"] = last.smart_money_quality
    f["has_socials"] = 1.0 if last.socials else 0.0
    f["launchpad_flag"] = 1.0 if last.launchpad else 0.0
    f["dex_boosted"] = 1.0 if last.dex_boosted else 0.0
    f["is_sellable"] = 0.0 if last.is_honeypot else (1.0 if last.is_honeypot is False else None)
    f["authorities_ok"] = 1.0 if last.mint_authority_revoked else (
        0.0 if last.mint_authority_revoked is False else None)
    f["lp_safe"] = 1.0 if last.lp_burned_or_locked else (
        0.0 if last.lp_burned_or_locked is False else None)
    f["risk_score"] = last.external_risk_score
    f["social_volume"] = last.social_volume
    f["social_sentiment"] = last.social_sentiment
    f["social_score"] = last.social_score
    return {k: v for k, v in f.items() if v is not None}


def _early_slice(snaps: list, anchor_ts: float, window_min: float):
    early = [s for s in snaps if (s.ts - anchor_ts) <= window_min * 60.0]
    early = early or snaps[:1]
    return early, early[0], early[-1]


def extract(ts: TokenTimeSeries, early_window_min: float = 30.0) -> FeatureVector:
    """Feature vector for a labeled (historical) token — training input."""
    fv = FeatureVector(chain=ts.chain, token_address=ts.token_address,
                       label=ts.outcome, peak_multiple=ts.peak_multiple)
    if not ts.snapshots:
        return fv
    early, first, last = _early_slice(ts.snapshots, ts.first_seen_ts, early_window_min)
    fv.features = _features_from(early, first, last)
    return fv


def extract_live(chain: Chain, token_address: str, snapshots: list,
                 early_window_min: float = 30.0) -> FeatureVector:
    """Same computation for a LIVE token from its snapshots so far."""
    fv = FeatureVector(chain=chain, token_address=token_address)
    if not snapshots:
        return fv
    anchor = snapshots[0].ts
    early, first, last = _early_slice(snapshots, anchor, early_window_min)
    fv.features = _features_from(early, first, last)
    return fv
