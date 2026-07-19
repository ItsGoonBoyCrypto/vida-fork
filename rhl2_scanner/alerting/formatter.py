"""Render a ScoreResult + TokenSnapshot into the spec's alert format.

Produces the exact shape from spec §4:

    🚨 EARLY GEM ALERT - Score: 82/100
    Token: $EXAMPLE (CA: 0x...)
    Age: 47m | MCAP: $187k | Liq: $42k
    Holders: 312 (↑ growing) | Top10: 22%
    Volume 1h: $28k (↑ accelerating) | Buy Ratio: 78%
    Safety: ✅ Revoked | ✅ LP Burned | Low Bundle
    Smart Money: 3 wallets active
    Links: DexScreener | Chart | TG
    Reasons: ...

Output is Telegram-HTML (links become anchors); ``to_plain`` gives a
terminal-friendly version for dry-run.
"""

from __future__ import annotations

from html import escape

from ..models import AlertLevel, ScoreResult, TokenSnapshot

_LEVEL_HEADER = {
    AlertLevel.STRONG: "🚨 EARLY GEM ALERT",
    AlertLevel.WATCH: "👀 WATCH",
    AlertLevel.SKIP: "⏭️ SKIP",
}


def _usd(x: float | None) -> str:
    if x is None:
        return "?"
    if x >= 1_000_000:
        return f"${x/1_000_000:.2f}M"
    if x >= 1_000:
        return f"${x/1_000:.0f}k"
    return f"${x:.0f}"


def _pct(x: float | None) -> str:
    return f"{x:.0f}%" if x is not None else "?"


def _age(minutes: float | None) -> str:
    if minutes is None:
        return "?"
    if minutes < 60:
        return f"{minutes:.0f}m"
    if minutes < 60 * 24:
        return f"{minutes/60:.1f}h"
    return f"{minutes/1440:.1f}d"


def _bar(pct: float | None, n: int = 10) -> str:
    """A 10-cell progress bar, e.g. ▰▰▰▰▰▰▱▱▱▱ for 60%."""
    if pct is None:
        return ""
    filled = max(0, min(n, round(pct / 100.0 * n)))
    return "▰" * filled + "▱" * (n - filled)


def _buy_pressure(snap: TokenSnapshot) -> str:
    """Compact 5m buy/sell pressure with a traffic-light dot, or '' if unknown."""
    b, s = snap.buys_5m, snap.sells_5m
    if b is None or s is None or (b + s) == 0:
        # fall back to 1h
        b, s = snap.buys_1h, snap.sells_1h
        win = "1h"
    else:
        win = "5m"
    if b is None or s is None or (b + s) == 0:
        return ""
    ratio = b / (b + s)
    dot = "🟢" if ratio >= 0.65 else "🟡" if ratio >= 0.5 else "🔴"
    return f"Buys {win}: {b}·{s} {dot} {ratio*100:.0f}%"


def _links_row(snap: TokenSnapshot) -> str:
    """One tap-row of the links that matter — pre-grad tokens have no DEX pair,
    so the launchpad trade link + explorer carry them; graduated ones get the chart."""
    bits = []
    if snap.trade_url:
        bits.append(f'<a href="{escape(snap.trade_url)}">🚀 Trade</a>')
    if snap.dexscreener_url:
        bits.append(f'<a href="{escape(snap.dexscreener_url)}">📈 Chart</a>')
    if snap.explorer_url:
        bits.append(f'<a href="{escape(snap.explorer_url)}">🔍 Scan</a>')
    if snap.socials.get("twitter"):
        bits.append(f'<a href="{escape(snap.socials["twitter"])}">𝕏</a>')
    if snap.socials.get("telegram"):
        bits.append(f'<a href="{escape(snap.socials["telegram"])}">💬 TG</a>')
    if snap.socials.get("website"):
        bits.append(f'<a href="{escape(snap.socials["website"])}">🌐 Web</a>')
    return " · ".join(bits)


