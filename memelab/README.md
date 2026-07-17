# memelab — multi-chain meme analytics & backtest engine

> Scaffold. Generalises the RH L2 scanner into a cross-chain (Robinhood, Solana,
> Ethereum, Base) platform that **derives the metric signature of past big
> pumpers and screens live tokens against it.** Rename freely.

## The core idea — "winner DNA"

We don't hand-pick thresholds. We let the data set them:

```
  1. SNAPSHOT   Continuously record every new token's metrics over time
                (per chain), building a point-in-time time-series DB.
  2. LABEL      Weeks later, label each token's OUTCOME (peak multiple from
                first-seen): WINNER (≥Nx), dud, or rug.
  3. DERIVE     Extract each token's feature vector AT DISCOVERY and find the
                feature ranges / combinations that separate winners from duds
                → a "signature" (threshold rules and/or a fitted model) with
                measured precision/recall.
  4. VALIDATE   Out-of-sample: does the signature predict winners on held-out
                tokens and later time windows? Guard against overfitting.
  5. SCREEN     Apply the signature to LIVE tokens, rank by pump-probability,
                alert the top of the funnel.
```

The platform's edge **compounds**: the longer it snapshots, the larger the
labeled dataset, the sharper the signatures. (The RH scanner's winner-harvest is
a preview of step 2–3 for wallets; this generalises it to full feature vectors.)

## Why these four chains fit one platform

| Chain     | Kind  | Market data      | New-token source            | Safety / rug            |
|-----------|-------|------------------|-----------------------------|-------------------------|
| Robinhood | EVM   | DexScreener      | flap curve + Uniswap V3     | honeypot sim, on-chain  |
| Base      | EVM   | DexScreener      | Uniswap/Aerodrome + launchpads | honeypot sim, GoPlus |
| Ethereum  | EVM   | DexScreener      | Uniswap V2/V3 + launchpads  | honeypot sim, GoPlus    |
| Solana    | SVM   | DexScreener      | pump.fun / Raydium          | RugCheck, SPL authorities|

**DexScreener is the unifying spine** — it indexes all four chains with the same
schema (price, liquidity, volume/txns per window, socials). Chain-specific
*adapters* handle discovery-at-source and safety enrichment behind one interface.

## Architecture

```
                  ┌─────────────── chains/ (adapters) ───────────────┐
                  │  evm(robinhood, ethereum, base)   solana         │
                  │  · discover new tokens at source                 │
                  │  · enrich safety (honeypot/authorities/holders)  │
                  └───────────────────────┬──────────────────────────┘
   ingest/dexscreener (unified market data, all chains)
                                          │
                          ┌───────────────▼───────────────┐
                          │  storage/  point-in-time       │
                          │  snapshots (token time-series) │
                          └───────┬───────────────┬────────┘
                        (live)    │               │  (historical)
             metrics/features ────┤               ├──── backtest/labeler (outcomes)
             (feature vector)     │               │
                          ┌───────▼───────┐ ┌─────▼────────────────┐
                          │ screener/     │ │ backtest/engine      │
                          │ score live vs │ │ derive+validate the  │
                          │ signature     │◄┤ winner signature     │
                          └───────┬───────┘ └──────────────────────┘
                          alerting (Telegram/…) + api/ (UI, queries)
```

## Layout

- `models.py`         — unified data model: `Chain`, `TokenSnapshot`, `Outcome`, `FeatureVector`, `Signature`
- `chains/base.py`    — `ChainAdapter` ABC (discover + enrich); `registry.py` chain configs
- `chains/evm.py`     — shared EVM adapter (RH/ETH/Base); `solana.py` — SVM adapter
- `ingest/dexscreener.py` — unified market data for every chain
- `storage.py`        — point-in-time snapshot store (the dataset that compounds)
- `metrics/features.py`   — feature extraction (the back-tested metrics)
- `backtest/labeler.py`   — label outcomes (winner/dud/rug) from the time-series
- `backtest/engine.py`    — derive + validate the winner signature
- `screener/engine.py`    — score live tokens against the signature
- `api/app.py`        — query/serve signatures, screens, backtests (UI backend)

## Reuse from `rhl2_scanner`

The RH scanner already has production-grade versions of several pieces —
DexScreener client, honeypot sim, bundle/sniper analysis, smart-money, scoring,
alerting, storage. The EVM adapter wraps those; Solana is the main net-new work.

## Run it

```
python -m memelab collect                 # snapshot loop, all 4 chains (leave running)
python -m memelab collect --chains solana,base
python -m memelab backtest                # derive + validate a signature now
python -m memelab screen 0xTOKEN --chain base
python -m memelab stats                   # dataset coverage + signature status
uvicorn "memelab.api.app:app"             # dashboard backend (pip install fastapi uvicorn)
```

Leave `collect` running — it accumulates the dataset the backtest learns from.
Once enough tokens have aged ~48h, `backtest` produces a validated signature and
`screen` (and the collector's alerting hook) start ranking live tokens.

## Status

**Implemented + tested (offline):**
- data model, storage (SQLite point-in-time store), labeler, feature extraction
  (incl. velocity/growth features: vol accel, buy-pressure trend, holder velocity,
  liquidity growth)
- **bootstrap prior signature** — screens + alerts from DAY ONE on proven
  meme-pumper heuristics; the backtest replaces it once a data-derived signature
  validates (and never overwrites it with an empty one)
- backtest engine (rule-mining + linear model, time-split validate: precision/recall/lift)
- screener (score live tokens vs signature, signature (de)serialisation)
- source-level discovery: pump.fun (Solana, pre-graduation) + DEX-factory logs
  (EVM — RH out of the box; ETH/Base when an RPC is set); DexScreener fallback
- alerting with tap-to-copy CA + Chart/𝕏/TG/Web links; autonomous 6h backtest loop
- DexScreener ingest mapping (schema-verified with fixtures), CLI, read-only API
- **29 tests** incl. a full-loop integration (record → relabel → backtest → screen)

**Verified on deploy (network blocked from the build env):** live DexScreener
HTTP, GoPlus (EVM) + RugCheck (Solana) enrichment calls.

**Enhancements next:** per-chain source-level discovery (pump.fun / Raydium for
Solana; DEX-factory + launchpad listeners for EVM — RH's already exist in
rhl2_scanner); wiring the collector's alerting hook to the RH Telegram bot.
