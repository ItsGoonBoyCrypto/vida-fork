"""Weekly review: composes a report and fires once per interval (restart-safe)."""

from __future__ import annotations

import time
import unittest

from rhl2_scanner.config import Config
from rhl2_scanner.scanner import Scanner


class TestWeeklyReview(unittest.IsolatedAsyncioTestCase):
    def _sc(self):
        cfg = Config()
        cfg.runtime.db_path = ":memory:"
        sc = Scanner(cfg)
        sc._sent = []

        async def fake_send(html, reply_to=None, reply_markup=None):
            sc._sent.append(html)
            return 1
        sc._send_html = fake_send  # type: ignore
        return sc

    def test_review_composes_with_no_data(self):
        sc = self._sc()
        try:
            html = sc._weekly_review()
            self.assertIn("Weekly review", html)
            self.assertIn("hand-values", html)   # weights not learned yet
        finally:
            sc.storage.close()

    async def test_first_run_seeds_clock_no_send(self):
        sc = self._sc()
        try:
            await sc._maybe_weekly_review()
            self.assertEqual(sc._sent, [])                       # seeds, doesn't fire
            self.assertIsNotNone(sc.storage.kv_get("last_weekly_review_ts"))
        finally:
            sc.storage.close()

    async def test_fires_after_interval(self):
        sc = self._sc()
        try:
            # pretend the last review was 8 days ago
            sc.storage.kv_set("last_weekly_review_ts", str(time.time() - 8 * 86400))
            await sc._maybe_weekly_review()
            self.assertTrue(any("Weekly review" in h for h in sc._sent))
        finally:
            sc.storage.close()

    async def test_quiet_within_interval(self):
        sc = self._sc()
        try:
            sc.storage.kv_set("last_weekly_review_ts", str(time.time() - 3600))
            await sc._maybe_weekly_review()
            self.assertEqual(sc._sent, [])
        finally:
            sc.storage.close()


if __name__ == "__main__":
    unittest.main()
