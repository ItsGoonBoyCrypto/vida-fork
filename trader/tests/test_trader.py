"""Trader safety rails, buttons, and dry-run engine."""

from __future__ import annotations

import unittest

from trader.buttons import buy_keyboard, parse_callback
from trader.config import TraderConfig
from trader.engine import preview_buy
from trader.safety import authorize

ADMIN = 4242
TOKEN = "0x3450598e419abb5609f60e4b2fda127ff0897777"


def _cfg(**kw):
    c = TraderConfig(enabled=True, dry_run=True, admin_user_ids=[ADMIN])
    for k, v in kw.items():
        setattr(c, k, v)
    return c


class TestSafety(unittest.TestCase):
    def test_admin_gate(self):
        c = _cfg()
        ok, why = authorize("base", TOKEN, 0.01, 999, alerted=True, spent_today=0, cfg=c)
        self.assertFalse(ok)
        self.assertIn("unauthor", why.lower())
        ok, _ = authorize("base", TOKEN, 0.01, ADMIN, alerted=True, spent_today=0, cfg=c)
        self.assertTrue(ok)

    def test_allowlist(self):
        c = _cfg()
        ok, why = authorize("base", TOKEN, 0.01, ADMIN, alerted=False, spent_today=0, cfg=c)
        self.assertFalse(ok)
        self.assertIn("allowlist", why)

    def test_per_trade_cap(self):
        c = _cfg(per_trade_cap={"base": 0.05})
        ok, why = authorize("base", TOKEN, 0.2, ADMIN, alerted=True, spent_today=0, cfg=c)
        self.assertFalse(ok)
        self.assertIn("per-trade cap", why)

    def test_daily_cap(self):
        c = _cfg(daily_cap={"base": 0.1})
        ok, why = authorize("base", TOKEN, 0.05, ADMIN, alerted=True, spent_today=0.08, cfg=c)
        self.assertFalse(ok)
        self.assertIn("daily cap", why)


class TestButtons(unittest.TestCase):
    def test_keyboard_and_roundtrip(self):
        c = _cfg(buy_presets={"solana": [0.05, 0.1]})
        kb = buy_keyboard("solana", TOKEN, c)
        self.assertIsNotNone(kb)
        btns = kb["inline_keyboard"][0]
        self.assertEqual(len(btns), 2)
        self.assertIn("SOL", btns[0]["text"])
        self.assertIn("🧪", btns[0]["text"])              # dry-run marked
        parsed = parse_callback(btns[1]["callback_data"])
        self.assertEqual(parsed, ("solana", TOKEN, 1))

    def test_disabled_no_keyboard(self):
        c = TraderConfig(enabled=False)
        self.assertIsNone(buy_keyboard("base", TOKEN, c))

    def test_callback_data_under_64_bytes(self):
        c = _cfg()
        kb = buy_keyboard("robinhood", TOKEN, c)
        for b in kb["inline_keyboard"][0]:
            self.assertLessEqual(len(b["callback_data"].encode()), 64)

    def test_parse_rejects_garbage(self):
        self.assertIsNone(parse_callback("x:base:tok:0"))
        self.assertIsNone(parse_callback("b:base:tok"))
        self.assertIsNone(parse_callback(""))


class TestEngine(unittest.TestCase):
    def test_dry_run_preview(self):
        c = _cfg()
        r = preview_buy("solana", TOKEN, 0.1, ADMIN, symbol="GEM", alerted=True,
                        spent_today=0, cfg=c, price_usd=0.0005, native_usd=150.0)
        self.assertTrue(r.ok)
        self.assertTrue(r.dry_run)
        self.assertIn("DRY-RUN", r.preview)
        self.assertGreater(r.est_tokens, 0)               # 0.1*150/0.0005*(1-slip)

    def test_rejects_when_disabled(self):
        c = TraderConfig(enabled=False, admin_user_ids=[ADMIN])
        r = preview_buy("base", TOKEN, 0.01, ADMIN, symbol="X", alerted=True,
                        spent_today=0, cfg=c)
        self.assertFalse(r.ok)
        self.assertIn("OFF", r.reason)

    def test_live_path_gated_off(self):
        c = _cfg(dry_run=False)
        r = preview_buy("base", TOKEN, 0.01, ADMIN, symbol="X", alerted=True,
                        spent_today=0, cfg=c)
        self.assertFalse(r.ok)                            # live not wired in phase 1
        self.assertIn("Phase 2", r.reason)


if __name__ == "__main__":
    unittest.main()
