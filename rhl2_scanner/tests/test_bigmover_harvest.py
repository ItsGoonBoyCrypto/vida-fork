"""Any tracked alert that peaks >= bigmover_harvest_mult is harvested once."""

from __future__ import annotations

import time
import unittest

from rhl2_scanner.config import Config
from rhl2_scanner.scanner import Scanner

BIG = "0x" + "b" * 40
SMALL = "0x" + "c" * 40


class TestBigMoverHarvest(unittest.IsolatedAsyncioTestCase):
    def _sc(self):
        cfg = Config()
        cfg.runtime.db_path = ":memory:"
        cfg.runtime.bigmover_harvest_mult = 10.0
        sc = Scanner(cfg)
        sc._session = object()          # non-None so the harvest path runs
        sc._sent = []

        async def fake_send(html, reply_to=None, reply_markup=None):
            sc._sent.append(html)
            return 1
        sc._send_html = fake_send  # type: ignore
        self._harvested = []

        async def fake_harvest(token, symbol, source_prefix, mult=0.0):
            self._harvested.append((token, round(mult, 1)))
            return 4
        sc._harvest_buyers = fake_harvest  # type: ignore
        return sc

    def _add(self, sc, token, peak):
        sc.storage._conn.execute(
            "INSERT INTO paper_trades (pair_address, token_address, symbol, chain, "
            "entry_ts, score, level, max_mult, min_mult, settled) "
            "VALUES (?,?,?,?,?,?,?,?,?,1)",
            (f"0xp{token[-4:]}", token, "SYM", "robinhood", time.time(),
             65, "watch", peak, 0.9))
        sc.storage._conn.commit()

    async def test_only_big_movers_harvested_once(self):
        sc = self._sc()
        try:
            self._add(sc, BIG, 132.0)     # a 132x runner (watch band)
            self._add(sc, SMALL, 3.0)     # a modest one, below threshold
            await sc._harvest_big_movers()
            self.assertEqual([t for t, _ in self._harvested], [BIG])
            self.assertEqual(self._harvested[0][1], 132.0)   # peak passed through
            self.assertTrue(any("Big mover harvested" in h and BIG in h for h in sc._sent))
            # second pass: already harvested -> no repeat
            self._harvested.clear()
            await sc._harvest_big_movers()
            self.assertEqual(self._harvested, [])
        finally:
            sc.storage.close()


if __name__ == "__main__":
    unittest.main()
