"""Tests for cross-chain smart-money: seed, membership, annotate, harvest."""

from __future__ import annotations

import unittest

from memelab.models import Chain, TokenSnapshot
from memelab.storage import Store
from memelab.smartmoney import SmartMoney, default_seeds, _RH_SEED

W1 = "0x" + "1" * 40
W2 = "0x" + "2" * 40
TOKEN = "0x" + "a" * 40


class TestSeedAndSet(unittest.TestCase):
    def test_rh_seed_loaded(self):
        s = Store(":memory:")
        try:
            SmartMoney(s, session=None)             # seeds default (RH wallets)
            self.assertEqual(s.smart_wallet_count(Chain.ROBINHOOD), len(_RH_SEED))
            self.assertTrue(s.is_smart_wallet(Chain.ROBINHOOD, _RH_SEED[0].upper()))
            self.assertFalse(s.is_smart_wallet(Chain.BASE, _RH_SEED[0]))
        finally:
            s.close()

    def test_custom_seed(self):
        s = Store(":memory:")
        try:
            SmartMoney(s, session=None, seeds=[(Chain.BASE, W1)])
            self.assertTrue(s.is_smart_wallet(Chain.BASE, W1))
            self.assertEqual(s.smart_wallet_count(Chain.ROBINHOOD), 0)
        finally:
            s.close()

    def test_env_seeds(self):
        import os
        os.environ["MEMELAB_SEED_WALLETS"] = f"base:{W1}, solana:{W2}"
        try:
            seeds = default_seeds()
            self.assertIn((Chain.BASE, W1), seeds)
            self.assertIn((Chain.SOLANA, W2), seeds)
        finally:
            del os.environ["MEMELAB_SEED_WALLETS"]


class TestAnnotateHarvest(unittest.IsolatedAsyncioTestCase):
    async def test_annotate_marks_smart_buyers(self):
        s = Store(":memory:")
        try:
            sm = SmartMoney(s, session=None, seeds=[(Chain.BASE, W1)])
            # stub the buyer source: W1 (smart) + W2 (not) bought the token
            async def fake_buyers(chain, token):
                return [W1, W2]
            sm._buyers = fake_buyers  # type: ignore
            snap = TokenSnapshot(chain=Chain.BASE, token_address=TOKEN)
            await sm.annotate(snap)
            self.assertEqual(snap.smart_money_wallets, [W1])   # only the smart one
        finally:
            s.close()

    async def test_harvest_adds_early_buyers_once(self):
        s = Store(":memory:")
        try:
            sm = SmartMoney(s, session=None, seeds=[])
            async def fake_early(chain, token, n):
                return [W1, W2]
            sm._early_buyers = fake_early  # type: ignore
            added = await sm.harvest_winner(Chain.SOLANA, TOKEN)
            self.assertEqual(added, 2)
            self.assertTrue(s.is_smart_wallet(Chain.SOLANA, W1))
            # dedup: harvesting the same winner again does nothing
            self.assertEqual(await sm.harvest_winner(Chain.SOLANA, TOKEN), 0)
        finally:
            s.close()

    async def test_no_annotate_without_set(self):
        s = Store(":memory:")
        try:
            sm = SmartMoney(s, session=None, seeds=[])   # empty set for BASE
            snap = TokenSnapshot(chain=Chain.BASE, token_address=TOKEN)
            await sm.annotate(snap)                       # no set → no-op, no crash
            self.assertEqual(snap.smart_money_wallets, [])
        finally:
            s.close()


if __name__ == "__main__":
    unittest.main()
