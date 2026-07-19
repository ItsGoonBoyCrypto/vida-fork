"""memelab: toxic/rug demotion, deployer win-rate, core-alpha alert."""

from __future__ import annotations

import unittest

from memelab.collector import Collector, CollectorConfig
from memelab.models import Chain, TokenSnapshot
from memelab.smartmoney import SmartMoney
from memelab.storage import Store

W_TOX = "0x" + "e" * 40
W_ALPHA = "0x" + "1" * 40


class TestToxicAndCoreAlphaLedger(unittest.TestCase):
    def setUp(self):
        self.s = Store(":memory:")

    def tearDown(self):
        self.s.close()

    def test_toxic_needs_repeat_rugs_net_negative(self):
        # one rug → not toxic; two rugs, no winners → toxic
        self.s.record_wallet_rug(Chain.BASE, W_TOX, "0xr1")
        self.assertNotIn(W_TOX.lower(), self.s.toxic_wallets(Chain.BASE, min_rugs=2))
        self.s.record_wallet_rug(Chain.BASE, W_TOX, "0xr2")
        self.assertIn(W_TOX.lower(), self.s.toxic_wallets(Chain.BASE, min_rugs=2))
        # a sharp who once aped a rug but has more winners is NOT toxic
        for t in ("0xw1", "0xw2", "0xw3"):
            self.s.record_wallet_winner(Chain.BASE, W_TOX, t, 4.0)
        self.assertNotIn(W_TOX.lower(), self.s.toxic_wallets(Chain.BASE, min_rugs=2))

    def test_core_alpha_set_per_chain(self):
        for t in ("0xw1", "0xw2", "0xw3"):
            self.s.record_wallet_winner(Chain.SOLANA, W_ALPHA, t, 5.0)
        self.assertIn(W_ALPHA.lower(), self.s.core_alpha_wallets(Chain.SOLANA, 3))
        self.assertNotIn(W_ALPHA.lower(), self.s.core_alpha_wallets(Chain.SOLANA, 4))
        self.assertNotIn(W_ALPHA.lower(), self.s.core_alpha_wallets(Chain.BASE, 3))

    def test_deployer_winrate_and_markers(self):
        self.s.record_deployer_token(Chain.BASE, "0xdev", "0xt1", "winner")
        self.s.record_deployer_token(Chain.BASE, "0xdev", "0xt2", "winner")
        self.s.record_deployer_token(Chain.BASE, "0xdev", "0xt3", "rug")
        top = self.s.top_deployers()
        self.assertEqual(top[0]["deployer"], "0xdev")
        self.assertEqual((top[0]["wins"], top[0]["rugs"], top[0]["total"]), (2, 1, 3))
        self.assertTrue(self.s.marker_new("k1"))
        self.assertFalse(self.s.marker_new("k1"))     # one-shot


class TestAnnotateFlags(unittest.IsolatedAsyncioTestCase):
    async def test_annotate_sets_toxic_and_core_alpha(self):
        s = Store(":memory:")
        try:
            sm = SmartMoney(s, session=None, seeds=[(Chain.BASE, W_ALPHA), (Chain.BASE, W_TOX)],
                            core_alpha_min_overlap=3, toxic_min_rugs=2)
            for t in ("0xw1", "0xw2", "0xw3"):
                s.record_wallet_winner(Chain.BASE, W_ALPHA, t, 5.0)
            s.record_wallet_rug(Chain.BASE, W_TOX, "0xr1")
            s.record_wallet_rug(Chain.BASE, W_TOX, "0xr2")

            async def fake_buyers(chain, token):
                return [W_ALPHA.lower(), W_TOX.lower()]
            sm._buyers = fake_buyers  # type: ignore
            snap = TokenSnapshot(chain=Chain.BASE, token_address="0xtok")
            await sm.annotate(snap)
            self.assertTrue(snap.toxic_buyer)
            self.assertTrue(snap.core_alpha_buyer)     # a proven sharp is present
        finally:
            s.close()

    async def test_harvest_rug_credits_and_dedups(self):
        s = Store(":memory:")
        try:
            sm = SmartMoney(s, session=None, seeds=[])
            async def fake_early(chain, token, n):
                return [W_TOX.lower()]
            sm._early_buyers = fake_early  # type: ignore
            n1 = await sm.harvest_rug(Chain.SOLANA, "0xrug")
            self.assertEqual(n1, 1)
            n2 = await sm.harvest_rug(Chain.SOLANA, "0xrug")   # dedup
            self.assertEqual(n2, 0)
        finally:
            s.close()


class TestCollectorAlertGating(unittest.IsolatedAsyncioTestCase):
    async def test_core_alpha_alert_and_toxic_suppression(self):
        s = Store(":memory:")
        sent = []

        class _Alerter:
            enabled = True
            async def send(self, html, reply_markup=None):
                sent.append(html)

        col = Collector(CollectorConfig(chains=[Chain.BASE]), s, {}, None,
                        alerter=_Alerter())
        try:
            # a core-alpha buyer present → 💎 fires even without a high score
            import time
            snap = TokenSnapshot(chain=Chain.BASE, token_address="0xtok", ts=time.time(),
                                 price_usd=1.0, symbol="GEM", core_alpha_buyer="0x1111…1111")
            s.record_snapshot(snap)
            await col._maybe_alert(Chain.BASE, "0xtok")
            self.assertTrue(any("CORE ALPHA BUY" in h for h in sent), sent)

            # a toxic buyer suppresses the normal signature alert path
            sent.clear()
            tox = TokenSnapshot(chain=Chain.BASE, token_address="0xtox", ts=time.time(),
                                price_usd=1.0, symbol="BAD", toxic_buyer=True)
            s.record_snapshot(tox)
            await col._maybe_alert(Chain.BASE, "0xtox")
            self.assertEqual(sent, [])
        finally:
            s.close()


if __name__ == "__main__":
    unittest.main()
