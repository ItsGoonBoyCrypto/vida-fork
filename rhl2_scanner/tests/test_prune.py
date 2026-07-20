"""DB retention: prune drops stale dedup/observation rows, never reputation."""

from __future__ import annotations

import time
import unittest

from rhl2_scanner.storage import Storage


class TestScannerPrune(unittest.TestCase):
    def test_prunes_old_markers_keeps_recent_and_reputation(self):
        s = Storage(":memory:")
        try:
            old = time.time() - 40 * 86400
            # a stale one-shot marker + a fresh one
            s._conn.execute("INSERT INTO pos_events (key, ts) VALUES ('k_old', ?)", (old,))
            s.pos_event_new("k_new")
            # reputation-class data that must survive
            s.record_honeypot_deployer("0xdev", "0xtok")
            s.record_toxic_funder("0xfunder", "0xdev", "0xtok")
            s._conn.commit()

            removed = s.prune(days=30)
            self.assertGreaterEqual(removed, 1)
            # old marker gone, fresh marker + blocklists intact
            self.assertIsNone(s._conn.execute(
                "SELECT 1 FROM pos_events WHERE key='k_old'").fetchone())
            self.assertIsNotNone(s._conn.execute(
                "SELECT 1 FROM pos_events WHERE key='k_new'").fetchone())
            self.assertEqual(s.is_honeypot_deployer("0xdev"), 1)
            self.assertEqual(s.is_toxic_funder("0xfunder"), 1)
        finally:
            s.close()


if __name__ == "__main__":
    unittest.main()
