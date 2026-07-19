"""Buy engine — authorise, quote, and (dry-run) preview a trade.

Phase 1: fully wired dry-run. It runs every safety rail, builds a real preview
(what you'd buy, estimated tokens, slippage, cap status), and signs NOTHING.
Live signing is Phase 2 (per-chain executors), explicitly gated behind
``cfg.dry_run == False`` + funded wallets + small-funds testing.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import TraderConfig
from .safety import authorize


@dataclass
class BuyResult:
    ok: bool
    chain: str
    token: str
    amount: float
    native: str
    dry_run: bool = True
    reason: str = ""
    preview: str = ""
    est_tokens: float = 0.0


def _fmt_usd(x) -> str:
    if x is None:
        return "?"
    if x >= 1_000_000:
        return f"${x/1e6:.2f}M"
    if x >= 1_000:
        return f"${x/1e3:.1f}k"
    if x >= 1:
        return f"${x:.2f}"
    return f"${x:.6f}".rstrip("0")


def preview_buy(chain: str, token: str, amount: float, user_id, *, symbol: str,
                alerted: bool, spent_today: float, cfg: TraderConfig,
                price_usd=None, native_usd=None) -> BuyResult:
    """Authorise + build the trade preview. Never signs (Phase 1)."""
    native = cfg.native_of(chain)
    base = BuyResult(ok=False, chain=chain, token=token, amount=amount,
                     native=native, dry_run=cfg.dry_run)
    if not cfg.enabled:
        base.reason = "trading is OFF (set TRADER_ENABLED=1 to arm)"
        return base
    ok, reason = authorize(chain, token, amount, user_id, alerted=alerted,
                           spent_today=spent_today, cfg=cfg)
    if not ok:
        base.reason = reason
        return base

    # Token-out estimate (only if we can price both sides).
    est = 0.0
    est_line = ""
    if price_usd and native_usd:
        gross = amount * native_usd / price_usd
        est = gross * (1.0 - cfg.slippage_pct / 100.0)
        est_line = f"\n≈ <b>{est:,.0f}</b> ${symbol} (after {cfg.slippage_pct:g}% slippage)"

    spend_usd = f" (~{_fmt_usd(amount * native_usd)})" if native_usd else ""
    if cfg.dry_run:
        base.ok = True
        base.est_tokens = est
        base.preview = (
            f"🧪 <b>DRY-RUN</b> — no funds moved\n"
            f"Would buy <b>{amount:g} {native}</b>{spend_usd} of ${symbol} on {chain}"
            f" @ {_fmt_usd(price_usd)}{est_line}\n"
            f"<i>Live trading is off. Fund the wallet + set TRADER_LIVE=1 to arm.</i>")
        return base

    # Live path — Phase 2. Deliberately not implemented until small-funds tested.
    base.ok = False
    base.reason = "live signing not enabled yet (Phase 2 — needs a funded wallet + testing)"
    return base
