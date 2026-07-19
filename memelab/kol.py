"""Seed KOL / influencer wallets into memelab's smart-money set.

KOLs (Ansem, Cobie, Hsaka, Cupsey, …) are almost all Solana traders whose buys
move markets. Tracked as a labeled class of smart wallet (source ``kol:<name>``)
so a token they buy scores higher and fires a 📣 KOL alert.

Addresses are OPERATOR-SUPPLIED, never hardcoded guesses — following a wrong
"Ansem wallet" (there are many impersonator lists) loses money. Supply verified
addresses two ways:

  • env  MEMELAB_KOL_WALLETS = "solana:<addr>:Ansem,solana:<addr>:Cobie"
  • file MEMELAB_KOL_FILE   = path to a YAML/JSON list of {chain,address,name}

Source verified addresses from Arkham Intelligence entity labels
(intel.arkm.com), Nansen, or the KOL's own posted wallet — see
kol_wallets.example.yaml.
"""

from __future__ import annotations

import json
import logging
import os

from .models import Chain

log = logging.getLogger("memelab.kol")


def _parse_env(raw: str) -> list:
    """"chain:addr:Name,chain:addr:Name" → [(Chain, addr, name)]."""
    out = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        parts = item.split(":")
        if len(parts) < 2:
            continue
        chain_s, addr = parts[0].strip().lower(), parts[1].strip()
        name = parts[2].strip() if len(parts) > 2 else "KOL"
        try:
            out.append((Chain(chain_s), addr, name))
        except ValueError:
            log.warning("KOL seed: unknown chain %r", chain_s)
    return out


def _parse_file(path: str) -> list:
    """A YAML or JSON list of {chain, address, name} → [(Chain, addr, name)]."""
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        log.warning("KOL file not found: %s", path)
        return []
    data = None
    try:
        data = json.loads(text)
    except ValueError:
        try:
            import yaml
            data = yaml.safe_load(text)
        except Exception:  # noqa: BLE001
            log.warning("KOL file unparseable (need JSON, or PyYAML for YAML): %s", path)
            return []
    rows = data.get("kols") if isinstance(data, dict) else data
    out = []
    for r in rows or []:
        try:
            chain = Chain(str(r["chain"]).lower())
            addr = str(r["address"]).strip()
            if addr and not addr.startswith("<"):     # skip template placeholders
                out.append((chain, addr, str(r.get("name") or "KOL")))
        except (KeyError, ValueError, TypeError):
            continue
    return out


def kol_seeds() -> list:
    """All configured KOL wallets from env + file, de-duplicated."""
    seeds = _parse_env(os.environ.get("MEMELAB_KOL_WALLETS", ""))
    path = os.environ.get("MEMELAB_KOL_FILE", "")
    if path:
        seeds += _parse_file(path)
    seen, out = set(), []
    for chain, addr, name in seeds:
        key = (chain.value, addr.lower())
        if key not in seen:
            seen.add(key)
            out.append((chain, addr, name))
    return out


def seed_kols(store) -> int:
    """Load configured KOL wallets into the smart set (source ``kol:<name>``).
    Returns the number added. Safe to call every boot (idempotent)."""
    added = 0
    for chain, addr, name in kol_seeds():
        if store.add_smart_wallet(chain, addr, source=f"kol:{name}"):
            added += 1
    if added:
        log.info("seeded %d KOL wallet(s) into the smart set", added)
    return added
