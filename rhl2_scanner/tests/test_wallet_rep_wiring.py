"""End-to-end wiring of the wallet-reputation layer: scoring bonus, toxic
demotion, core-alpha alert, and the /alpha /harvestrug commands."""

from __future__ import annotations

import unittest

from rhl2_scanner.config import Config
from rhl2_scanner.models import TokenSnapshot
from rhl2_scanner.scanner import Scanner
from rhl2_scanner.scoring import score_discovery


def _sc():
    cfg = Config()
    cfg.runtime.db_path = ":memory:"
    return Scanner(cfg)


class TestScoringUsesReputation(unittest.TestCase):
    def test_quality_bonus_beats_headcount_for_proven_wallets(self):
        th = Config().thresholds
        # A token with 2 proven sharps (high quality bonus) should out-score the
        # flat-headcount path that a raw pair of unknown wallets would get.
        proven = TokenSnapshot(chain="robinhood", pair_address="0xp", token_address="0xt",
                               smart_money_wallets=["0xa", "0xb"],
                               smart_money_quality_bonus=40.0,
                               core_alpha_wallets=["0xa", "0xb"])
        flat = TokenSnapshot(chain="robinhood", pair_address="0xp2", token_address="0xt2",
                             smart_money_wallets=["0xa", "0xb"])  # no bonus → headcount
        self.assertGreater(score_discovery(proven, th, 1.0).raw,
                           score_discovery(flat, th, 1.0).raw)

    def test_toxic_buyer_demotes(self):
        th = Config().thresholds
        clean = TokenSnapshot(chain="robinhood", pair_address="0xp", token_address="0xt",
                              smart_money_wallets=["0xa"], smart_money_quality_bonus=20.0)
        toxic = TokenSnapshot(chain="robinhood", pair_address="0xp", token_address="0xt",
                              smart_money_wallets=["0xa"], smart_money_quality_bonus=20.0,
                              toxic_buyer=True)
        self.assertLess(score_discovery(toxic, th, 1.0).raw,
                        score_discovery(clean, th, 1.0).raw)


class TestReputationCommands(unittest.IsolatedAsyncioTestCase):
    async def test_alpha_command(self):
        sc = _sc()
        sent = []
        async def fake_send(html, reply_to=None):
            sent.append(html)
        sc._send_html = fake_send  # type: ignore
        try:
            for tok in ("0xw1", "0xw2", "0xw3"):
                sc.storage.record_wallet_winner("0xSHARP", tok, mult=6.0)
            await sc._handle_command("/alpha")
            self.assertTrue(any("winner-overlap" in h for h in sent))
            self.assertTrue(any("0xsharp"[:8] in h.lower() for h in sent))
        finally:
            sc.storage.close()

    async def test_harvestrug_marks_toxic(self):
        sc = _sc()
        sent = []
        async def fake_send(html, reply_to=None):
            sent.append(html)
        sc._send_html = fake_send  # type: ignore
        # stub the deployer lookup (no session) — record a rug directly
        async def fake_dep(token, outcome):
            sc.storage.record_deployer_token("0xdev", token, outcome)
        sc._label_deployer = fake_dep  # type: ignore
        try:
            await sc._handle_command("/harvestrug 0x" + "e" * 40)
            self.assertTrue(any("Logged rug" in h for h in sent))
            # the rug outcome was recorded
            self.assertEqual(sc.storage.deployer_reputation("0xdev")["rugs"], 1)
        finally:
            sc.storage.close()

    async def test_core_alpha_alert_fires_on_single_wallet(self):
        sc = _sc()
        sent = []
        async def fake_send(html, reply_to=None):
            sent.append(html)
        sc._send_html = fake_send  # type: ignore
        # make 0xSHARP core-alpha (>= default min_overlap winners)
        for i in range(sc.cfg.runtime.core_alpha_min_overlap):
            sc.storage.record_wallet_winner("0xsharp", f"0xw{i}", mult=5.0)

        class _Ev:
            wallet = "0xsharp"
            token_address = "0xnewtoken"
            symbol = "NEW"
            chart_url = ""
        try:
            await sc._check_cluster(_Ev())
            self.assertTrue(any("CORE ALPHA BUY" in h for h in sent), sent)
            # dedup: second buy of same token doesn't re-alert
            sent.clear()
            await sc._check_cluster(_Ev())
            self.assertFalse(any("CORE ALPHA BUY" in h for h in sent))
        finally:
            sc.storage.close()


if __name__ == "__main__":
    unittest.main()
