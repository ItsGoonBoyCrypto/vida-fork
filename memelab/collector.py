"""The collection loop — runs continuously, builds the dataset.

This is the engine room of "how we collect the data": every cycle it discovers
new tokens on each enabled chain and re-snapshots the ones still inside the
tracking window, writing point-in-time rows to the Store. A separate, slower
cadence relabels aged tokens so outcomes stay current.

Design goals:
  · ALL FOUR CHAINS from day one — DexScreener market data needs no per-chain
    RPC, so a chain collects the moment its adapter.discover() (or the
    DexScreener discovery fallback) yields tokens.
  · Dense-early sampling — new tokens snapshot every `hot_interval` (the first
    `hot_window_min`), then every `warm_interval` out to `track_hours`. The
    predictive signal lives in the first minutes, so sample them tightly.
  · Failure-isolated per chain/token — one chain's RPC hiccup never stalls the rest.

Run:  python -m memelab.collect   (see api/__main__ wiring — TODO)
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from .models import Chain, TokenSnapshot
from .storage import Store

log = logging.getLogger("memelab.collector")


@dataclass
class CollectorConfig:
    chains: list                       # [Chain, …] to collect
    cycle_seconds: float = 30.0        # main loop tick
    hot_window_min: float = 60.0       # dense sampling for a token's first hour
    hot_interval_min: float = 3.0      # …every 3 min while hot
    warm_interval_min: float = 30.0    # …every 30 min after, out to track_hours
    track_hours: float = 72.0          # stop snapshotting a token after this
    relabel_interval_min: float = 60.0
    win_multiple: float = 3.0
    # Alerting + autonomous backtest
    alert_enabled: bool = True
    min_alert_score: float = 65.0      # floor; screener also gates on signature precision
    backtest_interval_min: float = 360.0  # re-derive the signature every 6h


class Collector:
    def __init__(self, cfg: CollectorConfig, store: Store, adapters: dict, feed,
                 alerter=None, smart_money=None):
        self.cfg = cfg
        self.store = store
        self.adapters = adapters        # {Chain: ChainAdapter}
        self.feed = feed                # DexScreenerFeed
        self.smart_money = smart_money  # SmartMoney | None
        from .alerting import TelegramAlerter
        from .screener.engine import Screener
        self.alerter = alerter or TelegramAlerter()
        # Seed the bootstrap prior so screening works from day one; the backtest
        # loop replaces it once a data-derived signature validates.
        if store.active_signature() is None:
            from .bootstrap import default_signature
            from .models import signature_to_json
            store.save_signature(signature_to_json(default_signature(cfg.chains)))
        self.screener = Screener(store)
        self.screener.reload_signature()
        self._last_relabel = 0.0
        self._last_backtest = 0.0

    async def run_forever(self) -> None:
        while True:
            try:
                await self.cycle()
            except Exception:
                log.exception("collector cycle failed")
            await asyncio.sleep(self.cfg.cycle_seconds)

    async def cycle(self) -> None:
        for chain in self.cfg.chains:
            try:
                await self._collect_chain(chain)
            except Exception:
                log.exception("collect %s failed", chain.value)
        await self._maybe_relabel()
        await self._maybe_backtest()

    async def _collect_chain(self, chain: Chain) -> None:
        adapter = self.adapters.get(chain)
        # 1) DISCOVER new tokens (source-level via adapter; falls back to the
        #    DexScreener discovery feed for chains without a source listener).
        fresh: list = []
        if adapter is not None:
            fresh += await adapter.discover()
        fresh += await self.feed.new_pairs(chain)
        for snap in fresh:
            snap.ts = snap.ts or time.time()
            self.store.record_snapshot(snap)   # entry anchor

        # 2) RE-SNAPSHOT tokens still inside the tracking window, on cadence.
        for row in self.store.tracked_tokens(chain, max_age_hours=self.cfg.track_hours):
            if not self._due(row):
                continue
            snap = await self.feed.market_for(chain, row["token_address"])
            if snap is None:
                continue                       # not indexed yet (still on curve) — skip
            # periodically refresh safety + smart-money (warm cadence, not every tick)
            if self._due(row, safety=True):
                if adapter is not None:
                    try:
                        await adapter.enrich_safety(snap)
                    except Exception:
                        log.debug("enrich %s failed", row["token_address"], exc_info=True)
                if self.smart_money is not None:
                    try:
                        await self.smart_money.annotate(snap)
                    except Exception:
                        log.debug("smart-money %s failed", row["token_address"], exc_info=True)
            snap.ts = time.time()
            self.store.record_snapshot(snap)
            await self._maybe_alert(chain, row["token_address"])

    def _due(self, row, safety: bool = False) -> bool:
        """Cadence gate: dense while hot, sparse while warm; safety even sparser."""
        now = time.time()
        age_min = (now - row["first_seen_ts"]) / 60.0
        since_min = (now - (row["last_snapshot_ts"] or 0)) / 60.0
        if safety:
            return since_min >= self.cfg.warm_interval_min      # safety on the warm cadence
        interval = (self.cfg.hot_interval_min if age_min <= self.cfg.hot_window_min
                    else self.cfg.warm_interval_min)
        return since_min >= interval

    async def _maybe_alert(self, chain: Chain, token_address: str) -> None:
        """Screen the token against the signature; alert once if it scores high."""
        if not self.cfg.alert_enabled or not self.screener.ready():
            return
        ts = self.store.time_series(chain, token_address)
        if not ts.snapshots:
            return
        scr = self.screener.screen(chain, token_address, ts.snapshots)
        floor = max(self.cfg.min_alert_score, self.screener.min_confident_score())
        if scr.score < floor:
            return
        if not self.store.screen_alert_is_new(chain, token_address, scr.score):
            return
        from .alerting import format_screen_html
        prec = self.screener._sig.precision if self.screener._sig else None
        await self.alerter.send(format_screen_html(scr, prec))
        log.info("ALERT %s $%s score=%.0f", chain.value, scr.snapshot.symbol, scr.score)

    async def _maybe_backtest(self) -> None:
        """Periodically re-derive the signature and reload it into the screener."""
        now = time.time()
        if (now - self._last_backtest) < self.cfg.backtest_interval_min * 60.0:
            return
        self._last_backtest = now
        from .backtest.engine import run_backtest
        try:
            sig = run_backtest(self.store, win_multiple=self.cfg.win_multiple)
            self.screener.reload_signature()
            log.info("backtest: trained_on=%d precision=%s — %s",
                     sig.trained_on, sig.precision, sig.notes)
        except Exception:
            log.exception("backtest failed")

    async def _maybe_relabel(self) -> None:
        now = time.time()
        if (now - self._last_relabel) < self.cfg.relabel_interval_min * 60.0:
            return
        self._last_relabel = now
        from .backtest.labeler import relabel_all
        try:
            counts = relabel_all(self.store, win_multiple=self.cfg.win_multiple)
            log.info("relabel: %s", counts)
        except Exception:
            log.exception("relabel failed")
        # Harvest confirmed winners' early buyers into the smart-money set.
        if self.smart_money is not None:
            for chain, token in self.store.winner_tokens():
                try:
                    await self.smart_money.harvest_winner(chain, token)
                except Exception:
                    log.debug("harvest %s failed", token, exc_info=True)
