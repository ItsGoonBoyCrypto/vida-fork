# RH L2 Early Memecoin Scanner

Real-time scanner that surfaces early-stage memecoins on **Robinhood's EVM L2**
(adaptable to Base and other EVM L2s) with a **safety-first, confluence-based**
scoring model and Telegram alerting. Implements the v1.0 spec: aggressive rug
gatekeepers first, then distribution / momentum / discovery scoring.

> ⚠️ **Informational only.** Memecoins are highly speculative and most fail even
> with clean metrics. This tool emits *alerts*, not financial advice. DYOR,
> size positions responsibly, use take-profits. See §5 of the spec.

---

## What's built (and what needs live endpoints)

This is a **framework**: the decision logic is real and fully tested; the data
plumbing is functional but must be pointed at RH L2's live endpoints.

| Component | Status |
|---|---|
| Composite scoring engine (Safety/Dist/Momentum/Discovery, weighted 0–100) | ✅ complete + unit-tested |
| Safety gatekeepers (authorities, LP, taxes, honeypot, top-holders, bundle) | ✅ complete + tested |
| Quick-start pre-screen filter stack (spec §6) | ✅ complete |
| Alert formatter (exact spec §4 format, Telegram HTML + plaintext) | ✅ complete |
| Bundle / sniper-cluster detection (pure, testable) | ✅ complete |
| DexScreener client (discovery + market/volume/tx) | ✅ real API client¹ |
| SQLite persistence (dedupe, cooldown, alert history) | ✅ complete |
| Telegram notifier + `/status /tier /set /addwallet` commands | ✅ complete² |
| Backtest / historical replay harness | ✅ harness done; supply your dataset |
| EVM chain client (verify, authorities, LP-burn, holders) | ⚙️ works on standard EVM; **set RH L2 rpc/explorer** |
| Honeypot / tax simulation | 🔌 hook stubbed — wire a simulator or honeypot API |
| New-pool log listener (earliest detection) | 🔌 stub — needs RH L2 DEX factory address |

¹ DexScreener must index RH L2 for its slug to return data. Until then set
`chain.dexscreener_chain: "base"` to exercise the full pipeline against a live L2.
² Requires `python-telegram-bot`; without it the notifier prints alerts to stdout.

**Robinhood Chain note:** RH's L2 (announced 2025, built on Arbitrum Orbit — EVM)
may not yet have public DexScreener/RugCheck coverage or finalized chain id / RPC.
Everything chain-specific is config-driven (`config/config.example.yaml`), so you
drop in real values without code changes. Retargeting to Base = change two lines.

---

## Architecture

```
discover ─► quick-start gate ─► enrich (concurrent) ─► safety gate ─► score ─► alert + persist
   │              │                    │                    │           │            │
DexScreener   cheap pre-      chain(safety, holders)   hard rug      weighted    Telegram +
+ (chain      screen on       bundle analysis          filters       0–100       SQLite
  listener)   feed data       smart-money match        (skip on fail)            (cooldown)
```

Confluence rule: **any safety-gate failure forces SKIP and zeroes the composite**,
regardless of momentum. Unknown safety facts are treated as failures in strict
(momentum) mode — better to miss than to alert on an unverified token.

### Module map
```
rhl2_scanner/
  models.py          TokenSnapshot / SafetyReport / ScoreResult (provider-agnostic)
  config.py          chain params, weights, per-tier thresholds (YAML + env)
  filters.py         quick_start_gate + safety_gate (hard rug filters)
  scoring.py         weighted composite engine (spec §3)
  bundle.py          same-block + common-funder cluster detection (pure fn + client)
  storage.py         SQLite: seen tokens, alert history, re-alert cooldown
  scanner.py         async orchestration loop
  backtest.py        historical replay -> alert precision
  sources/
    base.py          PairSource / SafetySource / DistributionSource / SmartMoneySource
    dexscreener.py   real DexScreener client
    chain.py         EVM RPC + explorer (verify, authorities, LP, holders)
    smartmoney.py    curated wallet matcher
  alerting/
    formatter.py     spec §4 alert rendering
    telegram.py      notifier + interactive command bot
  config/config.example.yaml
  tests/test_scanner_core.py
```

---

## Quick start

```bash
pip install -r rhl2_scanner/requirements.txt

# 1) smoke-test the scoring engine offline (no network, no config):
python -m rhl2_scanner selfcheck

# 2) configure
cp rhl2_scanner/config/config.example.yaml rhl2_scanner/config/config.yaml
#   edit chain.rpc_url / explorer_api_url; set dexscreener_chain to "base"
#   to try it live today. Secrets can go in env instead:
export TELEGRAM_BOT_TOKEN=... TELEGRAM_ALERT_CHAT_ID=... RHL2_RPC_URL=...

# 3) dry-run a single cycle (prints would-be alerts):
python -m rhl2_scanner scan-once --config rhl2_scanner/config/config.yaml

# 4) run the live loop:
python -m rhl2_scanner run --config rhl2_scanner/config/config.yaml --tier momentum
```

Run tests: `python -m unittest discover -s rhl2_scanner/tests -v`

`selfcheck` prints a fully-scored synthetic token in the exact alert format:

```
🚨 EARLY GEM ALERT - Score: 87/100
Token: $EXAMPLE (CA: 0xtoken)
Age: 47m | MCAP: $187k | Liq: $42k
Holders: 312 (↑ growing) | Top10: 22%
Volume 1h: $28k (↑ accelerating) | Buy Ratio: 78%
Safety: ✅ Revoked | ✅ LP Burned | Low Bundle
Smart Money: 3 wallets active
Breakdown: Safety: 95/100 | Distribution: 83/100 | Momentum: 92/100 | Discovery: 66/100
Reasons: verified contract, authorities revoked, LP burned, ~0 tax, dev 2.0%, top10 22%
```

---

## Risk tiers

- **sniper** — `<2h`, looser liquidity/holder floors, `strict_safety=False`
  (accepts unconfirmed facts for speed — documented higher risk).
- **momentum** — `2–24h`, stricter, `strict_safety=True` (unknown = fail).

Switch live via `/tier sniper` in Telegram, or `--tier` on the CLI. Tune any
threshold at runtime with `/set <field> <value>`.

---

## Scoring weights (default, tunable in config)

| Category | Weight | Drives |
|---|---|---|
| Safety | 38% | verified, authorities revoked, LP burned/locked, low tax, low dev, risk score |
| Distribution | 20% | top10 %, top1 %, holder count + growth, low bundle |
| Momentum | 25% | absolute + accelerating volume, vol/mcap, buy ratio, healthy price action |
| Discovery | 17% | freshness, smart-money wallets, socials, trending/boost |

Alert bands (momentum tier): **≥75 = Strong**, **60–74 = Watch**, **<60 / safety fail = Skip**.

---

## Wiring RH L2 for production (checklist)

1. `chain.chain_id`, `chain.rpc_url`, `chain.explorer_api_url` (+ key).
2. `chain.dexscreener_chain` once RH L2 is indexed by DexScreener.
3. `chain.excluded_holder_addresses` — LP pools, lockers, treasury.
4. Implement `EvmChainClient._simulate_taxes` (honeypot API or eth_call buy/sell sim).
5. LP-lock detection: add known RH L2 locker addresses in `chain.py`.
6. `EvmChainClient.iter_new_pools` — subscribe to the DEX factory `PairCreated`
   event for sub-DexScreener-latency discovery.
7. Populate `smart_money_wallets` with your curated high-win-rate list.
8. Archive live snapshots to JSONL and use `backtest` to tune thresholds by win rate.
```
