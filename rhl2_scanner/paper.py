"""Paper trading / live calibration.

Records every would-be entry (any safety-passing candidate at/above
``paper_record_floor`` — including sub-alert-band ones) and re-prices the open
positions at configured checkpoints (default 1h / 6h / 24h). Each position
tracks realized multiple, peak (max) and trough (min) multiple.

Feed weeks of this into ``report()`` to answer the questions that actually tune
the bot on *real* data:
  * At what composite score does precision (fraction that ≥2x) justify alerting?
  * Are STRONG alerts really better than WATCH?
  * Which category correlates with winners on this chain?

No capital at risk: entries are logged, not executed. Prices come from the same
DexScreener client the live scanner uses.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Optional

from .config import Config
from .models import AlertLevel, ScoreResult, TokenSnapshot
from .sources.dexscreener import DexScreenerClient
from .storage import Storage

log = logging.getLogger("rhl2.paper")


@dataclass
class PaperTrader:
    cfg: Config
    storage: Storage

    # -- recording -------------------------------------------------------

    def record(self, snap: TokenSnapshot, result: ScoreResult) -> Optional[int]:
        """Log a would-be entry if it passed safety and clears the record floor."""
        if not result.safety_passed:
            return None
        if result.composite < self.cfg.runtime.paper_record_floor:
            return None
        if snap.price_usd is None:
            return None  # need an entry price to compute realized multiples
        trade_id = self.storage.open_paper_trade(snap, result)
        if trade_id is not None:
            log.info(
                "paper entry #%s %s score=%.0f level=%s price=%s",
                trade_id, snap.symbol, result.composite, result.level.value, snap.price_usd,
            )
        return trade_id

    def record_alerted(self, snap: TokenSnapshot, result: ScoreResult) -> Optional[int]:
        """Record an ACTUAL alert for live performance tracking (no score floor).

        Unlike ``record`` (calibration: every safety-passing candidate over the
        floor), this logs only tokens we truly alerted on, so ``report`` becomes
        'how did my alerts do'. Needs an entry price; deduped per pair.
        """
        if snap.price_usd is None or not snap.pair_address:
            return None
        trade_id = self.storage.open_paper_trade(snap, result)
        if trade_id is not None:
            log.info("perf entry #%s %s score=%.0f level=%s price=%s",
                     trade_id, snap.symbol, result.composite, result.level.value, snap.price_usd)
        return trade_id

    # -- settling --------------------------------------------------------

    async def settle_open(self, dex: DexScreenerClient) -> int:
        """Re-price open positions and record any newly-reached checkpoints."""
        open_trades = self.storage.open_paper_trades()
        if not open_trades:
            return 0
        checkpoints_hours = sorted(self.cfg.runtime.paper_checkpoint_hours)
        final_hour = checkpoints_hours[-1] if checkpoints_hours else 24
        now = time.time()
        settled = 0

        for row in open_trades[: self.cfg.runtime.paper_settle_batch]:
            price = await self._price_for(dex, row["pair_address"], row["token_address"])
            entry_price = row["entry_price"]
            if not entry_price:
                continue

            checkpoints = json.loads(row["checkpoints_json"] or "{}")
            max_mult = row["max_mult"] or 1.0
            min_mult = row["min_mult"] or 1.0
            last_price = row["last_price"]

            if price is not None and price > 0:
                mult = price / entry_price
                max_mult = max(max_mult, mult)
                min_mult = min(min_mult, mult)
                last_price = price
                age_h = (now - row["entry_ts"]) / 3600.0
                for h in checkpoints_hours:
                    key = f"{h}h"
                    if key not in checkpoints and age_h >= h:
                        checkpoints[key] = {"ts": now, "price": price, "mult": round(mult, 4)}

            is_settled = (now - row["entry_ts"]) >= final_hour * 3600
            self.storage.update_paper_trade(
                row["id"], last_price, checkpoints, round(max_mult, 4),
                round(min_mult, 4), is_settled,
            )
            if is_settled:
                settled += 1
        return settled

    async def _price_for(self, dex: DexScreenerClient, pair: str, token: str) -> Optional[float]:
        stub = TokenSnapshot(chain=self.cfg.chain.dexscreener_chain, pair_address=pair, token_address=token)
        refreshed = await dex.refresh(stub)
        if refreshed.price_usd is not None:
            return refreshed.price_usd
        for p in await dex.pairs_for_token(token):
            if p.pair_address.lower() == pair.lower() and p.price_usd is not None:
                return p.price_usd
        return None

    # -- reporting -------------------------------------------------------

    def report(self, win_multiple: float = 2.0) -> dict:
        """Aggregate calibration stats over all recorded paper trades."""
        rows = self.storage.all_paper_trades()
        bands = [
            (">=75 (strong)", 75, 200),
            ("60-74 (watch)", 60, 75),
            ("45-59 (below band)", 45, 60),
        ]
        report: dict = {
            "total_recorded": len(rows),
            "settled": sum(1 for r in rows if r["settled"]),
            "win_multiple": win_multiple,
            "bands": [],
            "by_level": {},
        }

        def _stats(subset) -> dict:
            n = len(subset)
            if not n:
                return {"n": 0}
            max_mults = [r["max_mult"] or 1.0 for r in subset]
            wins = sum(1 for m in max_mults if m >= win_multiple)
            rugs = sum(1 for r in subset if (r["min_mult"] or 1.0) <= 0.5)
            return {
                "n": n,
                "hit_rate": round(wins / n, 3),          # fraction that ever ≥ win_multiple
                "rug_rate": round(rugs / n, 3),          # fraction that dropped ≥50%
                "avg_peak_mult": round(sum(max_mults) / n, 2),
                "median_peak_mult": round(sorted(max_mults)[n // 2], 2),
                "best_mult": round(max(max_mults), 2),
            }

        for label, lo, hi in bands:
            subset = [r for r in rows if lo <= (r["score"] or 0) < hi]
            report["bands"].append({"band": label, **_stats(subset)})

        for level in (AlertLevel.STRONG.value, AlertLevel.WATCH.value, AlertLevel.SKIP.value):
            subset = [r for r in rows if r["level"] == level]
            report["by_level"][level] = _stats(subset)

        return report


def format_digest_html(rep: dict, win_multiple: float = 2.0,
                       title: str = "Daily Calibration", noun: str = "recorded") -> str:
    """Telegram-HTML calibration/performance digest (compact, mobile-friendly)."""
    from html import escape

    lines = [
        f"📊 <b>RH L2 Scanner — {escape(title)}</b>",
        f"{noun} <b>{rep['total_recorded']}</b> · settled <b>{rep['settled']}</b> "
        f"· win = peak ≥ {win_multiple:g}x",
        "",
        "<b>By score band</b>",
    ]
    for b in rep["bands"]:
        if b.get("n"):
            lines.append(
                escape(
                    f"• {b['band']}: n={b['n']} · hit {b['hit_rate']:.0%} · "
                    f"rug {b['rug_rate']:.0%} · med peak {b['median_peak_mult']}x · "
                    f"best {b['best_mult']}x"
                )
            )
        else:
            lines.append(escape(f"• {b['band']}: no samples yet"))

    level_lines = []
    for level, s in rep["by_level"].items():
        if s.get("n"):
            level_lines.append(
                escape(f"• {level}: n={s['n']} · hit {s['hit_rate']:.0%} · rug {s['rug_rate']:.0%}")
            )
    if level_lines:
        lines.append("")
        lines.append("<b>By alert level</b>")
        lines.extend(level_lines)

    lines.append("")
    lines.append("<i>Signals, not advice. Tune thresholds off the hit/rug split.</i>")
    return "\n".join(lines)


async def send_digest(cfg: Config, storage: Storage, session=None) -> tuple[bool, str]:
    """Build the calibration digest and post it to Telegram (or stdout)."""
    trader = PaperTrader(cfg, storage)
    win = cfg.runtime.paper_digest_win_multiple
    rep = trader.report(win_multiple=win)
    # In live mode the tracked rows are real alerts, not calibration candidates.
    live = not cfg.runtime.paper_mode
    title = "Alert Performance" if live else "Daily Calibration"
    noun = "alerts" if live else "recorded"
    token = cfg.telegram.bot_token
    chat = cfg.telegram.alert_chat_id
    if token and chat:
        from .tgtools import send_message
        ok, detail, _ = await send_message(
            token, chat, format_digest_html(rep, win, title=title, noun=noun), session)
        return ok, detail
    print(format_report(rep), flush=True)
    return False, "telegram not configured — printed digest to stdout"


def format_report(rep: dict) -> str:
    lines = [
        "=== Paper-trading calibration ===",
        f"recorded={rep['total_recorded']} settled={rep['settled']} "
        f"(win = peak ≥ {rep['win_multiple']}x)",
        "",
        "By score band:",
    ]
    for b in rep["bands"]:
        if b.get("n"):
            lines.append(
                f"  {b['band']:<22} n={b['n']:<4} hit={b['hit_rate']:.0%} "
                f"rug={b['rug_rate']:.0%} avg_peak={b['avg_peak_mult']}x "
                f"med_peak={b['median_peak_mult']}x best={b['best_mult']}x"
            )
        else:
            lines.append(f"  {b['band']:<22} (no samples yet)")
    lines.append("")
    lines.append("By alert level:")
    for level, s in rep["by_level"].items():
        if s.get("n"):
            lines.append(
                f"  {level:<8} n={s['n']:<4} hit={s['hit_rate']:.0%} "
                f"rug={s['rug_rate']:.0%} avg_peak={s['avg_peak_mult']}x"
            )
    return "\n".join(lines)
