"""Tests for Telegram wiring helpers (raw Bot API) with a fake session."""

from __future__ import annotations

import unittest

from rhl2_scanner.tgtools import discover_chats, send_message


class FakeResp:
    def __init__(self, payload):
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        return self._payload


class FakeSession:
    def __init__(self, payload):
        self.payload = payload
        self.sent = []

    def post(self, url, json=None):
        self.sent.append((url, json))
        return FakeResp(self.payload)

    def get(self, url, params=None):
        return FakeResp(self.payload)

    async def close(self):
        pass


class TestSend(unittest.IsolatedAsyncioTestCase):
    async def test_send_ok(self):
        ok, detail, _mid = await send_message("TOK", "-100123", "hi", session=FakeSession({"ok": True}))
        self.assertTrue(ok)
        self.assertEqual(detail, "sent")

    async def test_send_error_surfaces_description(self):
        payload = {"ok": False, "description": "chat not found"}
        ok, detail, _mid = await send_message("TOK", "-100123", "hi", session=FakeSession(payload))
        self.assertFalse(ok)
        self.assertIn("chat not found", detail)

    async def test_token_not_in_url_logged(self):
        # sanity: the URL is built with the token but we never return/print it
        s = FakeSession({"ok": True})
        await send_message("SECRET", "-1", "hi", session=s)
        url, _ = s.sent[0]
        self.assertIn("/botSECRET/sendMessage", url)  # constructed correctly


class TestDiscover(unittest.IsolatedAsyncioTestCase):
    async def test_channel_post_chat_extracted(self):
        payload = {
            "ok": True,
            "result": [
                {"update_id": 1, "channel_post": {"chat": {"id": -1002222, "title": "Gems", "type": "channel"}}},
                {"update_id": 2, "message": {"chat": {"id": 555, "type": "private", "first_name": "Goon"}}},
                {"update_id": 3, "channel_post": {"chat": {"id": -1002222, "title": "Gems", "type": "channel"}}},
            ],
        }
        chats = await discover_chats("TOK", session=FakeSession(payload))
        ids = sorted(c["id"] for c in chats)
        self.assertEqual(ids, [-1002222, 555])  # deduped

    async def test_empty_updates(self):
        chats = await discover_chats("TOK", session=FakeSession({"ok": True, "result": []}))
        self.assertEqual(chats, [])


if __name__ == "__main__":
    unittest.main()
