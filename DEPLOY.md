# Deploy everything as ONE Railway service

The RH scanner, the memelab collector, and the memelab dashboard now run together
in a **single container** (`run_all.py` + the root `Dockerfile`). One service, one
volume, one deploy — nothing to keep straight.

Each part is supervised: if one crashes it restarts itself without taking the
others down.

## Set it up (one service)

1. **Railway → New Service → GitHub Repo** → this repo, branch
   `claude/rh-l2-memecoin-scanner-7sj2qa` (or `main` once merged).

2. **Settings → Build:**
   - Builder: **Dockerfile**
   - Dockerfile Path: **`Dockerfile`** (the repo-root one — *not* `rhl2_scanner/`
     or `memelab/`)
   - Leave Start Command **empty** (the image's `CMD` runs `run_all.py`).

3. **Add a Volume** at mount path **`/app/data`** (persists both DBs across
   deploys — `scanner.db` and `memelab.db` live here).

4. **Generate a public domain** (Settings → Networking → Generate Domain) and set
   the target port to **`8080`** — that's the memelab dashboard. The scanner and
   collector need no ports.

5. **Variables** — set everything on this one service:

   | Variable | For | Notes |
   |---|---|---|
   | `TELEGRAM_BOT_TOKEN` | scanner | your alert bot token |
   | `TELEGRAM_ALERT_CHAT_ID` | scanner | the alert chat/channel id |
   | `RHL2_FLAP_MANAGER` | scanner | `0x26605f322f7fF986f381bB9A6e3f5DAb0bEaEb09` |
   | `RHL2_BAGS_MANAGER` / `RHL2_BAGS_FACTORY` | scanner | optional — light up Bags |
   | `RHL2_SMART_AUTOSEED` / `RHL2_WINNER_HARVEST` | scanner | `1` to enable |
   | `MEMELAB_TELEGRAM_TOKEN` / `MEMELAB_TELEGRAM_CHAT` | memelab | optional |
   | `MEMELAB_LUNARCRUSH_KEY` | memelab | optional (social signal) |

   *(Use a **separate** Telegram bot token for memelab if you want its alerts on a
   different bot — otherwise omit and it logs to stdout.)*

6. **Deploy.** Logs should show, from the one service:
   ```
   run_all: starting 3 service(s) in one process
   scanner up | chain=robinhood tier=…
   memelab collecting on ['robinhood','solana','ethereum','base']
   Uvicorn running on http://0.0.0.0:8080
   ```

7. **Delete the old separate services** (the standalone scanner / memelab /
   dashboard) so nothing double-runs. ⚠️ **Two scanners on one bot token both get
   Telegram `409 Conflict` and neither answers commands** — make sure only this
   combined service uses `TELEGRAM_BOT_TOKEN`.

## Turn parts off

Set any of these to `0` on the service:
- `RUN_SCANNER=0` — skip the RH alert scanner
- `RUN_MEMELAB=0` — skip the memelab collector
- `RUN_DASHBOARD=0` — skip the web dashboard

## Verify

- Telegram: `/diag` and `/stats` should reply. `/diag` now shows a
  `discovery streams (last cycle):` line — any `ERR:` names a failing stream.
- Dashboard: open the public URL — coverage tiles, signature, live candidates.

## Notes

- **Persistence** — the Volume at `/app/data` holds both DBs; without it, data
  resets on each redeploy.
- **Rate limits** — DexScreener / the RH RPC are public and throttle; clients
  retry with backoff. The scanner's discovery is isolated per-stream, so one
  throttled source can't stall the others.
- **Separate services still work** if you prefer them — `rhl2_scanner/Dockerfile`
  and `memelab/Dockerfile` are unchanged. The root `Dockerfile` is just the
  easy-mode all-in-one.
