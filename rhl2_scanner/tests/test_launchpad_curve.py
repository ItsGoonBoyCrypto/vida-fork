"""Tests for flap.sh bonding-curve launchpad wiring + discovery boost."""

from __future__ import annotations

import unittest

from rhl2_scanner.config import Config
from rhl2_scanner.models import SafetyReport, TokenSnapshot
from rhl2_scanner.scoring import score_discovery
from rhl2_scanner.sources.launchpad_curve import (
    LaunchpadCurveListener,
    _candidate_addresses,
    _extract_token,
)

MANAGER = "0x1111111111111111111111111111111111111111"
TOKEN = "0x2222222222222222222222222222222222222222"
DEPLOYER = "0x3333333333333333333333333333333333333333"


def _pad(addr: str) -> str:
    return "0x" + "0" * 24 + addr[2:]


class TestConfigWiring(unittest.TestCase):
    def test_flap_entry_present_and_inert_by_default(self):
        cfg = Config()
        flap = cfg._launchpad("flap")
        self.assertIsNotNone(flap)
        self.assertEqual(flap["kind"], "curve")
        # No manager set -> not "configured", listener disabled.
        self.assertEqual(cfg.configured_launchpads(), [])
        self.assertEqual(cfg.known_launchpad_addresses(), {})

    def test_env_sets_manager(self):
        import os
        os.environ["RHL2_FLAP_MANAGER"] = MANAGER
        os.environ["RHL2_FLAP_CREATE_TOPIC"] = "0xabc"
        try:
            cfg = Config.load(None)
            flap = cfg._launchpad("flap")
            self.assertEqual(flap["manager"], MANAGER)
            self.assertEqual(flap["create_topic"], "0xabc")
            self.assertEqual(cfg.known_launchpad_addresses(),
                             {MANAGER.lower(): "flap"})
        finally:
            del os.environ["RHL2_FLAP_MANAGER"]
            del os.environ["RHL2_FLAP_CREATE_TOPIC"]


class TestDecode(unittest.TestCase):
    def test_extract_token_topic1(self):
        entry = {"topics": ["0xcreate", _pad(TOKEN), _pad(DEPLOYER)], "data": "0x"}
        self.assertEqual(_extract_token(entry, "topic1").lower(), TOKEN.lower())
        self.assertEqual(_extract_token(entry, "topic2").lower(), DEPLOYER.lower())

    def test_extract_token_data0(self):
        entry = {"topics": ["0xcreate"], "data": _pad(TOKEN)}
        self.assertEqual(_extract_token(entry, "data0").lower(), TOKEN.lower())

    def test_extract_token_out_of_range(self):
        entry = {"topics": ["0xcreate"], "data": "0x"}
        self.assertIsNone(_extract_token(entry, "topic1"))

    def test_candidate_addresses(self):
        entry = {"topics": ["0xcreate", _pad(TOKEN)], "data": _pad(DEPLOYER)}
        cands = [c.lower() for c in _candidate_addresses(entry)]
        self.assertIn(TOKEN.lower(), cands)
        self.assertIn(DEPLOYER.lower(), cands)


class TestListenerEnable(unittest.TestCase):
    def test_disabled_without_manager(self):
        cfg = Config()
        cfg.chain.rpc_url = "http://rpc"
        self.assertFalse(LaunchpadCurveListener(cfg).enabled())

    def test_enabled_with_manager_and_rpc(self):
        cfg = Config()
        cfg.chain.rpc_url = "http://rpc"
        cfg._launchpad("flap")["manager"] = MANAGER
        self.assertTrue(LaunchpadCurveListener(cfg).enabled())


class TestDiscoveryBoost(unittest.TestCase):
    def _snap(self, **over):
        s = TokenSnapshot(chain="robinhood", pair_address="0xp",
                          token_address=TOKEN, age_minutes=10)
        s.safety = SafetyReport(contract_verified=True)
        for k, v in over.items():
            setattr(s, k, v)
        return s

    def test_launchpad_origin_boosts_discovery(self):
        cfg = Config()
        w = cfg.weights.normalized().discovery
        plain = score_discovery(self._snap(), cfg.thresholds, w)
        flap = score_discovery(self._snap(launchpad="flap"), cfg.thresholds, w)
        self.assertGreater(flap.raw, plain.raw)
        self.assertTrue(any("via flap" in r for r in flap.reasons))

    def test_curve_pregrad_sweetspot_boost(self):
        cfg = Config()
        w = cfg.weights.normalized().discovery
        sweet = score_discovery(self._snap(launchpad="flap", curve_progress_pct=60),
                                cfg.thresholds, w)
        self.assertTrue(any("pre-grad" in r for r in sweet.reasons))


class TestAddrNormalization(unittest.TestCase):
    def test_norm_addr_adds_prefix_and_lowercases(self):
        from rhl2_scanner.sources.launchpad_curve import _norm_addr as nc
        from rhl2_scanner.sources.poollistener import _norm_addr as np
        for fn in (nc, np):
            self.assertEqual(fn("1f7D7550b1B028"), "0x1f7d7550b1b028")   # missing 0x
            self.assertEqual(fn("0x1F7d7550"), "0x1f7d7550")             # checksum -> lower
            self.assertEqual(fn("  0xABC  "), "0xabc")                   # trimmed
            self.assertEqual(fn(""), "")                                 # empty stays empty


if __name__ == "__main__":
    unittest.main()
