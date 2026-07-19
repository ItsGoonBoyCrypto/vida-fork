"""Contract-level rug detection: dangerous functions + wash-trading."""

from __future__ import annotations

import unittest

from rhl2_scanner.contract_audit import audit_functions, risk_summary, wash_trade
from rhl2_scanner.models import TokenSnapshot


class TestAuditFunctions(unittest.TestCase):
    def test_flags_critical_hooks(self):
        a = audit_functions(["transfer", "mint", "setBlacklist", "pause", "balanceOf"])
        self.assertTrue(any("mint" in x for x in a["critical"]))
        self.assertTrue(any("blacklist" in x for x in a["critical"]))
        self.assertTrue(any("pause" in x.lower() or "freeze" in x.lower()
                            for x in a["critical"]))

    def test_flags_owner_fee_setters_as_warning(self):
        a = audit_functions(["setTaxes", "setMaxTxAmount", "transfer"])
        self.assertTrue(a["warning"])
        self.assertFalse(a["critical"])

    def test_clean_contract(self):
        a = audit_functions(["transfer", "approve", "balanceOf", "renounceOwnership"])
        self.assertEqual(a["critical"], [])
        self.assertEqual(a["warning"], [])
        self.assertTrue(a["has_renounce"])

    def test_risk_summary_verdict(self):
        crit = audit_functions(["mint"])
        v, findings = risk_summary(crit, (False, ""))
        self.assertIn("HIGH RISK", v)
        clean = audit_functions(["transfer"])
        v2, _ = risk_summary(clean, (False, ""))
        self.assertIn("no contract red flags", v2)


class TestWashTrade(unittest.TestCase):
    def _snap(self, vol, mcap, hg):
        return TokenSnapshot(chain="robinhood", pair_address="0xp", token_address="0xt",
                             volume_1h=vol, market_cap_usd=mcap, holder_growth_1h=hg)

    def test_detects_wash(self):
        fire, why = wash_trade(self._snap(vol=120000, mcap=100000, hg=1))  # 1.2x, +1
        self.assertTrue(fire)
        self.assertIn("wash", why)

    def test_real_demand_not_flagged(self):
        fire, _ = wash_trade(self._snap(vol=120000, mcap=100000, hg=40))   # holders growing
        self.assertFalse(fire)

    def test_low_turnover_not_flagged(self):
        fire, _ = wash_trade(self._snap(vol=5000, mcap=100000, hg=0))      # 0.05x
        self.assertFalse(fire)

    def test_missing_data_safe(self):
        fire, _ = wash_trade(self._snap(vol=None, mcap=100000, hg=1))
        self.assertFalse(fire)


if __name__ == "__main__":
    unittest.main()
