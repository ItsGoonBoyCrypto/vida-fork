"""Curated alpha wallets are seeded, labelled, tracked, and persist."""

from __future__ import annotations

import os
import unittest
from unittest import mock

from rhl2_scanner.alpha_wallets import ALPHA_WALLETS, load_alpha_wallets
from rhl2_scanner.config import Config
from rhl2_scanner.scanner import Scanner


class TestLoad(unittest.TestCase):
    def test_all_lowercased_and_valid(self):
        wallets = load_alpha_wallets("")
        self.assertEqual(len(wallets), len(ALPHA_WALLETS))
        for addr in wallets:
            self.assertTrue(addr.startswith("0x") and len(addr) == 42)
            self.assertEqual(addr, addr.lower())

    def test_env_merge(self):
        extra = "0x" + "9" * 40
        wallets = load_alpha_wallets(f"{extra}=My Whale")
        self.assertEqual(wallets[extra], "My Whale")


class TestSeeding(unittest.TestCase):
    def _sc(self):
        cfg = Config()
        cfg.runtime.db_path = ":memory:"
        return Scanner(cfg)

    def test_seeded_into_smart_set_and_labelled(self):
        sc = self._sc()
        try:
            sample = next(iter(ALPHA_WALLETS))
            # in the active smart set…
            self.assertIn(sample, [w.lower() for w in sc.cfg.smart_money_wallets])
            # …persisted…
            self.assertIn(sample, sc.storage.smart_wallets())
            # …labelled for the feed…
            self.assertEqual(sc.cfg.wallet_watch.labels[sample], ALPHA_WALLETS[sample])
            # …and the feed is on.
            self.assertTrue(sc.cfg.wallet_watch.enabled)
            self.assertTrue(sc.cfg.wallet_watch.watch_smart_set)
        finally:
            sc.storage.close()

    def test_watcher_watches_alpha_wallets(self):
        from rhl2_scanner.walletwatch import WalletWatcher
        sc = self._sc()
        try:
            sc.cfg.chain.explorer_api_url = "https://exp/api"
            watched = WalletWatcher(sc.cfg, sc.storage)._watched_wallets()
            sample = next(iter(ALPHA_WALLETS))
            self.assertIn(sample, watched)
        finally:
            sc.storage.close()

    def test_respects_explicit_disable(self):
        with mock.patch.dict(os.environ, {"RHL2_WALLET_WATCH": "0"}):
            sc = self._sc()
            try:
                # still seeded to the smart set, but the feed stays off
                self.assertFalse(sc.cfg.wallet_watch.enabled)
                sample = next(iter(ALPHA_WALLETS))
                self.assertIn(sample, sc.storage.smart_wallets())
            finally:
                sc.storage.close()


if __name__ == "__main__":
    unittest.main()
