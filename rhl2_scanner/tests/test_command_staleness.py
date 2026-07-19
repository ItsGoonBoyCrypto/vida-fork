"""Stale Telegram commands (the getUpdates backlog after a redeploy) are skipped
so old /harvest etc. don't re-fire on every restart."""

from __future__ import annotations

import time
import unittest

import rhl2_scanner.tgtools as tgtools
from rhl2_scanner.config import Config
from rhl2_scanner.scanner import Scanner


def _update(uid, text, date):
    return {"update_id": uid,
            "message": {"text": text, "date": date, "chat": {"id": "123", "type": "private"}}}


class TestCommandStaleness(unittest.IsolatedAsyncioTestCase):
    def _sc(self):
        cfg = Config()
        cfg.runtime.db_path = ":memory:"
        cfg.telegram.bot_token = "x"
        cfg.telegram.alert_chat_id = "123"
        cfg.runtime.command_max_age_seconds = 180
        sc = Scanner(cfg)
        sc._session = object()          # non-None; get_updates is stubbed
        sc._handled = []
        async def _fake_handle(text):
            sc._handled.append(text)
        sc._handle_command = _fake_handle  # type: ignore
        return sc

    async def test_skips_backlog_processes_fresh(self):
        sc = self._sc()
        now = time.time()
        orig = tgtools.get_updates
        # one old command (10 min ago, backlog) + one fresh (just now)
        async def fake_updates(token, offset, session):
            return [_update(1, "/harvest 0xOLD", now - 600),
                    _update(2, "/harvest 0xNEW", now - 5)], 3
        tgtools.get_updates = fake_updates
        try:
            await sc._poll_commands()
            self.assertEqual(sc._handled, ["/harvest 0xNEW"])   # only the fresh one
            # offset still advanced past BOTH so they're never re-fetched
            self.assertEqual(sc.storage.kv_get("cmd_offset"), "3")
        finally:
            tgtools.get_updates = orig
            sc.storage.close()

    async def test_disabled_when_zero(self):
        sc = self._sc()
        sc.cfg.runtime.command_max_age_seconds = 0    # process everything
        now = time.time()
        orig = tgtools.get_updates
        async def fake_updates(token, offset, session):
            return [_update(1, "/diag", now - 9999)], 2
        tgtools.get_updates = fake_updates
        try:
            await sc._poll_commands()
            self.assertEqual(sc._handled, ["/diag"])
        finally:
            tgtools.get_updates = orig
            sc.storage.close()


if __name__ == "__main__":
    unittest.main()
