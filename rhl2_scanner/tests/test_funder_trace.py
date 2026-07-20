"""Funder tracing: blocklist the wallet that funds scam deployers.

Scammers cycle throwaway deployer EOAs but reuse one funding source. When a
honeypot is confirmed we trace the deployer's funder and blocklist it, so the
same funder's next launch is caught before it rugs.
"""

from __future__ import annotations

import unittest

from rhl2_scanner.config import Config
from rhl2_scanner.models import (AlertLevel, CategoryScore, SafetyReport,
                                 ScoreResult, TokenSnapshot)
from rhl2_scanner.scanner import Scanner
from rhl2_scanner.storage import Storage


class TestToxicFunderStore(unittest.TestCase):
    def test_record_count_forget_list(self):
        s = Storage(":memory:")
        try:
            self.assertEqual(s.is_toxic_funder("0xF"), 0)
            self.assertEqual(s.record_toxic_funder("0xF", "0xdev1", "0xt1"), 1)
            self.assertEqual(s.record_toxic_funder("0xf", "0xdev2", "0xt2"), 2)  # same funder
            self.assertEqual(s.is_toxic_funder("0xF"), 2)
            self.assertEqual(s.toxic_funders()[0]["count"], 2)
            self.assertTrue(s.forget_toxic_funder("0xF"))
            self.assertEqual(s.is_toxic_funder("0xF"), 0)
            self.assertFalse(s.forget_toxic_funder("0xF"))   # already gone
        finally:
            s.close()


class TestFunderExclusions(unittest.TestCase):
    def test_launchpad_and_config_excluded(self):
        cfg = Config()
        cfg.chain.funder_exclude_addresses = ["0x" + "C" * 40]   # a CEX hot wallet
        excl = cfg.funder_exclusions()
        self.assertIn("0x" + "c" * 40, excl)                     # lowercased
        for mgr in cfg.known_launchpad_addresses():
            self.assertIn(mgr, excl)


class _Resp:
    def __init__(self, payload, status=200):
        self._p, self.status = payload, status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        return self._p


class _ExplorerSession:
    def __init__(self, payload):
        self._payload = payload

    def get(self, url, params=None):
        return _Resp(self._payload)

    async def close(self):
        pass


def _sc():
    cfg = Config()
    cfg.runtime.db_path = ":memory:"
    cfg.runtime.contract_audit_on_alert = False    # keep _attach_alert_intel offline
    cfg.chain.explorer_api_url = "https://exp/api"
    sc = Scanner(cfg)
    sc._sent = []

    async def fake_send(html, reply_to=None, reply_markup=None):
        sc._sent.append(html)
    sc._send_html = fake_send  # type: ignore
    return sc


class TestFunderResolver(unittest.IsolatedAsyncioTestCase):
    async def test_picks_earliest_inbound_value_sender(self):
        sc = _sc()
        try:
            dev = "0x" + "d" * 40
            early_funder = "0x" + "1" * 40
            late = "0x" + "2" * 40
            sc._session = _ExplorerSession({"items": [
                {"value": "1000", "from": {"hash": late}, "to": {"hash": dev},
                 "block_number": 200},
                {"value": "5000", "from": {"hash": early_funder}, "to": {"hash": dev},
                 "block_number": 100},                              # earliest inbound
                {"value": "0", "from": {"hash": "0x" + "9" * 40}, "to": {"hash": dev},
                 "block_number": 50},                               # zero value -> ignored
            ]})  # type: ignore
            got = await sc._funder_of(dev)
            self.assertEqual(got, early_funder)
        finally:
            sc.storage.close()


