"""Security hardening rails: command auth, spend ledger, callback replay/lock,
buy-time safety veto, slippage clamp, and DB backup."""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from rhl2_scanner.config import Config
from rhl2_scanner.models import AlertLevel, CategoryScore, ScoreResult, TokenSnapshot
from rhl2_scanner.scanner import Scanner
from rhl2_scanner.storage import Storage
from trader.config import TraderConfig

ADMIN = 4242
TOKEN = "0x3450598e419abb5609f60e4b2fda127ff0897777"


def _scanner(enabled=True):
    cfg = Config()
    cfg.runtime.db_path = ":memory:"
    cfg.telegram.bot_token = "tok"
    cfg.telegram.alert_chat_id = "-100999"
    sc = Scanner(cfg)
    sc._trader = TraderConfig(enabled=enabled, dry_run=True, admin_user_ids=[ADMIN])
    sc._sent = []

    async def fake_send(html, reply_to=None, reply_markup=None):
        sc._sent.append(html)
        return 1
    sc._send_html = fake_send  # type: ignore
    return sc


def _mark_alerted(sc, token=TOKEN):
    snap = TokenSnapshot(chain="robinhood", pair_address="0xpair", token_address=token)
    sc.storage.mark_seen(snap)
    sc.storage.record_alert(snap, ScoreResult(composite=80, level=AlertLevel.STRONG,
                            categories=[CategoryScore("safety", 80, 0.4)],
                            safety_passed=True), rank=2)


class _NoAnswer:
    """Context manager stubbing tgtools.answer_callback."""

    def __enter__(self):
        import rhl2_scanner.tgtools as tg
        self._tg, self._orig = tg, tg.answer_callback
        self.popups = []

        async def fake_answer(token, cb_id, text="", session=None, show_alert=False):
            self.popups.append(text)
        tg.answer_callback = fake_answer
        return self

    def __exit__(self, *a):
        self._tg.answer_callback = self._orig
        return False


class TestCommandAuth(unittest.IsolatedAsyncioTestCase):
    async def _poll_with(self, sc, update):
        import rhl2_scanner.tgtools as tg
        orig = tg.get_updates

        async def fake_updates(token, offset=None, session=None):
            return [update], (offset or 0) + 1
        tg.get_updates = fake_updates
        sc._session = object()          # non-None gates _poll_commands open
        try:
            await sc._poll_commands()
        finally:
            tg.get_updates = orig

    def _dm(self, from_id, text="/muted"):
        import time
        return {"update_id": 1, "message": {
            "chat": {"id": 777, "type": "private"},
            "from": {"id": from_id}, "date": time.time(), "text": text}}

    async def test_stranger_dm_is_refused(self):
        sc = _scanner()
        try:
            sc.cfg.telegram.admin_user_ids = [ADMIN]
            await self._poll_with(sc, self._dm(from_id=999))
            self.assertTrue(any("Not authorized" in h for h in sc._sent), sc._sent)
            self.assertFalse(any("muted" in h.lower() for h in sc._sent))
        finally:
            sc.storage.close()

    async def test_admin_dm_is_served(self):
        sc = _scanner()
        try:
            sc.cfg.telegram.admin_user_ids = [ADMIN]
            await self._poll_with(sc, self._dm(from_id=ADMIN))
            self.assertFalse(any("Not authorized" in h for h in sc._sent), sc._sent)
            self.assertTrue(sc._sent)      # /muted replied something
        finally:
            sc.storage.close()

    async def test_trader_admin_counts_as_admin(self):
        sc = _scanner()
        try:
            sc.cfg.telegram.admin_user_ids = []     # only TRADER_ADMIN_IDS set
            await self._poll_with(sc, self._dm(from_id=ADMIN))
            self.assertFalse(any("Not authorized" in h for h in sc._sent), sc._sent)
        finally:
            sc.storage.close()

    async def test_alert_channel_post_is_served(self):
        import time
        sc = _scanner()
        try:
            upd = {"update_id": 1, "channel_post": {
                "chat": {"id": -100999, "type": "channel"},
                "date": time.time(), "text": "/muted"}}
            await self._poll_with(sc, upd)
            self.assertFalse(any("Not authorized" in h for h in sc._sent), sc._sent)
            self.assertTrue(sc._sent)
        finally:
            sc.storage.close()


