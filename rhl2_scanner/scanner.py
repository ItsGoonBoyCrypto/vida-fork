"""Main async orchestration loop.

Pipeline per cycle:
  1. discover new pairs (DexScreener + optional chain listener)
  2. quick-start gate (cheap pre-screen)   -> drop obvious non-candidates
  3. enrich (safety, distribution, bundle, smart money) with bounded concurrency
  4. full safety gate + composite score
  5. alert (respecting re-alert cooldown) + persist

Enrichment sources are optional: if RH L2 RPC/explorer aren't configured they
return unknown facts, which the strict gate treats as not-tradeable. That's the
safe default — better to miss than to alert on an unverified token.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

import aiohttp

from .bundle import BundleAnalyzer
from .config import Config
from .filters import is_stock_token, quick_start_gate, safety_gate
from .models import AlertLevel, RiskTier, ScoreResult, TokenSnapshot
from .paper import PaperTrader
from .scoring import score_token
from .sources.chain import EvmChainClient
from .sources.dexscreener import DexScreenerClient
from .sources.poollistener import PoolListener
from .sources.safety import CompositeSafetySource
from .sources.smartmoney import SmartMoneyClient
from .storage import Storage
from .walletwatch import WalletWatcher, format_whale_html
from .alerting.telegram import TelegramNotifier

log = logging.getLogger("rhl2.scanner")


class Scanner:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.storage = Storage(cfg.runtime.db_path)
        self.notifier = TelegramNotifier(cfg)
        self._sem = asyncio.Semaphore(cfg.runtime.max_concurrent_enrichments)
        self._session: Optional[aiohttp.ClientSession] = None
        self._stop = asyncio.Event()
        # PoolListener is stateful (tracks last scanned block) so it persists
        # across cycles; bound to the shared session in run_forever/scan-once.
        self._listener: Optional[PoolListener] = None
        self.paper = PaperTrader(cfg, self.storage) if cfg.runtime.paper_mode else None
        self._last_digest_ts: Optional[float] = None
        self._wallet_watcher = None  # bound to the shared session in run_once

    # -- lifecycle -------------------------------------------------------

    async def run_forever(self) -> None:
        timeout = aiohttp.ClientTimeout(total=self.cfg.runtime.request_timeout_seconds)
        self._session = aiohttp.ClientSession(timeout=timeout)
        log.info(
            "scanner up | chain=%s tier=%s dry_run=%s",
            self.cfg.chain.dexscreener_chain,
            self.cfg.active_tier.value,
            self.cfg.runtime.dry_run,
        )
        await self._send_startup_message()
        try:
            while not self._stop.is_set():
                try:
                    await self.run_once()
                except Exception:  # never let one cycle kill the loop
                    log.exception("scan cycle failed")
                await self._maybe_send_digest()
                await asyncio.wait(
                    [asyncio.create_task(self._stop.wait())],
                    timeout=self.cfg.runtime.poll_interval_seconds,
                )
        finally:
            await self._session.close()
            self.storage.close()

    def stop(self) -> None:
        self._stop.set()

    async def _send_html(self, text: str) -> None:
        """Send an HTML message to the alert channel (or stdout in dry-run)."""
        tg = self.cfg.telegram
        if tg.bot_token and tg.alert_chat_id:
            from .tgtools import send_message
            try:
                await send_message(tg.bot_token, tg.alert_chat_id, text, self._session)
                return
            except Exception:
                log.exception("send failed")
        print("\n" + text + "\n", flush=True)

    async def _poll_wallets(self) -> None:
        if not self.cfg.wallet_watch.enabled or not self.cfg.wallet_watch.wallets:
            return
        if self._wallet_watcher is None:
            self._wallet_watcher = WalletWatcher(self.cfg, self.storage, session=self._session)
        try:
            events = await self._wallet_watcher.poll()
        except Exception:
            log.exception("wallet watch poll failed")
            return
        for ev in events:
            await self._send_html(format_whale_html(ev))
            log.info("WHALE %s %s $%s by %s", ev.side, ev.symbol, ev.usd, ev.label)

    async def _send_startup_message(self) -> None:
        """Post a 'scanner online' message on boot (also a Telegram wiring test)."""
        rc, tg = self.cfg.runtime, self.cfg.telegram
        if not (rc.send_startup_message and tg.bot_token and tg.alert_chat_id):
            return
        mode = "calibration (paper)" if rc.dry_run else "LIVE alerts"
        text = (
            "🟢 <b>RH L2 Scanner online</b>\n"
            f"Chain: robinhood (4663) · tier: {self.cfg.active_tier.value} · mode: {mode}\n"
            "Watching for early gems…"
        )
        try:
            from .tgtools import send_message
            ok, detail = await send_message(tg.bot_token, tg.alert_chat_id, text, self._session)
            log.info("startup message: %s", detail)
        except Exception:
            log.exception("startup message failed")

    async def _maybe_send_digest(self) -> None:
        """Post the calibration digest every paper_digest_interval_hours."""
        rc = self.cfg.runtime
        if not rc.paper_digest_enabled:
            return
        now = time.time()
        interval = max(0.1, rc.paper_digest_interval_hours) * 3600.0
        if self._last_digest_ts is not None and (now - self._last_digest_ts) < interval:
            return
        if not self.storage.all_paper_trades():
            return  # nothing to report yet — don't start the clock until there's data
        try:
            from .paper import send_digest
            ok, detail = await send_digest(self.cfg, self.storage, self._session)
            self._last_digest_ts = now  # mark sent regardless, avoid retry storms
            log.info("digest: %s", detail)
        except Exception:
            log.exception("digest send failed")

    # -- one cycle -------------------------------------------------------

    async def run_once(self) -> list[ScoreResult]:
        assert self._session is not None
        dex = DexScreenerClient(self.cfg, session=self._session)
        if self._listener is None:
            self._listener = PoolListener(self.cfg, session=self._session)

        # Two discovery streams: DexScreener (indexed) + factory logs (earliest).
        dex_pairs, fresh_pairs = await asyncio.gather(
            dex.fetch_new_pairs(),
            self._listener.poll_new_pairs(),
        )

        # Merge, DexScreener winning on overlap (it carries market data). For
        # factory-only stubs, try to backfill market data from DexScreener.
        merged: dict[str, TokenSnapshot] = {}
        for p in fresh_pairs:
            merged[p.pair_address.lower()] = p
        for p in dex_pairs:
            merged[p.pair_address.lower()] = p
        for key, snap in list(merged.items()):
            if snap.liquidity_usd is None and snap.market_cap_usd is None:
                for filled in await dex.pairs_for_token(snap.token_address):
                    if filled.pair_address.lower() == key:
                        merged[key] = filled
                        break
        pairs = list(merged.values())
        log.info(
            "discovered %d pairs (dexscreener=%d, factory=%d)",
            len(pairs), len(dex_pairs), len(fresh_pairs),
        )

        # Drop tokenized stocks (MU/TSLA/… "• Robinhood Token") — not memecoins.
        if self.cfg.chain.exclude_stock_tokens:
            before = len(pairs)
            pairs = [p for p in pairs if not is_stock_token(p)]
            if before != len(pairs):
                log.info("excluded %d tokenized stocks", before - len(pairs))

        th = self.cfg.thresholds
        candidates = [p for p in pairs if quick_start_gate(p, th).passed]
        log.info("%d passed quick-start gate", len(candidates))

        tasks = [self._process(snap) for snap in candidates]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Visibility: why did candidates NOT alert this cycle? Aggregate the
        # gate-failure reasons + low-score count, and show the BEST candidate's
        # full score breakdown so tuning is precise (gate vs scoring vs data).
        pairs_scored = [(candidates[i], r) for i, r in enumerate(results) if isinstance(r, ScoreResult)]
        scored = [r for _, r in pairs_scored]
        alerted = sum(1 for r in scored if r.level in (AlertLevel.STRONG, AlertLevel.WATCH))
        if scored and alerted == 0:
            from collections import Counter
            reasons: Counter = Counter()
            for r in scored:
                if not r.safety_passed:
                    for f in r.gate_failures:
                        reasons[f.split(" (")[0]] += 1   # collapse "(unconfirmed)"
                elif r.level == AlertLevel.SKIP:
                    reasons["low score (<%d)" % self.cfg.thresholds.watch_alert_score] += 1
            top = ", ".join(f"{k} x{v}" for k, v in reasons.most_common(6))
            log.info("no alerts | top skip reasons: %s", top or "none")

            # Best near-miss, with per-category breakdown + missing data.
            snap, best = max(pairs_scored, key=lambda sr: sr[1].composite)
            cats = " ".join(f"{c.name[:3]}={c.raw:.0f}" for c in best.categories)
            missing = [k for k, v in (
                ("holders", snap.holder_count), ("top10", snap.top10_supply_pct),
                ("bundle", snap.safety.bundle_supply_pct), ("verified", snap.safety.contract_verified),
                ("lp", snap.safety.lp_burned if snap.safety.lp_burned is not None else snap.safety.lp_locked),
            ) if v is None]
            log.info(
                "best: $%s score=%.0f [%s] safety_passed=%s%s%s",
                snap.symbol or "?", best.composite, cats, best.safety_passed,
                (" gate=" + "; ".join(best.gate_failures[:3])) if best.gate_failures else "",
                (" missing_data=" + ",".join(missing)) if missing else "",
            )

        # Whale-wallet activity alerts (independent of the gem scan).
        await self._poll_wallets()

        # Re-price open paper positions and record any reached checkpoints.
        if self.paper is not None:
            try:
                settled = await self.paper.settle_open(dex)
                if settled:
                    log.info("paper: settled %d positions this cycle", settled)
            except Exception:
                log.exception("paper settle failed")

        return [r for r in results if isinstance(r, ScoreResult)]

    async def _process(self, snap: TokenSnapshot) -> Optional[ScoreResult]:
        async with self._sem:
            await self._enrich(snap)

        strict = self.cfg.active_tier is RiskTier.MOMENTUM
        # Live (non-dry-run) alerts may use pragmatic safety: strict on every
        # confirmable metric, tolerant of an unconfirmed honeypot/tax.
        pragmatic = (not self.cfg.runtime.dry_run) and self.cfg.runtime.live_pragmatic_safety
        result = score_token(snap, self.cfg, strict_safety=strict, pragmatic=pragmatic)
        self.storage.mark_seen(snap)
        self.storage.record_score(snap, result)

        # Paper mode records would-be entries (incl. below the alert band) for
        # calibration; it does not gate on cooldown so every candidate is logged.
        # Record on the LENIENT safety pass so that tokens whose safety is merely
        # *unconfirmed* (common on a new chain without 3rd-party coverage) still
        # generate calibration data — confirmed-bad (honeypot/high-tax) is still
        # excluded. Live alerts (below) keep using the strict, tier-driven result.
        if self.paper is not None:
            paper_result = result if not strict else score_token(snap, self.cfg, strict_safety=False)
            self.paper.record(snap, paper_result)

        if result.level in (AlertLevel.STRONG, AlertLevel.WATCH):
            if self.storage.in_cooldown(snap.pair_address, self.cfg.runtime.realert_cooldown_seconds):
                log.debug("%s in cooldown, skipping alert", snap.symbol)
            else:
                await self.notifier.send(snap, result)
                self.storage.record_alert(snap, result)
                log.info("ALERT %s %s score=%.0f", result.level.value, snap.symbol, result.composite)
        else:
            reasons = ", ".join(result.gate_failures[:3]) if result.gate_failures else "low score"
            log.debug("skip %s (%s)", snap.symbol, reasons)
        return result

    async def _enrich(self, snap: TokenSnapshot) -> None:
        """Attach safety, distribution, bundle, and smart-money facts.

        Each source is independent and failure-isolated; a source that isn't
        configured simply leaves its facts unknown.
        """
        assert self._session is not None
        safety = CompositeSafetySource(self.cfg, session=self._session)
        chain = EvmChainClient(self.cfg, session=self._session)
        bundle = BundleAnalyzer(self.cfg, session=self._session)
        smart = SmartMoneyClient(self.cfg, session=self._session)

        async def _safe(coro):
            try:
                return await coro
            except Exception as exc:
                log.debug("enrichment source failed: %s", exc)
                return None

        # Composite safety (GoPlus + on-chain + honeypot sim) merged conservatively.
        # Bundle analysis also writes into snap.safety, so run it first, then
        # overlay the merged report while preserving bundle fields.
        await _safe(bundle.enrich(snap))
        report = await _safe(safety.assess(snap))
        if report is not None:
            # keep bundle facts already computed
            report.bundle_supply_pct = report.bundle_supply_pct or snap.safety.bundle_supply_pct
            report.sniper_cluster_pct = report.sniper_cluster_pct or snap.safety.sniper_cluster_pct
            snap.safety = report

        await asyncio.gather(
            _safe(chain.enrich_distribution(snap)),
            _safe(smart.active_wallets(snap)),
        )