def _smart_line(snap: TokenSnapshot) -> str:
    n = len(snap.smart_money_wallets or [])
    if not n:
        return ""
    heads = ", ".join(w[:6] + "…" for w in snap.smart_money_wallets[:3])
    more = f" +{n-3}" if n > 3 else ""
    return f"🧠 Smart money: <b>{n}</b> in ({heads}{more})"


def _safety_line(snap: TokenSnapshot) -> str:
    s = snap.safety
    parts = []
    if s.mint_authority_revoked and s.freeze_authority_revoked:
        parts.append("✅ Revoked")
    elif s.mint_authority_revoked:
        parts.append("✅ Mint revoked")
    if s.lp_burned:
        parts.append("✅ LP Burned")
    elif s.lp_locked:
        if s.lp_lock_seconds:
            parts.append(f"✅ LP Locked {s.lp_lock_seconds // 86400}d")
        else:
            parts.append("✅ LP Locked")
    if s.bundle_supply_pct is not None:
        parts.append("Low Bundle" if s.bundle_supply_pct < 20 else f"⚠️ Bundle {s.bundle_supply_pct:.0f}%")
    if not parts:
        parts.append("⚠️ Unverified")
    return " | ".join(parts)


def build_lines(snap: TokenSnapshot, result: ScoreResult) -> list[str]:
    header = _LEVEL_HEADER.get(result.level, "ALERT")
    holders = f"{snap.holder_count}" if snap.holder_count is not None else "?"
    growing = " (↑ growing)" if (snap.holder_growth_1h or 0) > 0 else ""
    accel = " (↑ accelerating)" if snap.volume_accelerating else ""
    buy_ratio = f"{snap.buy_ratio_1h*100:.0f}%" if snap.buy_ratio_1h is not None else "?"

    lines = [
        f"{header} - Score: {result.composite:.0f}/100",
        f"Token: ${snap.symbol or '???'}",
        f"CA: {snap.token_address}",
    ]
    if snap.launchpad:
        origin = f"Launchpad: {snap.launchpad}"
        if snap.curve_progress_pct is not None:
            origin += f" | Curve: {snap.curve_progress_pct:.0f}% (pre-grad)"
        lines.append(origin)
    lines += [
        f"Age: {_age(snap.age_minutes)} | MCAP: {_usd(snap.market_cap_usd)} | Liq: {_usd(snap.liquidity_usd)}",
        f"Holders: {holders}{growing} | Top10: {_pct(snap.top10_supply_pct)}",
        f"Volume 1h: {_usd(snap.volume_1h)}{accel} | Buy Ratio: {buy_ratio}",
        f"Safety: {_safety_line(snap)}",
    ]
    if snap.smart_money_wallets:
        lines.append(f"Smart Money: {len(snap.smart_money_wallets)} wallets active")

    # Score breakdown (spec: "Safety: 90/100 | Momentum: 85/100 ...")
    breakdown = " | ".join(
        f"{c.name.capitalize()}: {c.raw:.0f}/100" for c in result.categories
    )
    lines.append(f"Breakdown: {breakdown}")

    reasons = result.reasons
    if reasons:
        lines.append("Reasons: " + ", ".join(reasons[:6]))
    return lines


def to_plain(snap: TokenSnapshot, result: ScoreResult) -> str:
    body = "\n".join(build_lines(snap, result))
    links = []
    if snap.dexscreener_url:
        links.append(f"DexScreener: {snap.dexscreener_url}")
    if snap.socials.get("telegram"):
        links.append(f"TG: {snap.socials['telegram']}")
    if links:
        body += "\n" + " | ".join(links)
    return body