class TestSpendLedger(unittest.TestCase):
    def test_record_and_read(self):
        s = Storage(":memory:")
        try:
            self.assertEqual(s.trade_spent_today("base"), 0.0)
            self.assertAlmostEqual(s.record_trade_spend("base", 0.05), 0.05)
            self.assertAlmostEqual(s.record_trade_spend("BASE", 0.02), 0.07)
            self.assertAlmostEqual(s.trade_spent_today("base"), 0.07, places=9)
            self.assertEqual(s.trade_spent_today("solana"), 0.0)
        finally:
            s.close()


class TestCallbackRails(unittest.IsolatedAsyncioTestCase):
    def _cb(self, cb_id="cb1", from_id=ADMIN):
        return {"id": cb_id, "from": {"id": from_id},
                "data": f"b:robinhood:{TOKEN}:0", "message": {"chat": {"id": "1"}}}

    async def test_replayed_callback_processed_once(self):
        sc = _scanner()
        _mark_alerted(sc)
        try:
            with _NoAnswer():
                await sc._handle_buy_callback(self._cb("dup"))
                n = len(sc._sent)
                self.assertGreater(n, 0)
                await sc._handle_buy_callback(self._cb("dup"))   # replayed backlog
                self.assertEqual(len(sc._sent), n)               # no second execution
        finally:
            sc.storage.close()

    async def test_daily_cap_enforced_from_ledger(self):
        sc = _scanner()
        _mark_alerted(sc)
        try:
            cap = sc._trader.daily_cap.get("robinhood", 0.5)
            sc.storage.record_trade_spend("robinhood", cap)      # day already spent
            with _NoAnswer():
                await sc._handle_buy_callback(self._cb("capped"))
            self.assertTrue(any("daily cap" in h.lower() for h in sc._sent), sc._sent)
        finally:
            sc.storage.close()

    async def test_muted_token_vetoed_at_buy_time(self):
        sc = _scanner()
        _mark_alerted(sc)
        try:
            sc.storage.mute_token(TOKEN)
            with _NoAnswer() as na:
                await sc._handle_buy_callback(self._cb("mute"))
            self.assertTrue(any("muted" in p for p in na.popups), na.popups)
        finally:
            sc.storage.close()

    async def test_memelab_honeypot_vetoed_at_buy_time(self):
        from memelab.models import Chain as MChain, TokenSnapshot as MSnap
        from memelab.storage import Store
        sc = _scanner()
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        try:
            st = Store(tmp.name)
            st.record_snapshot(MSnap(chain=MChain.BASE, token_address=TOKEN, ts=1.0,
                                     price_usd=1.0, is_honeypot=True))
            st.close()
            with mock.patch.dict(os.environ, {"MEMELAB_DB": tmp.name}):
                veto = await sc._buy_time_safety("base", TOKEN)
            self.assertIn("HONEYPOT", veto)
        finally:
            os.unlink(tmp.name)
            sc.storage.close()


class TestSlippageClamp(unittest.TestCase):
    def test_out_of_range_resets_to_default(self):
        for bad in ("200", "0", "-5"):
            with mock.patch.dict(os.environ, {"TRADER_SLIPPAGE": bad}):
                self.assertEqual(TraderConfig.from_env().slippage_pct, 15.0)
        with mock.patch.dict(os.environ, {"TRADER_SLIPPAGE": "25"}):
            self.assertEqual(TraderConfig.from_env().slippage_pct, 25.0)


class TestScannerBackup(unittest.TestCase):
    def test_backup_roundtrip(self):
        src = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        dst = tempfile.NamedTemporaryFile(suffix=".bak", delete=False)
        src.close(); dst.close()
        try:
            s = Storage(src.name)
            s.kv_set("boot_count", "7")
            s.backup(dst.name)
            s.close()
            r = Storage(dst.name)
            try:
                self.assertEqual(r.kv_get("boot_count"), "7")
            finally:
                r.close()
        finally:
            os.unlink(src.name)
            os.unlink(dst.name)


if __name__ == "__main__":
    unittest.main()
