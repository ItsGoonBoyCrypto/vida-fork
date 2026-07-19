"""Telegram inline-keyboard buy buttons + callback parsing.

A buy is an inline button on the alert (safer than a shareable URL — the click
carries the clicker's user id, which we authorise). callback_data is capped at
64 bytes by Telegram; our encoding stays well under.
"""

from __future__ import annotations

from typing import Optional

from .config import TraderConfig

_PREFIX = "b"          # buy callbacks: "b:<chain>:<token>:<preset_idx>"


def buy_keyboard(chain: str, token: str, cfg: TraderConfig) -> Optional[dict]:
    """Inline keyboard of buy-amount buttons for an alert, or None if disabled."""
    if not cfg.enabled or not token:
        return None
    presets = cfg.presets_for(chain)
    native = cfg.native_of(chain)
    tag = "" if not cfg.dry_run else " 🧪"      # mark dry-run so it's unmistakable
    row = []
    for i, amt in enumerate(presets):
        data = f"{_PREFIX}:{chain}:{token}:{i}"
        if len(data.encode()) > 64:             # Telegram hard limit — skip if over
            continue
        row.append({"text": f"Buy {amt:g} {native}{tag}", "callback_data": data})
    if not row:
        return None
    return {"inline_keyboard": [row]}


def parse_callback(data: str) -> Optional[tuple]:
    """"b:<chain>:<token>:<idx>" → (chain, token, preset_idx), or None."""
    if not data:
        return None
    parts = data.split(":")
    if len(parts) != 4 or parts[0] != _PREFIX:
        return None
    chain, token, idx = parts[1], parts[2], parts[3]
    if not chain or not token or not idx.isdigit():
        return None
    return chain, token, int(idx)
