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
3. ~~Switch flap detection from vanity-suffix to getTokenV2~~ ✅ DONE — the curve
   listener keeps the free suffix fast-path AND confirms non-suffix candidates via
   Portal.getTokenV2 (bounded budget + one-shot cache), so non-vanity flap tokens
   like $meow are now caught. Knob: `chain.curve_confirm_budget` (0 = suffix-only).

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
`/group`/`/ungroup`/`/groups` · `/wallet` `/deployer` `/flapstate` `/curvepattern`
`/curveprobe` `/calibrate`

## More launchpads (Noxa shut down 2026-07)

Noxa (the $12M launchpad, launched CASHCAT) closed. Others are live — and their
**graduated tokens are already caught via DexScreener**, so no coverage was lost
for those. Added as curve-launchpad entries (pre-grad edge + labeling), enable
with a host Variable once verified on Blockscout:

- **RobinFun** — `RHL2_ROBINFUN_MANAGER` (research: `0xD952A74C85a2221a7DaB185c62cfD7EBa8C94AFC`,
  curve → 100%-burned LP ~$44k). Discovery mode until create_topic/confirm_fn pinned.
- **Bags** (bags.fm) — `RHL2_BAGS_MANAGER` (curve → Uniswap v4 locked LP; has an API
  at docs.bags.fm/robinhood we could wire like flap's getTokenV2).
- **MetaLaunch** `0x49A3D384cd90A58815df31C1852dB4095B90c0De` — pool-from-day-one,
  no curve, already caught by DexScreener (no wiring needed).
- Others seen: ArrowPad, RH6900, Robinfun, RobinPad (not live yet), hood.fun.

The listener now confirms non-suffix candidates via a per-launchpad `confirm_fn`
(flap=getTokenV2). To finish RobinFun/Bags pre-grad: set their manager env → read
`curve discovery [robinfun]` host logs → pin create_topic or confirm_fn.

## Ongoing (user)

- Keep feeding `/calibrate 0xWinner …` — turns misses into exact threshold tunes.
  (Already fixed the "contract not verified" gate this way.)
- Flip `RHL2_SMART_AUTOSEED=1` and `RHL2_WINNER_HARVEST=1` when ready.

## 🧬 Pre-migration curve pattern (NEW — learns the winning setup)

Tracks on-curve flap tokens over time (curve velocity: how fast progress /
reserve / holders climb — the strongest pre-graduation tell), learns the profile
of tokens that graduated AND pumped ≥3x, and fires a **🧬 CURVE MATCH** alert on
fresh tokens matching that profile while actively climbing.

- `curve_pattern.py`: features + bootstrap prior + `match()` + `learn_profile()`
  (percentile bands from winners; dependency-free).
- Storage: `curve_observations` (time series) + `curve_setups` (labeled outcomes).
- Scanner: `_track_curve()` (observe + match + alert), labeling hooked into the
  winner-harvest reprice sweep, learned profile cached hourly, obs pruned to 7d.
- `/curvepattern` shows the active profile + learning progress (winners needed).
- Cold-start: uses the **bootstrap prior** until ≥5 graduated-3x winners exist,
  then switches to the LEARNED profile automatically.
- Config: `curve_pattern_enabled`, `curve_match_alert`, `curve_pattern_min_winners`,
  `curve_obs_interval_seconds`, `curve_obs_retention_hours`.

_198 tests passing. Last commit: pre-migration curve pattern (track → learn → 🧬 match)._