class TestAutoCaptureAndVeto(unittest.IsolatedAsyncioTestCase):
    def _snap(self, **kw):
        base = dict(chain="robinhood", pair_address="0xpair", token_address="0xtrap",
                    symbol="TRAP", market_cap_usd=50000, liquidity_usd=8000)
        base.update(kw)
        s = TokenSnapshot(**base)
        s.safety = SafetyReport(is_honeypot=True)
        return s

    def _res(self):
        return ScoreResult(composite=0.0, level=AlertLevel.SKIP, safety_passed=False,
                           categories=[CategoryScore(name="momentum", raw=80, weight=0.5)])

    async def test_honeypot_captures_funder(self):
        sc = _sc()
        try:
            async def creator(_):
                return "0xscammer"
            async def funder(_):
                return "0xbankroll"
            sc._creator_of = creator  # type: ignore
            sc._funder_of = funder    # type: ignore
            await sc._check_honeypot(self._snap(smart_money_wallets=["0xa"]), self._res())
            self.assertEqual(sc.storage.is_toxic_funder("0xbankroll"), 1)
        finally:
            sc.storage.close()

    async def test_excluded_funder_not_recorded(self):
        sc = _sc()
        try:
            sc.cfg.chain.funder_exclude_addresses = ["0xbankroll"]
            async def creator(_):
                return "0xscammer"
            async def funder(_):
                return "0xbankroll"
            sc._creator_of = creator  # type: ignore
            sc._funder_of = funder    # type: ignore
            await sc._check_honeypot(self._snap(smart_money_wallets=["0xa"]), self._res())
            self.assertEqual(sc.storage.is_toxic_funder("0xbankroll"), 0)   # CEX skipped
        finally:
            sc.storage.close()

    async def test_repeat_funder_vetoes_new_alert(self):
        sc = _sc()
        try:
            # a funder tied to >= funder_gate_min_hits scams
            for i in range(sc.cfg.runtime.funder_gate_min_hits):
                sc.storage.record_toxic_funder("0xbankroll", "0xoldev", f"0xt{i}")
            async def creator(_):
                return "0xnewdev"
            async def funder(_):
                return "0xbankroll"
            sc._creator_of = creator  # type: ignore
            sc._funder_of = funder    # type: ignore
            snap = self._snap(token_address="0xnew", symbol="NEW")
            snap.safety = SafetyReport()      # this one looks clean
            await sc._attach_alert_intel(snap, self._res())
            self.assertTrue(snap.funder_blocked)
            self.assertIn("funded by", snap.dev_note)
        finally:
            sc.storage.close()

    async def test_single_hit_warns_but_does_not_veto(self):
        sc = _sc()
        try:
            sc.storage.record_toxic_funder("0xbankroll", "0xoldev", "0xt0")  # 1 hit
            async def creator(_):
                return "0xnewdev"
            async def funder(_):
                return "0xbankroll"
            sc._creator_of = creator  # type: ignore
            sc._funder_of = funder    # type: ignore
            snap = self._snap(token_address="0xnew", symbol="NEW")
            snap.safety = SafetyReport()
            await sc._attach_alert_intel(snap, self._res())
            self.assertFalse(snap.funder_blocked)          # 1 < gate threshold
            self.assertIn("funded by", snap.dev_note)      # but still warned
        finally:
            sc.storage.close()


class TestSerialRuggerGate(unittest.IsolatedAsyncioTestCase):
    def _res(self):
        return ScoreResult(composite=0.0, level=AlertLevel.SKIP, safety_passed=False,
                           categories=[CategoryScore(name="m", raw=80, weight=0.5)])

    def _snap(self):
        s = TokenSnapshot(chain="robinhood", pair_address="0xp",
                          token_address="0xnew", symbol="NEW")
        s.safety = SafetyReport()
        return s

    async def _intel(self, sc, deployer):
        async def creator(_):
            return deployer
        async def funder(_):
            return ""                       # isolate the rugger path
        sc._creator_of = creator  # type: ignore
        sc._funder_of = funder    # type: ignore
        snap = self._snap()
        await sc._attach_alert_intel(snap, self._res())
        return snap

    async def test_serial_rugger_is_vetoed(self):
        sc = _sc()
        try:
            for i in range(sc.cfg.runtime.serial_rugger_gate_min):
                sc.storage.record_deployer_token("0xrugdev", f"0xr{i}", "rug")
            snap = await self._intel(sc, "0xrugdev")
            self.assertTrue(snap.dev_blocked)
            self.assertIn("serial rugger", snap.dev_note)
        finally:
            sc.storage.close()

    async def test_below_threshold_warns_only(self):
        sc = _sc()
        try:
            sc.storage.record_deployer_token("0xdev", "0xr0", "rug")   # 1 rug
            snap = await self._intel(sc, "0xdev")
            self.assertFalse(snap.dev_blocked)
            self.assertIn("rugged", snap.dev_note)
        finally:
            sc.storage.close()

    async def test_prolific_dev_with_more_wins_not_blocked(self):
        sc = _sc()
        try:
            for i in range(sc.cfg.runtime.serial_rugger_gate_min):
                sc.storage.record_deployer_token("0xdev", f"0xr{i}", "rug")
            for i in range(sc.cfg.runtime.serial_rugger_gate_min + 2):
                sc.storage.record_deployer_token("0xdev", f"0xw{i}", "winner")
            snap = await self._intel(sc, "0xdev")
            self.assertFalse(snap.dev_blocked)          # winners outweigh rugs
        finally:
            sc.storage.close()


if __name__ == "__main__":
    unittest.main()
