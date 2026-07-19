"""One-click trading for the unified alert bot — all 5 chains.

SAFETY-FIRST BY DESIGN. This package ships OFF (`TRADER_ENABLED=0`) and, even
when enabled, defaults to DRY-RUN (`TRADER_DRY_RUN=1`) — it fetches real quotes
and shows exactly what a buy would do, but signs nothing and moves no funds.
Live signing is a separate, explicitly-gated phase that requires funded wallets
and small-funds testing first.

Guardrails baked in: per-trade + daily caps, an allowlist (only tokens the bot
alerted), and admin-only authorisation on every buy trigger.
"""
