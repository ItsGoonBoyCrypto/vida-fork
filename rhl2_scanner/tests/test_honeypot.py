"""Honeypot warning alert + serial-scammer (deployer) blocklist."""

from __future__ import annotations

import unittest

from rhl2_scanner.config import Config
from rhl2_scanner.models import SafetyReport, ScoreResult, AlertLevel, CategoryScore, TokenSnapshot
from rhl2_scanner.scanner import Scanner
from rhl2_scanner.storage import Storage


def _snap(**kw):
    base = dict(chain="robinhood", pair_address="0xpair", token_address="0xtrap",
                symbol="TRAP", market_cap_usd=50000, liquidity_usd=8000)
    base.update(kw)
    s = TokenSnapshot(**base)
    s.safety = SafetyReport(is_honeypot=True)
    return s


def _res(safe=False):
    return ScoreResult(composite=0.0, level=AlertLevel.SKIP, safety_passed=safe,
                       categories=[CategoryScore(name="momentum", raw=80, weight=0.5)])


class TestHoneypotDeployerStore(unittest.TestCase):
    def test_record_and_count(self):
        s = Storage(":memory:")
        try:
            self.assertEqual(s.is_honeypot_deployer("0xdev"), 0)
            self.assertEqual(s.record_honeypot_deployer("0xDEV", "0xt1"), 1)
            self.assertEqual(s.record_honeypot_deployer("0xdev", "0xt2"), 2)
            self.assertEqual(s.is_honeypot_deployer("0xdev"), 2)
            self.assertEqual(s.honeypot_deployers()[0]["count"], 2)
        finally:
            s.close()


class TestHoneypotAlert(unittest.IsolatedAsyncioTestCase):
    def _sc(self):
        cfg = Config()
        cfg.runtime.db_path = ":memory:"
        sc = Scanner(cfg)
        sc._sent = []
        async def fake_send(html, reply_to=None):
            sc._sent.append(html)
        sc._send_html = fake_send  # type: ignore
        async def fake_creator(token):
            return "0xscammer"
        sc._creator_of = fake_creator  # type: ignore
        return sc

    async def test_warns_on_interesting_honeypot_and_logs_deployer(self):
        sc = self._sc()
        try:
            snap = _snap(smart_money_wallets=["0xa"])   # interesting: smart money in
            fired = await sc._check_honeypot(snap, _res())
            self.assertTrue(fired)
            self.assertTrue(any("HONEYPOT WARNING" in h for h in sc._sent))
            self.assertEqual(sc.storage.is_honeypot_deployer("0xscammer"), 1)
        finally:
            sc.storage.close()

    async def test_ignores_uninteresting_trap(self):
        sc = self._sc()
        try:
            # no smart money, not alerted, low would-be score → no spam
            snap = _snap(smart_money_wallets=[])
            fired = await sc._check_honeypot(snap, ScoreResult(
                composite=0, level=AlertLevel.SKIP, safety_passed=False,
                categories=[CategoryScore(name="momentum", raw=10, weight=0.2)]))
            self.assertFalse(fired)
            self.assertEqual(sc._sent, [])
        finally:
            sc.storage.close()

    async def test_preemptive_on_known_scammer_deployer(self):
        sc = self._sc()
        try:
            sc.storage.record_honeypot_deployer("0xscammer", "0xold")
            # this token isn't confirmed honeypot, but its deployer is known
            snap = _snap(smart_money_wallets=["0xa"])
            snap.safety = SafetyReport(is_honeypot=None)
            fired = await sc._check_honeypot(snap, _res())
            self.assertTrue(fired)
            self.assertTrue(any("serial scammer" in h.lower() or "serial honeypot" in h.lower()
                                for h in sc._sent))
        finally:
            sc.storage.close()

    async def test_dedup_one_shot(self):
        sc = self._sc()
        try:
            snap = _snap(smart_money_wallets=["0xa"])
            self.assertTrue(await sc._check_honeypot(snap, _res()))
            sc._sent.clear()
            self.assertFalse(await sc._check_honeypot(snap, _res()))  # same token
            self.assertEqual(sc._sent, [])
        finally:
            sc.storage.close()

    async def test_honeypots_command(self):
        sc = self._sc()
        try:
            sc.storage.record_honeypot_deployer("0xscammer", "0xt1")
            await sc._handle_command("/honeypots")
            self.assertTrue(any("honeypot deployers" in h.lower() for h in sc._sent))
        finally:
            sc.storage.close()


if __name__ == "__main__":
    unittest.main()
