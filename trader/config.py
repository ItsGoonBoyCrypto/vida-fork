"""Trader configuration — all knobs, safe defaults, env-loaded.

Ships disabled and dry-run. Fund a wallet and set the env explicitly to go live;
until then nothing signs.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

# Native gas/quote token per chain (what a buy is denominated in).
NATIVE_SYMBOL = {
    "solana": "SOL", "ethereum": "ETH", "base": "ETH", "bsc": "BNB",
    "robinhood": "ETH",
}

# Conservative default buy presets (native units) — the amount buttons per chain.
_DEFAULT_PRESETS = {
    "solana": [0.05, 0.1, 0.25],
    "ethereum": [0.01, 0.02, 0.05],
    "base": [0.01, 0.02, 0.05],
    "bsc": [0.02, 0.05, 0.1],
    "robinhood": [0.01, 0.02, 0.05],
}

# Per-trade hard cap (native) — a buy above this is rejected even if requested.
_DEFAULT_PER_TRADE = {
    "solana": 0.5, "ethereum": 0.1, "base": 0.1, "bsc": 0.2, "robinhood": 0.1,
}

# Daily spend cap (native) per chain — refuses once the day's total is reached.
_DEFAULT_DAILY = {
    "solana": 2.0, "ethereum": 0.5, "base": 0.5, "bsc": 1.0, "robinhood": 0.5,
}


@dataclass
class TraderConfig:
    enabled: bool = False           # master switch — OFF until you turn it on
    dry_run: bool = True            # never signs while True (real quotes, no funds)
    admin_user_ids: list = field(default_factory=list)   # who may click Buy
    slippage_pct: float = 15.0      # memecoins need headroom
    buy_presets: dict = field(default_factory=lambda: dict(_DEFAULT_PRESETS))
    per_trade_cap: dict = field(default_factory=lambda: dict(_DEFAULT_PER_TRADE))
    daily_cap: dict = field(default_factory=lambda: dict(_DEFAULT_DAILY))
    auto_tp: bool = False           # auto-sell at the learned exit levels
    allowlist_only: bool = True     # only buy tokens the bot actually alerted

    def presets_for(self, chain: str) -> list:
        return self.buy_presets.get(chain, _DEFAULT_PRESETS.get(chain, [0.01, 0.05]))

    def native_of(self, chain: str) -> str:
        return NATIVE_SYMBOL.get(chain, "?")

    @classmethod
    def from_env(cls) -> "TraderConfig":
        c = cls()
        env = os.environ
        c.enabled = env.get("TRADER_ENABLED", "0").lower() in ("1", "true", "yes")
        # dry-run defaults ON; only a deliberate TRADER_LIVE=1 arms live signing.
        c.dry_run = env.get("TRADER_LIVE", "0").lower() not in ("1", "true", "yes")
        if v := env.get("TRADER_ADMIN_IDS"):
            c.admin_user_ids = [int(x) for x in v.replace(" ", "").split(",") if x.strip().isdigit()]
        if v := env.get("TRADER_SLIPPAGE"):
            try:
                c.slippage_pct = float(v)
            except ValueError:
                pass
        # Clamp: 0/negative would inflate quotes, >50% tolerates near-total
        # value loss to an MEV sandwich once live. Fat fingers happen.
        if not (0.0 < c.slippage_pct <= 50.0):
            c.slippage_pct = 15.0
        c.auto_tp = env.get("TRADER_AUTO_TP", "0").lower() in ("1", "true", "yes")
        for key, attr in (("TRADER_BUY_PRESETS", "buy_presets"),
                          ("TRADER_PER_TRADE_CAP", "per_trade_cap"),
                          ("TRADER_DAILY_CAP", "daily_cap")):
            if v := env.get(key):
                try:
                    setattr(c, attr, {**getattr(c, attr), **json.loads(v)})
                except (ValueError, TypeError):
                    pass
        return c