def format_early_launch_html(snap: TokenSnapshot, result: ScoreResult) -> str:
    """Compact 'fresh & safe' early-entry alert (below the maturity score band)."""
    s = snap.safety
    safety_bits = []
    if s.mint_authority_revoked and s.freeze_authority_revoked:
        safety_bits.append("✅ Revoked")
    if s.lp_burned:
        safety_bits.append("✅ LP Burned")
    elif s.lp_locked:
        safety_bits.append("✅ LP Locked")
    if s.is_honeypot is False:
        safety_bits.append("✅ Sellable")
    if s.dev_holdings_pct is not None:
        safety_bits.append(f"dev {s.dev_holdings_pct:.0f}%")
    safety = " | ".join(safety_bits) if safety_bits else "⚠️ unverified"

    header = "🌱 EARLY LAUNCH"
    if snap.launchpad:
        header += f" · {escape(snap.launchpad)}"
    lines = [
        f"<b>{header} — ${escape(snap.symbol or '???')}</b>",
        f"<code>{escape(snap.token_address)}</code>",
    ]
    if snap.curve_progress_pct is not None:
        lines.append(f"curve {_bar(snap.curve_progress_pct)} {snap.curve_progress_pct:.0f}% (pre-grad)")
    lines.append(f"💰 MCAP {_usd(snap.market_cap_usd)} · Liq {_usd(snap.liquidity_usd)} "
                 f"· Age {_age(snap.age_minutes)}")
    lines.append(f"👥 Holders {snap.holder_count if snap.holder_count is not None else '?'} "
                 f"· Top10 {_pct(snap.top10_supply_pct)}")
    bp = _buy_pressure(snap)
    if bp:
        lines.append(f"📊 {bp}")
    lines.append(f"🛡 {safety}")
    smart = _smart_line(snap)
    if smart:
        lines.append(smart)
    links = _links_row(snap)
    if links:
        lines.append(links)
    lines.append("<i>Fresh launch — highest risk/reward. DYOR, size small.</i>")
    return "\n".join(lines)


def to_telegram_html(snap: TokenSnapshot, result: ScoreResult) -> str:
    """Rich, scannable Telegram alert (maturity tier)."""
    sym = escape(snap.symbol or "???")
    header = _LEVEL_HEADER.get(result.level, "ALERT")
    lines = [f"<b>{header} — ${sym}</b>  ·  {result.composite:.0f}/100",
             f"<code>{escape(snap.token_address)}</code>"]

    # Origin + graduation progress bar (pre-grad curve tokens).
    if snap.launchpad:
        origin = f"🏷 {escape(snap.launchpad)}"
        if snap.curve_progress_pct is not None:
            origin += f" · curve {_bar(snap.curve_progress_pct)} {snap.curve_progress_pct:.0f}% (pre-grad)"
        lines.append(origin)

    lines.append(f"💰 MCAP {_usd(snap.market_cap_usd)} · Liq {_usd(snap.liquidity_usd)} "
                 f"· Age {_age(snap.age_minutes)}")

    holders = snap.holder_count if snap.holder_count is not None else "?"
    grow = " ↑" if (snap.holder_growth_1h or 0) > 0 else ""
    lines.append(f"👥 Holders {holders}{grow} · Top10 {_pct(snap.top10_supply_pct)}")

    accel = " ↑" if snap.volume_accelerating else ""
    bp = _buy_pressure(snap)
    vol = f"📊 Vol1h {_usd(snap.volume_1h)}{accel}"
    lines.append(f"{vol} · {bp}" if bp else vol)

    lines.append(f"🛡 {_safety_line(snap)}")

    smart = _smart_line(snap)
    if smart:
        lines.append(smart)

    _abbr = {"safety": "Safe", "distribution": "Dist", "momentum": "Mom", "discovery": "Disc"}
    breakdown = " · ".join(f"{_abbr.get(c.name, c.name[:4].capitalize())} {c.raw:.0f}"
                           for c in result.categories)
    lines.append(f"🎯 {breakdown}")

    if result.reasons:
        lines.append("💡 " + escape(", ".join(result.reasons[:5])))

    links = _links_row(snap)
    if links:
        lines.append(links)
    return "\n".join(lines)
