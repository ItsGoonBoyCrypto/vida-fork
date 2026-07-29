"""Predatory-sniper DB: rug-dominant early wallets are flagged (ratio, not count)."""

from __future__ import annotations

import unittest

from rhl2_scanner.storage import Storage

SNIPER = "0x" + "5" * 40      # early on many rugs, no winners
SHARP = "0x" + "6" * 40       # early on many winners
MIXED = "0x" + "7" * 40       # a couple rugs but mostly winners


class TestPredatorySnipers(unittest.TestCase):
    def setUp(self):
        self.s = Storage(":memory:")

    def tearDown(self):
        self.s.close()

    def test_rug_dominant_flagged_sharp_and_mixed_spared(self):
        for i in range(4):
            self.s.record_wallet_rug(SNIPER, f"0xr{i}")
        for i in range(4):
            self.s.record_wallet_winner(SHARP, f"0xw{i}", 5.0)
        # mixed: 2 rugs, 5 winners -> ratio 0.29, below 0.7
        for i in range(2):
            self.s.record_wallet_rug(MIXED, f"0xm r{i}")
        for i in range(5):
            self.s.record_wallet_winner(MIXED, f"0xm w{i}", 3.0)

        snipers = self.s.predatory_snipers([SNIPER, SHARP, MIXED])
        self.assertIn(SNIPER.lower(), snipers)
        self.assertNotIn(SHARP.lower(), snipers)
        self.assertNotIn(MIXED.lower(), snipers)

    def test_needs_min_appearances(self):
        self.s.record_wallet_rug(SNIPER, "0xr1")   # only 1 appearance
        self.assertEqual(self.s.predatory_snipers([SNIPER]), set())


if __name__ == "__main__":
    unittest.main()
