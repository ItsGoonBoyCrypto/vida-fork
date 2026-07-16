# RH L2 Scanner — resume note

_Pinned to pick up next session. Branch: `claude/rh-l2-memecoin-scanner-7sj2qa`._

## ⏭️ Where we left off — the ONE open thread: flap curve pricing

flap tokens pump on the **bonding curve** and only hit DexScreener *after* they
graduate — so the scanner currently can't price/score them pre-graduation
(exactly the phase we want). To fix, we need flap's **on-chain price getter**.

Tooling is built and working: **`/curveprobe 0xToken`** probes the flap Portal
(`0x26605f322f7fF986f381bB9A6e3f5DAb0bEaEb09`) for common price/reserve/quote
functions; **`/deployer 0xToken`** confirms the Portal created it (via Blockscout).

**Blocked on:** a VALID flap token CA. The last CA tried
(`0xda4109d84a022b36b88273963f506ce02ef942ae`) is **not a contract** (both
tools confirmed) — a wallet or mistyped address.

**NEXT STEPS (tomorrow):**
1. Grab a real flap token CA from `flap.sh/robinhood/board` — one **currently
   bonding** (progress bar, not graduated). Verify on `robinhoodchain.blockscout.com`
   it shows a "Token" tab.
2. `/deployer 0x<token>` → expect `created by: 0x26605f…` (the Portal).
3. `/curveprobe 0x<token>` → paste the getter(s) that return data.
4. Claude pins the price function → wires **live curve pricing** → flap tokens
   get scored/alerted while still bonding (earliest entry on the chain).
5. Also then: switch flap-token detection from vanity-suffix (8888/7777) to
   "created by the Portal", since not all flap tokens use vanity addresses.

If `/curveprobe` returns "succeeded but empty", flap uses custom function names
— grab the read function from the flap **bonding-curve dev docs**.

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
`/group`/`/ungroup`/`/groups` · `/wallet` `/deployer` `/curveprobe` `/calibrate`

## Ongoing (user)

- Keep feeding `/calibrate 0xWinner …` — turns misses into exact threshold tunes.
  (Already fixed the "contract not verified" gate this way.)
- Flip `RHL2_SMART_AUTOSEED=1` and `RHL2_WINNER_HARVEST=1` when ready.

_173 tests passing. Last commit: gentle/trustworthy `/curveprobe`._
