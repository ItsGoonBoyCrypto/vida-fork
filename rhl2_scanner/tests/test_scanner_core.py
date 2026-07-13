"""Pure-logic tests for the scanner core (no network).

Run:  python -m pytest rhl2_scanner/tests -q
   or: python -m unittest discover -s rhl2_scanner/tests
"""

from __future__ import annotations

import unittest

from rhl2_scanner.bundle import Transfer, analyze_bundles
from rhl2_scanner.config import Config
from rhl2_scanner.filters import quick_start_gate, safety_gate
from rhl2_scanner.models import AlertLevel, SafetyReport, TokenSnapshot
from rhl2_scanner.scoring import score_token
from rhl2_scanner.alerting.formatter import build_lines, to_telegram_html


def clean_token(**over) -> TokenSnapshot:
    snap = TokenSnapshot(
        chain="base",
        pair_address="0xpair",
        token_address="0xtoken",
        symbol="GEM",
        market_cap_usd=187_000,
        liquidity_usd=42_000,
        age_minutes=47,
        volume_1h=28_000,
        volume_24h=210_000,
        buys_1h=180,
        sells_1h=50,
        price_change_1h=12,
        price_change_24h=140,
        holder_count=312,
        top10_supply_pct=22,
        top1_supply_pct=6,
        holder_growth_1h=40,
        smart_money_wallets=["0xa", "0xb", "0xc"],
        socials={"telegram": "https://t.me/x"},
        dexscreener_url="https://dexscreener.com/x",
    )
    snap.safety = SafetyReport(
        contract_verified=True,
        mint_authority_revoked=True,
        freeze_authority_revoked=True,
        lp_burned=True,
        buy_tax_pct=0,
        sell_tax_pct=0,
        is_honeypot=False,
        dev_holdings_pct=2.0,
        external_risk_score=92,
        bundle_supply_pct=8,
    )
    for k, v in over.items():
        if hasattr(snap, k):
            setattr(snap, k, v)
        elif hasattr(snap.safety, k):
            setattr(snap.safety, k, v)
    return snap


class TestScoring(unittest.TestCase):
    def setUp(self):
        self.cfg = Config()  # defaults, momentum tier

    def test_clean_token_scores_strong(self):
        r = score_token(clean_token(), self.cfg, strict_safety=True)
        self.assertTrue(r.safety_passed)
        self.assertEqual(r.level, AlertLevel.STRONG)
        self.assertGreaterEqual(r.composite, 75)

    def test_derived_buy_ratio(self):
        self.assertAlmostEqual(clean_token().buy_ratio_1h, 180 / 230, places=4)

    def test_volume_acceleration_flag(self):
        # 1h volume 28k vs avg hourly 210k/24 ≈ 8.75k -> accelerating
        self.assertTrue(clean_token().volume_accelerating)

    def test_unverified_contract_fails_gate_strict(self):
        r = score_token(clean_token(contract_verified=None), self.cfg, strict_safety=True)
        self.assertFalse(r.safety_passed)
        self.assertEqual(r.level, AlertLevel.SKIP)
        self.assertEqual(r.composite, 0.0)

    def test_honeypot_hard_skip(self):
        r = score_token(clean_token(is_honeypot=True), self.cfg, strict_safety=True)
        self.assertFalse(r.safety_passed)

    def test_high_top10_hard_skip(self):
        r = score_token(clean_token(top10_supply_pct=40), self.cfg, strict_safety=True)
        self.assertFalse(r.safety_passed)
        self.assertTrue(any("top10" in f for f in r.gate_failures))

    def test_high_tax_skips(self):
        r = score_token(clean_token(sell_tax_pct=25), self.cfg, strict_safety=True)
        self.assertFalse(r.safety_passed)

    def test_dev_dumping_skips(self):
        r = score_token(clean_token(dev_recent_sell=True), self.cfg, strict_safety=True)
        self.assertFalse(r.safety_passed)

    def test_no_lp_safety_skips(self):
        r = score_token(clean_token(lp_burned=None, lp_locked=None), self.cfg, strict_safety=True)
        self.assertFalse(r.safety_passed)

    def test_pragmatic_allows_unconfirmed_tax_and_honeypot(self):
        # Unknown tax + honeypot, but all confirmable facts good -> passes pragmatic.
        snap = clean_token(buy_tax_pct=None, sell_tax_pct=None, is_honeypot=None,
                           external_risk_score=None)
        strict = score_token(snap, self.cfg, strict_safety=True)
        pragma = score_token(snap, self.cfg, strict_safety=True, pragmatic=True)
        self.assertFalse(strict.safety_passed)   # strict rejects unconfirmed tax/honeypot
        self.assertTrue(pragma.safety_passed)     # pragmatic tolerates unknown

    def test_pragmatic_still_blocks_confirmed_bad(self):
        # A CONFIRMED honeypot or high tax must still fail, even pragmatic.
        r1 = score_token(clean_token(is_honeypot=True), self.cfg, strict_safety=True, pragmatic=True)
        r2 = score_token(clean_token(sell_tax_pct=25), self.cfg, strict_safety=True, pragmatic=True)
        r3 = score_token(clean_token(mint_authority_revoked=None), self.cfg, strict_safety=True, pragmatic=True)
        self.assertFalse(r1.safety_passed)   # confirmed honeypot
        self.assertFalse(r2.safety_passed)   # confirmed high tax
        self.assertFalse(r3.safety_passed)   # authority still required (confirmable)


