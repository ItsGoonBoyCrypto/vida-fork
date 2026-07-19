"""Tests for the retroactive winner harvest."""

from __future__ import annotations

import time
import unittest

from rhl2_scanner.config import Config
from rhl2_scanner.scanner import Scanner
from rhl2_scanner.storage import Storage

TOKEN = "0x" + "a" * 40
PAIR = "0x" + "p" * 39 + "1"


class TestHarvestCandidateStore(unittest.TestCase):
    def test_dedup_and_due_window(self):
        s = Storage(":memory:")
        try:
            s.add_harvest_candidate(TOKEN, PAIR, "RUN", 0.001)
            s.add_harvest_candidate(TOKEN, PAIR, "RUN", 0.999)  # ignored (dupe)
            row = s._conn.execute("SELECT entry_price FROM harvest_candidates").fetchone()
            self.assertAlmostEqual(row["entry_price"], 0.001)   # first price kept
            # freshly added -> not yet in the 24-36h window
            self.assertEqual(s.due_harvest_candidates(24*3600, 36*3600, 10), [])
            # backdate it 30h -> due
            s._conn.execute("UPDATE harvest_candidates SET entry_ts = ?",
                            (time.time() - 30 * 3600,))
            s._conn.commit()
            due = s.due_harvest_candidates(24*3600, 36*3600, 10)
            self.assertEqual(len(due), 1)
            s.mark_harvest_done(TOKEN)
            self.assertEqual(s.due_harvest_candidates(24*3600, 36*3600, 10), [])
        finally:
            s.close()


class _FakeDex:
    def __init__(self, price):
        self._price = price
    async def refresh(self, stub):
        stub.price_usd = self._price
        return stub
    async def pairs_for_token(self, token):
        return []


class TestHarvestSweep(unittest.IsolatedAsyncioTestCase):
    def _sc(self, current_price) -> Scanner:
        cfg = Config()
        cfg.runtime.db_path = ":memory:"
        cfg.runtime.winner_harvest_enabled = True
        cfg.runtime.winner_harvest_win_mult = 3.0
        sc = Scanner(cfg)
        # stub the buyer harvest so no network is needed
        sc._harvested = []
        async def fake_harvest(token, symbol, source_prefix, mult=0.0):
            sc._harvested.append((token, source_prefix))
            return 5
        sc._harvest_buyers = fake_harvest  # type: ignore
        return sc

    async def _seed_due(self, sc, entry, price_now):
        sc.storage.add_harvest_candidate(TOKEN, PAIR, "RUN", entry)
        sc.storage._conn.execute("UPDATE harvest_candidates SET entry_ts = ?",
                                 (time.time() - 30 * 3600,))
        sc.storage._conn.commit()
        await sc._harvest_winners(_FakeDex(price_now))

    async def test_harvests_a_winner(self):
        sc = self._sc(current_price=0.005)
        try:
            await self._seed_due(sc, entry=0.001, price_now=0.005)   # 5x
            self.assertEqual(len(sc._harvested), 1)
            self.assertEqual(sc._harvested[0][1], "winner")
            # marked done -> not re-swept
            await sc._harvest_winners(_FakeDex(0.005))
            self.assertEqual(len(sc._harvested), 1)
        finally:
            sc.storage.close()

    async def test_skips_a_non_winner(self):
        sc = self._sc(current_price=0.0012)
        try:
            await self._seed_due(sc, entry=0.001, price_now=0.0012)   # 1.2x
            self.assertEqual(sc._harvested, [])
            # still marked done (processed once)
            self.assertEqual(sc.storage.due_harvest_candidates(24*3600, 36*3600, 10), [])
        finally:
            sc.storage.close()

    async def test_disabled_noop(self):
        sc = self._sc(current_price=0.01)
        sc.cfg.runtime.winner_harvest_enabled = False
        try:
            await self._seed_due(sc, entry=0.001, price_now=0.01)
            self.assertEqual(sc._harvested, [])
        finally:
            sc.storage.close()


if __name__ == "__main__":
    unittest.main()
