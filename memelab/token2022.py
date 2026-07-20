"""Solana Token-2022 danger-extension audit.

The SPL Token-2022 program adds mint "extensions" that a token can pass every
classic check with (mint & freeze authority revoked, LP burned, low top-10) and
still rug — the single largest active Solana scam vector of 2026:

  · permanent-delegate  — an authority can transfer/burn ANY holder's tokens
                          without their signature (the industrial burn-scam).
  · transfer-hook       — an arbitrary program runs on every transfer, i.e. a
                          programmable honeypot (it can block your sell).
  · non-transferable    — the token literally cannot be sold.
  · default-frozen      — new token accounts start frozen; you can't sell until
                          an authority thaws you (selective honeypot).
  · transfer-fee        — a fee authority can raise the transfer fee post-launch
                          (a settable tax, up to 100%).

This module is a pure classifier over two possible sources:
  · a RugCheck report (`scan_rugcheck`) — works today, no Solana RPC key, since
    RugCheck already decodes the mint's extensions and names them in `risks`;
  · a direct getAccountInfo(jsonParsed) `extensions` list (`scan_extensions`) —
    the authoritative source when a Solana RPC is configured.

Returns which dangerous capabilities are present and whether they constitute a
HARD danger (treat as honeypot) vs. a settable-fee caution. Capability detection,
not intent — but with these extensions the authority *can* do it.
"""

from __future__ import annotations

from typing import Optional

# extension type (as it appears in getAccountInfo jsonParsed) -> (label, hard)
# hard=True => treat as honeypot-level (you can be blocked or drained).
_EXTENSIONS = {
    "permanentDelegate": ("permanent delegate (can seize your tokens)", True),
    "transferHook": ("transfer hook (programmable — can block sells)", True),
    "nonTransferable": ("non-transferable (cannot be sold)", True),
    "nonTransferableAccount": ("non-transferable (cannot be sold)", True),
    "defaultAccountState": ("default-frozen (new holders start frozen)", True),
    "transferFeeConfig": ("settable transfer fee (owner can tax you)", False),
    "transferFeeAmount": ("settable transfer fee (owner can tax you)", False),
}

# RugCheck risk names (lowercased, substring-matched) -> (label, hard).
_RUGCHECK_NAMES = [
    ("permanent delegate", ("permanent delegate (can seize your tokens)", True)),
    ("transfer hook", ("transfer hook (programmable — can block sells)", True)),
    ("non-transferable", ("non-transferable (cannot be sold)", True)),
    ("nontransferable", ("non-transferable (cannot be sold)", True)),
    ("default account state", ("default-frozen (new holders start frozen)", True)),
    ("freeze authority enabled", ("freeze authority live (can freeze your sell)", True)),
    ("transfer fee", ("settable transfer fee (owner can tax you)", False)),
]


def _blank() -> dict:
    return {"flags": [], "honeypot": False, "settable_fee": False, "fee_pct": None}


def scan_extensions(extensions) -> dict:
    """Classify a getAccountInfo(jsonParsed) `extensions` list.

    Each item looks like {"extension": "transferFeeConfig", "state": {...}}.
    Returns {flags, honeypot, settable_fee, fee_pct}.
    """
    out = _blank()
    if not isinstance(extensions, list):
        return out
    seen = set()
    for ext in extensions:
        if not isinstance(ext, dict):
            continue
        name = ext.get("extension") or ext.get("type") or ""
        hit = _EXTENSIONS.get(name)
        if not hit:
            continue
        label, hard = hit
        if name in ("defaultAccountState",):
            # only dangerous when the default is actually "frozen"
            state = ext.get("state")
            dv = state.get("accountState") if isinstance(state, dict) else state
            if str(dv).lower() not in ("frozen", "2"):
                continue
        if label not in seen:
            out["flags"].append(label)
            seen.add(label)
        if hard:
            out["honeypot"] = True
        else:
            out["settable_fee"] = True
            fee = _fee_pct(ext.get("state"))
            if fee is not None:
                out["fee_pct"] = max(out["fee_pct"] or 0.0, fee)
    return out


def scan_rugcheck(report: dict) -> dict:
    """Classify a RugCheck report's structured extension/risk data."""
    out = _blank()
    if not isinstance(report, dict):
        return out
    seen = set()

    def add(label: str, hard: bool):
        if label not in seen:
            out["flags"].append(label)
            seen.add(label)
        if hard:
            out["honeypot"] = True
        else:
            out["settable_fee"] = True

    # 1) Named risks (RugCheck decodes the extensions and lists them here).
    for r in (report.get("risks") or []):
        name = str(r.get("name", "")).lower()
        for needle, (label, hard) in _RUGCHECK_NAMES:
            if needle in name:
                add(label, hard)

    # 2) Explicit transfer-fee field, if present, gives the current fee %.
    fee = report.get("transferFee") or {}
    if isinstance(fee, dict):
        pct = _fee_pct(fee)
        if pct is not None and pct > 0:
            add("settable transfer fee (owner can tax you)", False)
            out["fee_pct"] = pct

    # 3) A token program marked token-2022 with any extension list echoed back.
    exts = report.get("tokenExtensions") or report.get("extensions")
    if isinstance(exts, list):
        sub = scan_extensions(exts)
        for f in sub["flags"]:
            if f not in seen:
                out["flags"].append(f)
                seen.add(f)
        out["honeypot"] = out["honeypot"] or sub["honeypot"]
        out["settable_fee"] = out["settable_fee"] or sub["settable_fee"]
        if sub["fee_pct"] is not None:
            out["fee_pct"] = max(out["fee_pct"] or 0.0, sub["fee_pct"])
    return out


def _fee_pct(state) -> Optional[float]:
    """Pull a transfer-fee percentage out of a fee state blob (basis points)."""
    if not isinstance(state, dict):
        return None
    # newer transfer fee sits under newerTransferFee.transferFeeBasispoints
    for key in ("newerTransferFee", "olderTransferFee"):
        blk = state.get(key)
        if isinstance(blk, dict):
            bps = blk.get("transferFeeBasisPoints") or blk.get("transferFeeBasisPoints".lower())
            if isinstance(bps, (int, float)):
                return round(float(bps) / 100.0, 2)
    bps = state.get("transferFeeBasisPoints") or state.get("basisPoints") or state.get("bps")
    if isinstance(bps, (int, float)):
        return round(float(bps) / 100.0, 2)
    pct = state.get("feePct") or state.get("pct")
    if isinstance(pct, (int, float)):
        return round(float(pct), 2)
    return None
