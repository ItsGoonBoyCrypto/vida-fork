"""Tests for /help and the /group runtime sybil-grouping command."""

from __future__ import annotations

import unittest

from rhl2_scanner.config import Config
from rhl2_scanner.scanner import Scanner

WA = "0x" + "a" * 40
WB = "0x" + "b" * 40
WC = "0x" + "c" * 40


class _Base(unittest.IsolatedAsyncioTestCase):
    def _sc(self) -> Scanner:
        cfg = Config()
        cfg.runtime.db_path = ":memory:"
        sc = Scanner(cfg)
        sc._sent = []
        async def fake_send(html, reply_to=None):
            sc._sent.append(html)
        sc._send_html = fake_send  # type: ignore
        return sc


class TestHelp(_Base):
    async def test_help_lists_key_commands(self):
        sc = self._sc()
        try:
            await sc._handle_command("/help")
            self.assertEqual(len(sc._sent), 1)
            body = sc._sent[0]
            for c in ("/diag", "/zero", "/block", "/smart", "/group",
                      "/calibrate", "/deployer", "/perf", "/wallet"):
                self.assertIn(c, body)
        finally:
            sc.storage.close()

    async def test_start_alias(self):
        sc = self._sc()
        try:
            await sc._handle_command("/start")
            self.assertIn("commands", sc._sent[0].lower())
        finally:
            sc.storage.close()


class TestGroupCommand(_Base):
    async def test_group_and_entity(self):
        sc = self._sc()
        try:
            await sc._handle_command(f"/group {WA} {WB}")
            self.assertEqual(sc._entity_of(WA), sc._entity_of(WB))   # same entity
            self.assertNotEqual(sc._entity_of(WA), sc._entity_of(WC))  # WC distinct
            self.assertTrue(sc._entity_of(WA).startswith("grp:"))
        finally:
            sc.storage.close()

    async def test_group_extends_existing(self):
        sc = self._sc()
        try:
            await sc._handle_command(f"/group {WA} {WB}")
            g = sc._entity_of(WA)
            await sc._handle_command(f"/group {WA} {WC}")   # add WC to WA's group
            self.assertEqual(sc._entity_of(WC), g)
        finally:
            sc.storage.close()

    async def test_ungroup(self):
        sc = self._sc()
        try:
            await sc._handle_command(f"/group {WA} {WB}")
            await sc._handle_command(f"/ungroup {WA}")
            self.assertNotEqual(sc._entity_of(WA), sc._entity_of(WB))
        finally:
            sc.storage.close()

    async def test_group_needs_two(self):
        sc = self._sc()
        try:
            await sc._handle_command(f"/group {WA}")
            self.assertIn("Usage", sc._sent[0])
        finally:
            sc.storage.close()

    async def test_groups_list_includes_config(self):
        sc = self._sc()
        sc.cfg.wallet_watch.wallet_groups = {WA: "CfgGroup", WB: "CfgGroup"}
        try:
            await sc._handle_command("/groups")
            self.assertIn("CfgGroup", sc._sent[0])
        finally:
            sc.storage.close()


if __name__ == "__main__":
    unittest.main()
