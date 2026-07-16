"""Tests for the /curveprobe on-chain price prober."""

from __future__ import annotations

import unittest

from rhl2_scanner.config import Config
from rhl2_scanner.scanner import Scanner

TOKEN = "0xda4109d84a022b36b88273963f506ce02ef942ae"


class TestCurveProbe(unittest.IsolatedAsyncioTestCase):
    def _sc(self):
        cfg = Config.load("rhl2_scanner/config/robinhood.example.yaml")
        cfg.runtime.db_path = ":memory:"
        cfg.chain.rpc_url = "http://rpc"
        return Scanner(cfg)

    async def test_reports_getter_that_returns_data(self):
        sc = self._sc()
        from rhl2_scanner.keccak import keccak256
        price_sel = keccak256(b"price(address)").hex()[:8]

        class FakeResp:
            def __init__(self, body): self._b = body
            status = 200
            async def json(self): return {"result": self._b}
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False

        class FakeSession:
            def post(self, url, json):
                if json["method"] == "eth_getCode":
                    return FakeResp("0x60016002")     # has code (is a contract)
                data = json["params"][0]["data"]
                # only price(address) returns a non-zero value
                if data[2:10] == price_sel:
                    return FakeResp("0x" + "0" * 63 + "5")
                return FakeResp("0x")

        sc._session = FakeSession()  # type: ignore
        try:
            out = await sc.curveprobe(TOKEN, delay=0)
            self.assertIn("CURVE PROBE", out)
            self.assertIn("Portal.price(address)", out)
            self.assertIn("returned data", out)
        finally:
            sc.storage.close()

    async def test_no_portal(self):
        cfg = Config()   # no flap manager
        cfg.runtime.db_path = ":memory:"
        sc = Scanner(cfg)
        sc._session = object()  # not used
        try:
            out = await sc.curveprobe(TOKEN)
            self.assertIn("no flap Portal", out)
        finally:
            sc.storage.close()


if __name__ == "__main__":
    unittest.main()
