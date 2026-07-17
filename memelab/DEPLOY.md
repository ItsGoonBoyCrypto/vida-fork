# Deploy memelab on Railway

memelab runs as a **second Railway service** in the same repo (alongside the RH
scanner). One service = the always-on collector; the dataset lives on a Volume.

## Step by step

1. **Railway → your project → New → GitHub Repo** → pick this repo (or, in an
   existing project, **New Service → GitHub Repo**). It's fine to have both the
   scanner and memelab services in one project.

2. **Point the service at memelab's Dockerfile.** Open the new service →
   **Settings → Build**:
   - Builder: **Dockerfile**
   - Dockerfile Path: `memelab/Dockerfile`
   - Root Directory: leave as the repo root (the Dockerfile copies `memelab/`).

3. **Set the branch** (Settings → Source): `claude/rh-l2-memecoin-scanner-7sj2qa`
   (or `main` once merged).

4. **Add a Volume** (so the dataset survives redeploys — critical, the data IS
   the moat): service → **Volumes → New Volume**, mount path **`/app/data`**.

5. **Variables** (service → Variables):
   - `MEMELAB_TELEGRAM_TOKEN` = your bot token  *(optional — omit to log alerts to stdout)*
   - `MEMELAB_TELEGRAM_CHAT`  = the chat/channel id
   - *(you can reuse the scanner's bot, or make a separate one for memelab alerts)*

6. **Deploy.** The `collect` loop starts on all four chains and begins writing
   snapshots to `/app/data/memelab.db`.

## What it does once live

- **Collects** continuously — every discovered token gets snapshotted (dense
  early, then hourly) out to 72h. All four chains from day one via DexScreener.
- **Relabels** hourly — tokens that aged ~48h get a winner/dud/rug outcome.
- **Backtests** every 6h — re-derives + validates the winner signature.
- **Alerts** — once a signature exists, live tokens that score above it (and the
  precision-scaled floor) fire a `🎯 memelab match` Telegram alert, deduped.

It is **cold at first** — no signature until enough tokens have aged ~48h. That's
expected; the value compounds. Watch progress with the CLI (Railway shell) or API.

## Inspect it

Railway service → **Shell** (or `railway run`):
```
python -m memelab stats                    # coverage + signature status
python -m memelab backtest                 # force a backtest now
python -m memelab screen 0xTOKEN --chain base
```

Optional dashboard API (separate service or local):
```
pip install fastapi uvicorn
uvicorn "memelab.api.app:app" --host 0.0.0.0 --port 8000
#  /stats  /signature  /token/{chain}/{addr}  /winners
```

## Notes

- **Separate from the scanner** — the RH scanner keeps its own service, DB, and
  Telegram wiring. memelab is additive; it doesn't touch the scanner.
- **Rate limits** — DexScreener/GoPlus/RugCheck are public and rate-limited; the
  clients retry with backoff. If you hit sustained limits, narrow `--chains` or
  raise the collector intervals.
- **Chains** — default is all four. To start narrower, set the start command to
  `collect --db /app/data/memelab.db --chains base,solana`.
