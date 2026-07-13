# Quick start — Robinhood L2 scanner, live on your VPS

This bot targets **Robinhood Chain (RH L2) only** — chain id **4663**, mainnet
live since 2026-07-01, DexScreener slug **`robinhood`**. The config ships with
RH L2's real RPC + explorer, so it works out of the box.

---

## Step 1 — Post the first message (30 seconds, no terminal)

Open a **web browser**, paste this into the address bar, press Enter:

```
https://api.telegram.org/bot<YOUR_BOT_TOKEN>/sendMessage?chat_id=-1004299219898&text=RH%20L2%20Early%20Gem%20Scanner%20is%20ONLINE
```

You should see `{"ok":true,...}` and the message appears in your channel. If you
get `"bot is not a member of the channel"`, add the bot to the channel as an
**admin** first, then retry. (Prereq: the bot must already be an admin to post.)

---

## Step 2 — Put the scanner on your VPS (runs 24/7)

SSH into your VPS, then:

```bash
# 1) get the code
git clone https://github.com/ItsGoonBoyCrypto/vida-fork /opt/rhl2-scanner
cd /opt/rhl2-scanner
git checkout claude/rh-l2-memecoin-scanner-7sj2qa

# 2) install deps in a venv
python3 -m venv .venv
.venv/bin/pip install -r rhl2_scanner/requirements.txt

# 3) config = Robinhood Chain (already has RH L2 RPC/explorer/slug)
cp rhl2_scanner/config/robinhood.example.yaml rhl2_scanner/config/config.yaml

# 4) secrets (never committed)
cp rhl2_scanner/.env.example rhl2_scanner/.env
nano rhl2_scanner/.env      # set TELEGRAM_BOT_TOKEN and TELEGRAM_ALERT_CHAT_ID=-1004299219898

# 5) confirm Telegram delivery
.venv/bin/python -m rhl2_scanner tg-test --config rhl2_scanner/config/config.yaml
#    expect:  Bot OK: @yourbot   and   send -> sent
```

---

## Step 3 — Run it 24/7 with auto-restart (systemd)

```bash
cp rhl2_scanner/deploy/rhl2-scanner.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now rhl2-scanner
journalctl -u rhl2-scanner -f          # live logs
```

That runs **calibration mode** (`paper`): it scans Robinhood Chain, records
would-be entries, re-prices them at 1h/6h/24h, and posts a nightly digest to
your channel. Let it run a few days, then:

```bash
.venv/bin/python -m rhl2_scanner paper-report --config rhl2_scanner/config/config.yaml
```

When the score bands look good, switch to **live alerts**: edit
`/etc/systemd/system/rhl2-scanner.service`, change `paper` to `run`, then
`systemctl daemon-reload && systemctl restart rhl2-scanner`.

Prefer Docker? Use `rhl2_scanner/deploy/docker-compose.yml` instead of systemd.

---

## What's already wired for RH L2 (nothing for you to find)
- **Discovery:** DexScreener `robinhood` slug (live) — works immediately.
- **Safety/holders/bundle:** Robinhood Chain Blockscout explorer + on-chain RPC.
- **Chain:** id 4663, RPC `rpc.mainnet.chain.robinhood.com`.

## Optional add-ons (fill from the explorer when you want them)
| Field in `config.yaml` | Enables |
|---|---|
| `chain.weth_address` (Robinhood Wrapped ETH) | on-chain honeypot sell-sim + factory listener |
| `chain.dex_factory_address` (Uniswap V2/V3 factory) | earliest new-pool detection |
| `chain.dex_router_address` (Uniswap router) | honeypot sell-simulation |
| compiled `HoneypotSimulator.sol` bytecode | exact buy/sell tax % |

Grab these from **robinhoodchain.blockscout.com** (search "Wrapped Ether",
"UniswapV2Factory", "Router") — or send me the addresses and I'll drop them in.

---

## Reminder
Alerts are signals, not advice. Early entries carry the highest rug risk even
with clean metrics — size positions, take profits, and treat `paper-report` as
the source of truth for whether the thresholds work.
