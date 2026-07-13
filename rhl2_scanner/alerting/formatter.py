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
        f"Token: ${snap.symbol or '???'} (CA: {snap.token_address})",
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


def to_telegram_html(snap: TokenSnapshot, result: ScoreResult) -> str:
    lines = [escape(line) for line in build_lines(snap, result)]
    link_bits = []
    if snap.dexscreener_url:
        link_bits.append(f'<a href="{escape(snap.dexscreener_url)}">DexScreener</a>')
    if snap.chart_url:
        link_bits.append(f'<a href="{escape(snap.chart_url)}">Chart</a>')
    if snap.socials.get("telegram"):
        link_bits.append(f'<a href="{escape(snap.socials["telegram"])}">TG</a>')
    if link_bits:
        lines.append("Links: " + " | ".join(link_bits))
    # Bold the first line (header + score).
    if lines:
        lines[0] = f"<b>{lines[0]}</b>"
    return "\n".join(lines)
