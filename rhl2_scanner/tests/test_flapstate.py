"""Tests for /flapstate — flap Portal getTokenV2 curve-state reader."""

from __future__ import annotations

import unittest

from rhl2_scanner.config import Config
from rhl2_scanner.scanner import Scanner
from rhl2_scanner.keccak import keccak256

TOKEN = "0x2999c66279f0b9504e49d1d03a579a1db3797777"
E18 = 10 ** 18


def _word(n: int) -> str:
    return f"{n:064x}"


def _tuple(status, reserve, circ, price, version, r, thresh) -> str:
    return "0x" + "".join(_word(x) for x in
                          (status, reserve, circ, price, version, r, thresh))


class _Resp:
    def __init__(self, body, status=200):
        self._b, self.status = body, status
    async def json(self):
        return self._b
    async def __aenter__(self):
        return self
    async def __aexit__(self, *a):
        return False


class TestFlapState(unittest.IsolatedAsyncioTestCase):
    def _sc(self):
        cfg = Config.load("rhl2_scanner/config/robinhood.example.yaml")
        cfg.runtime.db_path = ":memory:"
        cfg.chain.rpc_url = "http://rpc"
        return Scanner(cfg)

    async def test_decodes_tradable_curve(self):
        sc = self._sc()
        sel = keccak256(b"getTokenV2(address)").hex()[:8]
        # status=1 Tradable, reserve=2 ETH, circ=500k, price=1e12, r, thresh=1M
        body = _tuple(1, 2 * E18, 500_000 * E18, 10 ** 12, 0, 42, 1_000_000 * E18)

        class FakeSession:
            def post(self, url, json):
                assert json["params"][0]["data"][2:10] == sel  # Portal.getTokenV2
                return _Resp({"result": body})

        sc._session = FakeSession()  # type: ignore
        try:
            st = await sc.flap_state(TOKEN, delay=0)
            self.assertTrue(st["ok"])
            self.assertEqual(st["status_label"], "Tradable")
            self.assertTrue(st["tradable"])
            self.assertFalse(st["graduated"])
            self.assertAlmostEqual(st["progress_pct"], 50.0, places=3)
            self.assertEqual(st["price"], 10 ** 12)
            out = await sc.flapstate(TOKEN, delay=0)
            self.assertIn("FLAP STATE", out)
            self.assertIn("Tradable", out)
            self.assertIn("graduation progress: 50.0%", out)
        finally:
            sc.storage.close()

    async def test_graduated_status(self):
        sc = self._sc()
        body = _tuple(4, 0, 800_000 * E18, 5 * 10 ** 11, 0, 7, 800_000 * E18)

        class FakeSession:
            def post(self, url, json):
                return _Resp({"result": body})

        sc._session = FakeSession()  # type: ignore
        try:
            st = await sc.flap_state(TOKEN, delay=0)
            self.assertTrue(st["graduated"])
            out = await sc.flapstate(TOKEN, delay=0)
            self.assertIn("already on DEX", out)
        finally:
            sc.storage.close()

    async def test_rate_limited(self):
        sc = self._sc()

        class FakeSession:
            def post(self, url, json):
                return _Resp({}, status=429)

        sc._session = FakeSession()  # type: ignore
        try:
            st = await sc.flap_state(TOKEN, delay=0)
            self.assertFalse(st["ok"])
            self.assertEqual(st["error"], "rate-limited")
            out = await sc.flapstate(TOKEN, delay=0)
            self.assertIn("rate-limited", out)
        finally:
            sc.storage.close()


if __name__ == "__main__":
    unittest.main()
