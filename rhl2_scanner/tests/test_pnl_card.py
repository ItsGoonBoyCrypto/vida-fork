"""The /pnlcard summary renders from tracked-alert history."""

from __future__ import annotations

import time
import unittest

from rhl2_scanner.config import Config
from rhl2_scanner.scanner import Scanner


class TestPnlCard(unittest.TestCase):
    def _sc(self):
        cfg = Config()
        cfg.runtime.db_path = ":memory:"
        return Scanner(cfg)

    def _add(self, sc, symbol, score, conv, peak, trough, settled=1):
        sc.storage._conn.execute(
            "INSERT INTO paper_trades (pair_address, token_address, symbol, chain, "
            "entry_ts, score, level, conviction, max_mult, min_mult, settled) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (f"0xp{symbol}", f"0xt{symbol}", symbol, "robinhood", time.time(),
             score, "strong", conv, peak, trough, settled))
        sc.storage._conn.commit()

    def test_empty_history(self):
        sc = self._sc()
        try:
            self.assertIn("No alerts tracked", sc._pnl_card())
        finally:
            sc.storage.close()

    def test_card_reports_hits_and_top_mover(self):
        sc = self._sc()
        try:
            self._add(sc, "MOON", 80, 75, peak=5.0, trough=0.9)     # big winner
            self._add(sc, "MEH", 62, 55, peak=1.2, trough=0.8)      # flat
            self._add(sc, "RUG", 58, 40, peak=1.0, trough=0.3)      # rug
            card = sc._pnl_card()
            self.assertIn("Alerts: <b>3</b>", card)
            self.assertIn("$MOON", card)             # top mover
            self.assertIn("Best <b>5.0x</b>", card)
            self.assertIn("By conviction", card)
        finally:
            sc.storage.close()


if __name__ == "__main__":
    unittest.main()
