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


_STOCK_NAME_MARKERS = ("robinhood token",)   # tokenized equities (MU/TSLA/… • Robinhood Token)


def is_stock_token(t: TokenSnapshot) -> bool:
    """True for Robinhood tokenized-stock tokens (equities, not memecoins)."""
    name = (t.name or "").lower()
    return any(m in name for m in _STOCK_NAME_MARKERS)


def _norm_symbol(s: str) -> str:
    """Uppercase, strip a leading $, drop non-alphanumerics ($ROBIN-HOOD -> ROBINHOOD)."""
    s = (s or "").strip().lstrip("$")
    return "".join(ch for ch in s if ch.isalnum()).upper()


def is_blocked_symbol(t: TokenSnapshot, blocked: list[str]) -> bool:
    """True if the token's symbol (or name) matches a blocked scam-impersonator.

    Matches the normalised symbol exactly, and also catches the symbol appearing
    as a standalone word in the name — so fake ``$ROBINHOOD`` clones are dropped
    regardless of how they dress up the name.
    """
    if not blocked:
        return False
    wanted = {_norm_symbol(b) for b in blocked if b}
    if not wanted:
        return False
    if _norm_symbol(t.symbol) in wanted:
        return True
    # Also block when the name reduces to exactly a blocked token (e.g. name
    # "ROBINHOOD" with a different ticker used to sneak past a symbol check).
    if _norm_symbol(t.name) in wanted:
        return True
    return False


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
    if t.age_minutes is not None and th.min_age_minutes and t.age_minutes < th.min_age_minutes:
        fails.append(f"age {t.age_minutes:.0f}m < {th.min_age_minutes:.0f}m")
    # Liquidity floor: below the lower of thin/min = hard skip.
    liq_floor = max(th.thin_liquidity_usd, th.min_liquidity_usd)
    if t.liquidity_usd is not None and liq_floor and t.liquidity_usd < liq_floor:
        fails.append(f"liquidity ${t.liquidity_usd:,.0f} < ${liq_floor:,.0f}")
    if t.market_cap_usd is not None and t.market_cap_usd > th.max_market_cap_usd:
        fails.append(f"mcap ${t.market_cap_usd:,.0f} > ${th.max_market_cap_usd:,.0f}")
    if t.market_cap_usd is not None and th.min_market_cap_usd and t.market_cap_usd < th.min_market_cap_usd:
        fails.append(f"mcap ${t.market_cap_usd:,.0f} < ${th.min_market_cap_usd:,.0f}")

    return GateResult(passed=not fails, failures=fails)


def safety_gate(t: TokenSnapshot, th: Thresholds, strict: bool = True,
                pragmatic: bool = False) -> GateResult:
    """Full safety gate. Run after enrichment populates ``t.safety``.

    ``strict=True`` treats unknown (``None``) safety facts as failures.
    Set ``strict=False`` in sniper tier if you deliberately accept unverified
    facts for speed (documented risk).

    ``pragmatic=True`` keeps every *confirmable* gate strict (verified contract,
    mint/freeze revoked, LP burned/locked, top-holder & bundle ceilings) but
    tolerates an *unconfirmed* (None) honeypot/tax/risk-score — for chains where
    no tax oracle exists yet (e.g. a brand-new L2 without GoPlus coverage and no
    DEX router wired). A CONFIRMED-bad value (honeypot True, tax over the cap)
    still fails. Known-bad always loses; only "unknown" is tolerated.
    """
    s = t.safety
    fails: list[str] = []
    # Under pragmatic, facts we can't yet confirm on this chain (verified,
    # authorities, LP, honeypot/tax/score) are tolerated when *unknown* (None) —
    # but a CONFIRMED-bad (False / over-limit) still fails. This enforces the
    # signals we can compute on-chain (honeypot sell-sim, distribution, bundle,
    # dev) while not auto-skipping every token on a new V3 launchpad chain.
    tolerate_unknown = pragmatic

    def require_true(value, label: str) -> None:
        if value is True:
            return
        if value is None and (not strict or tolerate_unknown):
            return
        fails.append(label if value is False else f"{label} (unconfirmed)")

    # --- Contract / code safety ---
    # Verification is a hard gate only when configured (off for RH Chain, where
    # verification is rare); otherwise it's just a scoring bonus.
    if th.require_contract_verified:
        require_true(s.contract_verified, "contract not verified")
    if s.is_honeypot is True:
        fails.append("honeypot detected")
    elif s.is_honeypot is None and strict and not tolerate_unknown:
        fails.append("honeypot status unconfirmed")

    # Owner can still rug post-buy (live owner + settable tax / blacklist / pause /
    # mint in the bytecode). A CONFIRMED capability, so it fails even under
    # pragmatic — this is the "clean now, flips the tax later" vector. Tunable off.
    if s.owner_can_rug is True and getattr(th, "gate_owner_rug", True):
        hook = f" ({s.owner_hooks[0]})" if s.owner_hooks else ""
        fails.append(f"owner can rug post-buy{hook}")

    # --- Taxes ---
    for tax, label in ((s.buy_tax_pct, "buy"), (s.sell_tax_pct, "sell")):
        if tax is not None and tax > th.max_tax_pct:
            fails.append(f"{label} tax {tax:.1f}% > {th.max_tax_pct:.0f}%")
        elif tax is None and strict and not tolerate_unknown:
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
        # V3 LP is an NFT position, not a fungible token, so our V2-style
        # burn/lock read comes back None. Tolerate unknown under pragmatic;
        # only fail on an explicitly-unsafe LP.
        if (strict and not tolerate_unknown) or s.lp_locked is False:
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

    # --- Distribution hard ceilings (0 = disabled) ---
    if t.top10_supply_pct is not None and t.top10_supply_pct > th.skip_top10_pct:
        fails.append(f"top10 {t.top10_supply_pct:.1f}% > skip {th.skip_top10_pct:.0f}%")
    if th.max_top1_pct and t.top1_supply_pct is not None and t.top1_supply_pct > th.max_top1_pct:
        fails.append(f"top1 {t.top1_supply_pct:.1f}% > {th.max_top1_pct:.0f}%")
    if s.bundle_supply_pct is not None and s.bundle_supply_pct > th.skip_bundle_pct:
        fails.append(f"bundled {s.bundle_supply_pct:.1f}% > skip {th.skip_bundle_pct:.0f}%")
    # Sniper-cluster ceiling — only meaningful once the token has aged past its
    # launch window. On a minutes-old token "everyone bought at launch" so the
    # metric pins ~100% and is uninformative; enforcing it there skips every fresh
    # gem. Require a known age at/above the floor before gating on it.
    if (th.max_sniper_cluster_pct and s.sniper_cluster_pct is not None
            and t.age_minutes is not None
            and t.age_minutes >= th.sniper_cluster_min_age_minutes
            and s.sniper_cluster_pct > th.max_sniper_cluster_pct):
        fails.append(f"sniper cluster {s.sniper_cluster_pct:.1f}% > {th.max_sniper_cluster_pct:.0f}%")

    return GateResult(passed=not fails, failures=fails)
