"""BNB Smart Chain onboarding — enum, registry, adapter, safety wiring."""

from __future__ import annotations

import unittest

from memelab.chains.registry import REGISTRY, get_adapter
from memelab.models import Chain


class TestBnbChain(unittest.TestCase):
    def test_enum_and_evm(self):
        self.assertEqual(Chain.BNB.value, "bsc")     # DexScreener slug
        self.assertTrue(Chain.BNB.is_evm)
        self.assertIn(Chain.BNB, list(Chain))

    def test_registry_entry(self):
        cfg = REGISTRY[Chain.BNB]
        self.assertEqual(cfg.dexscreener_slug, "bsc")
        self.assertEqual(cfg.goplus_chain_id, "56")   # GoPlus covers BSC for safety

    def test_adapter_constructs(self):
        # BNB uses the shared EVM adapter (safety via GoPlus)
        adapter = get_adapter(Chain.BNB, session=None)
        self.assertIsNotNone(adapter)

    def test_all_five_chains_present(self):
        vals = {c.value for c in Chain}
        self.assertEqual(vals, {"robinhood", "solana", "ethereum", "base", "bsc"})

    def test_chain_badge_in_alert(self):
        from memelab.alerting import format_core_alpha_html
        html = format_core_alpha_html(Chain.BNB, "GEM", "0xabc", "0x1234…")
        self.assertIn("🟡 BNB", html)                 # colored badge + short name


if __name__ == "__main__":
    unittest.main()
