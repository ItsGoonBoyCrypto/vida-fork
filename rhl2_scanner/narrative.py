"""Narrative intelligence — which meta is printing right now.

Memecoin metas rotate: for a week it's dogs, then AI, then politics, then cats.
Being in the *current* narrative is a real edge, independent of any single
token's metrics. This classifies a token by its symbol/name into a narrative
bucket, then scores each narrative by how its recent tokens actually did — so a
token in a narrative that's been producing winners gets a boost, and a dead
narrative gets nothing.

Cheap (string matching) + reuses the paper-trade outcomes already recorded.
KOL/influencer wallets are handled as a labeled class of smart wallet (see the
scanner's /kol command), not here.
"""

from __future__ import annotations

# narrative → substrings that signal it (matched against lowercased symbol+name).
NARRATIVES = {
    "dog": ["doge", "shib", "inu", "dog", "bonk", "wif", "floki", "puppy"],
    "cat": ["cat", "meow", "popcat", "mew", "kitty"],
    "ai": ["ai", "gpt", "agent", "neural", "robot", "llm", "grok"],
    "frog": ["pepe", "frog", "kek", "wojak"],
    "politics": ["trump", "biden", "maga", "boden", "kamala", "potus", "elon"],
    "animal": ["bear", "bull", "monkey", "ape", "penguin", "goat", "hippo"],
    "finance": ["moon", "pump", "gem", "rich", "gold", "money", "bank"],
    "culture": ["chad", "based", "giga", "sigma", "meme", "coin"],
}


def classify(symbol: str, name: str = "") -> str:
    """Best-matching narrative for a token, or 'other'. First hit wins by the
    dict order (more specific metas listed first)."""
    text = f"{symbol or ''} {name or ''}".lower()
    for narrative, keys in NARRATIVES.items():
        if any(k in text for k in keys):
            return narrative
    return "other"


def narrative_stats(rows, win_mult: float = 2.0, rug_mult: float = 0.5) -> dict:
    """Per-narrative performance from settled paper trades.

    rows need 'symbol', 'max_mult', 'min_mult', 'settled'. Returns
    {narrative: {n, wins, rugs, hit_rate, rug_rate}} over settled rows.
    """
    stats: dict = {}
    for r in rows:
        if not r["settled"]:
            continue
        nar = classify(r["symbol"] or "")
        s = stats.setdefault(nar, {"n": 0, "wins": 0, "rugs": 0})
        s["n"] += 1
        if (r["max_mult"] or 1.0) >= win_mult:
            s["wins"] += 1
        if (r["min_mult"] or 1.0) <= rug_mult:
            s["rugs"] += 1
    for s in stats.values():
        s["hit_rate"] = round(s["wins"] / s["n"], 3) if s["n"] else 0.0
        s["rug_rate"] = round(s["rugs"] / s["n"], 3) if s["n"] else 0.0
    return stats


def hot_narratives(stats: dict, min_n: int = 4, min_hit_rate: float = 0.30) -> set:
    """Narratives currently worth a boost: enough sample and a real hit rate."""
    return {nar for nar, s in stats.items()
            if s["n"] >= min_n and s["hit_rate"] >= min_hit_rate and nar != "other"}
