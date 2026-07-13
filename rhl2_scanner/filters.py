"""Safety gatekeepers — the hard filters that run *before* scoring.

Philosophy from the spec: high rug rate on new launches => aggressive
safety filters. Any hard failure short-circuits to SKIP / "High Risk –
Review Manually" regardless of momentum. Unknown safety facts (``None``)
are treated as failures in strict mode, because we cannot confirm safety.

The quick-start filter stack (§6 of the spec) is also implemented here as
``quick_start_gate`` for a fast pre-screen before expensive enrichment.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import Thresholds
from .models import TokenSnapshot


@dataclass
class GateResult:
    passed: bool
    failures: list[str]

    def __bool__(self) -> bool:  # allow `if gate:`
        return self.passed


def quick_start_gate(t: TokenSnapshot, th: Thresholds) -> GateResult:
    """Cheap pre-screen using only DexScreener-level data (§6 filter stack).

    Runs on the raw new-pairs feed to decide whether a token is worth the
    expensive on-chain enrichment (holders, authorities, bundle analysis).
    Deliberately lenient on facts we haven't fetched yet — it only rejects on
    data we already have from the pairs feed.
    """
    fails: list[str] = []

    if t.age_minutes is not None and t.age_minutes > th.max_age_minutes:
        fails.append(f"age {t.age_minutes:.0f}m > {th.max_age_minutes:.0f}m")
    if t.liquidity_usd is not None and t.liquidity_usd < th.thin_liquidity_usd:
        fails.append(f"liquidity ${t.liquidity_usd:,.0f} < thin floor ${th.thin_liquidity_usd:,.0f}")
    if t.market_cap_usd is not None and t.market_cap_usd > th.max_market_cap_usd:
        fails.append(f"mcap ${t.market_cap_usd:,.0f} > ${th.max_market_cap_usd:,.0f}")

    return GateResult(passed=not fails, failures=fails)


def safety_gate(t: TokenSnapshot, th: Thresholds, strict: bool = True) -> GateResult:
    """Full safety gate. Run after enrichment populates ``t.safety``.

    ``strict=True`` treats unknown (``None``) safety facts as failures.
    Set ``strict=False`` in sniper tier if you deliberately accept unverified
    facts for speed (documented risk).
    """
    s = t.safety
    fails: list[str] = []

    def require_true(value, label: str) -> None:
        if value is True:
            return
        if value is None and not strict:
            return
        fails.append(label if value is False else f"{label} (unconfirmed)")

    # --- Contract / code safety ---
    require_true(s.contract_verified, "contract not verified")
    if s.is_honeypot is True:
        fails.append("honeypot detected")
    elif s.is_honeypot is None and strict:
        fails.append("honeypot status unconfirmed")

    # --- Taxes ---
    for tax, label in ((s.buy_tax_pct, "buy"), (s.sell_tax_pct, "sell")):
        if tax is not None and tax > th.max_tax_pct:
            fails.append(f"{label} tax {tax:.1f}% > {th.max_tax_pct:.0f}%")
        elif tax is None and strict:
            fails.append(f"{label} tax unconfirmed")

    # --- Authorities ---
    require_true(s.mint_authority_revoked, "mint authority not revoked")
    require_true(s.freeze_authority_revoked, "freeze authority not revoked")

    # --- LP safety (burned preferred; locked acceptable if long enough) ---
    if s.lp_burned is True:
        pass
    elif s.lp_locked is True:
        if s.lp_lock_seconds is not None and s.lp_lock_seconds < th.min_lp_lock_seconds:
            fails.append(
                f"LP lock {s.lp_lock_seconds // 86400}d < "
                f"{th.min_lp_lock_seconds // 86400}d min"
            )
    else:
        if strict or s.lp_locked is False:
            fails.append("LP not locked or burned")

    # --- External rug score / flags ---
    if s.high_risk_flags:
        fails.append("high-risk flags: " + ", ".join(s.high_risk_flags))
    if s.external_risk_score is not None and s.external_risk_score < th.min_external_risk_score:
        fails.append(
            f"risk score {s.external_risk_score:.0f} < {th.min_external_risk_score:.0f}"
        )

    # --- Dev wallet ---
    if s.dev_holdings_pct is not None and s.dev_holdings_pct > th.max_dev_holdings_pct:
        fails.append(f"dev holds {s.dev_holdings_pct:.1f}% > {th.max_dev_holdings_pct:.0f}%")
    if s.dev_recent_sell is True:
        fails.append("dev recently sold")

    # --- Distribution hard ceilings ---
    if t.top10_supply_pct is not None and t.top10_supply_pct > th.skip_top10_pct:
        fails.append(f"top10 {t.top10_supply_pct:.1f}% > skip {th.skip_top10_pct:.0f}%")
    if s.bundle_supply_pct is not None and s.bundle_supply_pct > th.skip_bundle_pct:
        fails.append(f"bundled {s.bundle_supply_pct:.1f}% > skip {th.skip_bundle_pct:.0f}%")

    return GateResult(passed=not fails, failures=fails)
