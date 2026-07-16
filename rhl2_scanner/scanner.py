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
from .filters import (
    _norm_symbol,
    is_blocked_symbol,
    is_stock_token,
    quick_start_gate,
    safety_gate,
)
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
from .walletwatch import WalletWatcher, format_cluster_html, format_whale_html
from .alerting.formatter import format_early_launch_html
from .alerting.telegram import TelegramNotifier

log = logging.getLogger("rhl2.scanner")


def _blocker_key(failure: str) -> str:
    """Collapse a gate-failure string to a stable key for aggregation.

    e.g. 'liquidity $3,200 < $5,000' -> 'liquidity', 'top10 62.0% > skip 60%'
    -> 'top10', 'mint authority not revoked' -> 'mint authority not revoked'.
    """
    numeric_heads = {
        "liquidity", "mcap", "age", "top10", "top1", "bundled", "dev",
        "buy", "sell", "sniper", "risk", "holders",
    }
    head = failure.split()[0] if failure else failure
    if head in numeric_heads:
        # keep a slightly longer key for tax to distinguish buy/sell
        if head in ("buy", "sell"):
            return f"{head} tax"
        return head
    return failure.split(" (")[0]


def _round_nice(x: float) -> float:
    """Round to a human-friendly value (2 significant-ish figures)."""
    if x <= 0:
        return 0.0
    import math
    if x >= 1000:
        step = 10 ** (len(str(int(x))) - 2)
        return float(int(round(x / step)) * step)
    if x >= 10:
        return float(int(round(x / 5)) * 5)
    return round(x, 1)


