"""memelab CLI — run the collector, backtests, and screens.

    python -m memelab collect                # run the snapshot loop (all chains)
    python -m memelab collect --chains solana,base
    python -m memelab backtest               # derive + validate a signature now
    python -m memelab screen 0x<token> --chain base   # score one live token
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
    import aiohttp
    from .collector import Collector, CollectorConfig
    from .chains.registry import get_adapter
    from .ingest.dexscreener import DexScreenerFeed

    store = Store(db)
    session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
    adapters = {c: get_adapter(c, session=session) for c in chains}
    feed = DexScreenerFeed(session=session)
    collector = Collector(CollectorConfig(chains=chains), store, adapters, feed)
    logging.info("memelab collecting on %s", [c.value for c in chains])
    try:
        await collector.run_forever()
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


def _stats(db: str) -> None:
    store = Store(db)
    try:
        cov = store.coverage()
        print(f"tokens: {cov['total_tokens']}  snapshots: {cov['total_snapshots']}")
        for ch, s in cov["by_chain"].items():
            print(f"  {ch:10} {s['tokens']:>6} tokens  {s['winners'] or 0:>4} winners")
        raw = store.active_signature()
        print("active signature:", "yes" if raw else "none")
    finally:
        store.close()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="memelab")
    p.add_argument("command", choices=["collect", "backtest", "screen", "stats"])
    p.add_argument("token", nargs="?", help="token address for `screen`")
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
    elif a.command == "stats":
        _stats(a.db)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
