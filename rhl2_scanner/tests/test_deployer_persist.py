"""Tests for the deployer/flap-manager finder + DB dir creation."""

from __future__ import annotations

import os
import tempfile
import unittest

from rhl2_scanner.config import Config
from rhl2_scanner.scanner import Scanner
from rhl2_scanner.storage import Storage


class TestStorageDirCreation(unittest.TestCase):
    def test_creates_missing_parent(self):
        d = tempfile.mkdtemp()
        nested = os.path.join(d, "data", "sub")   # does not exist yet
        path = os.path.join(nested, "scanner.db")
        s = Storage(path)                          # should mkdir -p the parent
        try:
            self.assertTrue(os.path.isdir(nested))
            self.assertTrue(os.path.exists(path))
        finally:
            s.close()

    def test_memory_ok(self):
        s = Storage(":memory:")   # must not try to mkdir
        s.close()


class TestFindDeployer(unittest.IsolatedAsyncioTestCase):
    def _scanner(self) -> Scanner:
        cfg = Config()
        cfg.runtime.db_path = ":memory:"
        cfg.chain.explorer_api_url = "https://x.blockscout.com/api"
        return Scanner(cfg)

    async def test_reports_creator_and_flap_hint(self):
        import aiohttp
        sc = self._scanner()

        class FakeResp:
            status = 200
            async def json(self):
                return {"creator_address_hash": "0xMANAGER",
                        "creation_tx_hash": "0xTX",
                        "token": {"symbol": "CAT"}}
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False

        class FakeSession:
            def get(self, url): return FakeResp()

        sc._session = FakeSession()  # type: ignore
        try:
            out = await sc.find_deployer("0x" + "a" * 40)
            self.assertIn("created by: 0xMANAGER", out)
            self.assertIn("RHL2_FLAP_MANAGER=0xMANAGER", out)
            self.assertIn("$CAT", out)
        finally:
            sc.storage.close()

    async def test_recognises_known_launchpad(self):
        sc = self._scanner()
        sc.cfg._launchpad("flap")["manager"] = "0xKNOWN"

        class FakeResp:
            status = 200
            async def json(self):
                return {"creator_address_hash": "0xknown", "token": {}}
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False

        class FakeSession:
            def get(self, url): return FakeResp()

        sc._session = FakeSession()  # type: ignore
        try:
            out = await sc.find_deployer("0x" + "b" * 40)
            self.assertIn("'flap' launchpad", out)
        finally:
            sc.storage.close()


if __name__ == "__main__":
    unittest.main()