def _fmt(x: float, unit: str) -> str:
    if x is None:
        return "?"
    if unit == "$":
        if x >= 1_000_000:
            return f"${x/1_000_000:.2f}M"
        if x >= 1_000:
            return f"${x/1_000:.1f}k"
        return f"${x:.0f}"
    if unit == "%":
        return f"{x:.0f}%"
    if unit == "m":
        return f"{x:.0f}m"
    return f"{x:.0f}"


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
        # Live performance tracker: records tokens we ACTUALLY alerted on and
        # re-prices them, so we can report how the alerts really did (peak x,
        # hit rate, rug rate). Only in live mode — paper_mode uses self.paper.
        self.perf = (PaperTrader(cfg, self.storage)
                     if (not cfg.runtime.paper_mode and not cfg.runtime.dry_run) else None)
        self._last_digest_ts: Optional[float] = None
        self._wallet_watcher = None  # bound to the shared session in run_once
        self._cmd_offset = None      # Telegram getUpdates offset (loaded from db)
        # Merge any persisted smart-money wallets (from prior /smart or autoseed)
        # into the config set so they take effect this run.
        self._merge_persisted_smart_wallets()

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
            f"Stock token: {is_stock_token(snap)} | Blocked symbol: "
            f"{is_blocked_symbol(snap, self._blocked_symbols())}",
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

    async def diag(self) -> str:
        """Probe the configured RPC and report whether log-based discovery works.

        The block-zero pool listener + flap curve listener both rely on
        ``eth_getLogs``. Many public RPCs reject it (rate/range limits or
        "method not supported"), which silently disables the earliest-catch
        streams. This runs on the live host (Railway has open internet) and
        gives a plain verdict + the exact error, so we know whether a dedicated
        RPC (RHL2_RPC_URL) is needed.
        """
        assert self._session is not None
        ch = self.cfg.chain
        rpc = ch.rpc_url or ""
        host = rpc.split("://")[-1].split("/")[0] if rpc else "(unset)"
        lines = [f"=== RPC DIAG ===", f"RPC host: {host}"]
        # DB persistence: on Railway the DB is ephemeral unless a Volume is
        # mounted at its directory. Surface the path + a rough persistence hint.
        import os as _os
        db = self.cfg.runtime.db_path
        persistent = bool(_os.environ.get("RHL2_DB_DIR")) and db not in (":memory:", "scanner.db")
        # Effective totals = config (always active, repo-baked) + runtime (DB).
        n_smart = len({w.lower() for w in self.cfg.smart_money_wallets}
                      | set(self.storage.smart_wallets()))
        n_blocked = len(self._blocked_symbols())
        state = (f"{n_smart} smart wallets, {n_blocked} blocked symbols, "
                 f"{len(self.storage.muted_tokens())} muted "
                 f"(+{len(self.storage.smart_wallets())} smart / "
                 f"{len(self.storage.blocked_symbols())} blocked from runtime, "
                 f"the part the Volume persists)")
        if persistent:
            lines.append(f"DB: {db} — persistent (Volume) · {state}")
        else:
            lines.append(f"DB: {db} — ⚠️ EPHEMERAL (attach a Railway Volume at its "
                         f"dir to keep autoseed/blocks/mutes) · {state}")
        if not rpc:
            return "\n".join(lines + ["RPC url is not set — set RHL2_RPC_URL."])

        async def _rpc(method, params):
            payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
            try:
                async with self._session.post(rpc, json=payload) as r:
                    if r.status != 200:
                        return None, f"HTTP {r.status}"
                    data = await r.json()
                    if isinstance(data, dict) and data.get("error"):
                        return None, str(data["error"])
                    return data.get("result"), None
            except Exception as exc:  # noqa: BLE001 — report any failure verbatim
                return None, f"{type(exc).__name__}: {exc}"

        # 1) reachability + chain id
        cid, err = await _rpc("eth_chainId", [])
        if err:
            return "\n".join(lines + [f"eth_chainId: ERROR {err}",
                                      "RPC unreachable — check RHL2_RPC_URL."])
        try:
            cid_dec = int(cid, 16)
        except (TypeError, ValueError):
            cid_dec = cid
        lines.append(f"eth_chainId: {cid_dec} (expected {ch.chain_id})")

        head, err = await _rpc("eth_blockNumber", [])
        if err:
            return "\n".join(lines + [f"eth_blockNumber: ERROR {err}"])
        head_n = int(head, 16)
        lines.append(f"eth_blockNumber: {head_n}")
        frm = max(0, head_n - 50)

        async def _getlogs(addr):
            flt = {"fromBlock": hex(frm), "toBlock": hex(head_n)}
            if addr:
                flt["address"] = addr
            return await _rpc("eth_getLogs", [flt])

        # 2) Isolate METHOD support from the ADDRESS: probe with no address first.
        # A param/validation error ("invalid address ...") means getLogs works and
        # the *address* is the problem, NOT that the RPC lacks getLogs.
        method_ok = False
        res, err = await _getlogs(None)
        if err is None:
            method_ok = True
            lines.append(f"eth_getLogs (no address, {frm}-{head_n}): ✅ ok, {len(res)} logs")
        elif "invalid" in err.lower() or "address" in err.lower() or "range" in err.lower():
            method_ok = True   # it parsed the request — method is supported
            lines.append(f"eth_getLogs (no address): parsed w/ validation error — {err}")
        else:
            lines.append(f"eth_getLogs (no address): ❌ {err}")

        # 3) Factory address — echo the exact value so a bad/empty one is obvious.
        fac = ch.dex_factory_address
        lines.append(f"dex_factory_address = {fac!r}")
        _is_addr = fac.strip().startswith("0x") and len(fac.strip()) == 42
        if fac and not _is_addr:
            lines.append("  ⚠️ that is NOT a 0x address (looks like a label). "
                         "Set RHL2_DEX_FACTORY=0x… or remove it to use the YAML default.")
        addr_ok = False
        if fac:
            res, err = await _getlogs(fac)
            if err is None:
                addr_ok = True
                lines.append(f"eth_getLogs (factory): ✅ ok, {len(res)} logs")
            else:
                lines.append(f"eth_getLogs (factory, as-is): ❌ {err}")
                # Retry with the same normalisation the listeners now apply
                # (ensure 0x prefix + lowercase) to confirm the self-heal.
                norm = fac.strip()
                if norm and not norm.lower().startswith("0x"):
                    norm = "0x" + norm
                norm = norm.lower()
                if norm != fac:
                    res2, err2 = await _getlogs(norm)
                    if err2 is None:
                        addr_ok = True
                        lines.append(f"  ↳ normalised {norm!r} WORKS ✅ (listeners auto-normalise)")
                    else:
                        lines.append(f"  ↳ normalised {norm!r} also failed: {err2}")
        else:
            lines.append("  (factory unset — pool listener can't run; is the YAML "
                         "config loaded? set RHL2_DEX_FACTORY or --config the yaml)")

        # 4) eth_getLogs on each configured curve launchpad (flap)
        for lp in self.cfg.configured_launchpads():
            res, err = await _getlogs(lp["manager"])
            name = lp.get("name", "launchpad")
            lines.append(f"eth_getLogs ({name}={lp['manager']!r}): "
                         + (f"✅ ok, {len(res)} logs" if err is None else f"❌ {err}"))
        if not self.cfg.configured_launchpads():
            lines.append("curve launchpads: none configured (set RHL2_FLAP_MANAGER)")

        # Feature status so config toggles can be confirmed at a glance.
        rc = self.cfg.runtime
        auto = "ON" if rc.smart_money_autoseed else "off"
        n_auto = sum(1 for r in self.storage.smart_wallets_detailed()
                     if (r["source"] or "").startswith("auto:"))
        lines.append(
            f"autoseed: {auto} (harvest {rc.smart_money_autoseed_buyers} buyers/winner, "
            f"cap {rc.smart_money_max_set}, {n_auto} harvested so far) | "
            f"cluster: {'on' if rc.smart_cluster_enabled else 'off'} "
            f"(≥{rc.smart_cluster_min_wallets} in {rc.smart_cluster_window_hours:g}h) | "
            f"whale pings: {'on' if self.cfg.wallet_watch.emit_alerts else 'off'}")

        lines.append("")
        if method_ok and addr_ok:
            lines.append("VERDICT: ✅ log-based discovery works — listeners are live.")
        elif method_ok and not fac:
            lines.append("VERDICT: ⚠️ getLogs works but no factory address is loaded. "
                         "The pool listener needs dex_factory_address — make sure the "
                         "run command has --config rhl2_scanner/config/robinhood.example.yaml "
                         "(or set RHL2_DEX_FACTORY).")
        elif method_ok:
            lines.append("VERDICT: ⚠️ getLogs works but the factory address was rejected. "
                         "Fix dex_factory_address / RHL2_DEX_FACTORY (see the error above).")
        else:
            lines.append("VERDICT: ❌ this RPC doesn't serve eth_getLogs — set RHL2_RPC_URL "
                         "to a dedicated RH Chain RPC that does.")
        return "\n".join(lines)

    async def research_wallet(self, wallet: str) -> str:
        """Report what tokens a wallet recently got into — early-entry research.

        Runs on the live host (chain reachable). Lists the wallet's recent ERC-20
        acquisitions so a proven-early wallet's picks can be studied, and flags
        whether it's already in the smart-money set / watch list.
        """
        assert self._session is not None
        smart = SmartMoneyClient(self.cfg, session=self._session)
        acqs = await smart.wallet_acquisitions(wallet, limit=30)
        w = wallet.lower()
        in_smart = w in {x.lower() for x in self.cfg.smart_money_wallets}
        in_watch = w in {x.lower() for x in (self.cfg.wallet_watch.wallets or [])}
        lines = [
            f"=== WALLET {wallet} ===",
            f"smart-money set: {'YES' if in_smart else 'no'} | "
            f"watch list: {'YES' if in_watch else 'no'}",
            f"recent token acquisitions: {len(acqs)}",
        ]
        if not acqs:
            lines.append("(none found — explorer transfer feed empty or unsupported)")
        for a in acqs[:25]:
            ts = str(a.get("ts") or "")[:19].replace("T", " ")
            lines.append(f"  ${a['symbol']:<10} {a['token']}  {ts}")
        if acqs:
            lines.append("")
            lines.append("Tip: /inspect any of these CAs to see how they score now.")
        return "\n".join(lines)

    async def find_deployer(self, token: str) -> str:
        """Report the contract/EOA that created a token — how to find a launchpad.

        For a bonding-curve launchpad (flap.sh) the token is deployed BY the
        manager contract, so a known flap token's *creator* is the flap manager
        to set as RHL2_FLAP_MANAGER. Runs on the live host (chain reachable).
        """
        assert self._session is not None
        base = (self.cfg.chain.explorer_api_url or "").rstrip("/")
        base = base + "/v2" if base.endswith("/api") else base
        lines = [f"=== DEPLOYER of {token} ==="]
        if not base:
            return "\n".join(lines + ["explorer_api_url not set."])
        try:
            async with self._session.get(f"{base}/addresses/{token}") as r:
                data = await r.json() if r.status == 200 else None
        except Exception as exc:  # noqa: BLE001
            return "\n".join(lines + [f"lookup failed: {exc}"])
        if not isinstance(data, dict):
            return "\n".join(lines + ["no data (address not found on explorer)."])
        creator = data.get("creator_address_hash") or data.get("creator_address")
        tx = data.get("creation_tx_hash") or data.get("creation_transaction_hash")
        tok = data.get("token") or {}
        sym = tok.get("symbol")
        if sym:
            lines.append(f"token: ${sym}")
        if not creator:
            lines.append("no creator on record (not a contract, or explorer lacks it).")
            return "\n".join(lines)
        known = self.cfg.known_launchpad_addresses().get(str(creator).lower())
        lines.append(f"created by: {creator}")
        if tx:
            lines.append(f"creation tx: {tx}")
        if known:
            lines.append(f"→ this is the '{known}' launchpad (already configured).")
        else:
            lines.append("→ if this token launched on flap, that creator IS the flap")
            lines.append(f"   manager. Set on Railway:  RHL2_FLAP_MANAGER={creator}")
        return "\n".join(lines)

    async def _calibrate_one(self, ca: str) -> dict:
        """Enrich + score one known winner; capture metrics + what blocked it."""
        assert self._session is not None
        dex = DexScreenerClient(self.cfg, session=self._session)
        pairs = await dex.pairs_for_token(ca)
        snap = pairs[0] if pairs else TokenSnapshot(
            chain=self.cfg.chain.dexscreener_chain, pair_address="", token_address=ca)
        await self._enrich(snap)
        th = self.cfg.thresholds
        strict = self.cfg.active_tier is RiskTier.MOMENTUM
        pragmatic = self.cfg.runtime.live_pragmatic_safety
        result = score_token(snap, self.cfg, strict_safety=strict, pragmatic=pragmatic)
        qs = quick_start_gate(snap, th)
        would_gem = result.level.value in ("strong", "watch")
        rc = self.cfg.runtime
        would_early = (
            rc.early_launch_enabled and not is_stock_token(snap)
            and snap.age_minutes is not None and snap.age_minutes <= rc.early_launch_max_age_minutes
            and (snap.liquidity_usd or 0) >= rc.early_launch_min_liquidity_usd
            and (result.safety_passed or not rc.early_launch_require_safety)
        )
        s = snap.safety
        tax = None
        if s.buy_tax_pct is not None or s.sell_tax_pct is not None:
            tax = max(s.buy_tax_pct or 0.0, s.sell_tax_pct or 0.0)
        return {
            "ca": ca, "symbol": snap.symbol or "?", "found": bool(pairs),
            "metrics": {
                "liquidity_usd": snap.liquidity_usd, "market_cap_usd": snap.market_cap_usd,
                "age_minutes": snap.age_minutes, "holder_count": snap.holder_count,
                "top10_supply_pct": snap.top10_supply_pct, "top1_supply_pct": snap.top1_supply_pct,
                "bundle_supply_pct": s.bundle_supply_pct, "sniper_cluster_pct": s.sniper_cluster_pct,
                "dev_holdings_pct": s.dev_holdings_pct, "max_tax_pct": tax,
            },
            "quick_fails": qs.failures, "safety_fails": result.gate_failures,
            "composite": result.composite, "would_gem": would_gem, "would_early": would_early,
            "would_alert": would_gem or would_early,
        }

    async def calibrate(self, cas: list[str]) -> str:
        """Run known winners through the pipeline; suggest thresholds that admit them.

        For every 'this should have alerted' CA, we see what actually blocked it
        and, for each numeric threshold, what value would let the winners through.
        The recommendations are computed to admit ALL analysed winners.
        """
        cas = [c.lower() for c in cas if c.startswith("0x") and len(c) == 42]
        if not cas:
            return "calibrate: give one or more token addresses (0x…)."
        rows = []
        for ca in cas:
            try:
                rows.append(await self._calibrate_one(ca))
            except Exception as exc:  # keep going; one bad CA shouldn't abort
                rows.append({"ca": ca, "error": str(exc)})

        ok = [r for r in rows if "error" not in r]
        alerts = sum(1 for r in ok if r["would_alert"])
        found = sum(1 for r in ok if r["found"])
        lines = [
            "=== CALIBRATION vs winners ===",
            f"analysed {len(ok)}/{len(rows)} | on DexScreener: {found} | "
            f"would alert now: {alerts}/{len(ok)}",
            "",
        ]
        for r in rows:
            if "error" in r:
                lines.append(f"  {r['ca']}  ERROR {r['error']}")
                continue
            verdict = "✅ ALERT" if r["would_alert"] else "❌ missed"
            why = ""
            if not r["would_alert"]:
                blk = (r["quick_fails"] + r["safety_fails"])[:2]
                why = " — " + "; ".join(blk) if blk else " — low score"
            lines.append(f"  ${r['symbol']:<9} {verdict} score={r['composite']:.0f}{why}")

        # Aggregate blockers.
        from collections import Counter
        blockers: Counter = Counter()
        for r in ok:
            for f in r["quick_fails"] + r["safety_fails"]:
                blockers[_blocker_key(f)] += 1
        if blockers:
            lines += ["", "Top blockers across winners:"]
            for k, v in blockers.most_common(8):
                lines.append(f"  {k}: {v}/{len(ok)}")

        # Numeric-threshold suggestions that admit all winners.
        specs = [
            ("liquidity_usd", "floor", ["thin_liquidity_usd", "min_liquidity_usd"], "$", 0.8),
            ("market_cap_usd", "floor", ["min_market_cap_usd"], "$", 0.8),
            ("market_cap_usd", "ceiling", ["max_market_cap_usd"], "$", 1.25),
            ("age_minutes", "ceiling", ["max_age_minutes"], "m", 1.25),
            ("holder_count", "floor", ["min_holders"], "", 0.8),
            ("top10_supply_pct", "ceiling", ["skip_top10_pct"], "%", 1.1),
            ("top1_supply_pct", "ceiling", ["max_top1_pct"], "%", 1.1),
            ("bundle_supply_pct", "ceiling", ["skip_bundle_pct"], "%", 1.1),
            ("sniper_cluster_pct", "ceiling", ["max_sniper_cluster_pct"], "%", 1.1),
            ("dev_holdings_pct", "ceiling", ["max_dev_holdings_pct"], "%", 1.1),
            ("max_tax_pct", "ceiling", ["max_tax_pct"], "%", 1.0),
        ]
        th = self.cfg.thresholds
        recs = []
        for metric, direction, attrs, unit, margin in specs:
            vals = [r["metrics"].get(metric) for r in ok]
            vals = [v for v in vals if v is not None]
            if not vals:
                continue
            for attr in attrs:
                cur = getattr(th, attr, None)
                if cur is None:
                    continue
                if direction == "floor":
                    need = min(vals)
                    if cur > need:  # a winner sat below the floor
                        rec = _round_nice(need * margin)
                        recs.append(f"  {attr}: {_fmt(cur, unit)} → {_fmt(rec, unit)} "
                                    f"(lowest winner {_fmt(need, unit)})")
                else:  # ceiling
                    need = max(vals)
                    if cur and cur < need:  # a winner sat above the ceiling (0=disabled, skip)
                        rec = _round_nice(need * margin)
                        recs.append(f"  {attr}: {_fmt(cur, unit)} → {_fmt(rec, unit)} "
                                    f"(highest winner {_fmt(need, unit)})")
        if recs:
            lines += ["", "Suggested threshold changes (admit all winners):"] + recs
        else:
            lines += ["", "No numeric threshold change needed — remaining misses are "
                      "safety-gate or data (see blockers above)."]
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
        elif cmd == "diag":
            try:
                from html import escape as _esc
                report = await self.diag()
                await self._send_html("<pre>" + _esc(report) + "</pre>")
            except Exception as exc:
                await self._send_html("diag failed: " + __import__("html").escape(str(exc)))
        elif cmd == "smart":
            if not valid_ca:
                await self._send_html("Usage: <code>/smart 0x&lt;wallet&gt;</code>")
                return
            new = self._add_smart_wallet(arg, source="manual")
            n = len(self.storage.smart_wallets())
            verb = "Added" if new else "Already tracking"
            await self._send_html(f"🧠 {verb} <code>{arg}</code> — {n} smart wallets.")
        elif cmd in ("unsmart", "unsmartwallet"):
            if not valid_ca:
                await self._send_html("Usage: <code>/unsmart 0x&lt;wallet&gt;</code>")
                return
            self.storage.remove_smart_wallet(arg)
            if arg in self.cfg.smart_money_wallets:
                self.cfg.smart_money_wallets.remove(arg)
            await self._send_html(f"🧠 Removed <code>{arg}</code>.")
        elif cmd in ("perf", "performance"):
            from .paper import PaperTrader, format_digest_html
            win = self.cfg.runtime.paper_digest_win_multiple
            rep = PaperTrader(self.cfg, self.storage).report(win_multiple=win)
            if not rep["total_recorded"]:
                await self._send_html("📊 No alert outcomes tracked yet — "
                                      "performance builds as alerted tokens age.")
                return
            await self._send_html(
                format_digest_html(rep, win, title="Alert Performance", noun="alerts"))
        elif cmd == "block":
            from .filters import _norm_symbol
            sym = _norm_symbol(parts[1]) if len(parts) > 1 else ""
            if not sym:
                await self._send_html("Usage: <code>/block SYMBOL</code> (e.g. /block ROBINHOOD)")
                return
            self.storage.block_symbol(sym)
            await self._send_html(f"🚫 Blocked <b>${sym}</b> — these will no longer alert.")
        elif cmd in ("unblock", "unblocksymbol"):
            from .filters import _norm_symbol
            sym = _norm_symbol(parts[1]) if len(parts) > 1 else ""
            if not sym:
                await self._send_html("Usage: <code>/unblock SYMBOL</code>")
                return
            # Only DB-added symbols can be removed; config defaults stay.
            self.storage.unblock_symbol(sym)
            still = sym in [s.upper() for s in (self.cfg.chain.blocked_symbols or [])]
            note = " (still blocked by config default)" if still else ""
            await self._send_html(f"✅ Unblocked <b>${sym}</b>{note}.")
        elif cmd in ("blocked", "blocklist"):
            syms = self._blocked_symbols()
            body = ", ".join(f"${s}" for s in syms) if syms else "none"
            await self._send_html(f"🚫 Blocked symbols ({len(syms)}): {body}")
        elif cmd == "calibrate":
            cas = [p.lower() for p in parts[1:] if p.lower().startswith("0x") and len(p) == 42]
            if not cas:
                await self._send_html("Usage: <code>/calibrate 0xCA1 0xCA2 …</code> "
                                      "(paste your known winners)")
                return
            try:
                from html import escape as _esc
                report = await self.calibrate(cas)
                await self._send_html("<pre>" + _esc(report) + "</pre>")
            except Exception as exc:
                await self._send_html("calibrate failed: " + __import__("html").escape(str(exc)))
        elif cmd in ("deployer", "creator"):
            if not valid_ca:
                await self._send_html("Usage: <code>/deployer 0x&lt;token&gt;</code> "
                                      "(a known flap token → finds the flap manager)")
                return
            try:
                from html import escape as _esc
                await self._send_html("<pre>" + _esc(await self.find_deployer(arg)) + "</pre>")
            except Exception as exc:
                await self._send_html("deployer lookup failed: " + __import__("html").escape(str(exc)))
        elif cmd == "wallet":
            if not valid_ca:
                await self._send_html("Usage: <code>/wallet 0x&lt;address&gt;</code>")
                return
            try:
                from html import escape as _esc
                report = await self.research_wallet(arg)
                await self._send_html("<pre>" + _esc(report) + "</pre>")
            except Exception as exc:
                await self._send_html("wallet research failed: " + __import__("html").escape(str(exc)))
        elif cmd in ("smartlist", "smarts"):
            rows = self.storage.smart_wallets_detailed()
            if not rows:
                await self._send_html("🧠 No smart-money wallets tracked yet.")
                return
            lines = [f"🧠 Smart-money wallets ({len(rows)}):"]
            for r in rows[:40]:
                src = r["source"] or "manual"
                tag = f" · {r['note']}" if r["note"] else ""
                lines.append(f"<code>{r['wallet']}</code> ({src}{tag})")
            await self._send_html("\n".join(lines))

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
        smart_set = {w.lower() for w in self.cfg.smart_money_wallets}
        blocked = set(self._blocked_symbols())
        emit = self.cfg.wallet_watch.emit_alerts
        for ev in events:
            if self.storage.is_muted(ev.token_address):
                continue   # /zero'd token
            if blocked and _norm_symbol(ev.symbol) in blocked:
                continue   # scam-impersonator symbol
            # Individual whale ping (optional — the data is still used below).
            if emit:
                await self._send_html(format_whale_html(ev))
                log.info("WHALE %s %s $%s by %s", ev.side, ev.symbol, ev.usd, ev.label)
            # Smart-money cluster: record buys by smart wallets, alert on convergence.
            # This runs regardless of emit_alerts — it's how we "use the data".
            if ev.side == "buy" and ev.wallet.lower() in smart_set:
                await self._check_cluster(ev)

    def _entity_of(self, addr: str) -> str:
        """Resolve a wallet to its sybil-group entity (or itself if ungrouped)."""
        return self.cfg.wallet_watch.wallet_groups.get(addr.lower(), addr.lower())

    async def _check_cluster(self, ev) -> None:
        """Fire a high-priority alert when N distinct ENTITIES converge on a token.

        Wallets grouped as one sybil entity count once, so a person running two
        wallets can't fake a convergence.
        """
        rc = self.cfg.runtime
        if not rc.smart_cluster_enabled:
            return
        token = ev.token_address
        self.storage.record_smart_buy(token, ev.wallet)
        if self.storage.cluster_already_alerted(token):
            return
        since = time.time() - rc.smart_cluster_window_hours * 3600.0
        buyers = self.storage.distinct_smart_buyers(token, since)
        # Collapse wallets to entities: one display label per distinct entity.
        reps: dict[str, str] = {}
        for w in buyers:
            ent = self._entity_of(w)
            if ent in reps:
                continue
            reps[ent] = ent if ent != w.lower() else \
                self.cfg.wallet_watch.labels.get(w, w[:6] + "…" + w[-4:])
        if len(reps) < rc.smart_cluster_min_wallets:
            return
        labels = list(reps.values())
        await self._send_html(format_cluster_html(ev.symbol, token, labels, ev.chart_url))
        self.storage.mark_cluster_alert(token, len(reps))
        log.info("CLUSTER %s %d entities in $%s", token, len(reps), ev.symbol)

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
        """Post the calibration/performance digest every interval hours."""
        rc = self.cfg.runtime
        # Enabled explicitly (paper), or implicitly in live mode where the perf
        # tracker gives real alert-outcome feedback.
        if not rc.paper_digest_enabled and self.perf is None:
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

        # Drop scam-impersonator symbols ($ROBINHOOD clones etc.).
        blocked = self._blocked_symbols()
        if blocked:
            before = len(pairs)
            pairs = [p for p in pairs if not is_blocked_symbol(p, blocked)]
            if before != len(pairs):
                log.info("excluded %d blocked-symbol tokens (%s)",
                         before - len(pairs), ", ".join(blocked))

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

        # Re-price open paper/perf positions and record any reached checkpoints.
        for tracker, name in ((self.paper, "paper"), (self.perf, "perf")):
            if tracker is not None:
                try:
                    settled = await tracker.settle_open(dex)
                    if settled:
                        log.info("%s: settled %d positions this cycle", name, settled)
                except Exception:
                    log.exception("%s settle failed", name)

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
        if is_blocked_symbol(snap, self._blocked_symbols()):
            return result   # scam-impersonator symbol ($ROBINHOOD clones) — never alert

        # Determine this cycle's alert tier: strong(2) > watch(1) > early(0).
        # A token that ESCALATES past the tier it was last alerted at gets an
        # immediate "upgrade" ping that bypasses the cooldown (tiered re-alert).
        if result.level == AlertLevel.STRONG:
            tier = 2
        elif result.level == AlertLevel.WATCH:
            tier = 1
        elif self._is_early_launch(snap, result):
            tier = 0
        else:
            reasons = ", ".join(result.gate_failures[:3]) if result.gate_failures else "low score"
            log.debug("skip %s (%s)", snap.symbol, reasons)
            return result

        rc = self.cfg.runtime
        prev = self.storage.alert_rank(snap.pair_address)
        escalated = rc.realert_on_escalation and prev >= 0 and tier > prev
        cooling = self.storage.in_cooldown(snap.pair_address, rc.realert_cooldown_seconds)

        if not (prev < 0 or escalated or not cooling):
            log.debug("%s in cooldown (tier=%d, prev=%d), skipping", snap.symbol, tier, prev)
            return result

        note = self._escalation_note(prev, tier) if escalated else ""
        if tier == 0:
            html = format_early_launch_html(snap, result)
            if note:
                html = note + "\n" + html
            await self._send_html(html)
            log.info("EARLY %s age=%.0fm liq=%s", snap.symbol, snap.age_minutes or 0, snap.liquidity_usd)
        else:
            await self.notifier.send(snap, result, note=note)
            tag = "UPGRADE" if escalated else "ALERT"
            log.info("%s %s %s score=%.0f", tag, result.level.value, snap.symbol, result.composite)
        # A token reaching STRONG for the first time is a confirmed runner —
        # harvest its earliest buyers into the smart-money set (if enabled).
        if tier == 2 and prev < 2:
            await self._maybe_autoseed(snap)
        self.storage.record_alert(snap, result, rank=tier)
        # Track this alert's real outcome (peak x / hit / rug) on first alert.
        if self.perf is not None and prev < 0:
            self.perf.record_alerted(snap, result)
        return result

    @staticmethod
    def _escalation_note(prev: int, tier: int) -> str:
        """Header line for an upgrade re-alert (early -> watch -> strong)."""
        names = {0: "🌱 Early", 1: "👀 Watch", 2: "🚨 Strong"}
        return f"🔼 <b>UPGRADED</b> {names.get(prev, '?')} → {names.get(tier, '?')}"

    # -- smart-money seeding --------------------------------------------

    def _blocked_symbols(self) -> list[str]:
        """Config blocklist + any /block'd symbols persisted this deploy."""
        from .filters import _norm_symbol
        cfg_syms = [_norm_symbol(s) for s in (self.cfg.chain.blocked_symbols or [])]
        db_syms = self.storage.blocked_symbols()
        return list(dict.fromkeys([s for s in cfg_syms + db_syms if s]))

    def _merge_persisted_smart_wallets(self) -> None:
        persisted = self.storage.smart_wallets()
        if not persisted:
            return
        merged = list(dict.fromkeys([w.lower() for w in self.cfg.smart_money_wallets] + persisted))
        self.cfg.smart_money_wallets = merged

    def _add_smart_wallet(self, addr: str, source: str = "manual", note: str = "") -> bool:
        """Persist + activate a smart-money wallet. Returns True if newly added."""
        addr = addr.lower()
        new = self.storage.add_smart_wallet(addr, source=source, note=note)
        if addr not in self.cfg.smart_money_wallets:
            self.cfg.smart_money_wallets.append(addr)
        return new

    async def _maybe_autoseed(self, snap: TokenSnapshot) -> None:
        """Harvest a confirmed winner's earliest buyers into the smart set.

        Called when a token reaches a STRONG alert. Those wallets bought a runner
        early — exactly the signal we want to weight on future launches. Gated by
        smart_money_autoseed and capped by smart_money_max_set.
        """
        rc = self.cfg.runtime
        if not rc.smart_money_autoseed or self._session is None:
            return
        if len(self.storage.smart_wallets()) >= rc.smart_money_max_set:
            return
        try:
            smart = SmartMoneyClient(self.cfg, session=self._session)
            buyers = await smart.early_buyers(snap.token_address, rc.smart_money_autoseed_buyers)
        except Exception:
            log.debug("autoseed early_buyers failed", exc_info=True)
            return
        added = 0
        for w in buyers:
            if self._add_smart_wallet(w, source=f"auto:{snap.token_address.lower()}",
                                      note=f"${snap.symbol}"):
                added += 1
        if added:
            log.info("autoseed: harvested %d early buyers of $%s (winner) into smart set",
                     added, snap.symbol)

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
