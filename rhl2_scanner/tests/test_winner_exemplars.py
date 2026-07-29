"""Scanner harvests curated winner exemplars once on startup."""

from __future__ import annotations

import unittest

from rhl2_scanner.config import Config
from rhl2_scanner.scanner import Scanner
from rhl2_scanner.winner_exemplars import WINNER_EXEMPLARS, load_winner_exemplars

CA_132X = "0xcdd50d73b45085d71cb05e2ca238d12c3bd7bebd"


class TestLoad(unittest.TestCase):
    def test_seeded_and_env_merge(self):
        self.assertIn(CA_132X, WINNER_EXEMPLARS)
        got = load_winner_exemplars("0x" + "2" * 40 + "=other")
        self.assertIn(CA_132X, got)
        self.assertEqual(got["0x" + "2" * 40], "other")


class TestHarvest(unittest.IsolatedAsyncioTestCase):
    async def test_harvests_once(self):
        cfg = Config()
        cfg.runtime.db_path = ":memory:"
        sc = Scanner(cfg)
        sc._session = object()
        sc._sent = []
        calls = []

        async def fake_send(html, reply_to=None, reply_markup=None):
            sc._sent.append(html)
            return 1

        async def fake_harvest(token, symbol, source_prefix, mult=0.0):
            calls.append(token)
            return 3
        sc._send_html = fake_send      # type: ignore
        sc._harvest_buyers = fake_harvest  # type: ignore
        try:
            await sc._harvest_winner_exemplars()
            self.assertIn(CA_132X, calls)
            self.assertTrue(any(CA_132X in h and "exemplar" in h.lower() for h in sc._sent))
            # second startup — already harvested, no repeat
            calls.clear()
            await sc._harvest_winner_exemplars()
            self.assertEqual(calls, [])
        finally:
            sc.storage.close()


if __name__ == "__main__":
    unittest.main()
