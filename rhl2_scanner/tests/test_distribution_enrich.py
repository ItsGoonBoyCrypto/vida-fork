"""Regression: Blockscout-v2 distribution enrichment completes end-to-end.

An undefined helper (_to_int) previously made _enrich_distribution_v2 raise
NameError on every SUCCESSFUL holders fetch — silently killing holder_count /
top10 / top1 forever (the concentration gates then never enforced). This test
drives the real code path with a mocked Blockscout payload so any regression in
the parsing chain fails loudly.
"""

from __future__ import annotations

import unittest

from rhl2_scanner.config import Config
from rhl2_scanner.models import TokenSnapshot
from rhl2_scanner.sources.chain import EvmChainClient, _to_int


class _Resp:
    def __init__(self, payload):
        self._p, self.status = payload, 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        return self._p


class _BlockscoutSession:
    """Routes /tokens/{addr} and /tokens/{addr}/holders to canned payloads."""

    def __init__(self, info, holders):
        self.info, self.holders = info, holders

    def get(self, url, params=None):
        if url.endswith("/holders"):
            return _Resp(self.holders)
        return _Resp(self.info)

    async def close(self):
        pass


def _holder(addr: str, value: str) -> dict:
    return {"address": {"hash": addr}, "value": value}


class TestDistributionV2(unittest.IsolatedAsyncioTestCase):
    async def _run(self, info, holders):
        cfg = Config()
        cfg.chain.explorer_api_url = "https://exp/api"
        client = EvmChainClient(cfg, session=_BlockscoutSession(info, holders))
        snap = TokenSnapshot(chain="robinhood", pair_address="0x" + "p" * 40,
                             token_address="0x" + "a" * 40)
        ok = await client._enrich_distribution_v2(snap)
        return ok, snap

    async def test_holders_and_concentration_populated(self):
        info = {"total_supply": "1000", "holders": "37"}
        holders = {"items": [
            _holder("0x" + "1" * 40, "600"),     # top1 = 60%
            _holder("0x" + "2" * 40, "300"),
            _holder("0x" + "p" * 40, "100"),     # the pair — excluded
        ]}
        ok, snap = await self._run(info, holders)
        self.assertTrue(ok)
        self.assertEqual(snap.holder_count, 37)          # true count from token info
        self.assertAlmostEqual(snap.top1_supply_pct, 60.0, places=1)
        self.assertAlmostEqual(snap.top10_supply_pct, 90.0, places=1)

    async def test_missing_holder_count_falls_back_to_page(self):
        info = {"total_supply": "1000"}                   # no holders field
        holders = {"items": [_holder("0x" + "1" * 40, "500"),
                             _holder("0x" + "2" * 40, "500")]}
        ok, snap = await self._run(info, holders)
        self.assertTrue(ok)
        self.assertEqual(snap.holder_count, 2)

    def test_to_int_handles_junk(self):
        self.assertEqual(_to_int("37"), 37)
        self.assertEqual(_to_int("37.0"), 37)
        self.assertEqual(_to_int(12), 12)
        self.assertIsNone(_to_int(None))
        self.assertIsNone(_to_int("n/a"))


if __name__ == "__main__":
    unittest.main()
