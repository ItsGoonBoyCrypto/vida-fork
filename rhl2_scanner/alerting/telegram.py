"""Telegram delivery + interactive commands.

Uses python-telegram-bot (v20+, async). The bot has two jobs:
  * push alerts to the configured channel/chat, and
  * accept operator commands to tune thresholds and switch risk tiers live.

If python-telegram-bot isn't installed or no token is set, ``TelegramNotifier``
degrades to a no-op / stdout sink so the scanner still runs in dry-run.
"""

from __future__ import annotations

import logging

from ..config import Config
from ..models import RiskTier, ScoreResult, TokenSnapshot
from .formatter import to_plain, to_telegram_html

log = logging.getLogger("rhl2.telegram")

try:
    from telegram import Update
    from telegram.constants import ParseMode
    from telegram.ext import (
        Application,
        CommandHandler,
        ContextTypes,
    )
    _HAS_PTB = True
except ImportError:  # pragma: no cover
    _HAS_PTB = False


class TelegramNotifier:
    """Alert sink. Falls back to stdout when Telegram isn't available."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.enabled = _HAS_PTB and bool(cfg.telegram.bot_token) and not cfg.runtime.dry_run
        self._app = None
        if self.enabled:
            self._app = Application.builder().token(cfg.telegram.bot_token).build()

    async def send(self, snap: TokenSnapshot, result: ScoreResult,
                   note: str = "", reply_to=None):
        """Send an alert; returns the message_id (for threading follow-ups) or None."""
        prefix = (note + "\n") if note else ""
        if not self.enabled or self._app is None:
            print("\n" + (note + "\n" if note else "") + to_plain(snap, result) + "\n", flush=True)
            return None
        try:
            msg = await self._app.bot.send_message(
                chat_id=self.cfg.telegram.alert_chat_id,
                text=prefix + to_telegram_html(snap, result),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                reply_to_message_id=reply_to,
            )
            return getattr(msg, "message_id", None)
        except Exception as exc:  # network / API errors shouldn't kill the loop
            log.warning("telegram send failed: %s", exc)
            print("\n" + to_plain(snap, result) + "\n", flush=True)
            return None


class CommandBot:
    """Optional interactive control surface (run alongside the scanner).

    Commands:
      /status              current tier + key thresholds
      /tier sniper|momentum  switch active risk tier
      /set <field> <value> override a threshold on the active tier
      /addwallet <addr>    add a smart-money wallet at runtime
    """

    def __init__(self, cfg: Config):
        if not _HAS_PTB:
            raise RuntimeError("python-telegram-bot is required for CommandBot")
        self.cfg = cfg
        self.app = Application.builder().token(cfg.telegram.bot_token).build()
        self.app.add_handler(CommandHandler("status", self._status))
        self.app.add_handler(CommandHandler("tier", self._tier))
        self.app.add_handler(CommandHandler("set", self._set))
        self.app.add_handler(CommandHandler("addwallet", self._addwallet))

    def _authorized(self, update: "Update") -> bool:
        ids = self.cfg.telegram.admin_user_ids
        if not ids:
            return True
        user = update.effective_user
        return bool(user and user.id in ids)

    async def _status(self, update: "Update", ctx: "ContextTypes.DEFAULT_TYPE") -> None:
        th = self.cfg.thresholds
        msg = (
            f"Tier: {self.cfg.active_tier.value}\n"
            f"Age<= {th.max_age_minutes:.0f}m | Liq>= {th.min_liquidity_usd:,.0f}\n"
            f"Holders>= {th.min_holders} | Top10<= {th.max_top10_pct:.0f}%\n"
            f"BuyRatio>= {th.min_buy_ratio_1h:.0%} | Strong>= {th.strong_alert_score:.0f}"
        )
        await update.message.reply_text(msg)

    async def _tier(self, update: "Update", ctx: "ContextTypes.DEFAULT_TYPE") -> None:
        if not self._authorized(update):
            return
        if not ctx.args:
            await update.message.reply_text("usage: /tier sniper|momentum")
            return
        try:
            self.cfg.active_tier = RiskTier(ctx.args[0].lower())
            await update.message.reply_text(f"tier -> {self.cfg.active_tier.value}")
        except ValueError:
            await update.message.reply_text("unknown tier")

    async def _set(self, update: "Update", ctx: "ContextTypes.DEFAULT_TYPE") -> None:
        if not self._authorized(update):
            return
        if len(ctx.args) != 2:
            await update.message.reply_text("usage: /set <field> <value>")
            return
        field, raw = ctx.args
        th = self.cfg.thresholds
        if not hasattr(th, field):
            await update.message.reply_text(f"unknown field {field}")
            return
        try:
            current = getattr(th, field)
            value = type(current)(raw)
            setattr(th, field, value)
            await update.message.reply_text(f"{field} = {value}")
        except (ValueError, TypeError):
            await update.message.reply_text("bad value")

    async def _addwallet(self, update: "Update", ctx: "ContextTypes.DEFAULT_TYPE") -> None:
        if not self._authorized(update):
            return
        if not ctx.args:
            await update.message.reply_text("usage: /addwallet <address>")
            return
        addr = ctx.args[0].lower()
        if addr not in self.cfg.smart_money_wallets:
            self.cfg.smart_money_wallets.append(addr)
        await update.message.reply_text(f"tracking {len(self.cfg.smart_money_wallets)} wallets")
