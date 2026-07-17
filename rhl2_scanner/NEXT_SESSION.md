# RH L2 Scanner — resume note

_Pinned to pick up next session. Branch: `claude/rh-l2-memecoin-scanner-7sj2qa`._

## ⏭️ Where we left off — the ONE open thread: flap curve pricing

flap tokens pump on the **bonding curve** and only hit DexScreener *after* they
graduate — so the scanner currently can't price/score them pre-graduation
(exactly the phase we want). To fix, we read flap's **on-chain curve state**.

**SOLVED how (flap dev docs, `docs.flap.sh/dev`):** the flap **Portal**
(`0x26605f322f7fF986f381bB9A6e3f5DAb0bEaEb09`) exposes
**`getTokenV2(address)`** → one `eth_call` returning the whole `TokenStateV2`
tuple: `(status, reserve, circulatingSupply, price, tokenVersion, r,
dexSupplyThresh)`. status enum: 0 Invalid · 1 Tradable · 2 InDuel · 3 Killed ·
4 DEX(graduated). progress = circulatingSupply / dexSupplyThresh.

Built + **WIRED INTO SCORING** (validated live 2026-07-17 on a real 79.4%-bonded
token — price/supply/reserve all internally consistent):

- **`/flapstate 0xToken`** — one call, decodes the tuple: status, graduation %,
  price (ETH/token), reserve, est. mcap. Survives the rate limit.
- **`_enrich_flap_curve()`** runs inside `_enrich`: when a token has no
  DexScreener market yet, it reads getTokenV2 and (if Tradable) fills
  `market_cap_usd` / `price_usd` / `liquidity_usd` / `curve_progress_pct` so the
  scorer ranks the token **pre-graduation**. Alerts already show
  `Launchpad: flap | Curve: NN% (pre-grad)` (formatter.py:89).
- **ETH/USD** is learned free from any graduated RH pair (priceUsd/priceNative,
  sanity-banded); bootstrap with env `RHL2_ETH_USD` for day-one. `/curveprobe`
  stays as a raw-getter fallback.

**NEXT STEPS (optional polish):**
1. Eyeball `/flapstate` ETH mcap vs flap.sh UI once more to be 100% on the
   18-dec scale (math already self-consistent; low risk).
2. Consider a scoring **boost for tokens at ~60-95% graduation** (about to
   graduate = prime entry) — small tweak in scoring.py.
3. Switch flap detection from vanity-suffix (8888/7777) to "getTokenV2 status
   != Invalid" so non-vanity flap tokens are caught too.

## ✅ Live & working (all on Railway, persistent Volume at /app/data)

- **Discovery:** block-zero pool listener + flap curve listener (Portal, vanity
  8888/7777) + DexScreener. RPC rate-limits handled by retry/backoff.
- **Scoring:** safety-gated (verification NOT required — RH barely verifies) +
  velocity (5m) + smart-money + launchpad boost.
- **Entry alerts:** 🚨 Gem / 👀 Watch / 🌱 Early · 🔼 Upgrades · 🧠🚨 Smart-money
  clusters (≥2 distinct entities in 6h).
- **Protect:** 🔴 smart-money exits · 📈 milestones (2x/5x/10x) · ⚠️ dump guard.
- **Learn:** real-time autoseed (STRONG winners) + retroactive winner harvest
  (24–36h, learns from MISSED winners) — set `RHL2_WINNER_HARVEST=1`.
- **14 seeded early wallets**; sybil groups: Early 5/6, Early 10/13.
- **$ROBINHOOD** and scam symbols blocked.

## Commands (`/help`)

`/diag` `/stats [h]` `/perf` `/inspect` · `/zero`/`/unzero`/`/muted` ·
`/block`/`/unblock`/`/blocked` · `/smart`/`/unsmart`/`/smartlist` ·
`/group`/`/ungroup`/`/groups` · `/wallet` `/deployer` `/flapstate` `/curveprobe`
`/calibrate`

## Ongoing (user)

- Keep feeding `/calibrate 0xWinner …` — turns misses into exact threshold tunes.
  (Already fixed the "contract not verified" gate this way.)
- Flip `RHL2_SMART_AUTOSEED=1` and `RHL2_WINNER_HARVEST=1` when ready.

_182 tests passing. Last commit: flap curve pricing wired into scoring (getTokenV2 + learned ETH/USD)._
