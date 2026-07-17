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


class TestFlapEnrich(unittest.IsolatedAsyncioTestCase):
    def _sc(self):
        cfg = Config.load("rhl2_scanner/config/robinhood.example.yaml")
        cfg.runtime.db_path = ":memory:"
        cfg.chain.rpc_url = "http://rpc"
        return Scanner(cfg)

    def test_learns_eth_usd_from_graduated_pair(self):
        from rhl2_scanner.models import TokenSnapshot
        sc = self._sc()
        try:
            # graduated RH pair: priceUsd/priceNative == ETH/USD
            snap = TokenSnapshot(chain="robinhood", pair_address="0xp", token_address="0xt",
                                 price_usd=0.007, price_native=0.000002)  # 3500 ETH/USD
            sc._note_eth_usd(snap)
            self.assertAlmostEqual(sc._eth_usd, 3500.0, places=1)
            self.assertAlmostEqual(sc._eth_usd_rate(), 3500.0, places=1)
        finally:
            sc.storage.close()

    def test_rejects_garbage_eth_usd(self):
        from rhl2_scanner.models import TokenSnapshot
        sc = self._sc()
        try:
            snap = TokenSnapshot(chain="robinhood", pair_address="0xp", token_address="0xt",
                                 price_usd=1.0, price_native=999.0)  # rate ~0.001 → rejected
            sc._note_eth_usd(snap)
            self.assertIsNone(sc._eth_usd)
        finally:
            sc.storage.close()

    async def test_prices_curve_token_in_usd(self):
        from rhl2_scanner.models import TokenSnapshot
        sc = self._sc()
        sc._eth_usd = 3500.0
        # real $... numbers: price raw 9554072748, circ 635,493,062, reserve 2.586183 ETH
        body = _tuple(1, 2586183000000000000, 635_493_062 * E18, 9554072748, 5, 7,
                      800_000_000 * E18)

        class FakeSession:
            def post(self, url, json):
                return _Resp({"result": body})

        sc._session = FakeSession()  # type: ignore
        try:
            snap = TokenSnapshot(chain="robinhood", pair_address="",
                                 token_address="0x4412fec147e1a2698a9b56dcbdea39ab9dd17777")
            await sc._enrich_flap_curve(snap)
            self.assertEqual(snap.launchpad, "flap")
            self.assertAlmostEqual(snap.curve_progress_pct, 79.4, places=1)
            # mcap = 6.0715 ETH * 3500 ≈ $21.25k (a real sub-$100k pre-grad gem)
            self.assertTrue(15000 < snap.market_cap_usd < 30000, snap.market_cap_usd)
            # liquidity = reserve 2.586 ETH * 3500 ≈ $9k
            self.assertTrue(7000 < snap.liquidity_usd < 12000, snap.liquidity_usd)
            self.assertGreater(snap.price_usd, 0)
        finally:
            sc.storage.close()

    async def test_skips_when_already_priced(self):
        from rhl2_scanner.models import TokenSnapshot
        sc = self._sc()
        sc._eth_usd = 3500.0
        called = {"n": 0}

        class FakeSession:
            def post(self, url, json):
                called["n"] += 1
                return _Resp({"result": "0x"})

        sc._session = FakeSession()  # type: ignore
        try:
            snap = TokenSnapshot(chain="robinhood", pair_address="0xp",
                                 token_address="0xt", market_cap_usd=50000.0)
            await sc._enrich_flap_curve(snap)
            self.assertEqual(called["n"], 0)   # never touched the Portal
            self.assertEqual(snap.market_cap_usd, 50000.0)
        finally:
            sc.storage.close()

    async def test_progress_only_without_eth_usd(self):
        from rhl2_scanner.models import TokenSnapshot
        sc = self._sc()
        sc._eth_usd = None
        import os
        os.environ.pop("RHL2_ETH_USD", None)
        body = _tuple(1, 2586183000000000000, 635_493_062 * E18, 9554072748, 5, 7,
                      800_000_000 * E18)

        class FakeSession:
            def post(self, url, json):
                return _Resp({"result": body})

        sc._session = FakeSession()  # type: ignore
        try:
            snap = TokenSnapshot(chain="robinhood", pair_address="", token_address="0xt")
            await sc._enrich_flap_curve(snap)
            self.assertEqual(snap.launchpad, "flap")           # origin still set
            self.assertAlmostEqual(snap.curve_progress_pct, 79.4, places=1)  # progress still set
            self.assertIsNone(snap.market_cap_usd)             # no USD without a rate
        finally:
            sc.storage.close()


if __name__ == "__main__":
    unittest.main()
