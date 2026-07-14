"""Tests for the simulator-contract decode path and LP-lock reader.

Pure logic + a fake JSON-RPC session so the eth_call plumbing is exercised
end-to-end without a real node. Run with the suite:
    python -m unittest discover -s rhl2_scanner/tests -v
"""

from __future__ import annotations

import unittest

from rhl2_scanner.config import Config
from rhl2_scanner.keccak import keccak256
from rhl2_scanner.simulator import HoneypotSimulator, _decode_words
from rhl2_scanner.sources.lplock import LpLockReader, _norm_lockers


def _word(x: int) -> str:
    return f"{x:064x}"


class FakeResp:
    def __init__(self, payload):
        self._payload = payload
        self.status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        return self._payload


class FakeSession:
    """Minimal aiohttp-like session: routes eth_call/getBlock by a handler."""

    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def post(self, url, json=None):
        self.calls.append(json)
        result = self.handler(json)
        return FakeResp({"jsonrpc": "2.0", "id": json.get("id"), "result": result})

    def get(self, url, params=None):  # unused here
        return FakeResp({})

    async def close(self):
        pass


class TestDecodeWords(unittest.TestCase):
    def test_decode_five_words(self):
        blob = "0x" + _word(150) + _word(9900) + _word(1000) + _word(500) + _word(1)
        got = _decode_words(blob, 5)
        self.assertEqual(got, (150, 9900, 1000, 500, 1))

    def test_short_blob_returns_none(self):
        self.assertIsNone(_decode_words("0x" + _word(1), 5))


class TestContractSim(unittest.IsolatedAsyncioTestCase):
    def _cfg(self) -> Config:
        cfg = Config()
        cfg.chain.rpc_url = "http://node"
        cfg.chain.dex_router_address = "0x" + "1" * 40
        cfg.chain.weth_address = "0x" + "2" * 40
        cfg.chain.honeypot_simulator_bytecode = "0x60006000"  # dummy; not executed by fake node
        return cfg

    async def test_high_sell_tax_flags_honeypot(self):
        cfg = self._cfg()
        # simulate() returns buyTaxBps=150 (1.5%), sellTaxBps=9900 (99%), ok=1
        ret = "0x" + _word(150) + _word(9900) + _word(1000) + _word(1) + _word(1)

        def handler(payload):
            if payload["method"] == "eth_call":
                return ret
            return None

        from rhl2_scanner.models import TokenSnapshot
        snap = TokenSnapshot(chain="base", pair_address="0xp", token_address="0x" + "a" * 40)
        sim = HoneypotSimulator(cfg, session=FakeSession(handler))
        report = await sim.check(snap)
        self.assertAlmostEqual(report.buy_tax_pct, 1.5, places=2)
        self.assertAlmostEqual(report.sell_tax_pct, 99.0, places=2)
        self.assertFalse(report.is_honeypot)  # ok=1, amounts>0 => technically sellable, tax gate catches 99%

    async def test_zero_out_is_honeypot(self):
        cfg = self._cfg()
        ret = "0x" + _word(0) + _word(0) + _word(1000) + _word(0) + _word(0)  # soldEth=0, ok=0

        def handler(payload):
            return ret if payload["method"] == "eth_call" else None

        from rhl2_scanner.models import TokenSnapshot
        snap = TokenSnapshot(chain="base", pair_address="0xp", token_address="0x" + "a" * 40)
        sim = HoneypotSimulator(cfg, session=FakeSession(handler))
        report = await sim.check(snap)
        self.assertTrue(report.is_honeypot)

    async def test_selector_is_correct(self):
        # Guards the ABI signature used to build calldata.
        sel = keccak256(b"simulate(address,address,address,uint256)")[:4].hex()
        self.assertEqual(len(sel), 8)


class TestV3SellSim(unittest.IsolatedAsyncioTestCase):
    def _cfg(self):
        cfg = Config()
        cfg.chain.rpc_url = "http://node"
        cfg.chain.dex_router_address = "0x" + "1" * 40
        cfg.chain.weth_address = "0x" + "2" * 40
        cfg.chain.dex_router_kind = "univ3"
        cfg.chain.dex_v3_fee_tiers = [10000, 3000]
        return cfg

    async def test_v3_selector_and_calldata_shape(self):
        # exactInputSingle selector is 0x04e45aaf (SwapRouter02, no deadline).
        sel = keccak256(
            b"exactInputSingle((address,address,uint24,address,uint256,uint256,uint160))"
        )[:4].hex()
        self.assertEqual(sel, "04e45aaf")

    async def test_v3_sell_success_marks_not_honeypot(self):
        cfg = self._cfg()
        calls = {"n": 0}

        def handler(payload):
            m = payload["method"]
            if m == "eth_call":
                data = payload["params"][0]["data"]
                # balance/allowance slot probes return the BIG override; decimals; swap ok
                if data.startswith("0x70a08231") or data.startswith("0xdd62ed3e"):
                    return "0x" + f"{10**30:064x}"      # slot found immediately
                if data.startswith("0x313ce567"):
                    return "0x" + f"{18:064x}"           # decimals
                if data.startswith("0x04e45aaf"):
                    calls["n"] += 1
                    return "0x" + f"{5:064x}"            # swap returns amountOut -> sellable
            return "0x"

        from rhl2_scanner.models import TokenSnapshot
        snap = TokenSnapshot(chain="robinhood", pair_address="0xp", token_address="0x" + "a" * 40)
        sim = HoneypotSimulator(cfg, session=FakeSession(handler))
        report = await sim.check(snap)
        self.assertFalse(report.is_honeypot)   # a V3 sell succeeded
        self.assertGreaterEqual(calls["n"], 1)


