"""Cross-pollination: import memelab's proven robinhood wallets into the scanner."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest

from rhl2_scanner.memelab_bridge import import_memelab_reputation
from rhl2_scanner.storage import Storage


def _memelab_db(rows):
    """A throwaway memelab.db with a wallet_winners table + given rows."""
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    conn = sqlite3.connect(f.name)
    conn.execute("CREATE TABLE wallet_winners (chain TEXT, wallet TEXT, token TEXT, "
                 "mult REAL, ts REAL)")
    conn.executemany("INSERT INTO wallet_winners VALUES (?,?,?,?,0)", rows)
    conn.commit()
    conn.close()
    return f.name


class TestMemelabBridge(unittest.TestCase):
    def setUp(self):
        self.s = Storage(":memory:")

    def tearDown(self):
        self.s.close()

    def test_imports_proven_wallets_only(self):
        db = _memelab_db([
            ("robinhood", "0xAAA", "0xt1", 5.0),   # 2 winners → imported
            ("robinhood", "0xAAA", "0xt2", 3.0),
            ("robinhood", "0xBBB", "0xt1", 4.0),   # 1 winner → below overlap
            ("solana",    "0xCCC", "0xt9", 9.0),   # wrong chain → ignored
        ])
        n = import_memelab_reputation(self.s, db, min_overlap=2)
        self.assertEqual(n, 1)
        # 0xAAA now in the smart set + core-alpha with overlap 2
        self.assertIn("0xaaa", self.s.smart_wallets())
        self.assertIn("0xaaa", self.s.core_alpha_wallets(min_overlap=2))
        self.assertNotIn("0xbbb", self.s.smart_wallets())

    def test_idempotent(self):
        db = _memelab_db([("robinhood", "0xAAA", "0xt1", 5.0),
                          ("robinhood", "0xAAA", "0xt2", 3.0)])
        import_memelab_reputation(self.s, db, min_overlap=2)
        import_memelab_reputation(self.s, db, min_overlap=2)   # again
        rep = self.s.wallet_reputation_rows(["0xaaa"])
        self.assertEqual(rep["0xaaa"]["winner_overlap"], 2)    # not doubled

    def test_missing_db_dormant(self):
        self.assertEqual(import_memelab_reputation(self.s, "/nope/x.db"), 0)
        self.assertEqual(import_memelab_reputation(self.s, ""), 0)


if __name__ == "__main__":
    unittest.main()
