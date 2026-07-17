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
        # 60% is in the graduation-imminent sweet spot (55-92%)
        sweet = score_discovery(self._snap(launchpad="flap", curve_progress_pct=60),
                                cfg.thresholds, w)
        self.assertTrue(any("graduation imminent" in r for r in sweet.reasons))
        # 30% is pre-grad but below the sweet spot → lower discovery raw
        early = score_discovery(self._snap(launchpad="flap", curve_progress_pct=30),
                                cfg.thresholds, w)
        self.assertTrue(any("pre-grad" in r for r in early.reasons))
        self.assertGreater(sweet.raw, early.raw)


NONVANITY = "0x8f6761371669509bdc457875c500f9bb5bd10aa9"  # $meow — no 8888/7777
VANITY = "0x1234567890123456789012345678901234567777"      # carries the suffix
TRADER = "0x9999999999999999999999999999999999999999"


class _Resp:
    def __init__(self, body, status=200):
        self._b, self.status = body, status
    async def json(self):
        return self._b
    async def __aenter__(self):
        return self
    async def __aexit__(self, *a):
        return False


def _word(n: int) -> str:
    return f"{n:064x}"


class _FakeSession:
    """Serves eth_blockNumber / eth_getLogs / eth_call for the listener."""
    def __init__(self, logs, flap_status):
        self.logs = logs                    # list of log entries eth_getLogs returns
        self.flap_status = flap_status      # {addr_lower: status_int} for getTokenV2
        self.calls = []                     # eth_call target addresses (for budget asserts)

    def post(self, url, json):
        m = json["method"]
        if m == "eth_blockNumber":
            return _Resp({"result": hex(1000)})
        if m == "eth_getLogs":
            return _Resp({"result": self.logs})
        if m == "eth_call":
            data = json["params"][0]["data"]
            addr = "0x" + data[-40:]
            self.calls.append(addr.lower())
            status = self.flap_status.get(addr.lower())
            if status is None:
                return _Resp({"result": "0x"})     # not a flap token → reverts/empty
            return _Resp({"result": "0x" + _word(status) + _word(0) * 6})
        return _Resp({"result": None})


class TestNonVanityConfirm(unittest.IsolatedAsyncioTestCase):
    def _listener(self, logs, flap_status):
        cfg = Config()
        cfg.chain.rpc_url = "http://rpc"
        cfg._launchpad("flap")["manager"] = MANAGER
        cfg._launchpad("flap")["token_suffixes"] = ["8888", "7777"]
        cfg.chain.curve_confirm_budget = 10
        lis = LaunchpadCurveListener(cfg, session=_FakeSession(logs, flap_status))
        lis._last_block = 999   # so from_block=1000=head, one small range
        return lis

    async def test_nonvanity_token_confirmed_and_emitted(self):
        # log carries a non-vanity token + a trader; only the token is a flap token
        log = {"topics": ["0xcreate", _pad(NONVANITY)], "data": _pad(TRADER)}
        lis = self._listener([log], {NONVANITY.lower(): 1})  # trader unknown → not flap
        snaps = await lis.poll_new_launches()
        addrs = [s.token_address.lower() for s in snaps]
        self.assertIn(NONVANITY.lower(), addrs)          # caught despite no suffix
        self.assertNotIn(TRADER.lower(), addrs)          # trader rejected by getTokenV2

    async def test_vanity_token_needs_no_confirmation(self):
        log = {"topics": ["0xcreate", _pad(VANITY)], "data": _pad(TRADER)}
        lis = self._listener([log], {})                  # getTokenV2 would say 'not flap'
        snaps = await lis.poll_new_launches()
        addrs = [s.token_address.lower() for s in snaps]
        self.assertIn(VANITY.lower(), addrs)             # suffix path, free
        # the vanity token was emitted without a getTokenV2 call for it
        self.assertNotIn(VANITY.lower(), lis._session.calls)

    async def test_budget_bounds_confirmations(self):
        log = {"topics": ["0xcreate", _pad(NONVANITY), _pad(TRADER),
                           _pad("0x" + "a" * 40)], "data": "0x"}
        lis = self._listener([log], {NONVANITY.lower(): 1})
        lis.cfg.chain.curve_confirm_budget = 1           # only one getTokenV2 allowed
        await lis.poll_new_launches()
        self.assertEqual(len(lis._session.calls), 1)     # budget respected

    async def test_checked_cache_prevents_recheck(self):
        log = {"topics": ["0xcreate", _pad(TRADER)], "data": "0x"}
        lis = self._listener([log], {})                  # trader is not a flap token
        await lis.poll_new_launches()
        first = len(lis._session.calls)
        lis._last_block = 999                            # re-scan same block window
        await lis.poll_new_launches()
        self.assertEqual(len(lis._session.calls), first)  # cached → not re-called


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