class TestLpLock(unittest.IsolatedAsyncioTestCase):
    def test_norm_lockers_merges_both_shapes(self):
        cfg = Config()
        cfg.chain.lp_lockers = [{"address": "0xAAA", "unlock_selector": "0x1234"}]
        cfg.chain.lp_locker_addresses = ["0xBBB", "0xAAA"]  # 0xAAA is a dupe
        out = _norm_lockers(cfg)
        addrs = [l["address"].lower() for l in out]
        self.assertIn("0xaaa", addrs)
        self.assertIn("0xbbb", addrs)
        self.assertEqual(len(out), 2)  # dupe collapsed

    async def test_reads_lock_and_duration(self):
        cfg = Config()
        cfg.chain.rpc_url = "http://node"
        locker = "0x" + "c" * 40
        cfg.chain.lp_lockers = [{"address": locker, "unlock_selector": "0xabcdef01", "arg": "lp"}]
        cfg.chain.lp_lock_min_fraction = 0.5

        pair = "0x" + "p" * 39 + "1"
        total_supply = 1000
        now_ts = 1_700_000_000
        unlock_ts = now_ts + 45 * 24 * 3600   # 45 days out

        def handler(payload):
            m = payload["method"]
            if m == "eth_getBlockByNumber":
                return {"timestamp": hex(now_ts)}
            if m == "eth_call":
                to = payload["params"][0]["to"].lower()
                data = payload["params"][0]["data"]
                if to == pair.lower() and data == "0x18160ddd":          # totalSupply
                    return "0x" + _word(total_supply)
                if to == pair.lower() and data.startswith("0x70a08231"):  # balanceOf(locker)
                    return "0x" + _word(900)                              # 90% locked
                if to == locker.lower() and data.startswith("0xabcdef01"):
                    return "0x" + _word(unlock_ts)
            return None

        reader = LpLockReader(cfg, session=FakeSession(handler))
        locked, remaining = await reader.read(pair)
        self.assertTrue(locked)
        self.assertIsNotNone(remaining)
        self.assertGreater(remaining, 44 * 24 * 3600)
        self.assertLessEqual(remaining, 45 * 24 * 3600)

    async def test_unlocked_when_below_fraction(self):
        cfg = Config()
        cfg.chain.rpc_url = "http://node"
        locker = "0x" + "c" * 40
        cfg.chain.lp_lockers = [{"address": locker}]
        pair = "0x" + "p" * 39 + "1"

        def handler(payload):
            m = payload["method"]
            if m == "eth_getBlockByNumber":
                return {"timestamp": hex(1_700_000_000)}
            if m == "eth_call":
                data = payload["params"][0]["data"]
                if data == "0x18160ddd":
                    return "0x" + _word(1000)
                if data.startswith("0x70a08231"):
                    return "0x" + _word(100)   # only 10% at locker
            return None

        reader = LpLockReader(cfg, session=FakeSession(handler))
        locked, remaining = await reader.read(pair)
        self.assertIsNone(locked)
        self.assertIsNone(remaining)


class TestLaunchpadLp(unittest.IsolatedAsyncioTestCase):
    def _cfg(self):
        cfg = Config()
        cfg.chain.rpc_url = "http://node"
        cfg.chain.launchpad_factory_address = "0x" + "f" * 40
        cfg.chain.nft_position_manager = "0x" + "e" * 40
        return cfg

    def _struct(self, deployer: str, position_id: int, exists: int = 1) -> str:
        w = ["0" * 64] * 13
        w[1] = _word_addr(deployer)         # deployer
        w[3] = "0" * 64                      # positionManager (0 -> use cfg NPM)
        w[4] = f"{position_id:064x}"         # positionId
        w[11] = f"{exists:064x}"             # exists
        return "0x" + "".join(w)

    async def _run(self, owner: str, deployer: str, exists: int = 1):
        from rhl2_scanner.sources.chain import (
            EvmChainClient, _SEL_GET_LAUNCHED, _SEL_OWNER_OF)
        from rhl2_scanner.models import SafetyReport, TokenSnapshot

        def handler(payload):
            data = payload["params"][0]["data"]
            if data.startswith("0x" + _SEL_GET_LAUNCHED):
                return self._struct(deployer, 42, exists)
            if data.startswith("0x" + _SEL_OWNER_OF):
                return "0x" + _word_addr(owner)
            return "0x"

        client = EvmChainClient(self._cfg(), session=FakeSession(handler))
        snap = TokenSnapshot(chain="robinhood", pair_address="0xp", token_address="0x" + "a" * 40)
        report = SafetyReport()
        await client._check_launchpad_lp(snap, report)
        return report

    async def test_deployer_holds_lp_is_unsafe(self):
        dep = "0x" + "d" * 40
        r = await self._run(owner=dep, deployer=dep)
        self.assertIs(r.lp_locked, False)          # deployer can pull -> rug risk

    async def test_burned_lp_is_safe(self):
        r = await self._run(owner="0x000000000000000000000000000000000000dEaD", deployer="0x" + "d" * 40)
        self.assertIs(r.lp_burned, True)

    async def test_protocol_held_is_locked(self):
        r = await self._run(owner="0x" + "c" * 40, deployer="0x" + "d" * 40)
        self.assertIs(r.lp_locked, True)

    async def test_non_launchpad_token_left_unknown(self):
        r = await self._run(owner="0x" + "c" * 40, deployer="0x" + "d" * 40, exists=0)
        self.assertIsNone(r.lp_locked)
        self.assertIsNone(r.lp_burned)


def _word_addr(addr: str) -> str:
    return addr.lower().replace("0x", "").rjust(64, "0")


if __name__ == "__main__":
    unittest.main()
