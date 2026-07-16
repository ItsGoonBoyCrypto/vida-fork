"""Tests for tiered re-alerting: escalation bypasses cooldown, dedupe by tier."""

from __future__ import annotations

import unittest

from rhl2_scanner.models import (
    AlertLevel,
    CategoryScore,
    ScoreResult,
    TokenSnapshot,
)
from rhl2_scanner.scanner import Scanner
from rhl2_scanner.config import Config
from rhl2_scanner.storage import Storage

PAIR = "0xPAIRaddress0000000000000000000000000001"
TOKEN = "0xTOKENaddress000000000000000000000000001"


def _snap() -> TokenSnapshot:
    return TokenSnapshot(chain="robinhood", pair_address=PAIR,
                         token_address=TOKEN, symbol="ESC")


def _result(level: AlertLevel, score: float) -> ScoreResult:
    return ScoreResult(composite=score, level=level,
                       categories=[CategoryScore("momentum", 50, 0.25)],
                       safety_passed=True)


class TestAlertRankStorage(unittest.TestCase):
    def test_rank_defaults_and_updates(self):
        s = Storage(":memory:")
        try:
            snap = _snap()
            s.mark_seen(snap)
            self.assertEqual(s.alert_rank(PAIR), -1)          # never alerted
            s.record_alert(snap, _result(AlertLevel.SKIP, 40), rank=0)  # early
            self.assertEqual(s.alert_rank(PAIR), 0)
            s.record_alert(snap, _result(AlertLevel.STRONG, 80), rank=2)
            self.assertEqual(s.alert_rank(PAIR), 2)
            # rank never regresses (MAX)
            s.record_alert(snap, _result(AlertLevel.WATCH, 62), rank=1)
            self.assertEqual(s.alert_rank(PAIR), 2)
        finally:
            s.close()

    def test_record_alert_without_rank_keeps_default(self):
        s = Storage(":memory:")
        try:
            snap = _snap()
            s.mark_seen(snap)
            s.record_alert(snap, _result(AlertLevel.WATCH, 62))  # no rank arg
            self.assertEqual(s.alert_rank(PAIR), -1)
            self.assertTrue(s.in_cooldown(PAIR, 3600))  # last_alerted still set
        finally:
            s.close()

    def test_migration_adds_column(self):
        import sqlite3
        import tempfile
        import os
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            # Simulate an OLD db without best_alert_rank.
            conn = sqlite3.connect(path)
            conn.execute("""CREATE TABLE seen_tokens (
                pair_address TEXT PRIMARY KEY, token_address TEXT NOT NULL,
                symbol TEXT, first_seen REAL NOT NULL, last_scored REAL,
                last_alerted REAL, best_score REAL DEFAULT 0)""")
            conn.commit()
            conn.close()
            s = Storage(path)          # should migrate without error
            try:
                self.assertEqual(s.alert_rank("0xanything"), -1)
            finally:
                s.close()
        finally:
            os.unlink(path)


class TestEscalationNote(unittest.TestCase):
    def test_note_shape(self):
        self.assertIn("UPGRADED", Scanner._escalation_note(0, 2))
        self.assertIn("Early", Scanner._escalation_note(0, 1))
        self.assertIn("Strong", Scanner._escalation_note(1, 2))


class TestEscalationDecision(unittest.IsolatedAsyncioTestCase):
    """Exercise the tier/cooldown branch logic via a real Storage + Config."""

    def _scanner(self) -> Scanner:
        cfg = Config()
        cfg.runtime.db_path = ":memory:"
        cfg.runtime.realert_cooldown_seconds = 9999
        return Scanner(cfg)

    async def test_escalation_bypasses_cooldown(self):
        sc = self._scanner()
        try:
            snap = _snap()
            sc.storage.mark_seen(snap)
            # First: early alert at tier 0.
            sc.storage.record_alert(snap, _result(AlertLevel.SKIP, 40), rank=0)
            self.assertTrue(sc.storage.in_cooldown(PAIR, 9999))
            prev = sc.storage.alert_rank(PAIR)
            # Escalation to strong (tier 2) should be allowed despite cooldown.
            tier = 2
            escalated = sc.cfg.runtime.realert_on_escalation and prev >= 0 and tier > prev
            cooling = sc.storage.in_cooldown(PAIR, sc.cfg.runtime.realert_cooldown_seconds)
            allowed = prev < 0 or escalated or not cooling
            self.assertTrue(escalated)
            self.assertTrue(allowed)
        finally:
            sc.storage.close()

    async def test_same_tier_in_cooldown_suppressed(self):
        sc = self._scanner()
        try:
            snap = _snap()
            sc.storage.mark_seen(snap)
            sc.storage.record_alert(snap, _result(AlertLevel.STRONG, 80), rank=2)
            prev = sc.storage.alert_rank(PAIR)
            tier = 2
            escalated = sc.cfg.runtime.realert_on_escalation and prev >= 0 and tier > prev
            cooling = sc.storage.in_cooldown(PAIR, sc.cfg.runtime.realert_cooldown_seconds)
            allowed = prev < 0 or escalated or not cooling
            self.assertFalse(escalated)
            self.assertFalse(allowed)   # same tier + cooling => suppressed
        finally:
            sc.storage.close()


if __name__ == "__main__":
    unittest.main()
