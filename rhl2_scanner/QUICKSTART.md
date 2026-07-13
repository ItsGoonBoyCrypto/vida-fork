# Quick start — get the scanner running ASAP

Two paths. Start with **A** to prove the whole pipeline live on real data today;
switch to **B** the moment you have Robinhood L2's endpoints.

---

## A. Live proving ground on Base (works right now)

Base is a public EVM L2 fully indexed by DexScreener + GoPlus, so every
subsystem — discovery, safety, honeypot sim, LP-lock, scoring, paper mode —
runs live against it. Same code you'll point at RH L2 later.

```bash
pip install -r rhl2_scanner/requirements.txt

cp rhl2_scanner/config/base.example.yaml rhl2_scanner/config/config.yaml
cp rhl2_scanner/.env.example rhl2_scanner/.env        # optional: add a Basescan key

# 1) Sanity check the scoring engine (offline):
python -m rhl2_scanner selfcheck

# 2) One live discovery+score cycle, dry-run (prints would-be alerts):
python -m rhl2_scanner scan-once --config rhl2_scanner/config/config.yaml

# 3) Calibration mode — records would-be entries and re-prices them at 1h/6h/24h:
python -m rhl2_scanner paper --config rhl2_scanner/config/config.yaml

# 4) After it has run a while, see win-rate by score band:
python -m rhl2_scanner paper-report --config rhl2_scanner/config/config.yaml
```

Leave `paper` running for a few days, then read `paper-report` to set your
alert bands on evidence, not guesses.

---

## B. Point it at Robinhood L2

Edit the `chain:` block in `config/config.yaml` with RH L2's values, then run
the same commands. Everything else is identical.

---

## What I need from you (the only gaps)

Nothing is required to run **A** in dry-run/paper. To go fully live you supply:

| Item | Needed for | Where |
|---|---|---|
| **Telegram bot token + chat id** | sending alerts (not needed for paper/dry-run) | `.env`: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALERT_CHAT_ID` |
| **Basescan (or RH L2 explorer) API key** | holder distribution, smart-money, bundle | `.env`: `RHL2_EXPLORER_API_KEY` |
| **RH L2 chain values** | targeting RH L2 instead of Base | `config.yaml` `chain:` — chain_id, rpc_url, explorer_api_url, dexscreener_chain, weth_address, dex_factory_address, dex_router_address, goplus_chain_id (if covered) |
| **Compiled simulator bytecode** *(optional)* | exact buy/sell tax % on-chain | `solc --optimize --bin-runtime rhl2_scanner/contracts/HoneypotSimulator.sol` → `.env` `RHL2_SIMULATOR_BYTECODE` |
| **Curated smart-money wallets** *(optional)* | the smart-money signal | `config.yaml` `smart_money_wallets: [...]` or `/addwallet` in Telegram |
| **Known LP locker addresses** *(optional)* | LP-lock + duration | `config.yaml` `chain.lp_locker_addresses` / `lp_lockers` |

### How to get the Telegram bits
1. DM **@BotFather** → `/newbot` → copy the token.
2. Add the bot to your alert channel/group as an admin.
3. Get the chat id (e.g. message **@RawDataBot**, or use the API `getUpdates`).

---

## Run it as a service (Docker)

```bash
docker build -t rhl2-scanner -f rhl2_scanner/Dockerfile .
docker run --rm \
  -v "$PWD/rhl2_scanner/config:/app/config" \
  -v "$PWD/data:/app/data" \
  --env-file rhl2_scanner/.env \
  rhl2-scanner paper --config config/config.yaml
```

The SQLite db (seen tokens, alerts, paper trades) persists in the mounted
`data/` volume across restarts.

---

## Reminder
Alerts are signals, not advice. Very early entries carry the highest rug risk
even with clean metrics — size positions, take profits, and treat `paper-report`
as the source of truth for whether the thresholds are actually working.
