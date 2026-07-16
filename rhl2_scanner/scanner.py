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
from .sources.launchpad_curve import LaunchpadCurveListener
from .sources.safety import CompositeSafetySource
from .sources.smartmoney import SmartMoneyClient
from .storage import Storage
from .walletwatch import WalletWatcher, format_whale_html
from .alerting.formatter import format_early_launch_html
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
        # Bonding-curve launchpad listener (flap.sh) — catches tokens on the
        # curve, before they graduate to a DEX (i.e. before DexScreener sees them).
        self._curve_listener: Optional[LaunchpadCurveListener] = None
        self.paper = PaperTrader(cfg, self.storage) if cfg.runtime.paper_mode else None
        self._last_digest_ts: Optional[float] = None
        self._wallet_watcher = None  # bound to the shared session in run_once
        self._cmd_offset = None      # Telegram getUpdates offset (loaded from db)

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

    async def inspect(self, ca: str) -> str:
        """Trace one token through the full live pipeline and return a report.

        Shows discovery, enriched facts, gate results, score, and whether it
        would alert (gem/early) — so a specific 'missed' token can be diagnosed.
        """
        assert self._session is not None
        dex = DexScreenerClient(self.cfg, session=self._session)
        pairs = await dex.pairs_for_token(ca)
        snap = pairs[0] if pairs else TokenSnapshot(
            chain=self.cfg.chain.dexscreener_chain, pair_address="", token_address=ca)
        await self._enrich(snap)

        strict = self.cfg.active_tier is RiskTier.MOMENTUM
        result = score_token(snap, self.cfg, strict_safety=strict,
                             pragmatic=self.cfg.runtime.live_pragmatic_safety)
        th = self.cfg.thresholds
        qs = quick_start_gate(snap, th)
        s = snap.safety
        rc = self.cfg.runtime
        would_early = (
            rc.early_launch_enabled and not is_stock_token(snap)
            and snap.age_minutes is not None and snap.age_minutes <= rc.early_launch_max_age_minutes
            and (snap.liquidity_usd or 0) >= rc.early_launch_min_liquidity_usd
            and (result.safety_passed or not rc.early_launch_require_safety)
        )
        would_gem = result.level.value in ("strong", "watch")

        def yn(v):
            return "?" if v is None else ("Y" if v else "N")

        lines = [
            f"=== INSPECT {ca} ===",
            f"Discovery: {'DexScreener FOUND' if pairs else 'NOT on DexScreener (pre-graduation / unlisted / wrong chain)'}"
            + (f" — ${snap.symbol} \"{snap.name}\"" if pairs else ""),
            f"Stock token: {is_stock_token(snap)}",
            f"Market: price=${snap.price_usd} mcap=${snap.market_cap_usd} liq=${snap.liquidity_usd} "
            f"age={None if snap.age_minutes is None else round(snap.age_minutes)}m "
            f"vol1h=${snap.volume_1h} buys/sells1h={snap.buys_1h}/{snap.sells_1h}",
            f"Distribution: holders={snap.holder_count} top10={snap.top10_supply_pct}% top1={snap.top1_supply_pct}%",
            f"Safety: verified={yn(s.contract_verified)} mint_revoked={yn(s.mint_authority_revoked)} "
            f"freeze_revoked={yn(s.freeze_authority_revoked)} lp_burned={yn(s.lp_burned)} lp_locked={yn(s.lp_locked)} "
            f"honeypot={yn(s.is_honeypot)} buy_tax={s.buy_tax_pct} sell_tax={s.sell_tax_pct} "
            f"dev={s.dev_holdings_pct}% bundle={s.bundle_supply_pct}% sniper={s.sniper_cluster_pct}%",
            f"Quick-start gate: {'PASS' if qs.passed else 'FAIL — ' + '; '.join(qs.failures)}",
            f"Safety gate: {'PASS' if result.safety_passed else 'FAIL — ' + '; '.join(result.gate_failures)}",
            f"Score: {result.composite:.0f} ({result.level.value}) "
            f"[{' '.join(f'{c.name[:3]}={c.raw:.0f}' for c in result.categories)}]",
            f"WOULD ALERT: gem={'YES' if would_gem else 'no'} early={'YES' if would_early else 'no'}",
        ]
        return "\n".join(lines)

    async def _poll_commands(self) -> None:
        """Receive /zero /unzero /muted from the alert channel and act on them."""
        tg = self.cfg.telegram
        if not tg.bot_token or not tg.alert_chat_id or self._session is None:
            return
        from .tgtools import get_updates
        if self._cmd_offset is None:
            saved = self.storage.kv_get("cmd_offset")
            # None offset => getUpdates returns pending commands, which we DO
            # process (so a /command sent right after a deploy isn't skipped).
            self._cmd_offset = int(saved) if saved is not None else None
        try:
            updates, nxt = await get_updates(tg.bot_token, self._cmd_offset, self._session)
        except Exception:
            log.debug("command poll failed")
            return
        for u in updates:
            msg = u.get("message") or u.get("channel_post") or u.get("edited_channel_post") or {}
            chat = msg.get("chat") or {}
            text = (msg.get("text") or "").strip()
            if not text.startswith("/"):
                continue
            if str(chat.get("id")) != str(tg.alert_chat_id):
                log.info("ignoring command from chat %s (expected %s)", chat.get("id"), tg.alert_chat_id)
                continue
            log.info("command: %s", text)
            await self._handle_command(text)
        if nxt is not None and nxt != self._cmd_offset:
            self._cmd_offset = nxt
            self.storage.kv_set("cmd_offset", str(nxt))

    async def _handle_command(self, text: str) -> None:
        if not text.startswith("/"):
            return
        parts = text.split()
        cmd = parts[0][1:].split("@")[0].lower()   # strip leading / and @botname
        arg = parts[1].lower() if len(parts) > 1 else ""
        valid_ca = arg.startswith("0x") and len(arg) == 42

        if cmd == "zero":
            if not valid_ca:
                await self._send_html("Usage: <code>/zero 0x&lt;address&gt;</code>")
                return
            self.storage.mute_token(arg)
            await self._send_html(f"🔇 Muted <code>{arg}</code> — alerts off for this token.")
        elif cmd in ("unzero", "unmute"):
            if not valid_ca:
                await self._send_html("Usage: <code>/unzero 0x&lt;address&gt;</code>")
                return
            self.storage.unmute_token(arg)
            await self._send_html(f"🔊 Unmuted <code>{arg}</code>.")
        elif cmd == "muted":
            lst = self.storage.muted_tokens()
            body = "\n".join(f"<code>{t}</code>" for t in lst) if lst else "none"
            await self._send_html(f"🔇 Muted tokens ({len(lst)}):\n{body}")
        elif cmd == "inspect":
            if not valid_ca:
                await self._send_html("Usage: <code>/inspect 0x&lt;address&gt;</code>")
                return
            try:
                from html import escape as _esc
                report = await self.inspect(arg)
                await self._send_html("<pre>" + _esc(report) + "</pre>")
            except Exception as exc:
                await self._send_html("inspect failed: " + __import__("html").escape(str(exc)))

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
            if self.storage.is_muted(ev.token_address):
                continue   # /zero'd token
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
        if self._curve_listener is None:
            self._curve_listener = LaunchpadCurveListener(self.cfg, session=self._session)

        # Three discovery streams: DexScreener (indexed), DEX factory logs
        # (earliest pool), and bonding-curve launchpad logs (earliest of all —
        # flap tokens before they graduate to a pool DexScreener can see).
        dex_pairs, fresh_pairs, curve_stubs = await asyncio.gather(
            dex.fetch_new_pairs(),
            self._listener.poll_new_pairs(),
            self._curve_listener.poll_new_launches(),
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

        # Curve stubs have no pool yet, so they key on the token address. If a
        # DexScreener/pool pair already covers that token (it graduated), keep the
        # richer pair but preserve the launchpad label; otherwise backfill any
        # market data that exists and carry the stub forward for scoring.
        tokens_in_merged = {s.token_address.lower() for s in merged.values()}
        for stub in curve_stubs:
            tok = stub.token_address.lower()
            if tok in tokens_in_merged:
                for s in merged.values():
                    if s.token_address.lower() == tok and not s.launchpad:
                        s.launchpad = stub.launchpad
                continue
            filled = await dex.pairs_for_token(stub.token_address)
            if filled:
                best = filled[0]
                best.launchpad = best.launchpad or stub.launchpad
                merged[best.pair_address.lower()] = best
            else:
                merged["curve:" + tok] = stub   # pre-graduation, no pool yet
        pairs = list(merged.values())
        log.info(
            "discovered %d pairs (dexscreener=%d, factory=%d, curve=%d)",
            len(pairs), len(dex_pairs), len(fresh_pairs), len(curve_stubs),
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

        # Handle inbound Telegram commands (/zero, /unzero, /muted).
        await self._poll_commands()

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

        if self.storage.is_muted(snap.token_address):
            return result   # /zero'd — suppress all alerts for this token

        if result.level in (AlertLevel.STRONG, AlertLevel.WATCH):
            if self.storage.in_cooldown(snap.pair_address, self.cfg.runtime.realert_cooldown_seconds):
                log.debug("%s in cooldown, skipping alert", snap.symbol)
            else:
                await self.notifier.send(snap, result)
                self.storage.record_alert(snap, result)
                log.info("ALERT %s %s score=%.0f", result.level.value, snap.symbol, result.composite)
        elif self._is_early_launch(snap, result):
            if not self.storage.in_cooldown(snap.pair_address, self.cfg.runtime.realert_cooldown_seconds):
                await self._send_html(format_early_launch_html(snap, result))
                self.storage.record_alert(snap, result)
                log.info("EARLY %s age=%.0fm liq=%s", snap.symbol, snap.age_minutes or 0, snap.liquidity_usd)
        else:
            reasons = ", ".join(result.gate_failures[:3]) if result.gate_failures else "low score"
            log.debug("skip %s (%s)", snap.symbol, reasons)
        return result

    def _is_early_launch(self, snap: TokenSnapshot, result: ScoreResult) -> bool:
        """A fresh, SAFE launch worth an early-entry ping even below the score band.

        This is the "catch runners pre/just-after graduation" path: young tokens
        can't reach the maturity-based score, so we alert on safety + freshness +
        a real (graduated) pool instead. Tokenized stocks excluded.
        """
        rc = self.cfg.runtime
        if not rc.early_launch_enabled or self.cfg.runtime.dry_run:
            return False
        if snap.age_minutes is None or snap.age_minutes > rc.early_launch_max_age_minutes:
            return False
        if (snap.liquidity_usd or 0) < rc.early_launch_min_liquidity_usd:
            return False
        if is_stock_token(snap):
            return False
        if rc.early_launch_require_safety and not result.safety_passed:
            return False
        return True

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
