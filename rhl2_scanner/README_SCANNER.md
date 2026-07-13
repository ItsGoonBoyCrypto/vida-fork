# RH L2 Early Memecoin Scanner

Real-time scanner that surfaces early-stage memecoins on **Robinhood Chain
(RH L2)** — chain id **4663**, mainnet live since 2026-07-01, DexScreener slug
**`robinhood`** — with a **safety-first, confluence-based** scoring model and
Telegram alerting. Implements the v1.0 spec: aggressive rug gatekeepers first,
then distribution / momentum / discovery scoring.

**Targets Robinhood Chain only.** The config ships with RH L2's real RPC
(`rpc.mainnet.chain.robinhood.com`) and Blockscout explorer, so discovery +
safety work out of the box. See `QUICKSTART.md` to deploy on a VPS.

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
| **Paper-trading / live calibration mode** | ✅ records would-be entries, re-prices at 1h/6h/24h, win-rate by band |
| Backtest / historical replay harness | ✅ harness done; supply your dataset |
| Real Robinhood Chain config + `.env` + Dockerfile + systemd | ✅ VPS-ready |
| EVM chain client (verify, authorities, LP-burn, holders) | ⚙️ works on standard EVM; **set RH L2 rpc/explorer** |
| **RugCheck-style safety (GoPlus) integration** | ✅ real client + parser³ |
| **Honeypot / tax on-chain simulation** | ✅ eth_call + stateOverride sell-sim (RPC-only)⁴ |
| **Exact buy/sell tax (simulator contract)** | ✅ stateOverride-injected buy+sell sim⁶ |
| **New-pool factory listener (earliest detection)** | ✅ real eth_getLogs polling (UniV2 + V3)⁵ |
| **LP lock detection + remaining duration** | ✅ locker balance + configurable unlock-time getter⁷ |
| Dependency-free Keccak-256 (topics + storage slots) | ✅ verified against EVM vectors |

