"""CLI entrypoint.

    python -m rhl2_scanner run          # run the live scanner loop
    python -m rhl2_scanner scan-once    # single cycle, print results (dry-run friendly)
    python -m rhl2_scanner backtest F   # replay a JSONL dataset
    python -m rhl2_scanner selfcheck    # score a synthetic token, no network

Config path via --config (default: config/config.yaml if present).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from .config import Config
from .scanner import Scanner


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _load(args) -> Config:
    cfg = Config.load(args.config)
    if args.tier:
        from .models import RiskTier
        cfg.active_tier = RiskTier(args.tier)
    if args.dry_run:
        cfg.runtime.dry_run = True
    _setup_logging(cfg.runtime.log_level)
    return cfg


async def _run(cfg: Config) -> None:
    scanner = Scanner(cfg)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, scanner.stop)
        except NotImplementedError:  # pragma: no cover (Windows)
            pass
    await scanner.run_forever()


async def _scan_once(cfg: Config) -> None:
    cfg.runtime.dry_run = True
    scanner = Scanner(cfg)
    import aiohttp

    scanner._session = aiohttp.ClientSession()
    try:
        results = await scanner.run_once()
        alerts = [r for r in results if r.safety_passed and r.composite >= cfg.thresholds.watch_alert_score]
        print(f"scored {len(results)} tokens, {len(alerts)} would alert")
    finally:
        await scanner._session.close()
        scanner.storage.close()


def _selfcheck(cfg: Config) -> None:
    """Score a synthetic 'clean' token with no network — smoke test the engine."""
    from .models import SafetyReport, TokenSnapshot
    from .scoring import score_token
    from .alerting.formatter import to_plain

    snap = TokenSnapshot(
        chain=cfg.chain.dexscreener_chain,
        pair_address="0xpair",
        token_address="0xtoken",
        symbol="EXAMPLE",
        market_cap_usd=187_000,
        liquidity_usd=42_000,
        age_minutes=47,
        volume_1h=28_000,
        volume_24h=210_000,
        buys_1h=180,
        sells_1h=50,
        price_change_1h=12,
        price_change_24h=140,
        holder_count=312,
        top10_supply_pct=22,
        top1_supply_pct=6,
        holder_growth_1h=40,
        smart_money_wallets=["0xa", "0xb", "0xc"],
        socials={"telegram": "https://t.me/example"},
        dexscreener_url="https://dexscreener.com/example",
    )
    snap.safety = SafetyReport(
        contract_verified=True,
        mint_authority_revoked=True,
        freeze_authority_revoked=True,
        lp_burned=True,
        buy_tax_pct=0,
        sell_tax_pct=0,
        is_honeypot=False,
        dev_holdings_pct=2.0,
        external_risk_score=92,
        bundle_supply_pct=8,
    )
    result = score_token(snap, cfg, strict_safety=True)
    print(to_plain(snap, result))
    print(f"\nsafety_passed={result.safety_passed} level={result.level.value}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="rhl2_scanner")
    parser.add_argument("command", choices=["run", "scan-once", "backtest", "selfcheck"])
    parser.add_argument("dataset", nargs="?", help="JSONL file for backtest")
    parser.add_argument("--config", default="config/config.yaml")
    parser.add_argument("--tier", choices=["sniper", "momentum"])
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    cfg = _load(args)

    if args.command == "run":
        asyncio.run(_run(cfg))
    elif args.command == "scan-once":
        asyncio.run(_scan_once(cfg))
    elif args.command == "selfcheck":
        _selfcheck(cfg)
    elif args.command == "backtest":
        if not args.dataset:
            print("backtest requires a JSONL dataset path", file=sys.stderr)
            return 2
        from .backtest import replay_file
        import json

        print(json.dumps(replay_file(args.dataset, cfg), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
