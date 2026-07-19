"""Quality floors: maturity alerts gate on composite, early-launch on conviction."""

from __future__ import annotations

import unittest

import rhl2_scanner.scanner as scan_mod
from rhl2_scanner.config import Config
from rhl2_scanner.models import AlertLevel, CategoryScore, ScoreResult, TokenSnapshot
from rhl2_scanner.scanner import Scanner


def _res(level, composite):
    return ScoreResult(composite=composite, level=level, safety_passed=True,
                       categories=[CategoryScore("safety", 60, 0.4),
                                   CategoryScore("momentum", 50, 0.21)])


class TestAlertFloor(unittest.IsolatedAsyncioTestCase):
    def _sc(self, **runtime):
        cfg = Config()
        cfg.runtime.db_path = ":memory:"
        cfg.runtime.dry_run = False
        cfg.thresholds.watch_alert_score = 50   # so composite 52 lands WATCH
        cfg.thresholds.strong_alert_score = 90
        for k, v in runtime.items():
            setattr(cfg.runtime, k, v)
        sc = Scanner(cfg)
        sc._sent = []
        async def fake_send(html, reply_to=None):
            sc._sent.append(html); return 1
        sc._send_html = fake_send            # type: ignore
        async def fake_enrich(snap):
            pass
        sc._enrich = fake_enrich             # type: ignore
        async def fake_notify(snap, result, note=""):
            sc._sent.append("NOTIFIER:" + (note or "")); return 1
        sc.notifier.send = fake_notify       # type: ignore
        async def fake_intel(snap, result):
            snap.conviction = getattr(snap, "_test_conv", 70.0)
        sc._attach_alert_intel = fake_intel  # type: ignore
        return sc

    def _snap(self, **kw):
        base = dict(chain="robinhood", pair_address="0xp", token_address="0xt",
                    symbol="GEM", price_usd=1.0, market_cap_usd=50000, liquidity_usd=8000)
        base.update(kw)
        return TokenSnapshot(**base)

    async def test_maturity_below_floor_suppressed(self):
        sc = self._sc(min_alert_score=55.0)
        self._orig = scan_mod.score_token
        scan_mod.score_token = lambda *a, **k: _res(AlertLevel.WATCH, 52)  # < 55
        try:
            await sc._process(self._snap())
            self.assertEqual(sc._sent, [])          # floored out
        finally:
            scan_mod.score_token = self._orig
            sc.storage.close()

    async def test_maturity_above_floor_alerts(self):
        sc = self._sc(min_alert_score=55.0)
        self._orig = scan_mod.score_token
        scan_mod.score_token = lambda *a, **k: _res(AlertLevel.WATCH, 58)  # >= 55
        try:
            await sc._process(self._snap())
            self.assertTrue(any("NOTIFIER" in h for h in sc._sent))
        finally:
            scan_mod.score_token = self._orig
            sc.storage.close()

    async def test_early_low_conviction_suppressed(self):
        sc = self._sc(early_launch_min_conviction=45.0)
        self._orig = scan_mod.score_token
        scan_mod.score_token = lambda *a, **k: _res(AlertLevel.SKIP, 30)
        sc._is_early_launch = lambda snap, result: True   # type: ignore
        try:
            snap = self._snap()
            snap._test_conv = 30.0            # below the 45 conviction floor
            await sc._process(snap)
            self.assertEqual(sc._sent, [])
        finally:
            scan_mod.score_token = self._orig
            sc.storage.close()

    async def test_early_high_conviction_alerts(self):
        sc = self._sc(early_launch_min_conviction=45.0)
        self._orig = scan_mod.score_token
        scan_mod.score_token = lambda *a, **k: _res(AlertLevel.SKIP, 30)
        sc._is_early_launch = lambda snap, result: True   # type: ignore
        try:
            snap = self._snap()
            snap._test_conv = 65.0            # strong confluence despite low score
            await sc._process(snap)
            self.assertTrue(sc._sent)         # early alert fired
        finally:
            scan_mod.score_token = self._orig
            sc.storage.close()


if __name__ == "__main__":
    unittest.main()
