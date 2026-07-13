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
from typing import Optional

import aiohttp

from .bundle import BundleAnalyzer
from .config import Config
from .filters import quick_start_gate, safety_gate
from .models import AlertLevel, RiskTier, ScoreResult, TokenSnapshot
from .scoring import score_token
from .sources.chain import EvmChainClient
from .sources.dexscreener import DexScreenerClient
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
                await asyncio.wait(
                    [asyncio.create_task(self._stop.wait())],
                    timeout=self.cfg.runtime.poll_interval_seconds,
                )
        finally:
            await self._session.close()
            self.storage.close()

    def stop(self) -> None:
        self._stop.set()

    # -- one cycle -------------------------------------------------------

    async def run_once(self) -> list[ScoreResult]:
        assert self._session is not None
        dex = DexScreenerClient(self.cfg, session=self._session)
        pairs = await dex.fetch_new_pairs()
        log.info("discovered %d pairs", len(pairs))

        th = self.cfg.thresholds
        candidates = [p for p in pairs if quick_start_gate(p, th).passed]
        log.info("%d passed quick-start gate", len(candidates))

        tasks = [self._process(snap) for snap in candidates]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return [r for r in results if isinstance(r, ScoreResult)]

    async def _process(self, snap: TokenSnapshot) -> Optional[ScoreResult]:
        async with self._sem:
            await self._enrich(snap)

        strict = self.cfg.active_tier is RiskTier.MOMENTUM
        result = score_token(snap, self.cfg, strict_safety=strict)
        self.storage.mark_seen(snap)
        self.storage.record_score(snap, result)

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
        chain = EvmChainClient(self.cfg, session=self._session)
        bundle = BundleAnalyzer(self.cfg, session=self._session)
        smart = SmartMoneyClient(self.cfg, session=self._session)

        async def _safe(coro):
            try:
                return await coro
            except Exception as exc:
                log.debug("enrichment source failed: %s", exc)
                return None

        # Safety report first (needed by the gate); then parallel distribution/bundle/smart.
        report = await _safe(chain.assess(snap))
        if report is not None:
            snap.safety = report

        await asyncio.gather(
            _safe(chain.enrich_distribution(snap)),
            _safe(bundle.enrich(snap)),
            _safe(smart.active_wallets(snap)),
        )
