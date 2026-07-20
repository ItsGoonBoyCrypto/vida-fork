"""Telegram wiring helpers — raw Bot API over HTTPS (no python-telegram-bot).

Used by the `tg-test` and `tg-chats` CLI commands to (a) confirm a bot token +
chat id actually deliver a message, and (b) discover the numeric chat id of a
channel/group from a private invite link (which the API can't derive directly —
the bot must join, then the id shows up in getUpdates).

Kept dependency-light (aiohttp only) so it works before the full bot stack is
installed. Never logs the token.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import aiohttp

log = logging.getLogger("rhl2.tgtools")

_API = "https://api.telegram.org"


async def _call(token: str, method: str, params: Optional[dict], session: aiohttp.ClientSession,
                post: bool = False) -> Optional[dict]:
    url = f"{_API}/bot{token}/{method}"
    try:
        if post:
            async with session.post(url, json=params or {}) as resp:
                return await resp.json()
        async with session.get(url, params=params or {}) as resp:
            return await resp.json()
    except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
        log.warning("telegram %s failed: %s", method, exc)
        return None


async def send_message(token: str, chat_id: str, text: str,
                       session: Optional[aiohttp.ClientSession] = None,
                       reply_to: Optional[int] = None,
                       reply_markup: Optional[dict] = None) -> tuple[bool, str, Optional[int]]:
    """Send a message. Returns (ok, detail, message_id). HTML, no link preview.

    ``reply_to`` threads this message as a reply to an earlier one (used to hang
    milestone/dump follow-ups under the original alert). ``reply_markup`` attaches
    an inline keyboard (e.g. the trader's buy buttons).
    """
    own = session is None
    session = session or aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
    try:
        payload: dict = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                         "disable_web_page_preview": True}
        if reply_to:
            payload["reply_parameters"] = {"message_id": reply_to,
                                           "allow_sending_without_reply": True}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        data = await _call(token, "sendMessage", payload, session, post=True)
        if not data:
            return False, "no response (network/egress blocked?)", None
        if data.get("ok"):
            mid = (data.get("result") or {}).get("message_id")
            return True, "sent", mid
        return False, f"telegram error: {data.get('description', 'unknown')}", None
    finally:
        if own:
            await session.close()


async def answer_callback(token: str, callback_id: str, text: str = "",
                          session: Optional[aiohttp.ClientSession] = None,
                          show_alert: bool = False) -> None:
    """Ack an inline-button tap (stops the client's loading spinner).

    ``show_alert`` makes ``text`` pop as a modal on the tapper's screen — used to
    surface the dry-run result immediately, without relying on a channel message.
    """
    own = session is None
    session = session or aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
    try:
        payload: dict = {"callback_query_id": callback_id}
        if text:
            payload["text"] = text[:200]
        if show_alert:
            payload["show_alert"] = True
        await _call(token, "answerCallbackQuery", payload, session, post=True)
    except Exception:  # noqa: BLE001
        pass
    finally:
        if own:
            await session.close()


async def send_document(token: str, chat_id: str, file_path: str, caption: str = "",
                        session: Optional[aiohttp.ClientSession] = None) -> bool:
    """Upload a file to a chat (used to ship DB backups off-volume weekly)."""
    own = session is None
    session = session or aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120))
    try:
        import os
        form = aiohttp.FormData()
        form.add_field("chat_id", str(chat_id))
        if caption:
            form.add_field("caption", caption[:1000])
        with open(file_path, "rb") as fh:
            form.add_field("document", fh.read(),
                           filename=os.path.basename(file_path),
                           content_type="application/octet-stream")
        url = f"{_API}/bot{token}/sendDocument"
        async with session.post(url, data=form) as resp:
            data = await resp.json()
            return bool(data and data.get("ok"))
    except (aiohttp.ClientError, TimeoutError, ValueError, OSError) as exc:
        log.warning("sendDocument failed: %s", exc)
        return False
    finally:
        if own:
            await session.close()


async def discover_chats(token: str,
                         session: Optional[aiohttp.ClientSession] = None) -> list[dict]:
    """Return distinct chats seen in recent updates (id, title, type).

    For a channel the bot must be an admin and a message must have been posted
    after it joined; for a group, send any message mentioning the bot.
    """
    own = session is None
    session = session or aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
    try:
        data = await _call(token, "getUpdates", {"timeout": 0, "allowed_updates": []}, session)
        if not data or not data.get("ok"):
            return []
        chats: dict[Any, dict] = {}
        for upd in data.get("result", []):
            for key in ("message", "channel_post", "edited_channel_post", "my_chat_member"):
                obj = upd.get(key)
                if isinstance(obj, dict) and isinstance(obj.get("chat"), dict):
                    c = obj["chat"]
                    chats[c.get("id")] = {
                        "id": c.get("id"),
                        "title": c.get("title") or c.get("username") or c.get("first_name"),
                        "type": c.get("type"),
                    }
        return list(chats.values())
    finally:
        if own:
            await session.close()


async def get_updates(token: str, offset: Optional[int] = None,
                      session: Optional[aiohttp.ClientSession] = None):
    """Return (updates, next_offset). Used to receive /commands from the channel."""
    own = session is None
    session = session or aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
    try:
        # Explicitly request callback_query so button taps are delivered — the
        # allowed_updates setting is sticky server-side and a prior call could
        # otherwise have narrowed it. We name every type we act on.
        params: dict = {
            "timeout": 0,
            "allowed_updates": [
                "message", "edited_message",
                "channel_post", "edited_channel_post",
                "callback_query",
            ],
        }
        if offset is not None:
            params["offset"] = offset
        data = await _call(token, "getUpdates", params, session)
        if not data or not data.get("ok"):
            if data and data.get("error_code") == 409:
                log.warning(
                    "getUpdates 409 Conflict — ANOTHER instance is polling this bot "
                    "token. Commands (/smart, /diag, …) are dropped until you delete "
                    "the duplicate service so only ONE process polls this token.")
            elif data:
                log.warning("getUpdates not ok: %s", data.get("description") or data)
            return [], offset
        updates = data.get("result", [])
        next_offset = (updates[-1]["update_id"] + 1) if updates else offset
        return updates, next_offset
    finally:
        if own:
            await session.close()


async def get_bot_username(token: str,
                           session: Optional[aiohttp.ClientSession] = None) -> Optional[str]:
    own = session is None
    session = session or aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
    try:
        data = await _call(token, "getMe", {}, session)
        if data and data.get("ok"):
            return data["result"].get("username")
        return None
    finally:
        if own:
            await session.close()
