"""Tests for the /stats activity snapshot."""

from __future__ import annotations

import time
import unittest

from rhl2_scanner.models import AlertLevel, CategoryScore, ScoreResult, TokenSnapshot
from rhl2_scanner.storage import Storage


def _snap():
    return TokenSnapshot(chain="r", pair_address="0xp", token_address="0xt",
                         symbol="X", price_usd=0.001)


def _res():
    return ScoreResult(composite=82, level=AlertLevel.STRONG,
                       categories=[CategoryScore("m", 60, 0.25)], safety_passed=True)


class TestActivityStats(unittest.TestCase):
    def test_counts(self):
        s = Storage(":memory:")
        try:
            snap = _snap()
            s.mark_seen(snap)
            s.record_score(snap, _res())
            s.record_alert(snap, _res(), rank=2)
            s.mark_cluster_alert("0xt", 2)
            s.pos_event_new("0xt|x2")
            s.pos_event_new("0xt|x5")
            s.pos_event_new("0xt|dump")
            s.pos_event_new("0xt|exit|0xw")
            st = s.activity_stats(time.time() - 3600)
            self.assertEqual(st["discovered"], 1)
            self.assertEqual(st["alerts"], 1)
            self.assertEqual(st["clusters"], 1)
            self.assertEqual(st["milestones"], 2)
            self.assertEqual(st["dumps"], 1)
            self.assertEqual(st["exits"], 1)
            self.assertEqual(st["best_score"], 82)
            # window excludes old activity
            future = s.activity_stats(time.time() + 100)
            self.assertEqual(future["alerts"], 0)
            self.assertEqual(future["clusters"], 0)
        finally:
            s.close()


if __name__ == "__main__":
    unittest.main()
