"""BNB smart-money via the Etherscan V2 unified API (BscScan) fallback."""

from __future__ import annotations

import os
import unittest

from memelab.chains.registry import REGISTRY
from memelab.models import Chain
from memelab.smartmoney import SmartMoney


class TestEtherscanFallback(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._key = os.environ.get("ETHERSCAN_API_KEY")
        os.environ["ETHERSCAN_API_KEY"] = "TESTKEY"

    def tearDown(self):
        if self._key is None:
            os.environ.pop("ETHERSCAN_API_KEY", None)
        else:
            os.environ["ETHERSCAN_API_KEY"] = self._key

    def test_bnb_has_etherscan_chain_id(self):
        self.assertEqual(REGISTRY[Chain.BNB].etherscan_chain_id, "56")
        self.assertEqual(REGISTRY[Chain.BNB].explorer_api_url, "")   # no blockscout

    async def test_receivers_use_etherscan_when_no_blockscout(self):
        s = None
        sm = SmartMoney.__new__(SmartMoney)   # skip __init__ (needs a store)
        sm._session = object()
        calls = {}

        async def fake_get(url, params):
            calls["url"] = url
            calls["params"] = params
            return {"status": "1", "result": [
                {"to": "0xAAA"}, {"to": "0xBBB"}, {"to": "0xAAA"}]}
        sm._get = fake_get  # type: ignore

        recv = await sm._evm_receivers(Chain.BNB, "0xtoken", newest_first=False)
        self.assertEqual(recv, ["0xaaa", "0xbbb"])       # deduped, lowercased
        self.assertIn("api.etherscan.io/v2", calls["url"])
        self.assertEqual(calls["params"]["chainid"], "56")
        self.assertEqual(calls["params"]["sort"], "asc")  # early buyers

    async def test_no_key_returns_empty(self):
        os.environ.pop("ETHERSCAN_API_KEY", None)
        os.environ.pop("BSCSCAN_API_KEY", None)
        sm = SmartMoney.__new__(SmartMoney)
        sm._session = object()
        recv = await sm._etherscan_receivers(Chain.BNB, "0xt", newest_first=False)
        self.assertEqual(recv, [])

    async def test_creator_via_etherscan(self):
        sm = SmartMoney.__new__(SmartMoney)
        sm._session = object()

        async def fake_get(url, params):
            return {"result": [{"contractCreator": "0xDEAD", "txHash": "0x1"}]}
        sm._get = fake_get  # type: ignore
        creator = await sm._creator_of(Chain.BNB, "0xtoken")
        self.assertEqual(creator, "0xdead")


if __name__ == "__main__":
    unittest.main()
