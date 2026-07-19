"""Cross-pollination: import the RH scanner's proven wallets into memelab."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest

from memelab.bridge import import_scanner_reputation
from memelab.models import Chain
from memelab.storage import Store


def _scanner_db(rows):
    """A throwaway scanner.db (robinhood-only, no chain column)."""
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    conn = sqlite3.connect(f.name)
    conn.execute("CREATE TABLE wallet_winners (wallet TEXT, token TEXT, mult REAL, ts REAL)")
    conn.executemany("INSERT INTO wallet_winners VALUES (?,?,?,0)", rows)
    conn.commit()
    conn.close()
    return f.name


class TestScannerBridge(unittest.TestCase):
    def setUp(self):
        self.s = Store(":memory:")

    def tearDown(self):
        self.s.close()

    def test_imports_into_robinhood_reputation(self):
        db = _scanner_db([("0xAAA", "0xt1", 5.0), ("0xAAA", "0xt2", 3.0),
                          ("0xBBB", "0xt1", 4.0)])   # BBB only 1 winner
        n = import_scanner_reputation(self.s, db, min_overlap=2)
        self.assertEqual(n, 1)
        rep = self.s.wallet_reputation_rows(Chain.ROBINHOOD, ["0xaaa"])
        self.assertEqual(rep["0xaaa"]["winner_overlap"], 2)
        self.assertIn("0xaaa", self.s.core_alpha_wallets(Chain.ROBINHOOD, 2))
        # credited to robinhood, not other chains
        self.assertNotIn("0xaaa", self.s.core_alpha_wallets(Chain.BASE, 2))
        self.assertIn("0xaaa", self.s.smart_wallets(Chain.ROBINHOOD))

    def test_dormant_when_absent(self):
        self.assertEqual(import_scanner_reputation(self.s, "/nope.db"), 0)
        self.assertEqual(import_scanner_reputation(self.s, ""), 0)


if __name__ == "__main__":
    unittest.main()
