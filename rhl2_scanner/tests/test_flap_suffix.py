"""Tests for flap vanity-suffix token extraction (8888 / 7777)."""

from __future__ import annotations

import unittest

from rhl2_scanner.config import Config
from rhl2_scanner.sources.launchpad_curve import (
    LaunchpadCurveListener,
    _tokens_by_suffix,
)

# Real addresses pulled from the flap Portal logs.
TOK_8888 = "0x50b6c0b23f5509c5467a76f8a5d2a4ef46be8888"
TOK_7777 = "0x824e03e49e51f34b151b386850ea2c0c19b47777"
TRADER   = "0xc0fab674ff7ddf8b891495ba9975b0fe1dcac735"
UINT_LEAK = "0x000000000000000001d88b1be65f3294c4015c57"  # a value, not a token


def _entry(*addrs, topics=None):
    data = "0x" + "".join("0" * 24 + a[2:] for a in addrs)
    return {"topics": topics or ["0xevent"], "data": data}


class TestSuffixExtraction(unittest.TestCase):
    def test_picks_vanity_tokens(self):
        s = ["8888", "7777"]
        self.assertEqual(_tokens_by_suffix(_entry(TOK_8888), s), [TOK_8888])
        self.assertEqual(_tokens_by_suffix(_entry(TOK_7777), s), [TOK_7777])

    def test_ignores_traders_and_uints(self):
        s = ["8888", "7777"]
        self.assertEqual(_tokens_by_suffix(_entry(TRADER), s), [])
        self.assertEqual(_tokens_by_suffix(_entry(UINT_LEAK), s), [])

    def test_mixed_event_isolates_token(self):
        s = ["8888", "7777"]
        # a trade event: token + trader + amount
        got = _tokens_by_suffix(_entry(TOK_8888, TRADER, UINT_LEAK), s)
        self.assertEqual(got, [TOK_8888])

    def test_no_suffixes_returns_nothing(self):
        self.assertEqual(_tokens_by_suffix(_entry(TOK_8888), []), [])


class TestListenerSuffixMode(unittest.IsolatedAsyncioTestCase):
    async def test_poll_emits_flap_tokens_once(self):
        cfg = Config.load("rhl2_scanner/config/robinhood.example.yaml")
        cfg.chain.rpc_url = "http://rpc"
        cfg.chain.curve_confirm_budget = 0   # suffix-only path (non-vanity confirm is tested separately)
        lis = LaunchpadCurveListener(cfg)

        # fake chain: head at 100, logs referencing two tokens + noise
        async def fake_block():
            return 100
        logs = [
            _entry(TOK_8888, TRADER),
            _entry(TOK_7777, UINT_LEAK),
            _entry(TRADER),              # no token -> nothing
        ]
        async def fake_get_logs(address, frm, to, topics):
            return logs
        lis._block_number = fake_block          # type: ignore
        lis._get_logs = fake_get_logs           # type: ignore
        lis._session = object()                 # bypass the None guard

        snaps = await lis.poll_new_launches()
        tokens = sorted(s.token_address.lower() for s in snaps)
        self.assertEqual(tokens, sorted([TOK_8888, TOK_7777]))
        self.assertTrue(all(s.launchpad == "flap" for s in snaps))
        # second poll (same run) doesn't re-emit already-seen tokens
        lis._last_block = 0
        again = await lis.poll_new_launches()
        self.assertEqual(again, [])


if __name__ == "__main__":
    unittest.main()
