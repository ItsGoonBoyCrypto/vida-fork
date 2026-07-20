"""Owner-mutability audit: bytecode selector scan + owner liveness + gating.

Covers the "clean now, flips the tax later" vector — a token whose owner hasn't
renounced and whose bytecode exposes an owner-only tax/blacklist/pause/mint hook.
"""

from __future__ import annotations

import unittest

from rhl2_scanner.config import Config
from rhl2_scanner.filters import safety_gate
from rhl2_scanner.models import SafetyReport, TokenSnapshot
from rhl2_scanner.owner_audit import _selector, owner_is_active, scan_bytecode

BURN = {"0x000000000000000000000000000000000000dead",
        "0x0000000000000000000000000000000000000000"}


def _code(*sigs: str) -> str:
    return "0x6080604052" + "".join(_selector(s) for s in sigs) + "00"


class TestScanBytecode(unittest.TestCase):
    def test_tax_setter_is_rug_capable(self):
        scan = scan_bytecode(_code("setFees(uint256,uint256)"))
        self.assertIn("tax", scan["categories"])
        self.assertTrue(scan["can_rug"])

    def test_blacklist_is_rug_capable(self):
        scan = scan_bytecode(_code("blacklist(address,bool)"))
        self.assertTrue(scan["can_rug"])
        self.assertIn("owner can blacklist wallets", scan["hooks"])

    def test_limits_only_is_not_a_gate(self):
        # max-tx alone is a soft signal — surfaced, but not a rug gate on its own
        scan = scan_bytecode(_code("setMaxTx(uint256)"))
        self.assertEqual(scan["categories"], {"limits"})
        self.assertFalse(scan["can_rug"])

    def test_clean_bytecode_has_no_hooks(self):
        scan = scan_bytecode("0x6080604052348015600f57600080fd00")
        self.assertEqual(scan["hooks"], [])
        self.assertFalse(scan["can_rug"])

    def test_empty_code_is_safe_default(self):
        for empty in ("", "0x", None):
            self.assertFalse(scan_bytecode(empty)["can_rug"])


class TestOwnerLiveness(unittest.TestCase):
    def test_renounced_addresses_are_inactive(self):
        self.assertIs(owner_is_active("0x" + "0" * 40, BURN), False)
        self.assertIs(owner_is_active("0x000000000000000000000000000000000000dEaD", BURN), False)

    def test_live_eoa_is_active(self):
        self.assertIs(owner_is_active("0x" + "1" * 40, BURN), True)

    def test_no_owner_is_unknown(self):
        self.assertIsNone(owner_is_active(None, BURN))
        self.assertIsNone(owner_is_active("", BURN))


class TestOwnerRugGate(unittest.TestCase):
    def setUp(self):
        self.th = Config().thresholds

    def _snap(self, **kw):
        s = TokenSnapshot(chain="robinhood", pair_address="0xp", token_address="0xt")
        s.safety = SafetyReport(**kw)
        return s

    def test_confirmed_owner_rug_is_gated_even_pragmatic(self):
        snap = self._snap(owner_can_rug=True, owner_hooks=["owner can set fees"])
        g = safety_gate(snap, self.th, strict=False, pragmatic=True)
        self.assertTrue(any("owner can rug" in f for f in g.failures), g.failures)

    def test_gate_can_be_disabled(self):
        self.th.gate_owner_rug = False
        snap = self._snap(owner_can_rug=True, owner_hooks=["owner can set fees"])
        g = safety_gate(snap, self.th, strict=False, pragmatic=True)
        self.assertFalse(any("owner can rug" in f for f in g.failures), g.failures)

    def test_renounced_owner_with_hooks_is_not_gated(self):
        # hooks present but ownership renounced -> capability is inert -> no gate
        snap = self._snap(owner_can_rug=False, owner_active=False,
                          owner_hooks=["owner can set fees"])
        g = safety_gate(snap, self.th, strict=False, pragmatic=True)
        self.assertFalse(any("owner can rug" in f for f in g.failures), g.failures)


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
    """Fake JSON-RPC: returns bytecode for eth_getCode, an owner for owner()."""

    def __init__(self, code, owner):
        self.code, self.owner = code, owner

    def post(self, url, json=None):
        m = json.get("method")
        if m == "eth_getCode":
            res = self.code
        elif m == "eth_call":
            res = "0x" + "0" * 24 + self.owner.replace("0x", "")   # owner() word
        else:
            res = None
        return _Resp({"jsonrpc": "2.0", "id": json.get("id"), "result": res})

    async def close(self):
        pass


class TestChainWiring(unittest.IsolatedAsyncioTestCase):
    async def test_active_owner_plus_tax_hook_flags_can_rug(self):
        from rhl2_scanner.sources.chain import EvmChainClient
        cfg = Config()
        cfg.chain.rpc_url = "http://node"
        owner = "0x" + "1" * 40
        sess = _Session(_code("setFees(uint256,uint256)"), owner)
        client = EvmChainClient(cfg, session=sess)
        report = SafetyReport()
        await client._assess_owner_mutability("0x" + "a" * 40, owner, report)
        self.assertIs(report.owner_active, True)
        self.assertTrue(report.owner_can_rug)
        self.assertIn("owner can set fees", report.owner_hooks)

    async def test_renounced_owner_neutralises_hooks(self):
        from rhl2_scanner.sources.chain import EvmChainClient
        cfg = Config()
        cfg.chain.rpc_url = "http://node"
        dead = "0x000000000000000000000000000000000000dead"
        sess = _Session(_code("setFees(uint256,uint256)"), dead)
        client = EvmChainClient(cfg, session=sess)
        report = SafetyReport()
        await client._assess_owner_mutability("0x" + "a" * 40, dead, report)
        self.assertIs(report.owner_active, False)
        self.assertIs(report.owner_can_rug, False)   # hooks inert once renounced


if __name__ == "__main__":
    unittest.main()
