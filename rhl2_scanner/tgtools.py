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
                       session: Optional[aiohttp.ClientSession] = None) -> tuple[bool, str]:
    """Send a message. Returns (ok, detail). HTML parse mode, no link preview."""
    own = session is None
    session = session or aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
    try:
        data = await _call(
            token, "sendMessage",
            {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
             "disable_web_page_preview": True},
            session, post=True,
        )
        if not data:
            return False, "no response (network/egress blocked?)"
        if data.get("ok"):
            return True, "sent"
        return False, f"telegram error: {data.get('description', 'unknown')}"
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
        params: dict = {"timeout": 0, "allowed_updates": '["message","channel_post"]'}
        if offset is not None:
            params["offset"] = offset
        data = await _call(token, "getUpdates", params, session)
        if not data or not data.get("ok"):
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
