"""Robustness rails: pool-listener never skips a failed block range, the sell-sim
doesn't misread 'no liquidity' as a honeypot, and DexScreener surfaces rate-limit
health."""

from __future__ import annotations

import unittest

from rhl2_scanner.config import Config
from rhl2_scanner.simulator import HoneypotSimulator
from rhl2_scanner.sources.poollistener import PoolListener


class _Resp:
    def __init__(self, payload, status=200):
        self._p, self.status = payload, status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        return self._p


class _RpcSession:
    """Scripts eth_blockNumber then eth_getLogs; getLogs can fail mid-range."""

    def __init__(self, head, logs_by_range, fail_from=None):
        self.head, self.logs_by_range, self.fail_from = head, logs_by_range, fail_from
        self.calls = []

    def post(self, url, json=None):
        m = json["method"]
        if m == "eth_blockNumber":
            return _Resp({"result": hex(self.head)})
        if m == "eth_getLogs":
            p = json["params"][0]
            frm = int(p["fromBlock"], 16)
            self.calls.append(frm)
            if self.fail_from is not None and frm >= self.fail_from:
                return _Resp({"error": {"message": "rate limited"}})   # RPC failure
            return _Resp({"result": self.logs_by_range.get(frm, [])})
        return _Resp({"result": None})

    async def close(self):
        pass


class TestPoolListenerGap(unittest.IsolatedAsyncioTestCase):
    def _cfg(self):
        cfg = Config()
        cfg.chain.rpc_url = "http://node"
        cfg.chain.dex_factory_address = "0x" + "f" * 40
        cfg.chain.dex_factory_kind = "univ2"
        cfg.chain.pool_scan_block_lookback = 10
        cfg.chain.pool_scan_max_range = 5
        return cfg

    async def test_checkpoint_held_at_failed_range(self):
        # head=20, lookback 10 -> scan from 11; chunks 11-15 ok, 16-20 fails.
        # Checkpoint must stop at 15 so 16-20 retries next cycle (not skipped).
        cfg = self._cfg()
        sess = _RpcSession(head=20, logs_by_range={11: []}, fail_from=16)
        pl = PoolListener(cfg, session=sess)
        persisted = {}
        pl.set_checkpoint(None, on_advance=lambda b: persisted.__setitem__("b", b))
        await pl.poll_new_pairs()
        self.assertEqual(pl._last_block, 15)          # not advanced to head=20
        self.assertEqual(persisted.get("b"), 15)      # checkpoint persisted

    async def test_resume_from_persisted_checkpoint(self):
        cfg = self._cfg()
        sess = _RpcSession(head=30, logs_by_range={26: []})
        pl = PoolListener(cfg, session=sess)
        pl.set_checkpoint(25)                          # resume, don't rescan lookback
        await pl.poll_new_pairs()
        self.assertEqual(sess.calls[0], 26)           # started right after 25


class _BodySession:
    """Returns the JSON-RPC body dict a handler produces for each call's calldata."""

    def __init__(self, handler):
        self.handler = handler

    def post(self, url, json=None):
        data = json["params"][0]["data"]
        return _Resp(self.handler(data))

    async def close(self):
        pass


class TestSellSimNoPool(unittest.IsolatedAsyncioTestCase):
    async def test_insufficient_liquidity_is_not_a_revert(self):
        cfg = Config()
        cfg.chain.rpc_url = "http://node"
        cfg.chain.dex_router_address = "0x" + "1" * 40
        cfg.chain.weth_address = "0x" + "2" * 40
        cfg.chain.dex_router_kind = "univ2"

        def handler(data):
            # slot probes succeed; the swap "reverts" with INSUFFICIENT_LIQUIDITY,
            # which must read as no-pool (unconfirmed), NOT as a honeypot.
            if data.startswith("0x70a08231") or data.startswith("0xdd62ed3e"):
                return {"result": "0x" + f"{10**30:064x}"}
            if data.startswith("0x313ce567"):
                return {"result": "0x" + f"{18:064x}"}
            return {"error": {"message": "execution reverted: INSUFFICIENT_LIQUIDITY"}}

        from rhl2_scanner.models import TokenSnapshot
        sim = HoneypotSimulator(cfg, session=_BodySession(handler))
        snap = TokenSnapshot(chain="robinhood", pair_address="0xp", token_address="0x" + "a" * 40)
        report = await sim.check(snap)
        self.assertIsNone(report.is_honeypot)      # no pool -> unconfirmed, NOT honeypot


if __name__ == "__main__":
    unittest.main()