¹ DexScreener indexes Robinhood Chain live under the slug `robinhood` (set as
the default), so discovery works today with no extra setup.
² Requires `python-telegram-bot`; without it the notifier prints alerts to stdout.
³ GoPlus is the de-facto EVM analog to RugCheck (authorities, taxes, honeypot,
LP lock/burn, holders, risk flags in one call). Set `chain.goplus_chain_id`. If
GoPlus doesn't cover RH L2 yet, the on-chain sources below fill the gap.
⁴ Simulates a sell via `eth_call` + `stateOverride` (detects the balance/allowance
storage slots, credits a burner, calls the router's fee-supporting sell) — needs
only `rpc_url` + `dex_router_address` + `weth_address`, no third party, no deployed
contract. Reliable **sellability/honeypot** signal; exact tax magnitude still comes
from GoPlus / a honeypot.is-style API (`honeypot_api_url`).
⁵ Polls the DEX factory's `PairCreated`/`PoolCreated` logs so tokens are caught
the moment liquidity is added, before DexScreener indexes them. Set
`dex_factory_address` + `dex_factory_kind`.
⁶ `contracts/HoneypotSimulator.sol` buys then sells in a single `eth_call` and
compares actual amounts to tax-free reserve quotes — the only way to get true
token transfer taxes. Its *runtime* bytecode is injected via `stateOverride`
(never deployed on-chain). Compile with `solc --bin-runtime` and set
`honeypot_simulator_bytecode`. Returns exact buy/sell tax in bps + sellability.
⁷ Detects LP locked when ≥ `lp_lock_min_fraction` of LP supply sits in a known
locker (works for any locker — they all custody the LP). Remaining duration is
read from a per-locker unlock-time getter configured in `lp_lockers`.

### How the three safety signals combine

`CompositeSafetySource` runs GoPlus + the on-chain EVM client + the honeypot
simulator concurrently and merges them **pessimistically** (`sources/safety.py`):
a fact is only credited safe when a source confirms it and none contradicts it;
any single credible red flag (honeypot, high tax, un-revoked authority) sinks the
token. Unknown facts stay unknown — and the strict gate treats unknown as fail.
This is what lets the scanner run safely on a new L2 where no single provider has
full coverage.

**Robinhood Chain:** live mainnet (2026-07-01), Arbitrum-Orbit EVM L2, chain id
4663, ETH gas, Blockscout explorer, Uniswap V2/V3/V4 from day one. The config
(`config/robinhood.example.yaml`) ships these real values — no discovery of
endpoints needed. Optional add-ons (WETH / factory / router addresses for the
pool-listener + honeypot sell-sim) are grabbed from the explorer when wanted.

---

## Architecture

```
discover ─► quick-start gate ─► enrich (concurrent) ─► safety gate ─► score ─► alert + persist
   │              │                    │                    │           │            │
DexScreener   cheap pre-      GoPlus + on-chain +      hard rug      weighted    Telegram +
+ factory     screen on       honeypot sim (merged);   filters       0–100       SQLite
  log         feed data       holders, bundle,         (skip on fail)            (cooldown)
  listener                    smart-money
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
  keccak.py          dependency-free Keccak-256 + EVM slot/topic helpers
  simulator.py       honeypot/tax simulation (sell-sim boolean + exact-tax contract path)
  contracts/HoneypotSimulator.sol   buy+sell simulator (runtime bytecode via stateOverride)
  storage.py         SQLite: seen tokens, alert history, re-alert cooldown
  scanner.py         async orchestration loop
  paper.py           paper-trading recorder + settler + calibration report
  backtest.py        historical replay -> alert precision
  sources/
    base.py          PairSource / SafetySource / DistributionSource / SmartMoneySource
    dexscreener.py   real DexScreener client
    chain.py         EVM RPC + explorer (verify, authorities, LP, holders)
    goplus.py        GoPlus token-security client (RugCheck-equivalent)
    safety.py        CompositeSafetySource — merges GoPlus + on-chain + simulator
    poollistener.py  new-pool factory log listener (earliest discovery)
    lplock.py        LP-lock detection + remaining-duration reader
    smartmoney.py    curated wallet matcher
  alerting/
    formatter.py     spec §4 alert rendering
    telegram.py      notifier + interactive command bot
  config/config.example.yaml
  tests/test_scanner_core.py
```

---

## Quick start

**See `QUICKSTART.md` for the copy-paste VPS deploy** (RH L2 config + secrets +
systemd/Docker for 24/7).

```bash
pip install -r rhl2_scanner/requirements.txt

# 1) smoke-test the scoring engine offline (no network, no config):
python -m rhl2_scanner selfcheck

# 2) Robinhood Chain config (ships with RH L2's real RPC/explorer/slug):
cp rhl2_scanner/config/robinhood.example.yaml rhl2_scanner/config/config.yaml
cp rhl2_scanner/.env.example rhl2_scanner/.env         # add secrets here (auto-loaded)

# 3) one live discovery+score cycle, dry-run (prints would-be alerts):
python -m rhl2_scanner scan-once --config rhl2_scanner/config/config.yaml

# 4) CALIBRATION: record would-be entries + realized 1h/6h/24h outcomes:
python -m rhl2_scanner paper --config rhl2_scanner/config/config.yaml
#    ...then read win-rate by score band:
python -m rhl2_scanner paper-report --config rhl2_scanner/config/config.yaml

# 5) go live (needs Telegram token + chat id):
python -m rhl2_scanner run --config rhl2_scanner/config/config.yaml --tier momentum
```

Run tests: `python -m unittest discover -s rhl2_scanner/tests -v`  (39 tests)

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
3. `chain.excluded_holder_addresses` / `chain.lp_locker_addresses` — LP pools,
   lockers, treasury.
4. **Honeypot/tax:** set `chain.dex_router_address` + `chain.weth_address` for the
   on-chain sell-simulation. For **exact tax %**, compile `contracts/HoneypotSimulator.sol`
   (`solc --bin-runtime`) into `chain.honeypot_simulator_bytecode`, or set
   `chain.honeypot_api_url`.
5. **RugCheck-style:** set `chain.goplus_chain_id` if GoPlus covers RH L2 (decimal
   chain id as string); otherwise the on-chain sources carry safety on their own.
6. **New-pool listener:** set `chain.dex_factory_address` + `chain.dex_factory_kind`
   (`univ2`/`univ3`) for sub-DexScreener-latency discovery.
7. **LP lock:** list locker addresses in `chain.lp_locker_addresses` (locked
   detection), or `chain.lp_lockers` with each locker's `unlock_selector` for
   remaining-duration readout.
8. Populate `smart_money_wallets` with your curated high-win-rate list.
9. Archive live snapshots to JSONL and use `backtest` to tune thresholds by win rate.

### Operator inputs (chain-specific, no code changes)
- **Simulator bytecode** — compile `contracts/HoneypotSimulator.sol` and paste the
  runtime bytecode into config. The contract + eth_call wiring are done; only the
  compile step is yours (keeps the repo free of a pinned solc toolchain).
- **Locker unlock selectors** — lockers differ; supply each locker's unlock-time
  getter selector in `lp_lockers`. Locked-detection works with just the address.
```
