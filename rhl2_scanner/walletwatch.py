"""Whale / smart-money wallet activity alerts.

Polls a curated set of wallets' recent ERC-20 transfers (via the Blockscout
Etherscan-compatible explorer) and alerts when one of them BUYS (receives a
token) — or optionally SELLS — a memecoin, with a USD-size floor to skip dust.

Each event is de-duplicated in SQLite so a given (wallet, tx, token) only ever
alerts once. USD size + symbol + chart link are enriched from DexScreener.

This is independent of the gem scanner's safety gates — it's pure "what are the
whales doing right now" signal.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

import aiohttp

from .config import Config
from .sources.dexscreener import DexScreenerClient
from .storage import Storage

log = logging.getLogger("rhl2.walletwatch")

# RH "Global Dollar" stablecoin — excluded from buy/sell detection alongside WETH.
_USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"


@dataclass
class WhaleEvent:
    wallet: str
    label: str
    side: str                 # "buy" | "sell"
    token_address: str
    symbol: str
    amount: float
    usd: Optional[float]
    tx_hash: str
    chart_url: str = ""


@dataclass
class DeployEvent:
    wallet: str
    label: str
    token_address: str
    tx_hash: str


class WalletWatcher:
    def __init__(self, cfg: Config, storage: Storage,
                 session: Optional[aiohttp.ClientSession] = None):
        self.cfg = cfg
        self.ww = cfg.wallet_watch
        self.storage = storage
        self._session = session
        self._owns_session = session is None
        self._excluded = {
            (cfg.chain.weth_address or "").lower(),
            _USDG,
            "",
        }

    async def __aenter__(self) -> "WalletWatcher":
        if self._session is None:
            timeout = aiohttp.ClientTimeout(total=self.cfg.runtime.request_timeout_seconds)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self

    async def __aexit__(self, *exc) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()

    def _watched_wallets(self) -> list[str]:
        """The wallets to watch: the static config list UNION our LEARNED
        reputation set (harvested smart wallets + core-alpha), capped and
        deduped. This turns the feed into a Cielo-style tracker of the very
        wallets our own winner-harvest proved sharp — not just a hand list."""
        seen: dict[str, None] = {}
        for w in self.ww.wallets:
            wl = (w or "").lower()
            if wl:
                seen[wl] = None
        if self.ww.watch_smart_set:
            try:
                learned = self.storage.smart_wallets()          # harvested + seeded
            except Exception:  # noqa: BLE001
                learned = []
            for w in learned:
                wl = (w or "").lower()
                if wl and wl not in seen:
                    seen[wl] = None
                if len(seen) >= self.ww.max_watched:
                    break
        return list(seen)[: self.ww.max_watched]

    def enabled(self) -> bool:
        has_wallets = bool(self.ww.wallets) or self.ww.watch_smart_set
        return bool(self.ww.enabled and has_wallets and self.cfg.chain.explorer_api_url)

    async def poll(self) -> list[WhaleEvent]:
        """Return NEW, size-filtered whale events since the last poll."""
        if not self.enabled() or self._session is None:
            return []
        events: list[WhaleEvent] = []
        dex = DexScreenerClient(self.cfg, session=self._session)
        want_sells = self.ww.alert_on == "buys_sells"

        for wallet in self._watched_wallets():
            # First time we ever poll a wallet: seed its recent history as "seen"
            # WITHOUT alerting, so we only alert on genuinely new activity after.
            seed_key = f"__seeded__|{wallet}"
            seeding = self.storage.wallet_event_is_new(seed_key)

            for tx in await self._transfers(wallet):
                event = self._classify(wallet, tx, want_sells)
                if event is None:
                    continue
                tx_key = f"{wallet}|{event.tx_hash}|{event.token_address}"
                if not self.storage.wallet_event_is_new(tx_key):
                    continue
                self.storage.mark_wallet_event(tx_key)  # mark seen regardless of size/seed
                if seeding:
                    continue                             # silent on the first poll
                if event.amount <= 0:
                    continue                             # zero-value transfer
                await self._enrich(event, dex)
                # Require a REAL, priced market ≥ min_usd. This structurally
                # excludes airdrop/spam tokens (no DexScreener price) and dust —
                # a whale "buy" alert should mean an actual, sized swap.
                if event.usd is None or event.usd < self.ww.min_usd:
                    continue
                events.append(event)

            if seeding:
                self.storage.mark_wallet_event(seed_key)
        return events

    def _classify(self, wallet: str, tx: dict, want_sells: bool) -> Optional[WhaleEvent]:
        frm = (tx.get("from") or "").lower()
        to = (tx.get("to") or "").lower()
        token = (tx.get("contractAddress") or "").lower()
        if token in self._excluded:
            return None

        if to == wallet and frm != wallet:
            side = "buy"
        elif frm == wallet and to != wallet and want_sells:
            side = "sell"
        else:
            return None

        amount = _to_amount(tx.get("value"), tx.get("tokenDecimal"))
        return WhaleEvent(
            wallet=wallet,
            label=self.ww.labels.get(wallet, _short(wallet)),
            side=side,
            token_address=token,
            symbol=tx.get("tokenSymbol") or "?",
            amount=amount or 0.0,
            usd=None,
            tx_hash=tx.get("hash") or "",
        )

    async def _enrich(self, event: WhaleEvent, dex: DexScreenerClient) -> None:
        for pair in await dex.pairs_for_token(event.token_address):
            if pair.price_usd is not None:
                event.usd = round(event.amount * pair.price_usd, 2)
                event.chart_url = pair.dexscreener_url
                if pair.symbol:
                    event.symbol = pair.symbol
                break

    async def poll_deploys(self) -> list["DeployEvent"]:
        """NEW token contracts deployed by a LABELLED (curated/alpha) wallet.

        This is the pre-index edge: an alpha deployer launching a token, caught
        the instant the creation tx lands — before it hits DexScreener or the
        crowd (the $WALLET / $PLTS pattern). Only labelled wallets are polled
        (they're the deployers), keeping explorer load bounded."""
        if not self.enabled() or self._session is None:
            return []
        out: list[DeployEvent] = []
        for wallet in list(self.ww.labels.keys())[: self.ww.max_watched]:
            seed_key = f"__dseeded__|{wallet}"
            seeding = self.storage.wallet_event_is_new(seed_key)
            for tx in await self._creations(wallet):
                created = (tx.get("contractAddress") or "").lower()
                txh = (tx.get("hash") or "").strip()
                if not created or created in ("", "0x") or not txh:
                    continue
                key = f"deploy|{wallet}|{txh}"
                if not self.storage.wallet_event_is_new(key):
                    continue
                self.storage.mark_wallet_event(key)
                if seeding:
                    continue          # first poll of this wallet: seed, don't alert history
                out.append(DeployEvent(
                    wallet=wallet, label=self.ww.labels.get(wallet, _short(wallet)),
                    token_address=created, tx_hash=txh))
            if seeding:
                self.storage.mark_wallet_event(seed_key)
        return out

    async def _creations(self, wallet: str) -> list[dict]:
        """Recent normal txs by a wallet that CREATED a contract (txlist)."""
        params: dict[str, Any] = {
            "module": "account", "action": "txlist", "address": wallet,
            "page": 1, "offset": self.ww.max_transfers_per_wallet, "sort": "desc",
        }
        if self.cfg.chain.explorer_api_key:
            params["apikey"] = self.cfg.chain.explorer_api_key
        try:
            async with self._session.get(self.cfg.chain.explorer_api_url, params=params) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
        except (aiohttp.ClientError, TimeoutError, ValueError):
            return []
        result = data.get("result")
        if not isinstance(result, list):
            return []
        # a contract creation has a populated contractAddress (and empty `to`)
        return [tx for tx in result if (tx.get("contractAddress") or "").strip()
                and not (tx.get("to") or "").strip()]

    async def _transfers(self, wallet: str) -> list[dict]:
        params: dict[str, Any] = {
            "module": "account",
            "action": "tokentx",
            "address": wallet,
            "page": 1,
            "offset": self.ww.max_transfers_per_wallet,
            "sort": "desc",
        }
        if self.cfg.chain.explorer_api_key:
            params["apikey"] = self.cfg.chain.explorer_api_key
        try:
            async with self._session.get(self.cfg.chain.explorer_api_url, params=params) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
        except (aiohttp.ClientError, TimeoutError, ValueError):
            return []
        result = data.get("result")
        return result if isinstance(result, list) else []


def format_deploy_html(e: "DeployEvent", explorer: str = "") -> str:
    from html import escape
    scan = f"\n🔍 <a href=\"{explorer}/token/{e.token_address}\">Scan</a>" if explorer else ""
    return "\n".join([
        f"🧬🚨 <b>ALPHA DEPLOY</b> — {escape(e.label)}",
        "just deployed a NEW token (pre-index — not on DexScreener yet):",
        f"<code>{escape(e.token_address)}</code>{scan}",
        "<i>A tracked deployer launched this. Highest-risk/earliest — DYOR, size "
        "tiny; the scanner will track its curve from here.</i>",
    ])


def _to_amount(value: Any, decimals: Any) -> Optional[float]:
    try:
        return int(value) / (10 ** int(decimals or 18))
    except (TypeError, ValueError):
        return None


def _short(addr: str) -> str:
    return f"{addr[:6]}…{addr[-4:]}" if addr and len(addr) > 12 else addr


def format_cluster_html(symbol: str, token: str, labels: list[str],
                        chart_url: str = "") -> str:
    """High-priority alert: multiple smart-money wallets bought the same token."""
    from html import escape
    who = ", ".join(escape(x) for x in labels)
    lines = [
        f"🧠🚨 <b>SMART MONEY CLUSTER</b> — {len(labels)} wallets in ${escape(symbol or '???')}",
        f"Buyers: {who}",
        f"CA: <code>{escape(token)}</code>",
        "<i>Multiple proven-early wallets converging — strongest early signal.</i>",
    ]
    if chart_url:
        lines.append(f'<a href="{escape(chart_url)}">Chart</a>')
    return "\n".join(lines)


def format_honeypot_html(symbol: str, token: str, reason: str,
                         deployer_hits: int = 0, chart_url: str = "") -> str:
    """Defensive alert: an interesting token is a can't-sell trap."""
    from html import escape
    lines = [
        f"🍯 <b>HONEYPOT WARNING</b> — ${escape(symbol or '???')}",
        escape(reason),
        f"CA: <code>{escape(token)}</code>",
    ]
    if deployer_hits >= 2:
        lines.append(f"🚫 Deployer has shipped <b>{deployer_hits}</b> honeypots — serial scammer.")
    lines.append("<i>Do NOT buy — you likely can't sell. Logged the deployer.</i>")
    if chart_url:
        lines.append(f'<a href="{escape(chart_url)}">Chart</a>')
    return "\n".join(lines)


def format_kol_html(kol_name: str, symbol: str, token: str, chart_url: str = "") -> str:
    """A tagged KOL/influencer wallet bought a token — attention incoming."""
    from html import escape
    lines = [
        f"📣 <b>KOL BUY</b> — {escape(kol_name)} bought ${escape(symbol or '???')}",
        f"CA: <code>{escape(token)}</code>",
        "<i>An influencer wallet just aped — attention/volume often follows.</i>",
    ]
    if chart_url:
        lines.append(f'<a href="{escape(chart_url)}">Chart</a>')
    return "\n".join(lines)


def format_top_zone_html(symbol: str, token: str, reason: str, current_mult: float,
                         chart_url: str = "") -> str:
    """Proactive learned take-profit: position entered the historical top zone."""
    from html import escape
    lines = [
        f"⏏️ <b>TOP ZONE</b> — ${escape(symbol or '???')} now {current_mult:.2g}x",
        escape(reason.capitalize()) + ".",
        f"CA: <code>{escape(token)}</code>",
        "<i>Where winners like this usually top — consider taking profit.</i>",
    ]
    if chart_url:
        lines.append(f'<a href="{escape(chart_url)}">Chart</a>')
    return "\n".join(lines)


def format_core_alpha_html(symbol: str, token: str, label: str, overlap: int,
                           chart_url: str = "") -> str:
    """A single proven-across-many-winners wallet bought a token — strong on its own."""
    from html import escape
    ov = f" (early on {overlap} past winners)" if overlap else ""
    lines = [
        f"💎 <b>CORE ALPHA BUY</b> — {escape(label)}{ov} bought ${escape(symbol or '???')}",
        f"CA: <code>{escape(token)}</code>",
        "<i>A wallet with a proven multi-winner track record just aped in.</i>",
    ]
    if chart_url:
        lines.append(f'<a href="{escape(chart_url)}">Chart</a>')
    return "\n".join(lines)


def format_exit_html(symbol: str, token: str, label: str, usd, chart_url: str = "") -> str:
    """Smart money is selling a token smart money had bought — exit signal."""
    from html import escape
    amt = f" (~${usd:,.0f})" if usd else ""
    lines = [
        f"🔴 <b>SMART MONEY EXIT</b> — {escape(label)} sold ${escape(symbol or '???')}{amt}",
        f"CA: <code>{escape(token)}</code>",
        "<i>A wallet that got in early is taking profit — consider trimming.</i>",
    ]
    if chart_url:
        lines.append(f'<a href="{escape(chart_url)}">Chart</a>')
    return "\n".join(lines)


def _since_alert(current_mult: float | None) -> str:
    """' · now 3.4x (+240%)' from the live multiple vs the alert price."""
    if not current_mult or current_mult <= 0:
        return ""
    pct = (current_mult - 1.0) * 100.0
    return f" · now {current_mult:.2g}x ({'+' if pct >= 0 else ''}{pct:.0f}%)"


def format_milestone_html(symbol: str, token: str, mult: float, chart_url: str = "",
                          current_mult: float | None = None) -> str:
    """An alerted token reached a multiple of its alert price."""
    from html import escape
    lines = [
        f"📈 <b>${escape(symbol or '???')} hit {mult:g}x</b> from alert{_since_alert(current_mult)}",
        f"CA: <code>{escape(token)}</code>",
    ]
    if chart_url:
        lines.append(f'<a href="{escape(chart_url)}">Chart</a>')
    return "\n".join(lines)


def format_dump_html(symbol: str, token: str, drawdown_pct: float, peak_mult: float,
                     chart_url: str = "", current_mult: float | None = None) -> str:
    """An alerted token that ran up is now falling hard — rug/dump guard."""
    from html import escape
    lines = [
        f"⚠️ <b>${escape(symbol or '???')} DUMPING</b> — −{drawdown_pct:.0f}% from peak "
        f"(peaked {peak_mult:g}x){_since_alert(current_mult)}",
        f"CA: <code>{escape(token)}</code>",
        "<i>Sharp drop from the high — protect any position.</i>",
    ]
    if chart_url:
        lines.append(f'<a href="{escape(chart_url)}">Chart</a>')
    return "\n".join(lines)


def format_whale_html(e: WhaleEvent) -> str:
    from html import escape
    emoji = "🐋🟢" if e.side == "buy" else "🐋🔴"
    verb = "BOUGHT" if e.side == "buy" else "SOLD"
    usd = f" (~${e.usd:,.0f})" if e.usd is not None else ""
    amt = f"{e.amount:,.2f}" if e.amount else "?"
    lines = [
        f"{emoji} <b>WHALE {verb}</b> — {escape(e.label)}",
        f"{verb.title()} {amt} <b>${escape(e.symbol)}</b>{usd}",
        f"CA: <code>{escape(e.token_address)}</code>",   # tap-to-copy in Telegram
    ]
    links = []
    if e.chart_url:
        links.append(f'<a href="{escape(e.chart_url)}">Chart</a>')
    if links:
        lines.append("Links: " + " | ".join(links))
    return "\n".join(lines)
