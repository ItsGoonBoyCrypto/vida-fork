"""A broken discovery stream must not mute the bot (commands still poll)."""

from __future__ import annotations

import unittest

from rhl2_scanner.config import Config
from rhl2_scanner.scanner import Scanner


class _Resp:
    def __init__(self, body):
        self._b = body
    async def json(self):
        return self._b
    @property
    def status(self):
        return 200
    async def __aenter__(self):
        return self
    async def __aexit__(self, *a):
        return False


class _Session:
    def get(self, url, *a, **k):
        return _Resp({"pairs": [], "result": []})
    def post(self, url, json=None, *a, **k):
        m = (json or {}).get("method", "")
        if m == "eth_blockNumber":
            return _Resp({"result": hex(1000)})
        return _Resp({"result": "0x" if m == "eth_call" else []})
    async def close(self):
        pass


class _Boom:
    """A discovery stream that always throws."""
    async def poll_new_launches(self):
        raise RuntimeError("simulated discovery failure")
    async def poll_new_pairs(self):
        raise RuntimeError("simulated discovery failure")


class TestCycleResilience(unittest.IsolatedAsyncioTestCase):
    def _sc(self):
        cfg = Config.load("rhl2_scanner/config/robinhood.example.yaml")
        cfg.runtime.db_path = ":memory:"
        cfg.runtime.dry_run = True
        return Scanner(cfg)

    async def test_commands_poll_even_when_discovery_throws(self):
        sc = self._sc()
        sc._session = _Session()  # type: ignore
        polled = {"n": 0}

        async def _fake_poll_commands():
            polled["n"] += 1
        sc._poll_commands = _fake_poll_commands  # type: ignore
        # Force the curve + pool listeners to blow up mid-cycle.
        sc._listener = _Boom()      # type: ignore
        sc._curve_listener = _Boom()  # type: ignore

        # run_once must NOT raise, and commands must have been polled.
        res = await sc.run_once()
        self.assertEqual(polled["n"], 1)      # /diag etc. still serviced
        self.assertIsInstance(res, list)      # cycle completed despite failures
        sc.storage.close()


if __name__ == "__main__":
    unittest.main()
