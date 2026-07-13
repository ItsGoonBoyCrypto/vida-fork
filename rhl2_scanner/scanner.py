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
from .filters import quick_start_gate, safety_gate
from .models import AlertLevel, RiskTier, ScoreResult, TokenSnapshot
from .paper import PaperTrader
from .scoring import score_token
from .sources.chain import EvmChainClient
from .sources.dexscreener import DexScreenerClient
from .sources.poollistener import PoolListener
from .sources.safety import CompositeSafetySource
from .sources.smartmoney import SmartMoneyClient
from .storage import Storage
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
        self._last_digest_day: Optional[tuple] = None

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

    async def _maybe_send_digest(self) -> None:
        """Post the nightly calibration digest once per day, at/after the hour."""
        rc = self.cfg.runtime
        if not rc.paper_digest_enabled:
            return
        tm = time.gmtime()
        today = (tm.tm_year, tm.tm_yday)
        if self._last_digest_day == today or tm.tm_hour < rc.paper_digest_hour_utc:
            return
        if not self.storage.all_paper_trades():
            return  # nothing to report yet
        try:
            from .paper import send_digest
            ok, detail = await send_digest(self.cfg, self.storage, self._session)
            self._last_digest_day = today  # mark sent regardless, avoid retry storms
            log.info("daily digest: %s", detail)
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

        th = self.cfg.thresholds
        candidates = [p for p in pairs if quick_start_gate(p, th).passed]
        log.info("%d passed quick-start gate", len(candidates))

        tasks = [self._process(snap) for snap in candidates]
        results = await asyncio.gather(*tasks, return_exceptions=True)

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
        result = score_token(snap, self.cfg, strict_safety=strict)
        self.storage.mark_seen(snap)
        self.storage.record_score(snap, result)

        # Paper mode records would-be entries (incl. below the alert band) for
        # calibration; it does not gate on cooldown so every candidate is logged.
        if self.paper is not None:
            self.paper.record(snap, result)

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
