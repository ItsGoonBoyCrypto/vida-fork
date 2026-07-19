"""memelab CLI — run the collector, backtests, and screens.

    python -m memelab collect                # run the snapshot loop (all chains)
    python -m memelab collect --chains solana,base
    python -m memelab backtest               # derive + validate a signature now
    python -m memelab screen 0x<token> --chain base   # score one live token
    python -m memelab harvest <token> --chain solana  # feed a winner we missed
    python -m memelab stats                  # dataset coverage

The collector is the piece to leave running — it accumulates the dataset the
backtest learns from. Start it early; the edge compounds with time.
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from .models import Chain
from .storage import Store


def _chains(arg: str) -> list:
    if not arg:
        return list(Chain)
    return [Chain(c.strip()) for c in arg.split(",") if c.strip()]


async def _collect(chains: list, db: str) -> None:
    import os
    import aiohttp
    from .collector import Collector, CollectorConfig
    from .chains.registry import get_adapter
    from .ingest.dexscreener import DexScreenerFeed
    from .alerting import TelegramAlerter

    store = Store(db)
    session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
    adapters = {c: get_adapter(c, session=session) for c in chains}
    feed = DexScreenerFeed(session=session)
    # One unified feed: fall back to the RH scanner's bot/channel when memelab's
    # own aren't set, so scanner + memelab alerts land in the same Telegram chat.
    alerter = TelegramAlerter(
        token=os.environ.get("MEMELAB_TELEGRAM_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        chat_id=os.environ.get("MEMELAB_TELEGRAM_CHAT") or os.environ.get("TELEGRAM_ALERT_CHAT_ID", ""),
        session=session)
    from .smartmoney import SmartMoney
    from .ingest.social import SocialFeed
    smart = SmartMoney(store, session=session)
    # Seed operator-supplied KOL/influencer wallets (Ansem, Cobie, …) as a
    # labeled class of smart money — their buys score higher + fire 📣 alerts.
    from .kol import seed_kols
    seed_kols(store)
    social = SocialFeed(api_key=os.environ.get("MEMELAB_LUNARCRUSH_KEY", ""), session=session)
    # Which chains memelab ALERTS on. Default = all collected; but if the scanner
    # is also running (combined deploy), it owns robinhood — set via env there.
    alert_env = os.environ.get("MEMELAB_ALERT_CHAINS", "")
    alert_chains = [Chain(c.strip()) for c in alert_env.split(",") if c.strip()] if alert_env else []
    collector = Collector(CollectorConfig(chains=chains, alert_chains=alert_chains),
                          store, adapters, feed,
                          alerter=alerter, smart_money=smart, social=social)
    logging.info("memelab collecting on %s (alerts=%s)",
                 [c.value for c in chains], "on" if alerter.enabled else "stdout")
    try:
        await collector.run_forever()
    finally:
        await session.close()
        store.close()


async def _harvest(chain: Chain, token: str, db: str) -> None:
    import aiohttp
    from .harvest import harvest_manual_winner

    store = Store(db)
    session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
    try:
        r = await harvest_manual_winner(store, chain, token, session)
        sym = r.get("symbol") or "?"
        if r.get("bundled_out"):
            print(f"${sym} on {chain.value}: {r.get('bundle_pct'):.0f}% bundled — "
                  "skipped buyer harvest (likely sybils). Exemplar recorded.")
        elif not r.get("found"):
            print(f"couldn't find {token} on {chain.value} via DexScreener — "
                  "check the address/chain.")
        else:
            print(f"${sym} on {chain.value}: added {r['added']} early buyer(s) → "
                  f"smart set (now {r['smart_count']}).")
    finally:
        await session.close()
        store.close()


def _backtest(chains: list, db: str) -> None:
    from .backtest.engine import run_backtest
    store = Store(db)
    try:
        for scope in ([None] + [[c] for c in chains]):
            sig = run_backtest(store, chains=scope)
            tag = "pooled" if scope is None else scope[0].value
            print(f"[{tag}] trained_on={sig.trained_on} precision={sig.precision} "
                  f"recall={sig.recall} — {sig.notes}")
    finally:
        store.close()


async def _screen(chain: Chain, token: str, db: str) -> None:
    import aiohttp
    from .ingest.dexscreener import DexScreenerFeed
    from .screener.engine import Screener

    store = Store(db)
    session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
    try:
        sc = Screener(store)
        if not sc.reload_signature():
            print("no signature yet — run `collect` for a while, then `backtest`.")
            return
        feed = DexScreenerFeed(session=session)
        snap = await feed.market_for(chain, token)
        snaps = [snap] if snap else []
        result = sc.screen(chain, token, snaps)
        print(f"${result.snapshot.symbol or token}  score={result.score}/100")
        for r in result.reasons:
            print("  ·", r)
    finally:
        await session.close()
        store.close()


def build_export(store, chain: Chain) -> dict:
    """Portable bridge artifact: harvested smart wallets + learned signature for
    a chain — feeds the RH scanner (paste wallets into RHL2_SMART_WALLETS)."""
    from .models import signature_from_json
    wallets = sorted(store.smart_wallets(chain))
    raw = store.active_signature()
    sig = signature_from_json(raw) if raw else None
    # Reputation: wallets proven on >= 2 distinct winners, with their overlap —
    # the scanner can prioritise these (core-alpha) rather than treating the set
    # as flat. (Live cross-pollination also happens DB-to-DB via bridge.py; this
    # is the portable snapshot for env/manual seeding.)
    core = [{"wallet": r["wallet"], "overlap": r["overlap"], "best_mult": r["best_mult"]}
            for r in store.top_reputation_wallets(chain, limit=100) if r["overlap"] >= 2]
    return {
        "chain": chain.value,
        "smart_wallets": wallets,
        "core_alpha": core,
        "signature": None if sig is None else {
            "rules": sig.rules, "precision": sig.precision,
            "trained_on": sig.trained_on, "notes": sig.notes},
    }


def _export(chain: Chain, db: str) -> None:
    import json
    store = Store(db)
    try:
        data = build_export(store, chain)
        print(json.dumps(data, indent=2))
        wl = data["smart_wallets"]
        if wl:
            print(f"\n# Paste into the RH scanner's RHL2_SMART_WALLETS ({len(wl)} wallets):")
            print(",".join(wl))
    finally:
        store.close()


def _stats(db: str) -> None:
    store = Store(db)
    try:
        cov = store.coverage()
        print(f"tokens: {cov['total_tokens']}  snapshots: {cov['total_snapshots']}")
        for ch, s in cov["by_chain"].items():
            print(f"  {ch:10} {s['tokens']:>6} tokens  {s['winners'] or 0:>4} winners")
        print("smart wallets:", store.smart_wallet_count())
        raw = store.active_signature()
        print("active signature:", "yes" if raw else "none")
    finally:
        store.close()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="memelab")
    p.add_argument("command",
                   choices=["collect", "backtest", "screen", "stats", "export", "harvest"])
    p.add_argument("token", nargs="?", help="token address for `screen` / `harvest`")
    p.add_argument("--chains", default="", help="comma list; default all")
    p.add_argument("--chain", default="base", help="chain for `screen`")
    p.add_argument("--db", default="memelab.db")
    p.add_argument("--log", default="INFO")
    a = p.parse_args(argv)
    logging.basicConfig(level=getattr(logging, a.log.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if a.command == "collect":
        asyncio.run(_collect(_chains(a.chains), a.db))
    elif a.command == "backtest":
        _backtest(_chains(a.chains), a.db)
    elif a.command == "screen":
        if not a.token:
            print("screen requires a token address"); return 2
        asyncio.run(_screen(Chain(a.chain), a.token, a.db))
    elif a.command == "harvest":
        if not a.token:
            print("harvest requires a token address"); return 2
        asyncio.run(_harvest(Chain(a.chain), a.token, a.db))
    elif a.command == "stats":
        _stats(a.db)
    elif a.command == "export":
        # RH scanner is Robinhood-only; default export to it (--chain to override).
        _export(Chain.ROBINHOOD if a.chain == "base" else Chain(a.chain), a.db)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
