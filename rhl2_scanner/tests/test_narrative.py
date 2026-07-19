"""Narrative classification + hit-rate stats + KOL tagging."""

from __future__ import annotations

import unittest

from rhl2_scanner.config import Config
from rhl2_scanner.narrative import classify, hot_narratives, narrative_stats
from rhl2_scanner.scanner import Scanner


class TestClassify(unittest.TestCase):
    def test_buckets(self):
        self.assertEqual(classify("DOGE"), "dog")
        self.assertEqual(classify("PEPE"), "frog")
        self.assertEqual(classify("TRUMP2024"), "politics")
        self.assertEqual(classify("neuralAI", "AI Agent"), "ai")
        self.assertEqual(classify("ZZZQ"), "other")


def _rows(items):
    # items: (symbol, max_mult, min_mult)
    return [{"symbol": s, "max_mult": mx, "min_mult": mn, "settled": 1}
            for s, mx, mn in items]


class TestStats(unittest.TestCase):
    def test_hit_rate_and_hot(self):
        rows = _rows([("DOGE", 3.0, 0.9), ("SHIBA", 4.0, 0.8),
                      ("DOGGO", 2.5, 0.9), ("INUKING", 1.1, 0.4),
                      ("PEPE", 1.0, 0.3), ("PEPE2", 1.0, 0.2)])
        stats = narrative_stats(rows)
        self.assertEqual(stats["dog"]["n"], 4)
        self.assertEqual(stats["dog"]["wins"], 3)
        hot = hot_narratives(stats, min_n=4, min_hit_rate=0.3)
        self.assertIn("dog", hot)          # 3/4 winners
        self.assertNotIn("frog", hot)      # 0/2 + too few


class TestKolAndNarrativeCommands(unittest.IsolatedAsyncioTestCase):
    def _sc(self):
        cfg = Config()
        cfg.runtime.db_path = ":memory:"
        return Scanner(cfg)

    async def test_kol_tag_and_alert(self):
        sc = self._sc()
        sent = []
        async def fake_send(html, reply_to=None):
            sent.append(html)
        sc._send_html = fake_send  # type: ignore
        try:
            w = "0x" + "a" * 40
            await sc._handle_command(f"/kol {w} Ansem")
            self.assertTrue(any("Tagged" in h and "Ansem" in h for h in sent))
            self.assertIn(w, sc.storage.kol_wallets())

            # a buy from that KOL fires a 📣 alert
            sent.clear()
            class _Ev:
                wallet = w
                token_address = "0xtok"
                symbol = "GEM"
                chart_url = ""
            await sc._check_cluster(_Ev())
            self.assertTrue(any("KOL BUY" in h for h in sent), sent)
        finally:
            sc.storage.close()

    async def test_narratives_command_empty(self):
        sc = self._sc()
        sent = []
        async def fake_send(html, reply_to=None):
            sent.append(html)
        sc._send_html = fake_send  # type: ignore
        try:
            await sc._handle_command("/narratives")
            self.assertTrue(any("No narrative data" in h for h in sent))
        finally:
            sc.storage.close()


if __name__ == "__main__":
    unittest.main()
