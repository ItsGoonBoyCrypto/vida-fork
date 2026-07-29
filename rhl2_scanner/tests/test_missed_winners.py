"""Missed-winner post-mortem store."""

from __future__ import annotations

import unittest

from rhl2_scanner.storage import Storage


class TestMissedWinners(unittest.TestCase):
    def test_record_list_stats(self):
        s = Storage(":memory:")
        try:
            s.record_missed_winner("0xa", "AAA", 12.0, "never alerted")
            s.record_missed_winner("0xb", "BBB", 40.0, "never alerted")
            s.record_missed_winner("0xa", "AAA", 15.0, "never alerted")   # upsert
            rows = s.missed_winners()
            self.assertEqual(rows[0]["symbol"], "BBB")     # sorted by peak desc
            self.assertEqual(rows[0]["peak"], 40.0)
            self.assertEqual(len(rows), 2)                 # 0xa upserted, not duped
            stats = s.missed_winner_stats()
            self.assertEqual(stats["count"], 2)
            self.assertEqual(stats["best"], 40.0)
        finally:
            s.close()


if __name__ == "__main__":
    unittest.main()
