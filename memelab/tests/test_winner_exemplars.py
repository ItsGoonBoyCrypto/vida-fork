"""Winner exemplars are harvested once into the smart set on startup."""

from __future__ import annotations

import unittest

from memelab.collector import Collector, CollectorConfig
from memelab.models import Chain
from memelab.storage import Store
from memelab.winner_exemplars import load_exemplars


class _Alerter:
    enabled = True

    async def send(self, html, reply_markup=None):
        pass


class TestLoad(unittest.TestCase):
    def test_curated_plus_env(self):
        got = load_exemplars("solana:MINTx=extra, base:0x" + "a" * 40 + "=b")
        vals = {(c.value, t.lower()) for c, t, _ in got}
        self.assertIn(("solana", "2c1kjiyqow66qfsnctoyuqfo3auxgpbmeoaq5oiixqdu"), vals)
        self.assertIn(("solana", "mintx"), vals)
        self.assertIn(("base", "0x" + "a" * 40), vals)

    def test_bad_chain_skipped(self):
        got = load_exemplars("notachain:xyz=k")
        self.assertTrue(all(c.value != "notachain" for c, _, _ in got))


class TestHarvestWiring(unittest.IsolatedAsyncioTestCase):
    async def test_exemplar_harvest_calls_smart_money_once(self):
        store = Store(":memory:")
        col = Collector(CollectorConfig(chains=[Chain.SOLANA], alert_chains=[]),
                        store, {}, None, alerter=_Alerter())

        calls = []

        class _SM:
            async def harvest_winner(self, chain, token, mult=0.0):
                calls.append((chain, token))
                return 5
        col.smart_money = _SM()
        try:
            import os
            from unittest import mock
            with mock.patch.dict(os.environ, {"MEMELAB_WINNER_EXEMPLARS": ""}):
                await col._harvest_exemplars()
            # the seeded Solana runner was harvested
            self.assertTrue(any(t == "2c1KjiyQow66QfsnCtoyuqfo3AuxgpBMEoAq5oiiXqdu"
                                for _, t in calls))
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
