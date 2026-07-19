"""Trade guardrails — pure checks, no I/O. Every buy passes through here.

These are the money-safety rails: per-trade cap, daily cap, allowlist, and a
sane positive amount. All independent of chain/execution so they're trivially
tested and impossible to bypass from the execution path.
"""

from __future__ import annotations

from .config import TraderConfig


def check_amount(chain: str, amount: float, cfg: TraderConfig) -> tuple[bool, str]:
    """Positive and within the per-trade cap for the chain."""
    if amount is None or amount <= 0:
        return False, "amount must be positive"
    cap = cfg.per_trade_cap.get(chain)
    if cap is not None and amount > cap:
        return False, f"over per-trade cap ({amount:g} > {cap:g} {cfg.native_of(chain)})"
    return True, ""


def check_daily(chain: str, amount: float, spent_today: float,
                cfg: TraderConfig) -> tuple[bool, str]:
    """Would this trade breach today's spend cap for the chain?"""
    cap = cfg.daily_cap.get(chain)
    if cap is not None and (spent_today + amount) > cap:
        return False, (f"daily cap reached ({spent_today:g}+{amount:g} > {cap:g} "
                       f"{cfg.native_of(chain)})")
    return True, ""


def check_allowlist(token: str, alerted: bool, cfg: TraderConfig) -> tuple[bool, str]:
    """Only buy tokens the bot actually alerted (guards against a spoofed token)."""
    if cfg.allowlist_only and not alerted:
        return False, "token not on the alert allowlist"
    return True, ""


def check_admin(user_id, cfg: TraderConfig) -> tuple[bool, str]:
    """Only an authorised admin may trigger a buy."""
    if not cfg.admin_user_ids:
        return False, "no admin configured (set TRADER_ADMIN_IDS)"
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return False, "unauthorised"
    if uid not in cfg.admin_user_ids:
        return False, "unauthorised"
    return True, ""


def authorize(chain: str, token: str, amount: float, user_id, *, alerted: bool,
              spent_today: float, cfg: TraderConfig) -> tuple[bool, str]:
    """Run every rail. Returns (ok, reason) — the single gate a buy must clear."""
    for ok, reason in (
        check_admin(user_id, cfg),
        check_allowlist(token, alerted, cfg),
        check_amount(chain, amount, cfg),
        check_daily(chain, amount, spent_today, cfg),
    ):
        if not ok:
            return False, reason
    return True, ""
