"""One-process launcher: RH scanner + memelab collector + dashboard together.

Runs all three in a single container so there's ONE Railway service, ONE volume
(/app/data), and ONE deploy to reason about. Each part is a *supervised* task:
if one crashes it restarts itself after a short delay without taking the others
down. Turn any part off with an env flag (set it to 0):

    RUN_SCANNER=0      # don't run the RH alert scanner
    RUN_MEMELAB=0      # don't run the memelab collector
    RUN_DASHBOARD=0    # don't serve the memelab web dashboard

The dashboard listens on $PORT (Railway sets it; default 8080) — point the
service's public domain there. The scanner + collector need no ports.

DB paths on the shared volume: scanner → $RHL2_DB_DIR/scanner.db,
memelab → $MEMELAB_DB. Secrets come from the usual env vars (TELEGRAM_*,
RHL2_*, MEMELAB_*).
"""

from __future__ import annotations

import asyncio
import logging
import os

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("run_all")


async def _run_scanner() -> None:
    from rhl2_scanner.config import Config
    from rhl2_scanner.scanner import Scanner

    cfg = Config.load(os.environ.get(
        "RHL2_CONFIG", "rhl2_scanner/config/robinhood.example.yaml"))
    cfg.runtime.dry_run = False   # live alerts (this is the `run` mode, not paper)
    await Scanner(cfg).run_forever()


async def _run_collector() -> None:
    from memelab.__main__ import _collect
    from memelab.models import Chain

    chains = [Chain(c.strip()) for c in os.environ.get(
        "MEMELAB_CHAINS", "robinhood,solana,ethereum,base,bsc").split(",") if c.strip()]
    db = os.environ.get("MEMELAB_DB", "/app/data/memelab.db")
    await _collect(chains, db)


async def _run_dashboard() -> None:
    import uvicorn
    from memelab.api.app import create_app

    app = create_app(os.environ.get("MEMELAB_DB", "/app/data/memelab.db"))
    port = int(os.environ.get("PORT", "8080"))
    server = uvicorn.Server(uvicorn.Config(
        app, host="0.0.0.0", port=port, log_level="warning"))
    await server.serve()


async def _supervise(name: str, factory) -> None:
    """Run a part forever; restart it on crash so one failure isn't fatal."""
    while True:
        try:
            await factory()
            log.warning("%s exited cleanly — restarting in 10s", name)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("%s crashed — restarting in 10s", name)
        await asyncio.sleep(10)


async def main() -> None:
    # Point memelab at the scanner's DB on the shared volume so the reputation
    # bridge (scanner → memelab) can cross-pollinate. Both DBs live together.
    if "SCANNER_DB" not in os.environ:
        db_dir = os.environ.get("RHL2_DB_DIR", "/app/data")
        os.environ["SCANNER_DB"] = os.path.join(db_dir, "scanner.db")

    scanner_on = os.environ.get("RUN_SCANNER", "1") != "0"
    memelab_on = os.environ.get("RUN_MEMELAB", "1") != "0"
    # ONE unified alert feed. memelab inherits the scanner's Telegram bot/channel
    # (via TELEGRAM_* fallback in _collect). To avoid double alerts on robinhood,
    # the scanner owns robinhood and memelab alerts only on the other chains.
    if scanner_on and memelab_on and "MEMELAB_ALERT_CHAINS" not in os.environ:
        os.environ["MEMELAB_ALERT_CHAINS"] = "solana,ethereum,base,bsc"

    parts = []
    if os.environ.get("RUN_SCANNER", "1") != "0":
        parts.append(_supervise("scanner", _run_scanner))
    if os.environ.get("RUN_MEMELAB", "1") != "0":
        parts.append(_supervise("collector", _run_collector))
    if os.environ.get("RUN_DASHBOARD", "1") != "0":
        parts.append(_supervise("dashboard", _run_dashboard))
    if not parts:
        log.error("nothing to run — RUN_SCANNER/RUN_MEMELAB/RUN_DASHBOARD all 0")
        return
    log.info("run_all: starting %d service(s) in one process", len(parts))
    await asyncio.gather(*parts)


if __name__ == "__main__":
    asyncio.run(main())
