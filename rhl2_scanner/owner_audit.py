"""Owner-mutability audit via 4-byte selector scan of a contract's bytecode.

A token can pass every launch-time safety check — sellable, LP burned, low
bundle — and still be a rug in waiting: if the owner hasn't renounced and the
contract exposes an owner-only ``setFees`` / ``blacklist`` / ``pause`` / ``mint``
function, the owner can flip the tax to 99%, block your sell, or dilute supply
*after* people buy. That capability is written into the bytecode, so we can
detect it even on an UNVERIFIED contract (the norm on a new L2) by scanning the
runtime code for the 4-byte selectors of known-dangerous functions.

This is deliberately capability-detection, not intent: presence of a hook plus a
live (non-renounced) owner = the owner *can* do it. The caller pairs this scan
with an on-chain ``owner()`` read to decide whether the capability is live.

Pure + dependency-free (keccak only): the caller fetches ``eth_getCode`` and
passes the hex in. Selector collisions are astronomically unlikely for a curated
list, but this is a heuristic — a custom contract with renamed functions can
evade it, which is why it *demotes/gates*, it doesn't stand alone.
"""

from __future__ import annotations

from .keccak import keccak256

# (signature, category, human label). Categories: "tax" (fee/tax setters),
# "blacklist" (block a holder's sell), "pause" (freeze trading), "mint" (inflate
# supply), "limits" (max-tx / max-wallet — softer, can wall you in). We cover the
# common OpenZeppelin / templated-memecoin signatures; exact arg types matter for
# the selector, so several variants of each are listed.
_SIGNATURES: list[tuple[str, str, str]] = [
    # -- fee / tax setters (owner can tax you to zero) --
    ("setFee(uint256)", "tax", "owner can set fee"),
    ("setFees(uint256,uint256)", "tax", "owner can set fees"),
    ("setFees(uint256,uint256,uint256)", "tax", "owner can set fees"),
    ("setTax(uint256)", "tax", "owner can set tax"),
    ("setTaxes(uint256,uint256)", "tax", "owner can set taxes"),
    ("setBuyTax(uint256)", "tax", "owner can set buy tax"),
    ("setSellTax(uint256)", "tax", "owner can set sell tax"),
    ("setBuyFee(uint256)", "tax", "owner can set buy fee"),
    ("setSellFee(uint256)", "tax", "owner can set sell fee"),
    ("setTaxFee(uint256)", "tax", "owner can set tax fee"),
    ("updateFee(uint256)", "tax", "owner can update fee"),
    ("updateFees(uint256,uint256)", "tax", "owner can update fees"),
    ("updateTax(uint256)", "tax", "owner can update tax"),
    ("setTotalFee(uint256)", "tax", "owner can set total fee"),
    ("setTaxes(uint256,uint256,uint256)", "tax", "owner can set taxes"),
    # -- blacklist / denylist (owner can block your sell) --
    ("blacklist(address)", "blacklist", "owner can blacklist wallets"),
    ("blacklist(address,bool)", "blacklist", "owner can blacklist wallets"),
    ("setBlacklist(address,bool)", "blacklist", "owner can blacklist wallets"),
    ("addBlacklist(address)", "blacklist", "owner can blacklist wallets"),
    ("setBlacklisted(address,bool)", "blacklist", "owner can blacklist wallets"),
    ("blocklist(address,bool)", "blacklist", "owner can blocklist wallets"),
    ("setBots(address,bool)", "blacklist", "owner can flag wallets as bots"),
    ("setBot(address,bool)", "blacklist", "owner can flag wallets as bots"),
    ("denylist(address,bool)", "blacklist", "owner can denylist wallets"),
    # -- pause / trading switch (owner can freeze the market) --
    ("pause()", "pause", "owner can pause transfers"),
    ("setPause(bool)", "pause", "owner can pause transfers"),
    ("setTradingEnabled(bool)", "pause", "owner controls a trading switch"),
    ("enableTrading()", "pause", "owner controls a trading switch"),
    ("setTrading(bool)", "pause", "owner controls a trading switch"),
    ("setSwapEnabled(bool)", "pause", "owner controls swap enablement"),
    # -- mint (owner can inflate supply) --
    ("mint(address,uint256)", "mint", "owner can mint (inflate supply)"),
    ("mint(uint256)", "mint", "owner can mint (inflate supply)"),
    # -- limits (softer — can wall holders in) --
    ("setMaxTx(uint256)", "limits", "owner can set max-tx limit"),
    ("setMaxTxAmount(uint256)", "limits", "owner can set max-tx limit"),
    ("setMaxWallet(uint256)", "limits", "owner can set max-wallet limit"),
    ("setMaxWalletAmount(uint256)", "limits", "owner can set max-wallet limit"),
]

# Categories that constitute a live post-buy rug vector (gate-worthy with an
# active owner). "limits" is intentionally excluded — it's a demotion, not a gate.
_RUG_CATEGORIES = {"tax", "blacklist", "pause", "mint"}


def _selector(sig: str) -> str:
    return keccak256(sig.encode())[:4].hex()


# Precomputed once: selector -> (category, label).
_SELECTOR_MAP: dict[str, tuple[str, str]] = {
    _selector(sig): (cat, label) for sig, cat, label in _SIGNATURES
}


def scan_bytecode(code_hex: str) -> dict:
    """Scan runtime bytecode for dangerous owner-only selectors.

    Returns {"hooks": [labels], "categories": {set}, "can_rug": bool}. ``can_rug``
    is True when a gate-worthy capability (tax/blacklist/pause/mint) is present —
    the caller still requires a live owner before acting on it.
    """
    code = (code_hex or "").lower()
    if code.startswith("0x"):
        code = code[2:]
    if not code or code == "":
        return {"hooks": [], "categories": set(), "can_rug": False}
    labels: list[str] = []
    cats: set[str] = set()
    seen: set[str] = set()
    for sel, (cat, label) in _SELECTOR_MAP.items():
        # A PUSH4 of the selector appears in the function dispatcher of any
        # contract that implements it — a plain substring match is sufficient.
        if sel in code and label not in seen:
            labels.append(label)
            seen.add(label)
            cats.add(cat)
    return {"hooks": labels, "categories": cats,
            "can_rug": bool(cats & _RUG_CATEGORIES)}


def owner_is_active(owner: str | None, burn_addresses: set[str]) -> bool | None:
    """Classify an ``owner()`` read: True = live (rug-capable), False = renounced,
    None = unknown (no owner function / call failed)."""
    if not owner:
        return None
    o = owner.lower()
    if o in burn_addresses or set(o.replace("0x", "")) == {"0"}:
        return False
    return True
