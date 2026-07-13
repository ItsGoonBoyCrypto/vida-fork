"""Tests for the new safety sources: keccak, GoPlus parse, merge, log decode.

All pure — no network. Run with the rest:
    python -m unittest discover -s rhl2_scanner/tests -v
"""

from __future__ import annotations

import unittest

from rhl2_scanner.keccak import (
    addr_to_topic,
    event_topic,
    keccak_hex,
    mapping_slot,
    nested_mapping_slot,
)
from rhl2_scanner.models import SafetyReport, TokenSnapshot
from rhl2_scanner.sources.goplus import GoPlusClient
from rhl2_scanner.sources.poollistener import decode_pair_created
from rhl2_scanner.sources.safety import merge_reports


class TestKeccak(unittest.TestCase):
    def test_known_vectors(self):
        self.assertEqual(
            keccak_hex(b""),
            "0xc5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470",
        )
        self.assertEqual(
            keccak_hex(b"abc"),
            "0x4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45",
        )

    def test_event_topics(self):
        self.assertEqual(
            event_topic("PairCreated(address,address,address,uint256)"),
            "0x0d3648bd0f6ba80134a33ba9275ac585d9d315f0ad8355cddefde31afa28d0e9",
        )
        self.assertEqual(
            event_topic("Transfer(address,address,uint256)"),
            "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
        )

    def test_mapping_slot_shape(self):
        s = mapping_slot("0x000000000000000000000000000000000000babe", 0)
        self.assertTrue(s.startswith("0x") and len(s) == 66)
        # deterministic
        self.assertEqual(s, mapping_slot("0x000000000000000000000000000000000000babe", 0))
        # different slot => different key
        self.assertNotEqual(s, mapping_slot("0x000000000000000000000000000000000000babe", 1))

    def test_nested_mapping_slot(self):
        s = nested_mapping_slot("0x" + "1" * 40, "0x" + "2" * 40, 3)
        self.assertEqual(len(s), 66)

    def test_addr_to_topic(self):
        self.assertEqual(
            addr_to_topic("0xabc0000000000000000000000000000000000001"),
            "0x000000000000000000000000abc0000000000000000000000000000000000001",
        )


class TestGoPlusParse(unittest.TestCase):
    def test_clean_token(self):
        entry = {
            "is_open_source": "1",
            "is_honeypot": "0",
            "buy_tax": "0",
            "sell_tax": "0",
            "is_mintable": "0",
            "transfer_pausable": "0",
            "owner_percent": "0.02",
            "creator_percent": "0.01",
            "holder_count": "312",
            "lp_holders": [
                {"address": "0x000000000000000000000000000000000000dead", "percent": "1", "is_locked": 0}
            ],
            "holders": [
                {"address": "0xaaa", "percent": "0.08", "is_locked": 0},
                {"address": "0xbbb", "percent": "0.05", "is_locked": 0},
            ],
        }
        snap = TokenSnapshot(chain="base", pair_address="0xp", token_address="0xt")
        r = GoPlusClient.parse(entry, snap)
        self.assertTrue(r.contract_verified)
        self.assertFalse(r.is_honeypot)
        self.assertTrue(r.mint_authority_revoked)
        self.assertTrue(r.freeze_authority_revoked)
        self.assertTrue(r.lp_burned)
        self.assertAlmostEqual(r.dev_holdings_pct, 2.0, places=3)
        self.assertEqual(snap.holder_count, 312)
        self.assertAlmostEqual(snap.top10_supply_pct, 13.0, places=1)
        self.assertEqual(r.external_risk_score, 100.0)

    def test_honeypot_token_flags_and_score(self):
        entry = {
            "is_open_source": "0",
            "is_honeypot": "1",
            "buy_tax": "0.05",
            "sell_tax": "0.99",
            "is_mintable": "1",
            "transfer_pausable": "1",
            "cannot_sell_all": "1",
        }
        r = GoPlusClient.parse(entry)
        self.assertTrue(r.is_honeypot)
        self.assertFalse(r.contract_verified)
        self.assertFalse(r.mint_authority_revoked)
        self.assertAlmostEqual(r.sell_tax_pct, 99.0, places=1)
        self.assertIn("is_honeypot", r.high_risk_flags)
        self.assertLess(r.external_risk_score, 60)


class TestMerge(unittest.TestCase):
    def test_danger_wins(self):
        safe = SafetyReport(is_honeypot=False, sell_tax_pct=1.0, contract_verified=True)
        bad = SafetyReport(is_honeypot=True, sell_tax_pct=50.0, contract_verified=None)
        m = merge_reports(safe, bad)
        self.assertTrue(m.is_honeypot)            # any True danger wins
        self.assertEqual(m.sell_tax_pct, 50.0)    # worst-case tax
        self.assertTrue(m.contract_verified)      # confirmed by one, not contradicted

    def test_unconfirmed_stays_none(self):
        a = SafetyReport()
        b = SafetyReport()
        m = merge_reports(a, b)
        self.assertIsNone(m.mint_authority_revoked)
        self.assertIsNone(m.is_honeypot)

    def test_safe_requires_confirmation_and_no_contradiction(self):
        a = SafetyReport(mint_authority_revoked=True)
        b = SafetyReport(mint_authority_revoked=False)
        self.assertFalse(merge_reports(a, b).mint_authority_revoked)  # contradiction => unsafe
        self.assertIsNone(merge_reports(SafetyReport()).mint_authority_revoked)


class TestPoolLogDecode(unittest.TestCase):
    def test_univ2_pair_created(self):
        token0 = addr_to_topic("0x" + "a" * 40)
        token1 = addr_to_topic("0x" + "b" * 40)
        pair = "c" * 40
        data = "0x" + pair.rjust(64, "0") + ("0" * 63 + "1")  # pair word + allPairsLength
        entry = {"topics": [event_topic("PairCreated(address,address,address,uint256)"), token0, token1], "data": data}
        t0, t1, p = decode_pair_created(entry, "univ2")
        self.assertEqual(t0, "0x" + "a" * 40)
        self.assertEqual(t1, "0x" + "b" * 40)
        self.assertEqual(p, "0x" + "c" * 40)

    def test_univ3_pool_created(self):
        token0 = addr_to_topic("0x" + "a" * 40)
        token1 = addr_to_topic("0x" + "b" * 40)
        fee = addr_to_topic("0x" + "0" * 36 + "0bb8")  # 3000
        pool = "d" * 40
        tick = "0" * 64
        data = "0x" + tick + pool.rjust(64, "0")
        entry = {"topics": ["0xtopic", token0, token1, fee], "data": data}
        t0, t1, p = decode_pair_created(entry, "univ3")
        self.assertEqual(p, "0x" + "d" * 40)


if __name__ == "__main__":
    unittest.main()
