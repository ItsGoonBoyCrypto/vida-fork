"""Telegram alerting for memelab — surface high-scoring live screens.

Dependency-light raw Bot API (aiohttp), same approach as the RH scanner. When
no token/chat is configured it degrades to stdout so the collector still runs.
"""

from __future__ import annotations

import logging
from html import escape
from typing import Optional

from .models import Chain, Screen

log = logging.getLogger("memelab.alerting")

# Colored chain badge for the unified multi-chain feed (⚪ = grey stand-in; no
# true grey circle emoji exists).
_CHAIN_BADGE = {Chain.ROBINHOOD: "🟢 RH", Chain.SOLANA: "🟣 SOL",
                Chain.ETHEREUM: "⚪ ETH", Chain.BASE: "🔵 BASE", Chain.BNB: "🟡 BNB"}


def _chain_badge(chain) -> str:
    return _CHAIN_BADGE.get(chain, f"• {getattr(chain, 'value', chain)}")


def format_screen_html(scr: Screen, signature_precision: Optional[float] = None) -> str:
    s = scr.snapshot
    conf = f" · sig P={signature_precision:.0%}" if signature_precision else ""
    lines = [
        f"🎯 <b>memelab match {scr.score:.0f}/100</b>{conf}",
        f"{_chain_badge(s.chain)} · <b>${escape(s.symbol or '???')}</b>",
        f"MCAP: {_usd(s.market_cap_usd)} | Liq: {_usd(s.liquidity_usd)} | "
        f"Age: {_age(s.age_minutes)}",
        f"CA: <code>{escape(s.token_address)}</code>",   # tap-to-copy
    ]
    if scr.reasons:
        lines.append("Why: " + ", ".join(escape(r) for r in scr.reasons[:6]))
    links = []
    if s.pair_address:
        links.append(f'<a href="https://dexscreener.com/{s.chain.value}/{escape(s.pair_address)}">Chart</a>')
    for label, url in _social_links(s.socials):
        links.append(f'<a href="{escape(url)}">{label}</a>')
    if links:
        lines.append(" · ".join(links))
    lines.append("<i>Signal from the backtested winner signature. DYOR, size small.</i>")
    return "\n".join(lines)


def format_kol_html(chain, kol_name: str, symbol: str, token: str) -> str:
    """A tagged KOL/influencer wallet bought a token — attention incoming."""
    return "\n".join([
        f"📣 <b>KOL BUY</b> — {escape(kol_name)}",
        f"{_chain_badge(chain)} · <b>${escape(symbol or '???')}</b>",
        f"CA: <code>{escape(token)}</code>",
        f'<a href="https://dexscreener.com/{chain.value}/{escape(token)}">Chart</a>',
        "<i>An influencer wallet just aped — attention/volume often follows.</i>",
    ])


def format_core_alpha_html(chain, symbol: str, token: str, label: str) -> str:
    """A single proven multi-winner wallet bought a token — strong on its own."""
    return "\n".join([
        f"💎 <b>CORE ALPHA BUY</b> — {escape(label)}",
        f"{_chain_badge(chain)} · <b>${escape(symbol or '???')}</b>",
        f"CA: <code>{escape(token)}</code>",
        f'<a href="https://dexscreener.com/{chain.value}/{escape(token)}">Chart</a>',
        "<i>A wallet with a proven multi-winner track record just aped in.</i>",
    ])


def _social_links(socials: dict):
    """Extract X (Twitter) + Telegram + website links from DexScreener socials."""
    out = []
    if not socials:
        return out
    # socials is {type: url}; DexScreener uses 'twitter'/'telegram'/'website'
    for key in ("twitter", "x"):
        if socials.get(key):
            out.append(("𝕏", socials[key]))
            break
    if socials.get("telegram"):
        out.append(("TG", socials["telegram"]))
    if socials.get("website"):
        out.append(("Web", socials["website"]))
    return out


def _usd(x) -> str:
    if x is None:
        return "?"
    if x >= 1_000_000:
        return f"${x/1_000_000:.2f}M"
    if x >= 1_000:
        return f"${x/1_000:.0f}k"
    return f"${x:.0f}"


def _age(m) -> str:
    if m is None:
        return "?"
    if m < 60:
        return f"{m:.0f}m"
    if m < 1440:
        return f"{m/60:.1f}h"
    return f"{m/1440:.1f}d"


class TelegramAlerter:
    def __init__(self, token: str = "", chat_id: str = "", session=None):
        self.token = token
        self.chat_id = chat_id
        self._session = session

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    async def send(self, html: str, reply_markup: dict = None) -> None:
        if not self.enabled or self._session is None:
            print("\n" + html + "\n", flush=True)
            return
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = {"chat_id": self.chat_id, "text": html, "parse_mode": "HTML",
                   "disable_web_page_preview": True}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        try:
            async with self._session.post(url, json=payload) as r:
                if r.status != 200:
                    log.warning("telegram send HTTP %s", r.status)
        except Exception:  # noqa: BLE001 — never let alerting kill the loop
            log.warning("telegram send failed", exc_info=True)
