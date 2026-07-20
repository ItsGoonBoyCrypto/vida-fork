"""Enrichment lands in the feature window; discovery doesn't re-anchor tokens."""

from __future__ import annotations

import time
import unittest

from memelab.collector import Collector, CollectorConfig
from memelab.models import Chain, TokenSnapshot
from memelab.storage import Store


class _Alerter:
    enabled = True

    async def send(self, html, reply_markup=None):
        pass


def _col(store):
    return Collector(CollectorConfig(chains=[Chain.BASE], alert_chains=[]),
                     store, {}, None, alerter=_Alerter())


class TestSafetyDue(unittest.TestCase):
    def test_fires_once_per_age_milestone(self):
        s = Store(":memory:")
        try:
            col = _col(s)
            token = "0x" + "a" * 40
            # a token first seen 16 min ago has crossed the 5 and 15 milestones
            row = {"token_address": token, "first_seen_ts": time.time() - 16 * 60,
                   "last_snapshot_ts": time.time() - 60}      # snapshotted a min ago
            self.assertTrue(col._safety_due(Chain.BASE, row))   # 5-min milestone
            self.assertTrue(col._safety_due(Chain.BASE, row))   # 15-min milestone
            self.assertFalse(col._safety_due(Chain.BASE, row))  # 30 not reached yet
        finally:
            s.close()

    def test_fresh_token_below_first_milestone_not_due(self):
        s = Store(":memory:")
        try:
            col = _col(s)
            row = {"token_address": "0x" + "b" * 40,
                   "first_seen_ts": time.time() - 60,          # 1 min old
                   "last_snapshot_ts": time.time()}
            self.assertFalse(col._safety_due(Chain.BASE, row))
        finally:
            s.close()


class TestDiscoveryGate(unittest.TestCase):
    def test_known_token_not_re_anchored(self):
        s = Store(":memory:")
        try:
            token = "0x" + "c" * 40
            s.record_snapshot(TokenSnapshot(chain=Chain.BASE, token_address=token,
                                            ts=100.0, price_usd=1.0))
            self.assertTrue(s.token_known(Chain.BASE, token))
            self.assertFalse(s.token_known(Chain.BASE, "0x" + "d" * 40))
        finally:
            s.close()


if __name__ == "__main__":
    unittest.main()
