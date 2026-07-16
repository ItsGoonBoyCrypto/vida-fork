"""Tests for scam-symbol blocking ($ROBINHOOD clones)."""

from __future__ import annotations

import unittest

from rhl2_scanner.config import Config
from rhl2_scanner.filters import is_blocked_symbol, _norm_symbol
from rhl2_scanner.models import TokenSnapshot
from rhl2_scanner.scanner import Scanner


def _tok(symbol="", name="") -> TokenSnapshot:
    return TokenSnapshot(chain="robinhood", pair_address="0xp",
                         token_address="0xt", symbol=symbol, name=name)


class TestNormAndMatch(unittest.TestCase):
    def test_norm_symbol(self):
        self.assertEqual(_norm_symbol("$ROBINHOOD"), "ROBINHOOD")
        self.assertEqual(_norm_symbol("robin-hood"), "ROBINHOOD")
        self.assertEqual(_norm_symbol("  RoBiNhOoD "), "ROBINHOOD")

    def test_blocks_symbol_variants(self):
        blk = ["ROBINHOOD"]
        self.assertTrue(is_blocked_symbol(_tok(symbol="ROBINHOOD"), blk))
        self.assertTrue(is_blocked_symbol(_tok(symbol="$ROBINHOOD"), blk))
        self.assertTrue(is_blocked_symbol(_tok(symbol="robin-hood"), blk))
        self.assertTrue(is_blocked_symbol(_tok(name="ROBINHOOD"), blk))   # name match
        self.assertFalse(is_blocked_symbol(_tok(symbol="PEPE"), blk))
        self.assertFalse(is_blocked_symbol(_tok(symbol="ROBINHOOD"), []))  # no list

    def test_partial_name_not_blocked(self):
        # A legit meme that merely mentions robinhood in a longer name is fine
        # unless its symbol/exact-name matches. "Robinhood Killer" -> name norm
        # "ROBINHOODKILLER" != "ROBINHOOD".
        self.assertFalse(is_blocked_symbol(_tok(symbol="RHK", name="Robinhood Killer"),
                                           ["ROBINHOOD"]))


class TestConfigAndEnv(unittest.TestCase):
    def test_default_blocks_robinhood(self):
        cfg = Config()
        self.assertIn("ROBINHOOD", [s.upper() for s in cfg.chain.blocked_symbols])

    def test_env_union(self):
        import os
        os.environ["RHL2_BLOCKED_SYMBOLS"] = "scamx, foo"
        try:
            cfg = Config.load(None)
            syms = [s.upper() for s in cfg.chain.blocked_symbols]
            self.assertIn("ROBINHOOD", syms)   # default preserved
            self.assertIn("SCAMX", syms)
            self.assertIn("FOO", syms)
        finally:
            del os.environ["RHL2_BLOCKED_SYMBOLS"]


class TestBlockCommands(unittest.IsolatedAsyncioTestCase):
    def _sc(self) -> Scanner:
        cfg = Config()
        cfg.runtime.db_path = ":memory:"
        return Scanner(cfg)

    async def test_block_unblock_flow(self):
        sc = self._sc()
        try:
            # config default already blocks ROBINHOOD
            self.assertIn("ROBINHOOD", sc._blocked_symbols())
            await sc._handle_command("/block SCAMCOIN")
            self.assertIn("SCAMCOIN", sc._blocked_symbols())
            self.assertTrue(is_blocked_symbol(_tok(symbol="scamcoin"),
                                              sc._blocked_symbols()))
            await sc._handle_command("/unblock SCAMCOIN")
            self.assertNotIn("SCAMCOIN", sc._blocked_symbols())
            # can't remove the config default from the DB
            await sc._handle_command("/unblock ROBINHOOD")
            self.assertIn("ROBINHOOD", sc._blocked_symbols())
        finally:
            sc.storage.close()


if __name__ == "__main__":
    unittest.main()
