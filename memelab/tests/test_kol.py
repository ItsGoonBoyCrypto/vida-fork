"""KOL wallet seeding + KOL-buy detection/alert."""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest

from memelab.kol import _parse_env, _parse_file, kol_seeds, seed_kols
from memelab.models import Chain, TokenSnapshot
from memelab.smartmoney import SmartMoney
from memelab.storage import Store

ADDR = "So11111111111111111111111111111111111111112"


class TestParse(unittest.TestCase):
    def test_parse_env(self):
        seeds = _parse_env(f"solana:{ADDR}:Ansem, base:0xabc:Cobie , junk")
        self.assertEqual(seeds[0], (Chain.SOLANA, ADDR, "Ansem"))
        self.assertEqual(seeds[1][2], "Cobie")
        self.assertEqual(len(seeds), 2)          # "junk" (no addr) dropped

    def test_parse_file_skips_placeholders(self):
        data = {"kols": [
            {"name": "Ansem", "chain": "solana", "address": ADDR},
            {"name": "Fake", "chain": "solana", "address": "<VERIFY on arkham>"},
        ]}
        f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(data, f); f.close()
        try:
            seeds = _parse_file(f.name)
            self.assertEqual(len(seeds), 1)      # placeholder skipped
            self.assertEqual(seeds[0][2], "Ansem")
        finally:
            os.unlink(f.name)

    def test_kol_seeds_dedup(self):
        os.environ["MEMELAB_KOL_WALLETS"] = f"solana:{ADDR}:Ansem,solana:{ADDR}:Dup"
        try:
            self.assertEqual(len(kol_seeds()), 1)
        finally:
            del os.environ["MEMELAB_KOL_WALLETS"]


class TestSeedAndDetect(unittest.IsolatedAsyncioTestCase):
    def test_seed_adds_tagged_wallets(self):
        s = Store(":memory:")
        os.environ["MEMELAB_KOL_WALLETS"] = f"solana:{ADDR}:Ansem"
        try:
            self.assertEqual(seed_kols(s), 1)
            self.assertEqual(s.kol_wallets(Chain.SOLANA), {ADDR.lower(): "Ansem"})
            self.assertTrue(s.is_smart_wallet(Chain.SOLANA, ADDR))   # also smart money
            self.assertEqual(seed_kols(s), 0)                        # idempotent
        finally:
            del os.environ["MEMELAB_KOL_WALLETS"]
            s.close()

    async def test_annotate_flags_kol_buyer(self):
        s = Store(":memory:")
        try:
            sm = SmartMoney(s, session=None, seeds=[])
            s.add_smart_wallet(Chain.SOLANA, ADDR, source="kol:Ansem")
            async def fake_buyers(chain, token):
                return [ADDR.lower()]
            sm._buyers = fake_buyers  # type: ignore
            snap = TokenSnapshot(chain=Chain.SOLANA, token_address="0xtok")
            await sm.annotate(snap)
            self.assertEqual(snap.kol_buyer, "Ansem")
        finally:
            s.close()


if __name__ == "__main__":
    unittest.main()
