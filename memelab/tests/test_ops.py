"""Tests for ops hardening: dataset backup + heartbeat/stall detection."""

from __future__ import annotations

import glob
import os
import tempfile
import unittest

from memelab.models import Chain, TokenSnapshot
from memelab.storage import Store
from memelab.collector import Collector, CollectorConfig


class TestBackup(unittest.TestCase):
    def test_backup_is_a_valid_copy(self):
        d = tempfile.mkdtemp()
        src = os.path.join(d, "memelab.db")
        s = Store(src)
        s.record_snapshot(TokenSnapshot(chain=Chain.BASE, token_address="0xt",
                                        ts=1.0, price_usd=1.0))
        dest = os.path.join(d, "copy.db")
        s.backup(dest)
        s.close()
        copy = Store(dest)
        try:
            self.assertEqual(copy.coverage()["total_tokens"], 1)
        finally:
            copy.close()

    def test_backup_rotation_keeps_n(self):
        d = tempfile.mkdtemp()
        src = os.path.join(d, "memelab.db")
        store = Store(src)
        store.record_snapshot(TokenSnapshot(chain=Chain.BASE, token_address="0xt",
                                            ts=1.0, price_usd=1.0))
        cfg = CollectorConfig(chains=[Chain.BASE], backup_interval_min=0, backup_keep=3)
        col = Collector(cfg, store, {}, None)
        try:
            for _ in range(5):
                col._last_backup = 0            # force due each call
                import time as _t
                col._maybe_backup()
                _t.sleep(0.01)                  # distinct timestamps
            self.assertLessEqual(len(glob.glob(f"{src}.bak.*")), 3)
        finally:
            store.close()


class TestHeartbeat(unittest.TestCase):
    def test_alive_and_stalled_text(self):
        store = Store(":memory:")
        store.record_snapshot(TokenSnapshot(chain=Chain.SOLANA, token_address="M",
                                            ts=1.0, price_usd=1.0))
        col = Collector(CollectorConfig(chains=[Chain.SOLANA]), store, {}, None)
        try:
            alive = col.heartbeat_text(stalled=False)
            self.assertIn("alive", alive)
            self.assertIn("tokens 1", alive)
            self.assertIn("STALLED", col.heartbeat_text(stalled=True))
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