class TestQuickStartGate(unittest.TestCase):
    def setUp(self):
        self.th = Config().thresholds

    def test_old_token_rejected(self):
        snap = clean_token(age_minutes=48 * 60)
        self.assertFalse(quick_start_gate(snap, self.th).passed)

    def test_thin_liquidity_rejected(self):
        snap = clean_token(liquidity_usd=1_000)
        self.assertFalse(quick_start_gate(snap, self.th).passed)

    def test_clean_passes(self):
        self.assertTrue(quick_start_gate(clean_token(), self.th).passed)


class TestBundles(unittest.TestCase):
    def test_common_funder_cluster_detected(self):
        # One funder seeds 4 fresh wallets that each hold 100 of 1000 supply.
        transfers = [Transfer(block=0, from_addr="0x0", to_addr="0xpool", value=1000)]
        for i in range(4):
            transfers.append(Transfer(block=1, from_addr="0xfunder", to_addr=f"0xw{i}", value=100))
        res = analyze_bundles(transfers, total_supply=1000, launch_block=0)
        self.assertGreaterEqual(res.largest_cluster_wallets, 4)
        self.assertGreater(res.bundle_supply_pct, 30)

    def test_sniper_window(self):
        transfers = [
            Transfer(block=0, from_addr="0x0", to_addr="0xpool", value=1000),
            Transfer(block=1, from_addr="0xpool", to_addr="0xsniper", value=200),
            Transfer(block=50, from_addr="0xpool", to_addr="0xlate", value=100),
        ]
        res = analyze_bundles(transfers, total_supply=1000, launch_block=0, launch_window_blocks=3)
        self.assertGreater(res.sniper_cluster_pct, 15)


class TestFormatter(unittest.TestCase):
    def test_alert_lines_shape(self):
        r = score_token(clean_token(), Config(), strict_safety=True)
        lines = build_lines(clean_token(), r)
        self.assertIn("EARLY GEM ALERT", lines[0])
        self.assertTrue(any(line.startswith("Token: $GEM") for line in lines))
        self.assertTrue(any("Buy Ratio" in line for line in lines))
        self.assertTrue(any("Breakdown:" in line for line in lines))

    def test_telegram_html_has_links(self):
        r = score_token(clean_token(), Config(), strict_safety=True)
        html = to_telegram_html(clean_token(), r)
        self.assertIn("<a href=", html)
        self.assertIn("<b>", html)


if __name__ == "__main__":
    unittest.main()
