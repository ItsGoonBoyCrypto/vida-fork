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
from .curve_pattern import (
    bootstrap_profile,
    extract_features,
    is_confirmed_climb,
    learn_profile,
    match as curve_match,
)
from .sources.chain import EvmChainClient
from .sources.dexscreener import DexScreenerClient
from .sources.poollistener import PoolListener
from .sources.launchpad_curve import LaunchpadCurveListener
from .sources.bags import BagsClient
from .sources.safety import CompositeSafetySource
from .sources.smartmoney import SmartMoneyClient
from .storage import Storage
from .walletwatch import (
    WalletWatcher,
    format_cluster_html,
    format_core_alpha_html,
    format_dump_html,
    format_exit_html,
    format_honeypot_html,
    format_kol_html,
    format_milestone_html,
    format_top_zone_html,
    format_whale_html,
)
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
        self._last_harvest_ts: Optional[float] = None
        self._wallet_watcher = None  # bound to the shared session in run_once
        self._cmd_offset = None      # Telegram getUpdates offset (loaded from db)
        self._reply_chat = None      # during command handling, reply to the sender's chat
        # Live ETH/USD, learned free from any graduated RH pair (priceUsd/priceNative).
        # Lets us convert flap's on-curve ETH figures into the USD the scorer uses.
        self._eth_usd: Optional[float] = None
        # Cached pre-migration curve profile (learned winning setup, else prior).
        self._curve_profile: Optional[dict] = None
        self._curve_profile_ts: Optional[float] = None
        # Cached memelab learned signature (dormant until trained) — the score bonus.
        self._mm_signature: Optional[dict] = None
        self._mm_signature_ts: Optional[float] = None
        # Bags launchpad: registry discovery cursor (last-seen token count).
        self._bags_last_total: Optional[int] = None
        # Last-cycle discovery-stream health (label -> "ok: N" / "ERR: …"), for /diag.
        self._discovery_health: dict[str, str] = {}
        # Merge any persisted smart-money wallets (from prior /smart or autoseed)
        # into the config set so they take effect this run.
        self._merge_persisted_smart_wallets()
        # Bounded auto-tune: remember the config weights as the immovable baseline,
        # then apply any persisted tuned override on top (reversible via /autotune).
        self._weights_baseline = {
            "safety": cfg.weights.safety, "distribution": cfg.weights.distribution,
            "momentum": cfg.weights.momentum, "discovery": cfg.weights.discovery,
        }
        self._last_autotune_ts: Optional[float] = None
        self._apply_weight_override()
        # Core-alpha wallet cache (proven multi-winner wallets → single-wallet alerts).
        self._core_alpha_cache: set = set()
        self._core_alpha_ts: float = 0.0
        self._mm_rep_ts: float = 0.0   # last memelab-reputation sync
        self._exit_model_cache: Optional[dict] = None   # learned top-zone model
        self._exit_model_ts: float = 0.0
        self._hot_nar_cache: set = set()   # narratives currently producing winners
        self._hot_nar_ts: float = 0.0

    # -- lifecycle -------------------------------------------------------

    async def run_forever(self) -> None:
        timeout = aiohttp.ClientTimeout(total=self.cfg.runtime.request_timeout_seconds)
        self._session = aiohttp.ClientSession(timeout=timeout)
        tg = self.cfg.telegram
        n_smart = len({w.lower() for w in self.cfg.smart_money_wallets}
                      | set(self.storage.smart_wallets()))
        log.info(
            "telegram: %s | alert_chat=%s | commands=%s | smart_wallets=%d",
            "TOKEN SET ✓" if tg.bot_token else "NO TOKEN ✗ (alerts go to stdout only)",
            tg.alert_chat_id or "UNSET ✗",
            "on" if (tg.bot_token and tg.alert_chat_id) else "OFF (needs token+chat)",
            n_smart,
        )
        # Real persistence check: a boot counter in the DB. If it stays at 1
        # across redeploys, the volume ISN'T actually persisting (data resets).
        try:
            boot = int(self.storage.kv_get("boot_count") or 0) + 1
            self.storage.kv_set("boot_count", str(boot))
        except Exception:  # noqa: BLE001
            boot = -1
        log.info(
            "scanner up | chain=%s tier=%s dry_run=%s | boot #%d%s",
            self.cfg.chain.dexscreener_chain,
            self.cfg.active_tier.value,
            self.cfg.runtime.dry_run, boot,
            "  ⚠️ (still 1 after redeploys ⇒ DB volume NOT persisting)" if boot == 1 else "",
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

    async def _send_html(self, text: str, reply_to: Optional[int] = None) -> Optional[int]:
        """Send HTML to the alert channel — or, while handling a command, back to
        whichever chat the command came from (so DM commands reply in the DM).

        Returns the sent message_id (for threading follow-ups), or None."""
        tg = self.cfg.telegram
        chat = self._reply_chat or tg.alert_chat_id
        if tg.bot_token and chat:
            from .tgtools import send_message
            try:
                ok, _detail, mid = await send_message(
                    tg.bot_token, chat, text, self._session, reply_to=reply_to)
                return mid if ok else None
            except Exception:
                log.exception("send failed")
        print("\n" + text + "\n", flush=True)
        return None

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
        boot = self.storage.kv_get("boot_count") or "?"
        # boot_count is the real test: >1 proves the DB survived a redeploy.
        if boot not in ("?", "1"):
            lines.append(f"DB: {db} — ✅ persisting (boot #{boot}) · {state}")
        elif persistent:
            lines.append(f"DB: {db} — volume configured, boot #{boot} "
                         f"(redeploy once; if still #1 the volume isn't mounted) · {state}")
        else:
            lines.append(f"DB: {db} — ⚠️ EPHEMERAL (boot #{boot}; attach a Railway Volume "
                         f"at its dir to keep data) · {state}")
        if not rpc:
            return "\n".join(lines + ["RPC url is not set — set RHL2_RPC_URL."])

        from .sources.poollistener import _backoff, _transient
        transient_hits = {"n": 0}

        async def _rpc(method, params):
            payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
            # Retry transient rate-limits/timeouts so the diag doesn't self-
            # inflict 429s from back-to-back getLogs calls.
            for attempt in range(4):
                try:
                    async with self._session.post(rpc, json=payload) as r:
                        if r.status in (429, 503, 504):
                            transient_hits["n"] += 1
                            if attempt < 3:
                                await _backoff(attempt)
                                continue
                            return None, f"HTTP {r.status} (rate-limited)"
                        if r.status != 200:
                            return None, f"HTTP {r.status}"
                        data = await r.json()
                        if isinstance(data, dict) and data.get("error"):
                            msg = str(data["error"])
                            if _transient(msg):
                                transient_hits["n"] += 1
                                if attempt < 3:
                                    await _backoff(attempt)
                                    continue
                            return None, msg
                        return data.get("result"), None
                except Exception as exc:  # noqa: BLE001
                    if attempt < 3:
                        await _backoff(attempt)
                        continue
                    return None, f"{type(exc).__name__}: {exc}"
            return None, "exhausted retries"

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
        wh = "ON" if rc.winner_harvest_enabled else "off"
        lines.append(
            f"autoseed: {auto} (harvest {rc.smart_money_autoseed_buyers} buyers/winner, "
            f"cap {rc.smart_money_max_set}, {n_auto} harvested so far) | "
            f"winner-harvest: {wh} (≥{rc.winner_harvest_win_mult:g}x at "
            f"{rc.winner_harvest_min_age_hours:g}-{rc.winner_harvest_max_age_hours:g}h) | "
            f"cluster: {'on' if rc.smart_cluster_enabled else 'off'} "
            f"(≥{rc.smart_cluster_min_wallets} in {rc.smart_cluster_window_hours:g}h) | "
            f"whale pings: {'on' if self.cfg.wallet_watch.emit_alerts else 'off'}")
        lines.append(
            f"exits: {'on' if rc.smart_exit_enabled else 'off'} | "
            f"milestones: {'/'.join(f'{m:g}x' for m in rc.milestone_multiples) if rc.position_monitor_enabled else 'off'} | "
            f"dump guard: {('−%.0f%% from peak' % rc.dump_drawdown_pct) if rc.position_monitor_enabled else 'off'}")

        # Last-cycle discovery-stream health — the fast tell for "why zero alerts":
        # an ERR here is the stream that's throwing (now isolated, not muting the bot).
        if self._discovery_health:
            lines.append("discovery streams (last cycle): " + " | ".join(
                f"{k}={v}" for k, v in self._discovery_health.items()))
        else:
            lines.append("discovery streams: no cycle completed yet")

        lines.append("")
        if method_ok and addr_ok:
            lines.append("VERDICT: ✅ log-based discovery works — listeners are live.")
        elif method_ok and not fac:
            lines.append("VERDICT: ⚠️ getLogs works but no factory address is loaded. "
                         "The pool listener needs dex_factory_address — make sure the "
                         "run command has --config rhl2_scanner/config/robinhood.example.yaml "
                         "(or set RHL2_DEX_FACTORY).")
        elif method_ok and _is_addr and transient_hits["n"]:
            lines.append("VERDICT: ⚠️ getLogs WORKS and the address is valid — the public RH "
                         "RPC is just RATE-LIMITING (429 / backend timeouts) under load. The "
                         "listeners now retry with backoff so discovery keeps working, but for "
                         "rock-solid block-zero catching set RHL2_RPC_URL to a dedicated RH "
                         "Chain RPC (higher rate limits).")
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

    async def _contract_methods(self, token: str) -> tuple:
        """(verified, [function names]) for a token contract via Blockscout."""
        base = (self.cfg.chain.explorer_api_url or "").rstrip("/")
        base = base + "/v2" if base.endswith("/api") else base
        if not base or self._session is None:
            return False, []
        try:
            async with self._session.get(f"{base}/smart-contracts/{token}") as r:
                data = await r.json() if r.status == 200 else None
        except Exception:  # noqa: BLE001
            return False, []
        if not isinstance(data, dict):
            return False, []
        abi = data.get("abi") or []
        names = [e.get("name") for e in abi
                 if isinstance(e, dict) and e.get("type") == "function" and e.get("name")]
        verified = bool(data.get("is_verified") or abi)
        return verified, names

    async def audit_contract(self, snap_or_ca) -> str:
        """Full contract-risk report: dangerous functions + wash-trade check."""
        from html import escape as _esc
        from .contract_audit import audit_functions, risk_summary, wash_trade
        ca = snap_or_ca if isinstance(snap_or_ca, str) else snap_or_ca.token_address
        ca = ca.lower()
        verified, names = await self._contract_methods(ca)
        audit = audit_functions(names)
        # wash-trade needs live snapshot data — enrich if we were given a bare CA.
        snap = snap_or_ca if not isinstance(snap_or_ca, str) else None
        wash = wash_trade(snap) if snap is not None else (False, "")
        verdict, findings = risk_summary(audit, wash)
        lines = [f"🔍 <b>Contract audit</b> — {verdict}", f"<code>{_esc(ca)}</code>", ""]
        if not verified:
            lines.append("⚠️ Contract source not verified — can't fully audit.")
        for sev, label in findings:
            dot = {"critical": "🔴", "warning": "🟡", "info": "🔹"}.get(sev, "•")
            lines.append(f"{dot} {_esc(label)}")
        if not findings and verified:
            lines.append("No mint / blacklist / pause / owner-fee hooks in the ABI.")
        if audit.get("has_renounce"):
            lines.append("<i>Has renounceOwnership (verify it's actually renounced).</i>")
        return "\n".join(lines)

    async def _creator_of(self, token: str) -> str:
        """Raw creator (contract/EOA) of a token via Blockscout, lowercased."""
        base = (self.cfg.chain.explorer_api_url or "").rstrip("/")
        base = base + "/v2" if base.endswith("/api") else base
        if not base or self._session is None:
            return ""
        try:
            async with self._session.get(f"{base}/addresses/{token}") as r:
                data = await r.json() if r.status == 200 else None
        except Exception:  # noqa: BLE001
            return ""
        if not isinstance(data, dict):
            return ""
        return (data.get("creator_address_hash") or data.get("creator_address") or "").lower()

    async def _label_deployer(self, token: str, outcome: str) -> None:
        """Record a token's deployer + outcome for per-creator win-rate.

        Skips known launchpad managers: on a bonding-curve launchpad the token's
        on-chain creator IS the shared manager/factory, not a per-launch deployer,
        so crediting it would be meaningless.
        """
        creator = await self._creator_of(token)
        if not creator or creator in self.cfg.known_launchpad_addresses():
            return
        self.storage.record_deployer_token(creator, token, outcome)

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

    async def curveprobe(self, token: str, delay: float = 1.2, func: str = "") -> str:
        """Find a flap token's on-chain price getter — probe its CREATOR contract.

        flap deploys each token from its own bonding-curve contract (the token's
        creator), which is where the curve state/price lives — NOT the Portal. So
        we resolve the creator via the explorer, then probe it (and the token)
        with a FOCUSED set of getters, gently paced to survive the rate-limited
        public RPC. Paste the output back and we pin the getter to price flap
        tokens pre-graduation.

        With ``func`` (e.g. /curveprobe 0x.. reserves) probe ONLY that function —
        a single call, which survives the rate limit where the full sweep can't.
        Use this once you have the function name from the flap docs.
        """
        from .keccak import keccak256
        import asyncio
        assert self._session is not None
        rpc_url = self.cfg.chain.rpc_url
        amt = (10 ** 16).to_bytes(32, "big").hex()
        tok = token.lower().replace("0x", "").rjust(64, "0")

        def sel(sig: str) -> str:
            return keccak256(sig.encode()).hex()[:8]

        async def rpc(method, params):
            payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
            for attempt in range(3):           # fewer retries → smaller bursts
                try:
                    async with self._session.post(rpc_url, json=payload) as r:
                        if r.status in (429, 503, 504):
                            if delay:          # delay=0 in tests → no retry sleeps
                                await asyncio.sleep(1.0 + attempt)
                            continue
                        if r.status != 200:
                            return None, f"HTTP {r.status}"
                        d = await r.json()
                        if isinstance(d, dict) and d.get("error"):
                            return None, str(d["error"])
                        return d.get("result"), None
                except Exception as exc:  # noqa: BLE001
                    if attempt < 2:
                        if delay:
                            await asyncio.sleep(1.0 + attempt)
                        continue
                    return None, str(exc)
            return None, "rate-limited"

        # Resolve the token's creator (the per-token curve contract) via Blockscout.
        base = (self.cfg.chain.explorer_api_url or "").rstrip("/")
        base = base + "/v2" if base.endswith("/api") else base
        creator = ""
        try:
            async with self._session.get(f"{base}/addresses/{token}") as r:
                d = await r.json() if r.status == 200 else {}
            creator = (d.get("creator_address_hash") or d.get("creator_address") or "")
        except Exception:  # noqa: BLE001
            pass

        lines = [f"=== CURVE PROBE {token} ===",
                 f"creator (curve contract?): {creator or 'unknown'}"]
        stats = {"errors": 0, "empties": 0}

        def interesting(res) -> bool:
            return bool(res) and res != "0x" and set(res.replace("0x", "")) != {"0"}

        async def call(target, data):
            res, err = await rpc("eth_call", [{"to": target, "data": "0x" + data}, "latest"])
            if err:
                stats["errors"] += 1
            elif not interesting(res):
                stats["empties"] += 1
            return res

        # Single-function mode: probe ONLY this getter — 2-3 calls survive the rate
        # limit where the 35-call sweep can't. Try no-arg on creator + token, then
        # addr-arg on the creator (factory-style: getter(token)).
        if func:
            name = func.replace("(", "").replace(")", "").strip()
            targets = []
            if creator and creator.lower() != token.lower():
                targets.append(("creator", creator, "", ""))
            targets.append(("token", token, "", ""))
            if creator and creator.lower() != token.lower():
                targets.append(("creator", creator, "address", tok))
            fhits = 0
            for label, target, arg, suffix in targets:
                res = await call(target, sel(f"{name}({arg})") + suffix)
                if interesting(res):
                    fhits += 1
                    lines.append(f"{label}.{name}({arg}) → {res[:260]}")
                if delay:
                    await asyncio.sleep(delay)
            lines.append("")
            if fhits:
                lines.append(f"✅ {name}() returned data — paste this back to pin pricing.")
            elif stats["errors"] and not stats["empties"]:
                lines.append(f"⚠️ {name}() rate-limited — re-run in a minute.")
            else:
                lines.append(f"{name}() matched nothing ({stats['empties']} empty, "
                             f"{stats['errors']} errored) — try another name or arg shape.")
            return "\n".join(lines)

        # Focused getters. no-arg = curve-on-creator/token; addr-arg = factory-style.
        noarg = ["price", "getPrice", "currentPrice", "reserves", "getReserves",
                 "virtualReserves", "getVirtualReserves", "getState", "state",
                 "marketCap", "getMarketCap", "progress", "bondingCurve", "info"]
        addr = ["getTokenState", "tokenState", "getPool", "getCurve", "getReserves",
                "getInfo", "priceOf"]

        hits = 0

        async def sweep(label, target, sigs, suffix):
            nonlocal hits
            for name in sigs:
                res = await call(target, sel(f"{name}({'address' if suffix==tok else ''})") + suffix)
                if interesting(res):
                    hits += 1
                    lines.append(f"{label}.{name}({'address' if suffix==tok else ''}) → {res[:260]}")
                await asyncio.sleep(delay)

        # Probe the creator first (most likely the curve), then the token.
        if creator and creator.lower() != token.lower():
            await sweep("creator", creator, noarg, "")
            if not hits:
                await sweep("creator", creator, addr, tok)
        if not hits:
            await sweep("token", token, noarg, "")

        lines.append("")
        if hits:
            lines.append(f"✅ {hits} getter(s) returned data — paste this back to pin pricing.")
        elif stats["errors"] and not stats["empties"]:
            lines.append(f"⚠️ {stats['errors']} calls rate-limited — re-run in a minute "
                         "(the public RPC is throttling). This version paces slower.")
        else:
            lines.append(f"No known getter matched ({stats['empties']} empty, "
                         f"{stats['errors']} errored). flap uses custom names — I'll need "
                         "the read function from the flap bonding-curve dev docs.")
        return "\n".join(lines)

    # flap Portal read function (per flap dev docs): getTokenV2(address) returns the
    # whole bonding-curve state in ONE call — status, reserve, circulating supply,
    # price, token version, curve r, and the DEX-graduation supply threshold.
    _FLAP_STATUS = {0: "Invalid", 1: "Tradable", 2: "InDuel", 3: "Killed", 4: "DEX/graduated"}

    async def flap_state(self, token: str, delay: float = 1.2) -> dict:
        """Read a flap token's live bonding-curve state via Portal.getTokenV2(token).

        A single ``eth_call`` to the flap Portal — survives the rate-limited public
        RPC where the getter-sweep can't — decoding the 7-field ``TokenStateV2``
        tuple: (status, reserve, circulatingSupply, price, tokenVersion, r,
        dexSupplyThresh). Returns a dict; ``ok`` is False with ``error`` on failure.
        """
        from .keccak import keccak256
        import asyncio
        assert self._session is not None
        lp = self.cfg._launchpad("flap") or {}
        portal = str(lp.get("manager") or "").strip()
        if not portal:
            return {"ok": False, "error": "no flap Portal configured (set RHL2_FLAP_MANAGER)"}
        sel = keccak256(b"getTokenV2(address)").hex()[:8]
        arg = token.lower().replace("0x", "").rjust(64, "0")
        payload = {"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                   "params": [{"to": portal, "data": "0x" + sel + arg}, "latest"]}
        result, err = None, None
        for attempt in range(3):
            try:
                async with self._session.post(self.cfg.chain.rpc_url, json=payload) as r:
                    if r.status in (429, 503, 504):
                        err = "rate-limited"
                        if delay:
                            await asyncio.sleep(1.0 + attempt)
                        continue
                    if r.status != 200:
                        return {"ok": False, "error": f"HTTP {r.status}", "portal": portal}
                    d = await r.json()
                    if isinstance(d, dict) and d.get("error"):
                        return {"ok": False, "error": str(d["error"]), "portal": portal}
                    result = d.get("result")
                    err = None
                    break
            except Exception as exc:  # noqa: BLE001
                err = str(exc)
                if attempt < 2 and delay:
                    await asyncio.sleep(1.0 + attempt)
        if not result or result == "0x":
            return {"ok": False, "error": err or "empty result", "portal": portal}
        body = result[2:] if result.startswith("0x") else result
        words = [body[i:i + 64] for i in range(0, len(body), 64)]
        if len(words) < 7:
            return {"ok": False, "error": f"short return ({len(words)} words)",
                    "portal": portal, "raw": result[:200]}
        v = [int(w, 16) for w in words[:7]]
        status, reserve, circ, price, version, r_, thresh = v
        progress = (circ / thresh * 100.0) if thresh else 0.0
        return {"ok": True, "portal": portal, "status": status,
                "status_label": self._FLAP_STATUS.get(status, f"?{status}"),
                "reserve": reserve, "circulating_supply": circ, "price": price,
                "token_version": version, "r": r_, "dex_supply_thresh": thresh,
                "progress_pct": progress, "graduated": status == 4,
                "tradable": status == 1}

    async def flapstate(self, token: str, delay: float = 1.2) -> str:
        """Human-readable flap curve state for the /flapstate command."""
        st = await self.flap_state(token, delay=delay)
        lines = [f"=== FLAP STATE {token} ===", f"Portal: {st.get('portal', '?')}"]
        if not st.get("ok"):
            err = st.get("error", "unknown")
            if err == "rate-limited":
                lines.append("⚠️ rate-limited — re-run in a few seconds (single call, "
                             "usually gets through).")
            else:
                lines.append(f"❌ {err}")
                if st.get("raw"):
                    lines.append(f"raw: {st['raw']}")
                lines.append("If status is Invalid/empty, this token isn't a flap "
                             "Portal token (or already graduated to DEX).")
            return "\n".join(lines)
        e18 = 10 ** 18
        price_eth = st["price"] / e18
        # mcap ≈ price(ETH/token) × circulating supply(tokens); both 18-dec assumed.
        mcap_eth = st["price"] * st["circulating_supply"] / (e18 * e18)
        lines += [
            f"status: {st['status_label']}",
            f"graduation progress: {st['progress_pct']:.1f}%  "
            f"({st['circulating_supply'] / e18:,.0f} / {st['dex_supply_thresh'] / e18:,.0f} supply)",
            f"price: {price_eth:.10f} ETH/token  (raw {st['price']})",
            f"reserve: {st['reserve'] / e18:.6f} ETH",
            f"est. mcap: {mcap_eth:.4f} ETH   ← ×(ETH/USD) for USD mcap",
            f"curve r: {st['r']}   token version: {st['token_version']}",
        ]
        if st["graduated"]:
            lines.append("→ already on DEX — DexScreener has the live pair; use that.")
        elif st["tradable"]:
            lines.append("→ live on the curve (pre-graduation) — this is the pricing we "
                         "wire into scoring.")
        lines.append("")
        lines.append("If these ETH figures match flap.sh's UI, the 18-dec assumption is "
                     "right and I'll pin USD mcap/scoring straight in.")
        return "\n".join(lines)

    def _note_eth_usd(self, snap: TokenSnapshot) -> None:
        """Learn live ETH/USD, free, from a graduated RH pair.

        On Robinhood Chain every DexScreener pair is quoted in ETH, so
        priceUsd / priceNative == ETH/USD. We cache the latest sane value and
        use it to price flap tokens still on the curve (which have no pair yet).
        """
        pu, pn = snap.price_usd, snap.price_native
        if pu and pn and pn > 0:
            rate = pu / pn
            if 100.0 <= rate <= 100000.0:      # sanity band — reject garbage quotes
                self._eth_usd = rate

    def _eth_usd_rate(self) -> Optional[float]:
        """Current ETH/USD: the learned live value, else the RHL2_ETH_USD hint."""
        if self._eth_usd:
            return self._eth_usd
        import os
        try:
            hint = float(os.environ.get("RHL2_ETH_USD", "") or 0)
        except ValueError:
            hint = 0.0
        return hint or None

    async def _enrich_flap_curve(self, snap: TokenSnapshot) -> None:
        """Price a flap token still on the bonding curve, via Portal.getTokenV2.

        Only runs when the token has no DexScreener market yet (pre-graduation).
        One eth_call gives status/reserve/supply/price; combined with live ETH/USD
        it fills market_cap_usd / price_usd / liquidity_usd so the scorer can rank
        the token BEFORE it graduates — the earliest possible entry on the chain.
        """
        if snap.market_cap_usd:            # already priced by DexScreener (listed/graduated)
            return
        lp = self.cfg._launchpad("flap") or {}
        if not lp.get("manager"):          # no flap Portal configured → nothing to read
            return
        st = await self.flap_state(snap.token_address)
        if not st.get("ok") or not st.get("tradable"):
            return                          # Invalid (not a flap token) or already on DEX
        snap.launchpad = snap.launchpad or "flap"
        snap.curve_progress_pct = st["progress_pct"]
        e18 = 10 ** 18
        rate = self._eth_usd_rate()
        if not rate:                        # no ETH/USD yet → progress only, no USD scoring
            return
        price_usd = (st["price"] / e18) * rate
        circ = st["circulating_supply"] / e18
        snap.price_native = st["price"] / e18
        snap.price_usd = price_usd
        snap.market_cap_usd = price_usd * circ
        snap.liquidity_usd = (st["reserve"] / e18) * rate

    # -- Bags launchpad (registry discovery + curve pricing) ------------

    def _bags(self) -> Optional["BagsClient"]:
        """A BagsClient bound to the session, or None if Bags isn't configured."""
        if self._session is None:
            return None
        lp = self.cfg._launchpad("bags") or {}
        lens = str(lp.get("manager") or "").strip()
        factory = str(lp.get("factory") or "").strip()
        if not lens and not factory:
            return None
        return BagsClient(self.cfg, self._session, lens=lens, factory=factory)

    async def _poll_bags(self) -> list[TokenSnapshot]:
        """Discover new Bags launches from the factory registry tail."""
        bags = self._bags()
        if bags is None or not bags.can_discover():
            return []
        try:
            toks, total = await bags.newest_tokens(
                limit=25, known_total=self._bags_last_total)
        except Exception:
            log.debug("bags discovery failed", exc_info=True)
            return []
        first_run = self._bags_last_total is None
        self._bags_last_total = total
        if first_run or not toks:
            return []   # on cold start just set the cursor; don't flood with history
        out = []
        for t in toks:
            out.append(TokenSnapshot(
                chain=self.cfg.chain.dexscreener_chain, pair_address="",
                token_address=t, age_minutes=0.0, launchpad="bags"))
        log.info("bags: %d new launch(es) from registry (total=%d)", len(out), total)
        return out

    async def bags_state(self, token: str) -> dict:
        """Read a Bags token's on-curve state (for /bagsstate + enrichment)."""
        bags = self._bags()
        if bags is None or not bags.can_price():
            return {"ok": False, "error": "Bags not configured (set RHL2_BAGS_MANAGER=<lens>)"}
        st = await bags.token_state(token)
        if st is None:
            return {"ok": False, "error": "no state (rate-limited or lens wrong)"}
        if not st.get("exists"):
            return {"ok": False, "error": "not a Bags token (exists=false)"}
        return {"ok": True, **st}

    async def _enrich_bags_curve(self, snap: TokenSnapshot) -> None:
        """Price a Bags token still on the curve, via BagsLens.getTokenState."""
        if snap.market_cap_usd:                      # already priced (listed/migrated)
            return
        bags = self._bags()
        if bags is None or not bags.can_price():
            return
        st = await bags.token_state(snap.token_address)
        if not st or not st.get("exists") or st.get("migrated"):
            return                                    # not Bags, or already on the pool
        snap.launchpad = snap.launchpad or "bags"
        snap.curve_progress_pct = float(st.get("bondingProgressPct") or 0)
        e18 = 10 ** 18
        rate = self._eth_usd_rate()
        if not rate:
            return
        price_usd = (st.get("priceQuotePerToken", 0) / e18) * rate
        snap.price_native = st.get("priceQuotePerToken", 0) / e18
        snap.price_usd = price_usd
        snap.liquidity_usd = (st.get("realQuoteReserves", 0) / e18) * rate
        # Circulating ≈ tokens sold off the curve = virtual - real token reserves;
        # mcap = spot price × circulating (both 18-dec), a sound curve-phase proxy.
        circ = max(0, st.get("virtualTokenReserves", 0) - st.get("realTokenReserves", 0))
        if circ:
            snap.market_cap_usd = price_usd * (circ / e18)

    def bagsstate_report(self, token: str, st: dict) -> str:
        if not st.get("ok"):
            return f"=== BAGS STATE {token} ===\n❌ {st.get('error', 'unknown')}"
        e18 = 10 ** 18
        price_eth = st.get("priceQuotePerToken", 0) / e18
        resv = st.get("realQuoteReserves", 0) / e18
        thr = st.get("thresholdQuote", 0) / e18
        prog = st.get("bondingProgressPct", 0)
        return "\n".join([
            f"=== BAGS STATE {token} ===",
            f"status: {'MIGRATED (on DEX)' if st.get('migrated') else 'on curve'}",
            f"graduation: {prog}%  ({st.get('totalRaised',0)/e18:.4f} / {thr:.4f} ETH)",
            f"price: {price_eth:.12f} ETH/token",
            f"reserves: {resv:.6f} ETH  (real quote)",
            f"curve: {st.get('curve','?')}",
            "",
            ("→ on the curve — this is the pre-graduation pricing we score."
             if not st.get("migrated") else
             "→ migrated — read live price from the Uniswap v4 pool / DexScreener."),
        ])

    # -- pre-migration curve pattern ------------------------------------

    def _active_curve_profile(self) -> dict:
        """The learned winning-setup profile if enough winners exist, else the
        bootstrap prior. Cached for an hour to avoid re-deriving every cycle."""
        now = time.time()
        if (self._curve_profile is not None and self._curve_profile_ts
                and now - self._curve_profile_ts < 3600):
            return self._curve_profile
        learned = None
        try:
            setups = self.storage.curve_setups()
            learned = learn_profile(
                setups, min_winners=self.cfg.runtime.curve_pattern_min_winners)
        except Exception:
            log.debug("curve profile learn failed", exc_info=True)
        self._curve_profile = learned or bootstrap_profile()
        self._curve_profile_ts = now
        return self._curve_profile

    def _is_on_curve(self, snap: TokenSnapshot) -> bool:
        """A token still on a bonding curve (priced pre-graduation, no DEX pair)."""
        return (snap.curve_progress_pct is not None
                and (snap.launchpad or "") in ("flap", "bags"))

    async def _track_curve(self, snap: TokenSnapshot) -> None:
        """Observe an on-curve token, match it to the winning profile, maybe alert."""
        rc = self.cfg.runtime
        if not rc.curve_pattern_enabled or not self._is_on_curve(snap):
            return
        now = time.time()
        # Velocity baseline: the most recent observation at least one interval old,
        # so Δ over ~10min is meaningful (not per-20s-cycle noise).
        baseline = self.storage.curve_observation_before(
            snap.token_address, now - rc.curve_obs_interval_seconds) \
            or self.storage.last_curve_observation(snap.token_address)
        feats = extract_features(snap, baseline, now)

        # Persist at most once per interval so the table stays bounded.
        last = self.storage.last_curve_observation(snap.token_address)
        if last is None or now - float(last["ts"]) >= rc.curve_obs_interval_seconds:
            self.storage.record_curve_observation(
                snap.token_address, snap.curve_progress_pct, feats.get("reserve_eth"),
                snap.market_cap_usd, snap.price_usd, snap.holder_count,
                int(feats.get("smart_count") or 0), snap.age_minutes, 1)

        # Evaluate the match regardless of alerting so conviction can use it.
        profile = self._active_curve_profile()
        matched, score, hits, _misses = curve_match(feats, profile)
        snap.curve_matched = bool(matched and is_confirmed_climb(feats))

        if not rc.curve_match_alert or self.cfg.runtime.dry_run:
            return
        if snap.curve_matched:
            if self.storage.pos_event_new("curve_match|" + snap.token_address.lower()):
                learned = self._curve_profile_is_learned()
                await self._send_html(self._curve_match_html(snap, score, feats, learned))
                log.info("CURVE MATCH $%s score=%.0f progress=%.0f%%",
                         snap.symbol, score, snap.curve_progress_pct or 0)

    def _curve_profile_is_learned(self) -> bool:
        """Whether the active profile came from data (vs the bootstrap prior)."""
        try:
            setups = self.storage.curve_setups()
            return learn_profile(
                setups, min_winners=self.cfg.runtime.curve_pattern_min_winners) is not None
        except Exception:
            return False

    def _curve_match_html(self, snap: TokenSnapshot, score: float, feats: dict,
                          learned: bool) -> str:
        from html import escape as _esc
        src = "learned" if learned else "bootstrap"
        prog = feats.get("progress") or 0
        pv = feats.get("progress_velocity") or 0
        hv = feats.get("holder_velocity") or 0
        sym = _esc(snap.symbol or "???")
        lines = [
            f"🧬 <b>CURVE MATCH — ${sym}</b>  ({score:.0f}/100, {src})",
            f"CA: <code>{snap.token_address}</code>",
            f"Curve: {prog:.0f}% filled · climbing +{pv:.1f}%/h · "
            f"holders +{hv:.1f}/h",
            f"MCAP: ${self._fmt_usd(snap.market_cap_usd)} · "
            f"Liq: ${self._fmt_usd(snap.liquidity_usd)}",
        ]
        if snap.smart_money_wallets:
            lines.append(f"🧠 {len(snap.smart_money_wallets)} smart wallet(s) in")
        lines.append("<i>Matches the winning pre-graduation setup — early entry, "
                     "still on the curve.</i>")
        return "\n".join(lines)

    @staticmethod
    def _fmt_usd(x) -> str:
        if not x:
            return "?"
        if x >= 1000:
            return f"{x/1000:.1f}k"
        return f"{x:.0f}"

    def _curve_setup_features(self, token: str) -> dict:
        """Summarise a token's on-curve trajectory into a learnable feature vector.

        Built from the first and last stored observations: last-values for level
        features, first→last slope for velocities. Mirrors extract_features so the
        learned bands are comparable to what a live token is matched on.
        """
        first = self.storage.first_curve_observation(token)
        last = self.storage.last_curve_observation(token)
        if not first or not last:
            return {}
        dt_h = max((float(last["ts"]) - float(first["ts"])) / 3600.0, 1e-6)

        def slope(col):
            a, b = first[col], last[col]
            return ((float(b) - float(a)) / dt_h) if (a is not None and b is not None) else 0.0

        return {
            "progress": last["progress"],
            "progress_velocity": slope("progress"),
            "reserve_eth": last["reserve_eth"],
            "reserve_velocity": slope("reserve_eth"),
            "mcap_usd": last["mcap_usd"],
            "holders": last["holders"],
            "holder_velocity": slope("holders"),
            "smart_count": float(last["smart_count"] or 0),
            "age_min": last["age_min"],
        }

    def _label_curve_setup(self, token: str, entry_price, peak_now, win_mult: float) -> None:
        """Record a tracked on-curve token's setup + realized outcome (win >= mult).

        Multiple is peak / curve-entry price, where peak is the best of the price
        we saw on the curve and the current (post-graduation) reprice — capturing
        both the on-curve run and any post-graduation pump.
        """
        if self.storage.has_curve_setup(token):
            return
        if self.storage.curve_observation_count(token) < 2:
            return
        first = self.storage.first_curve_observation(token)
        entry = (first["price_usd"] if first and first["price_usd"] else entry_price) or 0
        obs_peak = self.storage.curve_price_peak(token) or 0
        peak = max(peak_now or 0, obs_peak)
        mult = (peak / entry) if entry else 0.0
        feats = self._curve_setup_features(token)
        if not feats:
            return
        self.storage.save_curve_setup(token, feats, win=mult >= win_mult, peak_mult=mult)
        log.info("curve setup labeled: %s mult=%.1fx win=%s", token, mult, mult >= win_mult)

    def curvepattern_report(self) -> str:
        """Text summary of the active curve profile + learning progress."""
        wins, total = self.storage.curve_setup_counts()
        tracked = len(self.storage.tracked_curve_tokens())
        learned = self._curve_profile_is_learned()
        need = self.cfg.runtime.curve_pattern_min_winners
        profile = self._active_curve_profile()
        lines = ["=== CURVE PATTERN ===",
                 f"tracking {tracked} on-curve token(s)",
                 f"labeled setups: {total} ({wins} winners ≥ "
                 f"{self.cfg.runtime.winner_harvest_win_mult:.0f}x)",
                 f"profile: {'LEARNED from winners' if learned else f'bootstrap prior (need {need} winners to learn)'}",
                 ""]
        for key, (lo, hi, w) in profile.items():
            bound = []
            if lo is not None:
                bound.append(f"≥{lo:g}")
            if hi is not None:
                bound.append(f"≤{hi:g}")
            lines.append(f"  {key}: {' and '.join(bound) or 'any'}  (w{w:g})")
        lines.append("")
        lines.append("🧬 alerts fire when a fresh on-curve token matches this "
                     "profile AND is actively climbing.")
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
        except Exception as exc:
            log.warning("command poll failed: %r", exc)
            return
        max_age = self.cfg.runtime.command_max_age_seconds
        now = time.time()
        for u in updates:
            msg = u.get("message") or u.get("channel_post") or u.get("edited_channel_post") or {}
            chat = msg.get("chat") or {}
            text = (msg.get("text") or "").strip()
            if not text.startswith("/"):
                continue
            # Skip stale commands: after a redeploy, getUpdates returns the pending
            # backlog. Without this, every restart re-runs old /harvest etc. We
            # still advance the offset past them (below) so they're not re-fetched.
            msg_date = msg.get("date")
            if max_age and msg_date and (now - msg_date) > max_age:
                log.info("skipping stale command %r (%.0fs old)", text, now - msg_date)
                continue
            chat_id = str(chat.get("id"))
            from_id = (msg.get("from") or {}).get("id")
            is_alert = chat_id == str(tg.alert_chat_id)
            is_private = chat.get("type") == "private"          # a DM to the bot
            is_admin = bool(tg.admin_user_ids) and from_id in tg.admin_user_ids
            if not (is_alert or is_private or is_admin):
                log.info("ignoring command from chat %s (not alert chat / DM / admin)", chat_id)
                continue
            log.info("command: %s (chat=%s)", text, chat_id)
            self._reply_chat = chat_id      # reply where the command came from
            try:
                await self._handle_command(text)
            finally:
                self._reply_chat = None
        if nxt is not None and nxt != self._cmd_offset:
            self._cmd_offset = nxt
            self.storage.kv_set("cmd_offset", str(nxt))

    @staticmethod
    def _help_text() -> str:
        return "\n".join([
            "🤖 <b>RH L2 Scanner — commands</b>",
            "",
            "<b>Diagnostics</b>",
            "<code>/diag</code> — RPC + DB + feature health check",
            "<code>/stats [hours]</code> — activity snapshot (discovered/alerts/clusters)",
            "<code>/perf</code> — how your alerts have performed (peak x, hit/rug rate)",
            "<code>/pnl [tp] [sl]</code> — simulated P&amp;L if you traded the alerts "
            "(TP/SL, expectancy, drawdown, by-conviction)",
            "<code>/inspect 0xCA</code> — trace one token through the full pipeline",
            "",
            "<b>Mute a token</b>",
            "<code>/zero 0xCA</code> — stop alerts for this token",
            "<code>/unzero 0xCA</code> — un-mute it",
            "<code>/muted</code> — list muted tokens",
            "",
            "<b>Block scam symbols</b>",
            "<code>/block SYMBOL</code> — never alert this ticker (e.g. /block ROBINHOOD)",
            "<code>/unblock SYMBOL</code> — remove a runtime block",
            "<code>/blocked</code> — list blocked symbols",
            "",
            "<b>Smart-money wallets</b>",
            "<code>/smart 0xWallet</code> — add a wallet to the smart-money set",
            "<code>/unsmart 0xWallet</code> — remove one",
            "<code>/smartlist</code> — list them (manual vs auto-harvested)",
            "<code>/group 0xA 0xB</code> — merge sybil wallets into one entity (cluster counts them once)",
            "<code>/ungroup 0xWallet</code> — undo a runtime group",
            "<code>/groups</code> — list wallet groups",
            "",
            "<b>Research &amp; tuning</b>",
            "<code>/wallet 0xWallet</code> — what tokens a wallet recently bought",
            "<code>/audit 0xToken</code> — scan the contract for mint/blacklist/pause/fee hooks + wash-trading",
            "<code>/deployer 0xToken</code> — who created a token (finds a launchpad's manager)",
            "<code>/flapstate 0xToken</code> — live flap curve price + graduation progress "
            "(one call, Portal getTokenV2)",
            "<code>/bagsstate 0xToken</code> — live Bags curve price + graduation progress "
            "(BagsLens getTokenState)",
            "<code>/curvepattern</code> — the learned pre-migration winning-setup profile "
            "+ 🧬 match progress",
            "<code>/curveprobe 0xToken [fn]</code> — raw getter probe (fallback; add a fn name "
            "for a single rate-limit-proof call)",
            "<code>/harvest 0xCA</code> — feed a winner we MISSED: harvests its early "
            "buyers into the smart set so future similar setups score higher",
            "<code>/harvestrug 0xCA</code> — log a rug: marks repeat-rugger wallets "
            "toxic so tokens they buy get demoted",
            "<code>/alpha</code> — top wallets by winner-overlap (⭐ = core-alpha)",
            "<code>/deployers</code> — deployer win-rates (direct-deploy tokens)",
            "<code>/honeypots</code> — known honeypot deployers (serial-scammer blocklist)",
            "<code>/narratives</code> — which meta is printing (hit-rate by narrative)",
            "<code>/kol 0xWallet Name</code> — tag an influencer wallet (📣 on its buys)",
            "<code>/calibrate 0xCA1 0xCA2 …</code> — tune thresholds against your known winners",
            "<code>/autotune [run|reset]</code> — bounded auto-tuned scoring weights "
            "(view state, force a run, or revert to baseline)",
            "",
            "<i>Alerts you'll get: 🚨 Gem / 👀 Watch / 🌱 Early · 🔼 Upgrades · "
            "🧠🚨 Smart-money clusters · 📈 Milestones (Nx) · ⚠️ Dumps · "
            "🔴 Smart-money exits.</i>",
        ])

    async def _handle_command(self, text: str) -> None:
        if not text.startswith("/"):
            return
        parts = text.split()
        cmd = parts[0][1:].split("@")[0].lower()   # strip leading / and @botname
        arg = parts[1].lower() if len(parts) > 1 else ""
        valid_ca = arg.startswith("0x") and len(arg) == 42

        if cmd in ("help", "start", "commands"):
            await self._send_html(self._help_text())
        elif cmd in ("group",):
            addrs = [p.lower() for p in parts[1:] if p.lower().startswith("0x") and len(p) == 42]
            if len(addrs) < 2:
                await self._send_html("Usage: <code>/group 0xWalletA 0xWalletB [more…]</code> "
                                      "— merge sybil wallets into one entity")
                return
            # Reuse an existing group if any address already has one, else make one.
            existing = self._entity_of(addrs[0])
            group = existing if existing != addrs[0] else f"grp:{addrs[0][:8]}"
            for a in addrs:
                self.storage.set_wallet_group(a, group)
            await self._send_html(f"🔗 Grouped {len(addrs)} wallets as <b>{group}</b> — "
                                  "they now count as one buyer for cluster alerts.")
        elif cmd in ("ungroup",):
            if not valid_ca:
                await self._send_html("Usage: <code>/ungroup 0x&lt;wallet&gt;</code>")
                return
            self.storage.remove_wallet_group(arg)
            await self._send_html(f"🔗 Ungrouped <code>{arg}</code> (config groups, if any, remain).")
        elif cmd in ("groups", "grouplist"):
            merged: dict[str, list] = {}
            allg = {**(self.cfg.wallet_watch.wallet_groups or {}), **self.storage.wallet_groups()}
            for w, g in allg.items():
                merged.setdefault(g, []).append(w)
            if not merged:
                await self._send_html("🔗 No wallet groups.")
                return
            lines = ["🔗 <b>Wallet groups</b> (count as one buyer each):"]
            for g, ws in merged.items():
                lines.append(f"<b>{g}</b>: " + ", ".join(f"<code>{w[:6]}…{w[-4:]}</code>" for w in ws))
            await self._send_html("\n".join(lines))
        elif cmd == "zero":
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
        elif cmd in ("stats", "activity"):
            hours = 24.0
            if len(parts) > 1:
                try:
                    hours = max(1.0, float(parts[1]))
                except ValueError:
                    pass
            st = self.storage.activity_stats(time.time() - hours * 3600.0)
            await self._send_html(
                f"📊 <b>Activity — last {hours:g}h</b>\n"
                f"Discovered: <b>{st['discovered']}</b> new tokens "
                f"(tracking {st['tracked_total']} total)\n"
                f"Alerts: <b>{st['alerts']}</b> · Clusters: <b>{st['clusters']}</b>\n"
                f"Follow-ups: 📈 {st['milestones']} · ⚠️ {st['dumps']} · 🔴 {st['exits']}\n"
                f"Best score: {st['best_score']:.0f} · Monitoring {st['open_positions']} positions")
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
        elif cmd in ("pnl", "strategy", "backtest"):
            from .paper import PaperTrader, format_pnl_html, strategy_pnl
            # optional args: /pnl [tp] [sl]
            def _num(i, default):
                try:
                    return float(parts[i])
                except (IndexError, ValueError):
                    return default
            tp, sl = _num(1, 2.5), _num(2, 0.55)
            rows = self.storage.all_paper_trades()
            p = strategy_pnl(rows, tp=tp, sl=sl)
            await self._send_html(format_pnl_html(p))
        elif cmd in ("autotune", "weights"):
            from html import escape as _esc
            from . import autotune as _at
            arg = parts[1].lower() if len(parts) > 1 else ""
            if arg == "reset":
                _at.reset(self.storage)
                self._apply_weight_override()   # no override now → reload does nothing, but
                for k, v in self._weights_baseline.items():   # snap live weights back to baseline
                    setattr(self.cfg.weights, k, v)
                await self._send_html("🔧 Auto-tune <b>reset</b> — weights back to baseline.")
                return
            if arg in ("run", "now"):
                self._last_autotune_ts = None
                await self._maybe_autotune()
            rc = self.cfg.runtime
            state = _at.describe(self.storage, self._weights_baseline)
            gate = ("on" if rc.autotune_enabled else "off (RHL2_AUTOTUNE=1 to enable)")
            await self._send_html(
                "🔧 <b>Scoring weights</b>\n"
                f"{_esc(state)}\n"
                f"<i>auto-tune {gate}; dormant until {rc.autotune_min_settled} settled. "
                f"/autotune reset to revert.</i>")
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
        elif cmd in ("harvest", "winner", "gotcha"):
            candidates = [p for p in parts[1:] if len(p) >= 32]   # plausible token ids
            evm = [p.lower() for p in candidates
                   if p.lower().startswith("0x") and len(p) == 42]
            # Solana (base58, not 0x) and other non-EVM ids can't be harvested here:
            # this scanner is Robinhood-Chain-only. Flag them instead of dropping.
            nonevm = [p for p in candidates
                      if not (p.lower().startswith("0x") and len(p) == 42)]
            if not evm and not nonevm:
                await self._send_html(
                    "Usage: <code>/harvest 0xCA [0xCA2 …]</code>\n"
                    "Feed a Robinhood-Chain token that pumped (that we missed) — "
                    "harvests its earliest buyers into the smart set so future "
                    "similar setups score higher, and shows what blocked it.")
                return
            if nonevm:
                await self._send_html(
                    f"⚠️ Skipped {len(nonevm)} non-Robinhood token(s). This scanner "
                    "is <b>Robinhood-Chain only</b> — Solana &amp; Base winners belong "
                    "in <b>memelab</b> (the multi-chain half), not here.")
            for ca in evm[:5]:            # cap per message (each does explorer reads)
                try:
                    await self._send_html(await self._harvest_manual_winner(ca))
                except Exception as exc:  # noqa: BLE001
                    await self._send_html("harvest failed: " + __import__("html").escape(str(exc)))
        elif cmd in ("harvestrug", "rug", "logrug"):
            cas = [p.lower() for p in parts[1:]
                   if p.lower().startswith("0x") and len(p) == 42]
            if not cas:
                await self._send_html(
                    "Usage: <code>/harvestrug 0xCA</code>\n"
                    "Log a rug: marks its deployer a rugger and credits its early "
                    "wallets toward toxicity, so tokens repeat-ruggers buy get demoted.")
                return
            for ca in cas[:5]:
                try:
                    await self._send_html(await self._harvest_rug(ca))
                except Exception as exc:  # noqa: BLE001
                    await self._send_html("rug log failed: " + __import__("html").escape(str(exc)))
        elif cmd in ("alpha", "alphas", "reputation", "rep"):
            top = self.storage.top_reputation_wallets(limit=15)
            if not top:
                await self._send_html(
                    "💎 No wallet reputation yet — it builds as winners are harvested "
                    "(/harvest) and the winner-sweep labels early buyers.")
                return
            core_min = self.cfg.runtime.core_alpha_min_overlap
            lines = ["💎 <b>Top wallets by winner-overlap</b> "
                     f"(≥{core_min} = core-alpha, fires 💎 on its own)"]
            for r in top:
                star = " ⭐" if r["overlap"] >= core_min else ""
                lab = self.cfg.wallet_watch.labels.get(r["wallet"], "")
                tag = f" ({__import__('html').escape(lab)})" if lab else ""
                lines.append(f"<code>{r['wallet'][:8]}…{r['wallet'][-4:]}</code>{tag} — "
                             f"{r['overlap']} winners, best {r['best_mult']:.0f}x{star}")
            await self._send_html("\n".join(lines))
        elif cmd in ("narratives", "meta", "metas"):
            from .narrative import hot_narratives, narrative_stats
            stats = narrative_stats(self.storage.all_paper_trades())
            settled = {k: v for k, v in stats.items() if v["n"]}
            if not settled:
                await self._send_html("🔥 No narrative data yet — builds as alerts settle.")
                return
            hot = hot_narratives(stats)
            ranked = sorted(settled.items(), key=lambda kv: (-kv[1]["hit_rate"], -kv[1]["n"]))
            lines = ["🔥 <b>Narratives by hit-rate</b> (settled alerts)"]
            for nar, s in ranked[:10]:
                star = " 🔥" if nar in hot else ""
                lines.append(f"• <b>{nar}</b>: n={s['n']} · hit {s['hit_rate']:.0%} · "
                             f"rug {s['rug_rate']:.0%}{star}")
            lines.append("<i>🔥 = boosted right now. Rotates as the meta shifts.</i>")
            await self._send_html("\n".join(lines))
        elif cmd == "kol":
            wallets = [p.lower() for p in parts[1:]
                       if p.lower().startswith("0x") and len(p) == 42]
            if not wallets:
                await self._send_html(
                    "Usage: <code>/kol 0xWallet Name</code> — tag an influencer/KOL "
                    "wallet; its buys fire a 📣 alert and it counts as smart money.")
                return
            name = " ".join(p for p in parts[2:] if not p.lower().startswith("0x")) or "KOL"
            w = wallets[0]
            self._add_smart_wallet(w, source="kol", note=name)
            await self._send_html(
                f"📣 Tagged <code>{w}</code> as KOL <b>{__import__('html').escape(name)}</b> — "
                "its buys now fire a dedicated alert.")
        elif cmd in ("honeypots", "traps", "scammers"):
            hps = self.storage.honeypot_deployers()
            if not hps:
                await self._send_html("🍯 No honeypot deployers logged yet — builds "
                                      "as 🍯 warnings fire on trap tokens.")
                return
            lines = ["🍯 <b>Known honeypot deployers</b> (their next launch is flagged)"]
            for h in hps:
                lines.append(f"<code>{h['deployer'][:8]}…{h['deployer'][-4:]}</code> — "
                             f"<b>{h['count']}</b> trap(s)")
            await self._send_html("\n".join(lines))
        elif cmd in ("deployers", "creators"):
            devs = self.storage._conn.execute(
                "SELECT deployer, "
                "SUM(CASE WHEN outcome='winner' THEN 1 ELSE 0 END) wins, "
                "SUM(CASE WHEN outcome='rug' THEN 1 ELSE 0 END) rugs, COUNT(*) total "
                "FROM deployer_tokens GROUP BY deployer "
                "ORDER BY wins DESC, total DESC LIMIT 15").fetchall()
            if not devs:
                await self._send_html(
                    "🏭 No deployer stats yet. (Note: on flap/Bags the on-chain "
                    "creator is the shared manager, so per-deployer stats only build "
                    "for direct-deploy tokens.)")
                return
            lines = ["🏭 <b>Deployers by win-rate</b>"]
            for d in devs:
                wr = (d["wins"] / d["total"]) if d["total"] else 0
                lines.append(f"<code>{d['deployer'][:8]}…{d['deployer'][-4:]}</code> — "
                             f"{d['wins']}W/{d['rugs']}R of {d['total']} ({wr:.0%})")
            await self._send_html("\n".join(lines))
        elif cmd in ("audit", "contract"):
            if not valid_ca:
                await self._send_html("Usage: <code>/audit 0x&lt;token&gt;</code> — "
                                      "scan the contract for mint/blacklist/pause/fee hooks")
                return
            try:
                await self._send_html(await self.audit_contract(arg))
            except Exception as exc:  # noqa: BLE001
                await self._send_html("audit failed: " + __import__("html").escape(str(exc)))
        elif cmd in ("curveprobe", "probe"):
            if not valid_ca:
                await self._send_html("Usage: <code>/curveprobe 0x&lt;flap token&gt;</code>")
                return
            func = parts[2] if len(parts) > 2 else ""   # /curveprobe 0x.. reserves → single-call
            try:
                from html import escape as _esc
                await self._send_html("<pre>" + _esc(await self.curveprobe(arg, func=func)) + "</pre>")
            except Exception as exc:
                await self._send_html("curveprobe failed: " + __import__("html").escape(str(exc)))
        elif cmd in ("flapstate", "flap", "curve"):
            if not valid_ca:
                await self._send_html("Usage: <code>/flapstate 0x&lt;flap token&gt;</code> "
                                      "— live bonding-curve price + graduation progress")
                return
            try:
                from html import escape as _esc
                await self._send_html("<pre>" + _esc(await self.flapstate(arg)) + "</pre>")
            except Exception as exc:
                await self._send_html("flapstate failed: " + __import__("html").escape(str(exc)))
        elif cmd in ("bagsstate", "bags"):
            if not valid_ca:
                await self._send_html("Usage: <code>/bagsstate 0x&lt;bags token&gt;</code> "
                                      "— live Bags curve price + graduation progress")
                return
            try:
                from html import escape as _esc
                st = await self.bags_state(arg)
                await self._send_html("<pre>" + _esc(self.bagsstate_report(arg, st)) + "</pre>")
            except Exception as exc:
                await self._send_html("bagsstate failed: " + __import__("html").escape(str(exc)))
        elif cmd in ("curvepattern", "pattern"):
            try:
                from html import escape as _esc
                await self._send_html("<pre>" + _esc(self.curvepattern_report()) + "</pre>")
            except Exception as exc:
                await self._send_html("curvepattern failed: " + __import__("html").escape(str(exc)))
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
        else:
            # Never leave a /command in silence — that reads as "the bot is dead".
            from html import escape as _esc
            await self._send_html(
                f"❓ Unknown command <code>/{_esc(cmd)}</code>. "
                "Send <code>/help</code> for the full list.")

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
            # Smart-money EXIT: a smart wallet selling a token smart money bought.
            elif ev.side == "sell" and ev.wallet.lower() in smart_set:
                await self._check_exit(ev)

    async def _check_exit(self, ev) -> None:
        """Alert when a smart wallet sells a token smart money had bought."""
        if not self.cfg.runtime.smart_exit_enabled:
            return
        token = ev.token_address
        if not self.storage.has_smart_buy(token):
            return  # not a token we were tracking via smart-money buys
        if not self.storage.pos_event_new(f"{token.lower()}|exit|{ev.wallet.lower()}"):
            return  # already flagged this wallet's exit from this token
        label = self.cfg.wallet_watch.labels.get(ev.wallet.lower(),
                                                 ev.wallet[:6] + "…" + ev.wallet[-4:])
        await self._send_html(format_exit_html(ev.symbol, token, label, ev.usd, ev.chart_url))
        log.info("EXIT %s sold by %s", ev.symbol, label)

    def _exit_model(self) -> Optional[dict]:
        """Learned top-zone model (cached hourly). None until enough winners."""
        rc = self.cfg.runtime
        now = time.time()
        if self._exit_model_ts and now - self._exit_model_ts < 3600:
            return self._exit_model_cache
        self._exit_model_ts = now
        try:
            from .exit_intel import learn_exit_model
            self._exit_model_cache = learn_exit_model(
                self.storage.all_paper_trades(),
                win_multiple=rc.paper_digest_win_multiple,
                min_winners=rc.exit_min_winners)
        except Exception:  # noqa: BLE001
            self._exit_model_cache = None
        return self._exit_model_cache

    async def _monitor_positions(self, dex) -> None:
        """After re-pricing, ping milestones (📈 Nx), dumps (⚠️), and the learned
        top zone (⏏️) on alerted tokens."""
        rc = self.cfg.runtime
        if self.perf is None or not rc.position_monitor_enabled:
            return
        exit_model = self._exit_model() if rc.exit_intel_enabled else None
        for row in self.storage.open_paper_trades():
            entry = row["entry_price"]
            last = row["last_price"]
            peak = row["max_mult"] or 1.0
            if not entry or not last:
                continue
            pair, token, sym = row["pair_address"], row["token_address"], row["symbol"]
            cur = last / entry
            chart = f"https://dexscreener.com/{self.cfg.chain.dexscreener_chain}/{pair}"
            # Thread follow-ups under the original alert (one live thread/token).
            reply_to = None
            mval = self.storage.kv_get("msgid:" + token.lower())
            if mval:
                try:
                    reply_to = int(mval)
                except ValueError:
                    reply_to = None
            # Milestones (📈) — highest crossed only, one ping each; shows live x.
            for m in sorted(rc.milestone_multiples):
                if cur >= m and self.storage.pos_event_new(f"{token}|x{m:g}"):
                    await self._send_html(
                        format_milestone_html(sym, token, m, chart, current_mult=cur),
                        reply_to=reply_to)
                    log.info("MILESTONE %s %gx", sym, m)
            # Learned top zone (⏏️) — proactive take-profit before the full dump.
            if exit_model is not None:
                from .exit_intel import top_zone
                fire, reason = top_zone(cur, peak, exit_model,
                                        rc.exit_early_giveback_pct)
                if fire and self.storage.pos_event_new(f"{token}|topzone"):
                    await self._send_html(
                        format_top_zone_html(sym, token, reason, cur, chart),
                        reply_to=reply_to)
                    log.info("TOP ZONE %s at %.2gx", sym, cur)

            # Dump guard (⚠️) — ran up then fell back hard.
            if peak >= rc.dump_min_peak_mult:
                drawdown = (1.0 - (cur / peak)) * 100.0
                if drawdown >= rc.dump_drawdown_pct and self.storage.pos_event_new(f"{token}|dump"):
                    await self._send_html(
                        format_dump_html(sym, token, drawdown, peak, chart, current_mult=cur),
                        reply_to=reply_to)
                    log.info("DUMP %s -%.0f%% from peak", sym, drawdown)

    def _entity_of(self, addr: str) -> str:
        """Resolve a wallet to its sybil-group entity (or itself if ungrouped).

        DB groups (/group) override config groups; ungrouped wallets are their
        own entity.
        """
        a = addr.lower()
        groups = {**(self.cfg.wallet_watch.wallet_groups or {}), **self.storage.wallet_groups()}
        return groups.get(a, a)

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

        # KOL/influencer buy — a tagged public wallet aping in moves markets.
        kols = self.storage.kol_wallets()
        if ev.wallet.lower() in kols \
                and self.storage.pos_event_new(f"{token.lower()}|kol|{ev.wallet.lower()}"):
            await self._send_html(format_kol_html(
                kols[ev.wallet.lower()], ev.symbol, token, ev.chart_url))
            log.info("KOL %s bought $%s", kols[ev.wallet.lower()], ev.symbol)

        # Core-alpha: a single wallet proven across many winners is signal enough
        # on its own — fire a 💎 alert without waiting for a cluster (once/token).
        if rc.core_alpha_alert and self._is_core_alpha(ev.wallet) \
                and self.storage.pos_event_new(f"{token.lower()}|corealpha"):
            label = self.cfg.wallet_watch.labels.get(ev.wallet) \
                or self._entity_of(ev.wallet)
            ov = self.storage.wallet_reputation_rows([ev.wallet]).get(
                ev.wallet, {}).get("winner_overlap", 0)
            await self._send_html(format_core_alpha_html(
                ev.symbol, token, label, ov, ev.chart_url))
            log.info("CORE-ALPHA %s bought $%s (overlap %s)", ev.wallet, ev.symbol, ov)

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
            "Watching for early gems… (type /help for commands)"
        )
        try:
            from .tgtools import send_message
            ok, detail, _ = await send_message(tg.bot_token, tg.alert_chat_id, text, self._session)
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

        # Poll Telegram commands FIRST, so the bot stays responsive even if
        # discovery/enrichment fails or stalls this cycle. A broken cycle must
        # never mute /diag, /stats, etc. — that's how we lose the ability to
        # debug it live.
        try:
            await self._poll_commands()
        except Exception:
            log.exception("command poll failed")

        # Three discovery streams: DexScreener (indexed), DEX factory logs
        # (earliest pool), and bonding-curve launchpad logs (earliest of all —
        # flap tokens before they graduate to a pool DexScreener can see).
        # return_exceptions=True: one stream throwing must NOT abort the cycle
        # (it would take alerts AND command-polling down with it).
        try:
            _streams = await asyncio.wait_for(
                asyncio.gather(
                    dex.fetch_new_pairs(),
                    self._listener.poll_new_pairs(),
                    self._curve_listener.poll_new_launches(),
                    self._poll_bags(),
                    return_exceptions=True,
                ),
                timeout=max(60.0, self.cfg.runtime.request_timeout_seconds * 6),
            )
        except asyncio.TimeoutError:
            log.warning("discovery gather timed out — skipping discovery this cycle")
            _streams = [[], [], [], []]
        _labels = ("dexscreener", "poollistener", "curve", "bags")

        def _stream(i):
            r = _streams[i]
            if isinstance(r, Exception):
                log.warning("discovery stream '%s' failed: %r", _labels[i], r)
                self._discovery_health[_labels[i]] = f"ERR: {type(r).__name__}: {r}"
                return []
            self._discovery_health[_labels[i]] = f"ok: {len(r)}"
            return r
        dex_pairs, fresh_pairs, curve_stubs, bags_stubs = (
            _stream(0), _stream(1), _stream(2), _stream(3))
        # Bags registry stubs join the curve stubs (both are pre-graduation).
        curve_stubs = list(curve_stubs) + list(bags_stubs)

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

        # Record every discovered priced token for the retroactive winner
        # harvest — BEFORE the quick-start gate, so we also learn from winners
        # that we filter out now (their early buyers are the real prize).
        if self.cfg.runtime.winner_harvest_enabled:
            for p in pairs:
                if p.price_usd and p.pair_address:
                    self.storage.add_harvest_candidate(
                        p.token_address, p.pair_address, p.symbol, p.price_usd)

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

        # (Commands are polled at the TOP of the cycle now — see run_once start —
        # so the bot stays responsive even if the scan above fails/stalls.)

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

        # Follow-up alerts on tokens we alerted on: milestones (📈) + dumps (⚠️).
        try:
            await self._monitor_positions(dex)
        except Exception:
            log.exception("position monitor failed")

        # Retroactive winner harvest — periodic sweep (not every cycle).
        rc = self.cfg.runtime
        if rc.winner_harvest_enabled:
            now = time.time()
            interval = max(0.1, rc.winner_harvest_interval_hours) * 3600.0
            if self._last_harvest_ts is None or (now - self._last_harvest_ts) >= interval:
                self._last_harvest_ts = now
                try:
                    await self._harvest_winners(dex)
                except Exception:
                    log.exception("winner harvest failed")

        # Bounded auto-tune of scoring weights (periodic, dormant until settled).
        await self._maybe_autotune()
        # Cross-pollinate: pull memelab's proven robinhood wallets (hourly).
        self._maybe_sync_memelab_reputation()

        return [r for r in results if isinstance(r, ScoreResult)]

    def _apply_weight_override(self) -> None:
        """Load a persisted auto-tuned weight set (if any) onto the live config."""
        try:
            from .autotune import load_override
            w = load_override(self.storage)
        except Exception:  # noqa: BLE001
            w = None
        if not w:
            return
        for k in ("safety", "distribution", "momentum", "discovery"):
            if k in w:
                setattr(self.cfg.weights, k, float(w[k]))
        log.info("autotune: applied persisted weights %s", w)

    async def _maybe_autotune(self) -> None:
        """Periodically re-tune scoring weights off settled-alert performance."""
        rc = self.cfg.runtime
        if not rc.autotune_enabled or self.perf is None:
            return
        now = time.time()
        interval = max(0.1, rc.autotune_interval_hours) * 3600.0
        if self._last_autotune_ts is not None and (now - self._last_autotune_ts) < interval:
            return
        self._last_autotune_ts = now
        try:
            from .autotune import apply, propose_weights
            rep = self.perf.report(win_multiple=rc.paper_digest_win_multiple)
            current = {
                "safety": self.cfg.weights.safety, "distribution": self.cfg.weights.distribution,
                "momentum": self.cfg.weights.momentum, "discovery": self.cfg.weights.discovery,
            }
            result = propose_weights(
                rep, self._weights_baseline, current,
                min_settled=rc.autotune_min_settled, min_bucket=rc.autotune_min_bucket,
                step=rc.autotune_step, max_drift=rc.autotune_max_drift,
            )
            if result.get("changed") and apply(self.storage, result):
                self._apply_weight_override()
                log.info("autotune: %s", result.get("reason"))
        except Exception:  # noqa: BLE001
            log.exception("autotune failed")

    def _maybe_sync_memelab_reputation(self) -> None:
        """Hourly: pull memelab's proven robinhood wallets into our reputation.
        Best-effort + additive — dormant when memelab.db isn't on the volume."""
        import os
        now = time.time()
        if self._mm_rep_ts and now - self._mm_rep_ts < 3600:
            return
        self._mm_rep_ts = now
        try:
            from .memelab_bridge import import_memelab_reputation
            n = import_memelab_reputation(
                self.storage, os.environ.get("MEMELAB_DB", ""),
                min_overlap=self.cfg.runtime.memelab_bridge_min_overlap)
            if n:
                self._core_alpha_ts = 0.0   # force the core-alpha cache to refresh
        except Exception:  # noqa: BLE001
            log.debug("memelab reputation sync failed", exc_info=True)

    def _active_mm_signature(self) -> Optional[dict]:
        """memelab's learned signature (cached hourly). None until it's trained —
        so this whole feature is dormant while memelab is cold, then auto-activates."""
        import os
        now = time.time()
        if self._mm_signature_ts and now - self._mm_signature_ts < 3600:
            return self._mm_signature
        self._mm_signature_ts = now
        try:
            from .signature_bonus import load_signature
            self._mm_signature = load_signature(os.environ.get("MEMELAB_DB", ""))
        except Exception:  # noqa: BLE001
            self._mm_signature = None
        if self._mm_signature:
            log.info("memelab signature active (trained_on=%s, precision=%s) — scoring bonus on",
                     self._mm_signature.get("trained_on"), self._mm_signature.get("precision"))
        return self._mm_signature

    def _apply_signature_bonus(self, snap: TokenSnapshot, result: ScoreResult) -> ScoreResult:
        """Add a composite bonus for matching memelab's learned winner-signature."""
        if not result.safety_passed:
            return result
        sig = self._active_mm_signature()
        if not sig:
            return result
        from dataclasses import replace
        from .signature_bonus import signature_bonus
        bonus = signature_bonus(snap, sig)
        if bonus <= 0:
            return result
        new_comp = min(100.0, result.composite + bonus)
        th = self.cfg.thresholds
        lvl = (AlertLevel.STRONG if new_comp >= th.strong_alert_score
               else AlertLevel.WATCH if new_comp >= th.watch_alert_score
               else AlertLevel.SKIP)
        log.info("signature bonus +%.1f on $%s (%.0f→%.0f)", bonus, snap.symbol,
                 result.composite, new_comp)
        return replace(result, composite=new_comp, level=lvl)

    def _compute_conviction(self, snap: TokenSnapshot, result: ScoreResult):
        """Fuse every independent signal into a 0-100 conviction (confluence)."""
        from .conviction import fuse
        sig_match = None
        sig = self._active_mm_signature()
        if sig:
            try:
                from .signature_bonus import match_fraction
                frac, judged = match_fraction(snap, sig)
                sig_match = frac if judged >= 3 else None
            except Exception:  # noqa: BLE001
                sig_match = None
        return fuse({
            "composite": result.composite,
            "safety_passed": result.safety_passed,
            "signature_match": sig_match,
            "curve_match": snap.curve_matched,
            "core_alpha": len(snap.core_alpha_wallets or []),
            "smart_quality": snap.smart_money_quality_bonus or 0.0,
            "social": None,
            "toxic": snap.toxic_buyer,
        })

    async def _attach_alert_intel(self, snap: TokenSnapshot, result: ScoreResult) -> None:
        """Compute every signal into ONE alert: conviction, exit plan, KOL among
        buyers, and (gated) a contract audit. All attached to the snapshot so the
        formatter renders them in a single message."""
        conv = self._compute_conviction(snap, result)
        snap.conviction = conv.score
        snap.conviction_factors = conv.factors

        # Exit plan from the learned top-zone model + the configured stop.
        model = self._exit_model() if self.cfg.runtime.exit_intel_enabled else None
        if model:
            snap.exit_target = (f"TP ~{model['p50']:g}x–{model['p75']:g}x "
                                f"(winners' median/upper) · trail −{self.cfg.runtime.dump_drawdown_pct:.0f}%")

        # KOL/influencer wallets among this token's smart buyers.
        try:
            kols = self.storage.kol_wallets()
            snap.kol_labels = [kols[w.lower()] for w in (snap.smart_money_wallets or [])
                               if w.lower() in kols]
        except Exception:  # noqa: BLE001
            pass

        # Contract audit (few alerts/hr, so the extra explorer call is affordable).
        if self.cfg.runtime.contract_audit_on_alert:
            try:
                from .contract_audit import audit_functions, risk_summary, wash_trade
                _verified, names = await self._contract_methods(snap.token_address)
                audit = audit_functions(names)
                snap.contract_risk, snap.contract_findings = risk_summary(
                    audit, wash_trade(snap))
            except Exception:  # noqa: BLE001
                log.debug("alert-time contract audit failed", exc_info=True)

        # Dev-wallet track record — has this deployer shipped winners/rugs before?
        # (Skips shared launchpad managers, so on flap this only fires for
        # direct-deploy tokens — most useful on the EVM chains via memelab.)
        try:
            creator = await self._creator_of(snap.token_address)
            if creator and creator not in self.cfg.known_launchpad_addresses():
                hp = self.storage.is_honeypot_deployer(creator)
                rep = self.storage.deployer_reputation(creator)
                if hp:
                    snap.dev_note = f"⚠️ dev shipped {hp} honeypot(s) before"
                elif rep["rugs"]:
                    snap.dev_note = f"⚠️ dev rugged {rep['rugs']}× before"
                elif rep["wins"]:
                    snap.dev_note = f"🔥 dev launched {rep['wins']} prior winner(s)"
        except Exception:  # noqa: BLE001
            log.debug("dev-note resolution failed", exc_info=True)

    async def _check_honeypot(self, snap: TokenSnapshot, result: ScoreResult) -> bool:
        """Fire a 🍯 warning for an INTERESTING can't-sell trap (instead of the
        silent gate-skip), and record its deployer so the scammer's next launch is
        flagged. Returns True if a warning fired. Gated on interest to avoid
        warning on every one of the chain's countless scam tokens."""
        rc = self.cfg.runtime
        s = snap.safety
        sell_tax = s.sell_tax_pct
        is_hp = (s.is_honeypot is True) or \
                (sell_tax is not None and sell_tax >= rc.honeypot_sell_tax_pct)

        # Interest gate (cheap): smart money in, already alerted, or would-be score.
        interesting = bool(snap.smart_money_wallets) \
            or self.storage.alert_rank(snap.pair_address) >= 0
        if not interesting:
            would = sum(c.weighted for c in result.categories)
            interesting = would >= self.cfg.thresholds.watch_alert_score
        if not interesting:
            return False

        # Resolve the deployer once (skip shared launchpad managers) to both check
        # the serial-scammer blocklist and record a newly-found honeypot deployer.
        deployer, dep_hits = "", 0
        if rc.honeypot_probe_deployer:
            try:
                creator = await self._creator_of(snap.token_address)
                if creator and creator not in self.cfg.known_launchpad_addresses():
                    deployer = creator
                    dep_hits = self.storage.is_honeypot_deployer(deployer)
            except Exception:  # noqa: BLE001
                pass

        if not is_hp and dep_hits < 1:
            return False
        if not self.storage.pos_event_new("honeypot|" + snap.token_address.lower()):
            return False

        if is_hp and deployer:
            dep_hits = self.storage.record_honeypot_deployer(deployer, snap.token_address)

        if s.is_honeypot is True:
            reason = "Can't-sell honeypot confirmed (sell simulation failed)."
        elif sell_tax is not None and sell_tax >= rc.honeypot_sell_tax_pct:
            reason = f"Sell tax {sell_tax:.0f}% — effectively a trap."
        else:
            reason = "Deployer is a known serial honeypot scammer."
        chart = f"https://dexscreener.com/{self.cfg.chain.dexscreener_chain}/{snap.pair_address}" \
            if snap.pair_address else ""
        await self._send_html(format_honeypot_html(
            snap.symbol, snap.token_address, reason, dep_hits, chart))
        log.info("HONEYPOT %s (%s) deployer_hits=%d", snap.symbol, reason, dep_hits)
        return True

    async def _process(self, snap: TokenSnapshot) -> Optional[ScoreResult]:
        async with self._sem:
            await self._enrich(snap)

        strict = self.cfg.active_tier is RiskTier.MOMENTUM
        # Live (non-dry-run) alerts may use pragmatic safety: strict on every
        # confirmable metric, tolerant of an unconfirmed honeypot/tax.
        pragmatic = (not self.cfg.runtime.dry_run) and self.cfg.runtime.live_pragmatic_safety
        result = score_token(snap, self.cfg, strict_safety=strict, pragmatic=pragmatic)
        # Boost tokens matching memelab's learned winner-signature (dormant until
        # memelab is trained — see _active_mm_signature).
        result = self._apply_signature_bonus(snap, result)
        self.storage.mark_seen(snap)
        self.storage.record_score(snap, result)

        # Pre-migration curve tracking: observe on-curve flap tokens over time and
        # fire a 🧬 alert when one matches the learned winning setup while climbing.
        try:
            await self._track_curve(snap)
        except Exception:
            log.debug("curve tracking failed", exc_info=True)

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

        # Defensive honeypot warning: an interesting can't-sell trap fires a 🍯
        # alert (+ logs the deployer) instead of vanishing silently at the gate.
        if self.cfg.runtime.honeypot_alert_enabled:
            try:
                if await self._check_honeypot(snap, result):
                    return result   # warned — don't also emit a normal alert
            except Exception:  # noqa: BLE001
                log.debug("honeypot check failed", exc_info=True)

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

        # Quality floor — cut low-score noise. Maturity alerts (watch/strong)
        # gate on composite; fresh early-launch gates on CONVICTION instead, since
        # a brand-new token's composite is structurally low (few holders/volume)
        # yet real gems still show confluence. Signal-bypass alerts (core-alpha,
        # cluster, KOL, honeypot) are score-independent and handled elsewhere.
        if tier in (1, 2) and result.composite < rc.min_alert_score:
            log.debug("floor: %s composite %.0f < %.0f", snap.symbol,
                      result.composite, rc.min_alert_score)
            return result

        prev = self.storage.alert_rank(snap.pair_address)
        escalated = rc.realert_on_escalation and prev >= 0 and tier > prev
        cooling = self.storage.in_cooldown(snap.pair_address, rc.realert_cooldown_seconds)

        if not (prev < 0 or escalated or not cooling):
            log.debug("%s in cooldown (tier=%d, prev=%d), skipping", snap.symbol, tier, prev)
            return result

        # Fuse EVERY signal into this one alert (conviction, exit plan, KOL,
        # contract audit) — attached to the snapshot for the formatter.
        await self._attach_alert_intel(snap, result)

        # Early-launch conviction floor (composite can't gate fresh tokens fairly).
        if tier == 0 and (snap.conviction or 0.0) < rc.early_launch_min_conviction:
            log.debug("floor: early %s conviction %.0f < %.0f", snap.symbol,
                      snap.conviction or 0.0, rc.early_launch_min_conviction)
            return result

        note = self._escalation_note(prev, tier) if escalated else ""
        if tier == 0:
            html = format_early_launch_html(snap, result)
            if note:
                html = note + "\n" + html
            mid = await self._send_html(html)
            log.info("EARLY %s age=%.0fm liq=%s conv=%.0f", snap.symbol,
                     snap.age_minutes or 0, snap.liquidity_usd, snap.conviction or 0)
        else:
            mid = await self.notifier.send(snap, result, note=note)
            tag = "UPGRADE" if escalated else "ALERT"
            log.info("%s %s %s score=%.0f", tag, result.level.value, snap.symbol, result.composite)
        # Remember the FIRST alert's message id so milestone/dump follow-ups can
        # reply to it (one live thread per token).
        if mid and prev < 0:
            self.storage.kv_set("msgid:" + snap.token_address.lower(), str(mid))
        # A token reaching STRONG for the first time is a confirmed runner —
        # harvest its earliest buyers into the smart-money set (if enabled).
        if tier == 2 and prev < 2:
            await self._maybe_autoseed(snap)
        self.storage.record_alert(snap, result, rank=tier)
        # Track this alert's real outcome (peak x / hit / rug) on first alert.
        if self.perf is not None and prev < 0:
            self.perf.record_alerted(snap, result, conviction=snap.conviction or 0.0)
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
        if not rc.smart_money_autoseed:
            return
        added = await self._harvest_buyers(snap.token_address, snap.symbol,
                                           source_prefix="auto")
        if added:
            log.info("autoseed: harvested %d early buyers of $%s (winner)", added, snap.symbol)

    async def _harvest_buyers(self, token: str, symbol: str, source_prefix: str,
                              mult: float = 0.0) -> int:
        """Add a winner's earliest buyers to the smart set. Returns count added.

        Every buyer is also credited in the reputation ledger against this winner
        (wallet_winners) — DISTINCT winners per wallet = its overlap/quality, even
        for a wallet already in the set (so overlap keeps climbing).
        """
        rc = self.cfg.runtime
        if self._session is None:
            return 0
        try:
            smart = SmartMoneyClient(self.cfg, session=self._session)
            buyers = await smart.early_buyers(token, rc.smart_money_autoseed_buyers)
        except Exception:
            log.debug("early_buyers failed", exc_info=True)
            return 0
        capped = len(self.storage.smart_wallets()) >= rc.smart_money_max_set
        added = 0
        for w in buyers:
            # Ledger credit always (drives reputation) even if the set is full.
            try:
                self.storage.record_wallet_winner(w, token, mult)
            except Exception:  # noqa: BLE001
                pass
            if not capped and self._add_smart_wallet(
                    w, source=f"{source_prefix}:{token.lower()}", note=f"${symbol}"):
                added += 1
        return added

    async def _harvest_manual_winner(self, ca: str) -> str:
        """Learn from a winner the operator spotted that we never caught.

        Harvests the token's earliest buyers into the smart set (so future
        tokens THOSE wallets buy score higher + can fire 🧠 cluster alerts —
        our strongest 'similar setup' detector), snapshots its live metrics as
        a hand-picked exemplar, and shows whether it would alert now / what
        blocked it (so we can see what to loosen).
        """
        from html import escape as _esc
        ca = ca.lower()
        already = self.storage.has_manual_winner(ca)
        # Enrich + score it (reuse the calibrate path for metrics + blockers).
        try:
            info = await self._calibrate_one(ca)
        except Exception as exc:  # noqa: BLE001
            return "harvest failed to read token: " + _esc(str(exc))
        symbol = info.get("symbol") or "?"
        m = info.get("metrics") or {}
        # Not on Robinhood Chain → almost certainly a Base/Sol token pasted here.
        # Don't pretend to harvest; point it at memelab.
        if not info.get("found"):
            return ("🔎 Couldn't find <code>" + _esc(ca) + "</code> on Robinhood "
                    "Chain.\nIf it's a <b>Base or Solana</b> token, it belongs in "
                    "<b>memelab</b> (the multi-chain half) — this scanner only reads RH.")

        # Bundle guard: harvesting 'early buyers' from a heavily-bundled launch
        # would inject sybil/bundle wallets into the smart set (poisoning cluster
        # signals). Record the exemplar, but SKIP the wallet harvest.
        bundle = m.get("bundle_supply_pct")
        bundle_cap = self.cfg.runtime.harvest_max_bundle_pct
        bundled_out = bundle is not None and bundle > bundle_cap

        added = 0
        if not bundled_out:
            try:
                # Manual winners assert a real pump — credit the ledger as a winner.
                added = await self._harvest_buyers(
                    ca, symbol, source_prefix="manual",
                    mult=self.cfg.runtime.winner_harvest_win_mult)
            except Exception:  # noqa: BLE001
                log.debug("manual harvest buyers failed", exc_info=True)
            try:
                self.storage.record_token_outcome(
                    ca, self.cfg.runtime.winner_harvest_win_mult, is_rug=False)
                await self._label_deployer(ca, "winner")
            except Exception:  # noqa: BLE001
                log.debug("manual outcome/deployer labeling failed", exc_info=True)
        # Persist the exemplar (metrics snapshot) for the winners dataset.
        try:
            self.storage.save_manual_winner(ca, symbol, m, added)
        except Exception:  # noqa: BLE001
            log.debug("save_manual_winner failed", exc_info=True)

        verdict = ("✅ would alert now" if info.get("would_alert")
                   else "❌ still wouldn't alert")
        blockers = (info.get("quick_fails") or []) + (info.get("safety_fails") or [])
        cap = len(self.storage.smart_wallets())
        if bundled_out:
            harvest_line = (f"⚠️ <b>Skipped buyer harvest</b> — {bundle:.0f}% bundled "
                            f"(&gt; {bundle_cap:.0f}%). Those 'early buyers' are likely "
                            "sybils; adding them would poison cluster alerts.")
        else:
            harvest_line = (f"🧠 Added <b>{added}</b> early buyer(s) → smart set "
                            f"(now {cap}).\nFuture tokens these wallets buy score "
                            "higher &amp; can fire 🧠 cluster alerts.")
        lines = [
            f"🧪 <b>Harvested winner ${_esc(symbol)}</b>"
            + ("  <i>(already had it — refreshed)</i>" if already else ""),
            f"<code>{_esc(ca)}</code>",
            "",
            harvest_line,
            "",
            f"<b>Setup at harvest</b> — {verdict} (score {info.get('composite', 0):.0f})",
            _esc(f"• liq {_fmt(m.get('liquidity_usd'), '$')} · mcap "
                 f"{_fmt(m.get('market_cap_usd'), '$')} · age {_fmt(m.get('age_minutes'), 'm')}"),
            _esc(f"• holders {_fmt(m.get('holder_count'), '')} · top10 "
                 f"{_fmt(m.get('top10_supply_pct'), '%')} · dev {_fmt(m.get('dev_holdings_pct'), '%')}"),
        ]
        if not info.get("would_alert") and blockers:
            lines.append("")
            lines.append("<b>What blocked it</b> (tune these to catch the next one):")
            for b in blockers[:4]:
                lines.append(_esc("• " + b))
        return "\n".join(lines)

    async def _harvest_rug(self, ca: str) -> str:
        """Learn from a RUG: mark its deployer a rugger + credit its early buyers
        toward toxicity. A wallet appearing across several rugs (and net-negative
        vs winners) becomes toxic → tokens it buys get demoted.

        One-off apers aren't blacklisted: toxicity needs toxic_min_rugs distinct
        rugs AND a rug count that outweighs the wallet's winner overlap.
        """
        from html import escape as _esc
        ca = ca.lower()
        rc = self.cfg.runtime
        try:
            self.storage.record_token_outcome(ca, 0.0, is_rug=True)
            await self._label_deployer(ca, "rug")
        except Exception:  # noqa: BLE001
            log.debug("rug outcome/deployer labeling failed", exc_info=True)
        credited = 0
        if self._session is not None:
            try:
                smart = SmartMoneyClient(self.cfg, session=self._session)
                buyers = await smart.early_buyers(ca, rc.smart_money_autoseed_buyers)
                for w in buyers:
                    self.storage.record_wallet_rug(w, ca)
                    credited += 1
            except Exception:  # noqa: BLE001
                log.debug("rug buyer credit failed", exc_info=True)
        toxic_now = len(self.storage.toxic_wallets(rc.toxic_min_rugs))
        return "\n".join([
            f"☠️ <b>Logged rug</b> <code>{_esc(ca)}</code>",
            "",
            f"Marked deployer a rugger &amp; credited {credited} early wallet(s) "
            "toward toxicity.",
            f"A wallet in ≥{rc.toxic_min_rugs} rugs (net-negative vs winners) is "
            f"toxic → its buys get demoted. <b>{toxic_now}</b> wallet(s) toxic so far.",
        ])

    async def _harvest_winners(self, dex) -> None:
        """Retroactive sweep: harvest early buyers of tokens that ran >= win_mult.

        Learns from winners we NEVER alerted on. Runs periodically (not every
        cycle); re-prices a capped batch of candidates aged 24-36h and harvests
        the ones that peaked past the win multiple, then marks them processed.
        """
        rc = self.cfg.runtime
        if not rc.winner_harvest_enabled:
            return
        due = self.storage.due_harvest_candidates(
            rc.winner_harvest_min_age_hours * 3600.0,
            rc.winner_harvest_max_age_hours * 3600.0,
            rc.winner_harvest_batch)
        if not due:
            return
        winners = 0
        for row in due:
            token, pair, entry = row["token"], row["pair"], row["entry_price"]
            self.storage.mark_harvest_done(token)   # process once regardless
            if not entry:
                continue
            try:
                price = await self._price_now(dex, pair, token)
            except Exception:
                price = None
            if price is None or price <= 0:
                continue
            mult = price / entry
            # Record the realized outcome (forward-pick validation feed) + label
            # the deployer, for every settled candidate — winner OR rug.
            is_rug = mult <= rc.rug_peak_mult_ceiling
            try:
                self.storage.record_token_outcome(token, mult, is_rug)
                await self._label_deployer(token, "winner" if mult >= rc.winner_harvest_win_mult
                                           else "rug" if is_rug else "neutral")
            except Exception:  # noqa: BLE001
                log.debug("outcome/deployer labeling failed", exc_info=True)
            # If we tracked this token on the curve, label its pre-migration setup
            # (win >= win_mult) so the curve-pattern matcher can learn from it.
            if self.storage.curve_observation_count(token) >= 2:
                try:
                    self._label_curve_setup(token, entry, price, rc.winner_harvest_win_mult)
                except Exception:
                    log.debug("curve setup labeling failed", exc_info=True)
            if mult >= rc.winner_harvest_win_mult:
                added = await self._harvest_buyers(token, row["symbol"] or "?",
                                                   source_prefix="winner", mult=mult)
                winners += 1
                log.info("winner-harvest: $%s ran %.1fx — harvested %d early buyers",
                         row["symbol"], mult, added)
        if winners:
            log.info("winner-harvest swept %d candidates, %d winners", len(due), winners)
        # Keep the curve-observation table bounded.
        try:
            pruned = self.storage.prune_curve_observations(
                self.cfg.runtime.curve_obs_retention_hours * 3600.0)
            if pruned:
                log.debug("pruned %d stale curve observations", pruned)
        except Exception:
            log.debug("curve prune failed", exc_info=True)

    async def _price_now(self, dex, pair: str, token: str):
        stub = TokenSnapshot(chain=self.cfg.chain.dexscreener_chain,
                             pair_address=pair or "", token_address=token)
        refreshed = await dex.refresh(stub)
        if refreshed.price_usd is not None:
            return refreshed.price_usd
        for p in await dex.pairs_for_token(token):
            if p.price_usd is not None:
                return p.price_usd
        return None

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
        # Quality gates — reject dead/quiet launches (the main source of noise).
        # Each applies only when the metric is known, so thin brand-new data still
        # passes; a token with data that FAILS the bar is filtered.
        if rc.early_launch_min_holders and snap.holder_count is not None \
                and snap.holder_count < rc.early_launch_min_holders:
            return False
        if rc.early_launch_min_buy_ratio_1h and snap.buy_ratio_1h is not None \
                and snap.buy_ratio_1h < rc.early_launch_min_buy_ratio_1h:
            return False
        if rc.early_launch_min_volume_1h_usd and snap.volume_1h is not None \
                and snap.volume_1h < rc.early_launch_min_volume_1h_usd:
            return False
        return True

    async def _enrich(self, snap: TokenSnapshot) -> None:
        """Attach safety, distribution, bundle, and smart-money facts.

        Each source is independent and failure-isolated; a source that isn't
        configured simply leaves its facts unknown.
        """
        assert self._session is not None
        # Learn live ETH/USD from this snap if it carries a native price (any
        # graduated RH pair does) — powers flap curve pricing below.
        self._note_eth_usd(snap)

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

        # flap/Bags tokens still on the curve have no DexScreener market — price
        # them from the launchpad so the scorer can rank them pre-graduation.
        await _safe(self._enrich_flap_curve(snap))
        if not snap.market_cap_usd:
            await _safe(self._enrich_bags_curve(snap))

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

        # Quality-weight the smart buyers + flag core-alpha / toxic wallets.
        self._attach_reputation(snap)
        # Classify the meta + flag whether it's currently hot (producing winners).
        try:
            from .narrative import classify
            snap.narrative = classify(snap.symbol or "", snap.name or "")
            snap.narrative_hot = snap.narrative in self._hot_narratives()
        except Exception:  # noqa: BLE001
            pass
        # Attach one-tap links for the alert (explorer + launchpad trade page).
        self._attach_links(snap)

    def _hot_narratives(self) -> set:
        """Narratives currently producing winners (cached hourly from outcomes)."""
        now = time.time()
        if self._hot_nar_ts and now - self._hot_nar_ts < 3600:
            return self._hot_nar_cache
        self._hot_nar_ts = now
        try:
            from .narrative import hot_narratives, narrative_stats
            self._hot_nar_cache = hot_narratives(
                narrative_stats(self.storage.all_paper_trades()))
        except Exception:  # noqa: BLE001
            self._hot_nar_cache = set()
        return self._hot_nar_cache

    def _is_core_alpha(self, wallet: str) -> bool:
        """True if the wallet is on >= core_alpha_min_overlap distinct winners.
        Cached per cycle-ish (60s) so the per-buy check stays cheap."""
        now = time.time()
        if not self._core_alpha_cache or now - self._core_alpha_ts > 60:
            try:
                self._core_alpha_cache = set(self.storage.core_alpha_wallets(
                    self.cfg.runtime.core_alpha_min_overlap))
            except Exception:  # noqa: BLE001
                self._core_alpha_cache = set()
            self._core_alpha_ts = now
        return wallet.lower() in self._core_alpha_cache

    def _attach_reputation(self, snap: TokenSnapshot) -> None:
        """Score this token's smart buyers by reputation → quality bonus + flags.

        Turns the flat smart-wallet headcount into a quality-weighted discovery
        bonus, marks any core-alpha (multi-winner) wallets, and flags a toxic
        (rug/dumper) buyer. Dormant until the ledger has data — quality of a
        freshly-harvested wallet is a modest positive, not zero.
        """
        wallets = [w.lower() for w in (snap.smart_money_wallets or [])]
        if not wallets:
            return
        try:
            from . import wallet_intel as wi
            rep = self.storage.wallet_reputation_rows(wallets)
            quals = [wi.quality(rep.get(w, {})) for w in wallets]
            snap.smart_money_quality_bonus = wi.smart_money_quality_bonus(quals)
            core = set(self.storage.core_alpha_wallets(
                self.cfg.runtime.core_alpha_min_overlap))
            snap.core_alpha_wallets = [w for w in wallets if w in core]
            toxic = self.storage.toxic_wallets(self.cfg.runtime.toxic_min_rugs)
            snap.toxic_buyer = any(w in toxic for w in wallets)
        except Exception:  # noqa: BLE001
            log.debug("reputation attach failed", exc_info=True)

    def _attach_links(self, snap: TokenSnapshot) -> None:
        """Populate explorer_url (Blockscout token page) + trade_url (launchpad)."""
        base = (self.cfg.chain.explorer_api_url or "").rstrip("/")
        if base.endswith("/api"):
            base = base[:-4]
        if base and snap.token_address:
            snap.explorer_url = f"{base}/token/{snap.token_address}"
        lp = self.cfg._launchpad(snap.launchpad) if snap.launchpad else None
        tmpl = (lp or {}).get("trade_url_template") if lp else ""
        if tmpl and snap.token_address:
            snap.trade_url = tmpl.replace("{token}", snap.token_address)
        # Human labels for the active smart wallets (group name, else /smart note).
        if snap.smart_money_wallets:
            try:
                groups = self.storage.wallet_groups()
                notes = {r["wallet"]: r["note"] for r in self.storage.smart_wallets_detailed()
                         if r["note"]}
                labels = {}
                for w in snap.smart_money_wallets:
                    lab = groups.get(w.lower()) or notes.get(w.lower())
                    if lab:
                        labels[w.lower()] = lab
                snap.smart_money_labels = labels
            except Exception:  # noqa: BLE001
                pass
