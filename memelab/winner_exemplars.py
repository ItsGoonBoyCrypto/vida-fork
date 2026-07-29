"""Curated WINNER exemplars to harvest — known big movers we want to learn from.

For each entry the collector, on startup, pulls the token's earliest/top buyers
into the smart-money set and credits them with a winner in the reputation ledger
(distinct-winner overlap = the primary quality signal). This is how we teach the
system the wallets behind plays like the recent Solana runner — so the NEXT time
those wallets buy early, we catch it.

Harvest runs once per exemplar (deduped in SQLite), so re-listing is safe. The
live host does the fetch (RugCheck top-holders on Solana / explorer transfers on
EVM) — it has the network reach this build environment doesn't.

Extend without a code change via MEMELAB_WINNER_EXEMPLARS:
    "solana:<mint>=label, base:0x<token>=label, robinhood:0x<token>=label"
"""

from __future__ import annotations

from .models import Chain

# (Chain, token_address, label). Seeded with the user's recent Solana runner;
# add the 116x and any other confirmed movers here (or via the env var).
WINNER_EXEMPLARS: list = [
    (Chain.SOLANA, "2c1KjiyQow66QfsnCtoyuqfo3AuxgpBMEoAq5oiiXqdu", "SOL runner (last week)"),
]


def load_exemplars(env_value: str = "") -> list:
    """Curated list merged with MEMELAB_WINNER_EXEMPLARS entries. Returns
    [(Chain, token, label), …]; unknown chains are skipped."""
    out: list = []
    seen: set = set()
    for chain, token, label in WINNER_EXEMPLARS:
        key = (chain.value, token.lower())
        if key not in seen:
            seen.add(key)
            out.append((chain, token, label))
    for pair in (env_value or "").split(","):
        pair = pair.strip()
        if ":" not in pair:
            continue
        chain_s, rest = pair.split(":", 1)
        token, _, label = rest.partition("=")
        token = token.strip()
        try:
            chain = Chain(chain_s.strip().lower())
        except ValueError:
            continue
        key = (chain.value, token.lower())
        if token and key not in seen:
            seen.add(key)
            out.append((chain, token, label.strip() or "winner exemplar"))
    return out
