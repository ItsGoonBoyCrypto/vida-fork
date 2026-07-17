"""Bags launchpad: getTokenState decode, registry discovery, curve pricing."""

from __future__ import annotations

import unittest

from rhl2_scanner.config import Config
from rhl2_scanner.scanner import Scanner
from rhl2_scanner.sources.bags import (
    BagsClient,
    decode_address_array,
    decode_token_state,
)

LENS = "0x1111111111111111111111111111111111111111"
FACTORY = "0x2222222222222222222222222222222222222222"
TOKEN = "0x56a98db16cf501b686c14ba00a5dec02e87083fa"
E18 = 10 ** 18


def _w(n: int) -> str:
    return f"{n:064x}"


def _addr_word(a: str) -> str:
    return "0" * 24 + a[2:]


def _state(exists=1, migrated=0, price=10 ** 12, real_q=2 * E18, real_t=300_000 * E18,
           virt_t=800_000 * E18, prog=62, thr=5 * E18, raised=3 * E18) -> str:
    return "0x" + "".join([
        _w(exists), _w(migrated), _addr_word("0x" + "a" * 40), _addr_word("0x" + "b" * 40),
        _w(0),                       # poolId
        _w(thr), _w(real_q), _w(real_t), _w(virt_t),
        _w(4 * E18),                 # virtualQuoteReserves
        _w(price), _w(prog), _w(raised),
    ])


class _Resp:
    def __init__(self, body, status=200):
        self._b, self.status = body, status
    async def json(self):
        return self._b
    async def __aenter__(self):
        return self
    async def __aexit__(self, *a):
        return False


class TestDecode(unittest.TestCase):
    def test_token_state_fields(self):
        st = decode_token_state(_state(prog=62, price=10 ** 12))
        self.assertTrue(st["exists"])
        self.assertFalse(st["migrated"])
        self.assertEqual(st["bondingProgressPct"], 62)
        self.assertEqual(st["priceQuotePerToken"], 10 ** 12)
        self.assertEqual(st["realQuoteReserves"], 2 * E18)

    def test_token_state_short_returns_none(self):
        self.assertIsNone(decode_token_state("0x"))
        self.assertIsNone(decode_token_state("0x" + "0" * 64))  # too few words

    def test_address_array(self):
        a = "0x" + "c" * 40
        b = "0x" + "d" * 40
        enc = "0x" + _w(0x20) + _w(2) + _addr_word(a) + _addr_word(b)
        self.assertEqual([x.lower() for x in decode_address_array(enc)], [a, b])
        self.assertEqual(decode_address_array("0x"), [])


class TestClient(unittest.IsolatedAsyncioTestCase):
    def _cfg(self):
        cfg = Config()
        cfg.chain.rpc_url = "http://rpc"
        return cfg

    async def test_token_state_call(self):
        sel = BagsClient(self._cfg(), None, lens=LENS).lens  # noqa
        body = _state(prog=70)

        class S:
            def post(self, url, json):
                assert json["params"][0]["to"] == LENS
                return _Resp({"result": body})

        c = BagsClient(self._cfg(), S(), lens=LENS)
        st = await c.token_state(TOKEN, delay=0)
        self.assertTrue(st["exists"])
        self.assertEqual(st["bondingProgressPct"], 70)

    async def test_newest_tokens_tail(self):
        a = "0x" + "c" * 40
        toks_enc = "0x" + _w(0x20) + _w(1) + _addr_word(a)

        class S:
            def post(self, url, json):
                data = json["params"][0]["data"]
                if data[2:10] == __import__("rhl2_scanner.sources.bags", fromlist=["_SEL_LEN"])._SEL_LEN:
                    return _Resp({"result": _w(5)})     # allTokensLength = 5
                return _Resp({"result": toks_enc})       # getTokens → [a]

        c = BagsClient(self._cfg(), S(), lens=LENS, factory=FACTORY)
        toks, total = await c.newest_tokens(limit=10, known_total=4, delay=0)
        self.assertEqual(total, 5)
        self.assertEqual([t.lower() for t in toks], [a])


class TestScannerIntegration(unittest.IsolatedAsyncioTestCase):
    def _sc(self):
        cfg = Config.load("rhl2_scanner/config/robinhood.example.yaml")
        cfg.runtime.db_path = ":memory:"
        cfg.chain.rpc_url = "http://rpc"
        cfg._launchpad("bags")["manager"] = LENS
        cfg._launchpad("bags")["factory"] = FACTORY
        return Scanner(cfg)

    async def test_enrich_prices_bags_curve_token(self):
        from rhl2_scanner.models import TokenSnapshot
        sc = self._sc()
        sc._eth_usd = 3500.0
        body = _state(price=10 ** 12, real_q=2 * E18, real_t=300_000 * E18,
                      virt_t=800_000 * E18, prog=62)

        class S:
            def post(self, url, json):
                return _Resp({"result": body})

        sc._session = S()  # type: ignore
        try:
            snap = TokenSnapshot(chain="robinhood", pair_address="", token_address=TOKEN)
            await sc._enrich_bags_curve(snap)
            self.assertEqual(snap.launchpad, "bags")
            self.assertAlmostEqual(snap.curve_progress_pct, 62.0)
            self.assertGreater(snap.market_cap_usd, 0)
            self.assertGreater(snap.liquidity_usd, 0)   # realQuoteReserves 2 ETH * 3500
        finally:
            sc.storage.close()

    async def test_bags_discovery_cold_start_sets_cursor(self):
        sc = self._sc()

        class S:
            def post(self, url, json):
                import rhl2_scanner.sources.bags as b
                if json["params"][0]["data"][2:10] == b._SEL_LEN:
                    return _Resp({"result": _w(3)})
                return _Resp({"result": "0x" + _w(0x20) + _w(0)})

        sc._session = S()  # type: ignore
        try:
            stubs = await sc._poll_bags()      # cold start: set cursor, emit nothing
            self.assertEqual(stubs, [])
            self.assertEqual(sc._bags_last_total, 3)
        finally:
            sc.storage.close()

    async def test_bagsstate_report_not_configured(self):
        sc = Scanner(Config.load("rhl2_scanner/config/robinhood.example.yaml"))
        sc.cfg.runtime.db_path = ":memory:"

        class S:
            def post(self, url, json):
                return _Resp({"result": "0x"})
        sc._session = S()  # type: ignore
        try:
            st = await sc.bags_state(TOKEN)   # no lens configured
            self.assertFalse(st["ok"])
        finally:
            sc.storage.close()


if __name__ == "__main__":
    unittest.main()
