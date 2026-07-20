"""Buyer-set hygiene: infra never counts as a buyer; entry price backfills.

The smart-money signal is only as good as its inputs: routers/pairs/contracts
must never be promoted to "smart wallets", Solana must track owner wallets (not
per-mint token accounts), and adapter-discovered tokens must gain their entry
anchor from the first PRICED snapshot.
"""

from __future__ import annotations

import unittest

from memelab.models import Chain, TokenSnapshot
from memelab.smartmoney import SmartMoney, _EVM_ROUTERS
from memelab.storage import Store

UNI_ROUTER = "0x7a250d5630b4cf539739df2c5dacb4c659f2488d"
PAIR = "0x" + "9" * 40
TOKEN = "0x" + "a" * 40
HUMAN1 = "0x" + "1" * 40
HUMAN2 = "0x" + "2" * 40


class _Resp:
    def __init__(self, payload):
        self._p, self.status = payload, 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        return self._p


class _Session:
    def __init__(self, payload):
        self._payload = payload

    def get(self, url, params=None):
        return _Resp(self._payload)


def _transfer(to_hash, is_contract=False):
    return {"to": {"hash": to_hash, "is_contract": is_contract}}


class TestEvmBuyerFiltering(unittest.IsolatedAsyncioTestCase):
    async def test_contracts_routers_pair_and_token_excluded(self):
        s = Store(":memory:")
        try:
            # the pair is known via the token row
            s.record_snapshot(TokenSnapshot(chain=Chain.BASE, token_address=TOKEN,
                                            pair_address=PAIR, ts=1.0, price_usd=1.0))
            payload = {"items": [
                _transfer(HUMAN1),
                _transfer(UNI_ROUTER),                 # curated router
                _transfer(PAIR),                       # the AMM pool (a sell!)
                _transfer(TOKEN),                      # the token contract
                _transfer("0x" + "c" * 40, is_contract=True),   # flagged contract
                _transfer(HUMAN2),
            ]}
            sm = SmartMoney(s, session=_Session(payload), seeds=[])
            got = await sm._evm_receivers(Chain.BASE, TOKEN, newest_first=True)
            self.assertEqual(got, [HUMAN1, HUMAN2])
        finally:
            s.close()

    def test_router_set_is_lowercase(self):
        for a in _EVM_ROUTERS:
            self.assertEqual(a, a.lower())


class TestSolanaOwnerWallets(unittest.IsolatedAsyncioTestCase):
    async def test_owner_preferred_and_infra_filtered(self):
        payload = {"topHolders": [
            {"address": "ATAaccount111", "owner": "HumanWallet111"},
            {"address": "ATAaccount222", "owner": "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1"},  # Raydium
            {"address": "BareAccount333"},               # no owner -> fall back
        ]}
        s = Store(":memory:")
        try:
            sm = SmartMoney(s, session=_Session(payload), seeds=[])
            got = await sm._solana_holders("MINT")
            self.assertEqual(got, ["humanwallet111", "bareaccount333"])
        finally:
            s.close()


class TestEntryBackfill(unittest.TestCase):
    def test_first_priced_snapshot_becomes_entry(self):
        s = Store(":memory:")
        try:
            # adapter stub: discovered with no price
            s.record_snapshot(TokenSnapshot(chain=Chain.BASE, token_address=TOKEN,
                                            pair_address="", ts=100.0, price_usd=None))
            # first priced snapshot backfills entry + pair
            s.record_snapshot(TokenSnapshot(chain=Chain.BASE, token_address=TOKEN,
                                            pair_address=PAIR, ts=200.0, price_usd=2.0))
            # a later pump computes the multiple off the backfilled entry
            s.record_snapshot(TokenSnapshot(chain=Chain.BASE, token_address=TOKEN,
                                            pair_address=PAIR, ts=300.0, price_usd=6.0))
            ts_ = s.time_series(Chain.BASE, TOKEN)
            self.assertEqual(ts_.entry_price, 2.0)
            self.assertAlmostEqual(ts_.peak_multiple, 3.0, places=6)
            self.assertEqual(s.pair_address(Chain.BASE, TOKEN), PAIR)
        finally:
            s.close()


if __name__ == "__main__":
    unittest.main()
