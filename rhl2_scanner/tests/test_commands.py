"""Tests for the /zero mute command + muted-token storage."""

from __future__ import annotations

import unittest

from rhl2_scanner.config import Config
from rhl2_scanner.scanner import Scanner
from rhl2_scanner.storage import Storage

CA = "0x28c605831A47CA5749608b1fFc19CCa86F717382"


class TestMuteStorage(unittest.TestCase):
    def test_mute_unmute(self):
        s = Storage(":memory:")
        try:
            self.assertFalse(s.is_muted(CA))
            s.mute_token(CA)
            self.assertTrue(s.is_muted(CA))                 # case-insensitive
            self.assertTrue(s.is_muted(CA.lower()))
            self.assertIn(CA.lower(), s.muted_tokens())
            s.unmute_token(CA)
            self.assertFalse(s.is_muted(CA))
        finally:
            s.close()

    def test_kv(self):
        s = Storage(":memory:")
        try:
            self.assertIsNone(s.kv_get("cmd_offset"))
            s.kv_set("cmd_offset", "42")
            self.assertEqual(s.kv_get("cmd_offset"), "42")
            s.kv_set("cmd_offset", "43")               # upsert
            self.assertEqual(s.kv_get("cmd_offset"), "43")
        finally:
            s.close()


class TestZeroCommand(unittest.IsolatedAsyncioTestCase):
    async def test_zero_and_unzero(self):
        cfg = Config()
        cfg.runtime.db_path = ":memory:"
        scanner = Scanner(cfg)
        try:
            await scanner._handle_command(f"/zero {CA}")
            self.assertTrue(scanner.storage.is_muted(CA))
            await scanner._handle_command(f"/unzero {CA}")
            self.assertFalse(scanner.storage.is_muted(CA))
            # bad input doesn't crash / doesn't mute
            await scanner._handle_command("/zero notanaddress")
            self.assertEqual(scanner.storage.muted_tokens(), [])
        finally:
            scanner.storage.close()

    async def test_inspect_bad_ca_no_crash(self):
        cfg = Config()
        cfg.runtime.db_path = ":memory:"
        scanner = Scanner(cfg)
        try:
            await scanner._handle_command("/inspect notacontract")  # should not raise
        finally:
            scanner.storage.close()

    async def test_diag_no_rpc(self):
        import aiohttp
        cfg = Config()
        cfg.runtime.db_path = ":memory:"
        cfg.chain.rpc_url = ""              # no RPC -> early, no network
        scanner = Scanner(cfg)
        scanner._session = aiohttp.ClientSession()
        try:
            report = await scanner.diag()
            self.assertIn("RPC DIAG", report)
            self.assertIn("not set", report)
        finally:
            await scanner._session.close()
            scanner.storage.close()

    async def test_muted_and_at_suffix(self):
        cfg = Config()
        cfg.runtime.db_path = ":memory:"
        scanner = Scanner(cfg)
        try:
            await scanner._handle_command(f"/zero@MyBot {CA}")   # @botname stripped
            self.assertTrue(scanner.storage.is_muted(CA))
        finally:
            scanner.storage.close()


if __name__ == "__main__":
    unittest.main()
