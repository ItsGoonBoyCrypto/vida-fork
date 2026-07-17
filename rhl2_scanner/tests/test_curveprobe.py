"""Tests for the /curveprobe on-chain price prober (creator-targeted)."""

from __future__ import annotations

import unittest

from rhl2_scanner.config import Config
from rhl2_scanner.scanner import Scanner
from rhl2_scanner.keccak import keccak256

TOKEN = "0x8f6761371669509bdc457875c500f9bb5bd10aa9"
CREATOR = "0xa5aab3f0c6eeadf30ef1d3eb997108e976351feb"


def _sel(sig: str) -> str:
    return keccak256(sig.encode()).hex()[:8]


class _Resp:
    def __init__(self, body, status=200):
        self._b, self.status = body, status
    async def json(self):
        return self._b
    async def __aenter__(self):
        return self
    async def __aexit__(self, *a):
        return False


class TestCurveProbe(unittest.IsolatedAsyncioTestCase):
    def _sc(self):
        cfg = Config.load("rhl2_scanner/config/robinhood.example.yaml")
        cfg.runtime.db_path = ":memory:"
        cfg.chain.rpc_url = "http://rpc"
        return Scanner(cfg)

    async def test_finds_getter_on_creator(self):
        sc = self._sc()
        reserves_sel = _sel("reserves()")

        class FakeSession:
            def get(self, url):                      # Blockscout creator lookup
                return _Resp({"creator_address_hash": CREATOR})
            def post(self, url, json):               # eth_call
                p = json["params"][0]
                if (p["to"].lower() == CREATOR and p["data"][2:10] == reserves_sel):
                    return _Resp({"result": "0x" + "0" * 63 + "7"})
                return _Resp({"result": "0x"})

        sc._session = FakeSession()  # type: ignore
        try:
            out = await sc.curveprobe(TOKEN, delay=0)
            self.assertIn("CURVE PROBE", out)
            self.assertIn(CREATOR, out)              # creator surfaced
            self.assertIn("creator.reserves()", out) # the getter that returned data
            self.assertIn("returned data", out)
        finally:
            sc.storage.close()

    async def test_rate_limited_summary(self):
        sc = self._sc()

        class FakeSession:
            def get(self, url):
                return _Resp({"creator_address_hash": CREATOR})
            def post(self, url, json):
                return _Resp({}, status=429)         # everything throttled

        sc._session = FakeSession()  # type: ignore
        try:
            out = await sc.curveprobe(TOKEN, delay=0)
            self.assertIn("rate-limited", out)
        finally:
            sc.storage.close()


if __name__ == "__main__":
    unittest.main()
