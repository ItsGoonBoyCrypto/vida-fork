"""The 🌱 early-launch quality gates — reject dead/quiet launches (anti-spam)."""

from __future__ import annotations

import unittest

from rhl2_scanner.config import Config
from rhl2_scanner.models import AlertLevel, ScoreResult, TokenSnapshot
from rhl2_scanner.scanner import Scanner


def _result(safety=True):
    return ScoreResult(composite=40.0, level=AlertLevel.SKIP, categories=[],
                       safety_passed=safety)


class TestEarlyLaunchGates(unittest.IsolatedAsyncioTestCase):
    def _sc(self, **runtime):
        cfg = Config.load("rhl2_scanner/config/robinhood.example.yaml")
        cfg.runtime.db_path = ":memory:"
        cfg.runtime.dry_run = False
        # deterministic gate values for the test
        cfg.runtime.early_launch_enabled = True
        cfg.runtime.early_launch_max_age_minutes = 90
        cfg.runtime.early_launch_min_liquidity_usd = 3500
        cfg.runtime.early_launch_min_holders = 25
        cfg.runtime.early_launch_min_buy_ratio_1h = 0.55
        cfg.runtime.early_launch_min_volume_1h_usd = 800
        for k, v in runtime.items():
            setattr(cfg.runtime, k, v)
        return Scanner(cfg)

    def _snap(self, **kw):
        base = dict(chain="robinhood", pair_address="0xp", token_address="0xt",
                    age_minutes=20.0, liquidity_usd=5000.0, holder_count=40,
                    buys_1h=8, sells_1h=2, volume_1h=1500.0)
        base.update(kw)
        return TokenSnapshot(**base)

    def test_healthy_launch_passes(self):
        sc = self._sc()
        try:
            self.assertTrue(sc._is_early_launch(self._snap(), _result()))
        finally:
            sc.storage.close()

    def test_dead_launch_few_holders_rejected(self):
        sc = self._sc()
        try:
            self.assertFalse(sc._is_early_launch(self._snap(holder_count=5), _result()))
        finally:
            sc.storage.close()

    def test_no_net_buying_rejected(self):
        sc = self._sc()
        try:
            # 2 buys / 8 sells = 20% buy ratio → below 0.55
            self.assertFalse(sc._is_early_launch(
                self._snap(buys_1h=2, sells_1h=8), _result()))
        finally:
            sc.storage.close()

    def test_quiet_no_volume_rejected(self):
        sc = self._sc()
        try:
            self.assertFalse(sc._is_early_launch(self._snap(volume_1h=100.0), _result()))
        finally:
            sc.storage.close()

    def test_thin_liquidity_rejected(self):
        sc = self._sc()
        try:
            self.assertFalse(sc._is_early_launch(self._snap(liquidity_usd=1000.0), _result()))
        finally:
            sc.storage.close()

    def test_unknown_metrics_still_pass(self):
        # Genuinely-fresh token with no txn/holder data yet must not be filtered.
        sc = self._sc()
        try:
            snap = self._snap(holder_count=None, buys_1h=None, sells_1h=None,
                              volume_1h=None)
            self.assertTrue(sc._is_early_launch(snap, _result()))
        finally:
            sc.storage.close()

    def test_gates_disabled_when_zero(self):
        sc = self._sc(early_launch_min_holders=0, early_launch_min_buy_ratio_1h=0.0,
                      early_launch_min_volume_1h_usd=0.0)
        try:
            # dead launch passes when the quality gates are turned off
            snap = self._snap(holder_count=3, buys_1h=0, sells_1h=5, volume_1h=10.0)
            self.assertTrue(sc._is_early_launch(snap, _result()))
        finally:
            sc.storage.close()


if __name__ == "__main__":
    unittest.main()
