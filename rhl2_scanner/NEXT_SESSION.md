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

Built: **`/flapstate 0xToken`** → single call, decodes that tuple, shows status,
graduation %, price (ETH/token), reserve, est. mcap. Survives the rate limit
(one call, not a 35-getter sweep). `scanner.flap_state()` returns it as a dict
ready to feed scoring. `/curveprobe` stays as a raw-getter fallback.

**NEXT STEPS:**
1. `/flapstate 0x<bonding token>` on a live curve token → confirm the ETH price
   / mcap match what flap.sh's UI shows (validates the 18-dec assumption).
2. If they match: wire `flap_state()` into `_enrich` — when a token has no
   DexScreener pair yet AND is a flap Portal token, populate price/mcap/progress
   from getTokenV2 so scoring runs pre-graduation. Add ETH/USD conversion.
3. Switch flap-token detection from vanity-suffix (8888/7777) to "getTokenV2
   returns status != Invalid" (robust — not all flap tokens use vanity addrs).

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

_177 tests passing. Last commit: `/flapstate` — Portal getTokenV2 curve pricing._
