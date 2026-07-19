"""Scanner ↔ trader wiring: buy keyboard on alerts + callback dry-run preview."""

from __future__ import annotations

import unittest

from rhl2_scanner.config import Config
from rhl2_scanner.models import TokenSnapshot
from rhl2_scanner.scanner import Scanner
from trader.config import TraderConfig

ADMIN = 4242
TOKEN = "0x3450598e419abb5609f60e4b2fda127ff0897777"


class TestTraderWiring(unittest.IsolatedAsyncioTestCase):
    def _sc(self, enabled=True):
        cfg = Config()
        cfg.runtime.db_path = ":memory:"
        sc = Scanner(cfg)
        sc._trader = TraderConfig(enabled=enabled, dry_run=True, admin_user_ids=[ADMIN])
        sc._sent = []
        async def fake_send(html, reply_to=None, reply_markup=None):
            sc._sent.append(html)
            return 1
        sc._send_html = fake_send  # type: ignore
        return sc

    def test_keyboard_only_when_enabled(self):
        sc = self._sc(enabled=True)
        try:
            snap = TokenSnapshot(chain="robinhood", pair_address="0xp", token_address=TOKEN)
            kb = sc._buy_keyboard(snap)
            self.assertIsNotNone(kb)
            self.assertTrue(kb["inline_keyboard"][0][0]["text"].startswith("Buy"))
            sc._trader.enabled = False
            self.assertIsNone(sc._buy_keyboard(snap))
        finally:
            sc.storage.close()

    async def test_callback_dry_run_preview_for_alerted_token(self):
        sc = self._sc()
        # mark the token as alerted (allowlist rail) + stub answer_callback
        from rhl2_scanner.models import ScoreResult, AlertLevel, CategoryScore
        snap = TokenSnapshot(chain="robinhood", pair_address="0xpair", token_address=TOKEN)
        sc.storage.mark_seen(snap)
        sc.storage.record_alert(snap, ScoreResult(composite=80, level=AlertLevel.STRONG,
                                categories=[CategoryScore("safety", 80, 0.4)],
                                safety_passed=True), rank=2)
        import rhl2_scanner.tgtools as tg
        orig = tg.answer_callback
        async def fake_answer(*a, **k):
            pass
        tg.answer_callback = fake_answer
        try:
            cb = {"id": "cbid", "from": {"id": ADMIN}, "data": f"b:robinhood:{TOKEN}:0",
                  "message": {"chat": {"id": "123"}}}
            await sc._handle_buy_callback(cb)
            self.assertTrue(any("DRY-RUN" in h for h in sc._sent), sc._sent)
        finally:
            tg.answer_callback = orig
            sc.storage.close()

    async def test_callback_rejects_non_admin(self):
        sc = self._sc()
        import rhl2_scanner.tgtools as tg
        orig = tg.answer_callback
        async def fake_answer(*a, **k):
            pass
        tg.answer_callback = fake_answer
        try:
            cb = {"id": "cbid", "from": {"id": 999}, "data": f"b:robinhood:{TOKEN}:0",
                  "message": {"chat": {"id": "123"}}}
            await sc._handle_buy_callback(cb)
            self.assertTrue(any("unauthor" in h.lower() for h in sc._sent), sc._sent)
        finally:
            tg.answer_callback = orig
            sc.storage.close()


if __name__ == "__main__":
    unittest.main()
