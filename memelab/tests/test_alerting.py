"""Tests for the alerting hook: dedup, formatting, and collector wiring."""

from __future__ import annotations

import unittest

from memelab.models import Chain, Screen, Signature, TokenSnapshot, signature_to_json
from memelab.storage import Store
from memelab.alerting import format_screen_html
from memelab.collector import Collector, CollectorConfig


class _FakeAlerter:
    enabled = True
    def __init__(self):
        self.sent = []
    async def send(self, html):
        self.sent.append(html)


class TestFormatAndDedup(unittest.TestCase):
    def test_format(self):
        snap = TokenSnapshot(chain=Chain.SOLANA, token_address="MINT", symbol="WOW",
                             market_cap_usd=60000, liquidity_usd=15000, pair_address="P")
        html = format_screen_html(Screen(snapshot=snap, score=82.0, reasons=["buy_ratio_5m ≥ 0.6"]), 0.7)
        self.assertIn("memelab match 82", html)
        self.assertIn("$WOW", html)
        self.assertIn("MINT", html)

    def test_dedup(self):
        s = Store(":memory:")
        try:
            self.assertTrue(s.screen_alert_is_new(Chain.BASE, "0xtok", 80))
            self.assertFalse(s.screen_alert_is_new(Chain.BASE, "0xtok", 90))  # once only
        finally:
            s.close()


class TestCollectorAlerts(unittest.IsolatedAsyncioTestCase):
    async def test_alerts_once_on_high_score(self):
        store = Store(":memory:")
        # a validated signature with one clear rule
        sig = Signature(chains=[Chain.BASE],
                        rules=[{"feature": "buy_ratio_5m", "op": ">=", "value": 0.6, "weight": 1.0}],
                        model={}, precision=0.7, recall=0.6)
        store.save_signature(signature_to_json(sig))

        # a tracked token whose early snapshot satisfies the rule
        store.record_snapshot(TokenSnapshot(chain=Chain.BASE, token_address="0xTOK",
                              ts=1000.0, price_usd=1.0, symbol="GEM",
                              buys_5m=45, sells_5m=5, market_cap_usd=60000, liquidity_usd=15000))

        alerter = _FakeAlerter()
        col = Collector(CollectorConfig(chains=[Chain.BASE]), store, {}, None, alerter=alerter)
        try:
            self.assertTrue(col.screener.ready())
            await col._maybe_alert(Chain.BASE, "0xTOK")
            self.assertEqual(len(alerter.sent), 1)
            self.assertIn("memelab match", alerter.sent[0])
            await col._maybe_alert(Chain.BASE, "0xTOK")   # dedup — no second alert
            self.assertEqual(len(alerter.sent), 1)
        finally:
            store.close()

    async def test_no_alert_without_signature(self):
        store = Store(":memory:")
        store.record_snapshot(TokenSnapshot(chain=Chain.BASE, token_address="0xT2",
                              ts=1.0, price_usd=1.0, buys_5m=45, sells_5m=5))
        alerter = _FakeAlerter()
        col = Collector(CollectorConfig(chains=[Chain.BASE]), store, {}, None, alerter=alerter)
        try:
            await col._maybe_alert(Chain.BASE, "0xT2")
            self.assertEqual(alerter.sent, [])            # no signature → no alerts
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
