"""Curated WINNER exemplars (Robinhood chain) to harvest on startup.

Confirmed big movers whose earliest buyers we want in the smart-money set, so
the wallets behind them are tracked and their NEXT early buy is caught. The
scanner harvests each once (via the Blockscout transfer feed on the live host),
crediting winner-overlap in the reputation ledger.

This is belt-and-braces alongside the automatic ≥10x big-mover harvest: an
exemplar is pulled deterministically on deploy even if its tracked peak has
aged out of the paper ledger.

Extend without a code change via RHL2_WINNER_EXEMPLARS:
    "0x<token>=label, 0x<token>=label"
"""

from __future__ import annotations

# token address (lowercased) -> label
WINNER_EXEMPLARS: dict[str, str] = {
    "0xcdd50d73b45085d71cb05e2ca238d12c3bd7bebd": "132x runner",
}


def load_winner_exemplars(env_value: str = "") -> dict[str, str]:
    """Curated map merged with RHL2_WINNER_EXEMPLARS ("0xaddr=label,..").
    EVM addresses only (RH chain); lowercased."""
    out = {a.lower(): lbl for a, lbl in WINNER_EXEMPLARS.items()}
    for pair in (env_value or "").split(","):
        pair = pair.strip()
        if "=" not in pair:
            continue
        addr, _, lbl = pair.partition("=")
        addr = addr.strip().lower()
        if addr.startswith("0x") and len(addr) == 42:
            out[addr] = lbl.strip() or "winner exemplar"
    return out
