"""Industrial alert format: progress bar, buy pressure, link row, smart money."""

from __future__ import annotations

import unittest

from rhl2_scanner.alerting.formatter import (
    _bar, _buy_pressure, _links_row, _smart_line,
    format_early_launch_html, to_telegram_html,
)
from rhl2_scanner.config import Config
from rhl2_scanner.models import SafetyReport, TokenSnapshot
from rhl2_scanner.scoring import score_token


def _snap(**kw):
    base = dict(chain="robinhood", pair_address="0xp", token_address="0xtoken",
                symbol="GEM", market_cap_usd=30000, liquidity_usd=9000, age_minutes=20,
                holder_count=80, top10_supply_pct=22, volume_1h=15000,
                buys_5m=42, sells_5m=8)
    base.update(kw)
    s = TokenSnapshot(**base)
    s.safety = SafetyReport(is_honeypot=False, mint_authority_revoked=True,
                            freeze_authority_revoked=True, lp_burned=True,
                            bundle_supply_pct=5.0)
    return s


class TestHelpers(unittest.TestCase):
    def test_bar(self):
        self.assertEqual(_bar(0), "▱" * 10)
        self.assertEqual(_bar(100), "▰" * 10)
        self.assertEqual(_bar(60).count("▰"), 6)
        self.assertEqual(_bar(None), "")

    def test_buy_pressure_dot(self):
        self.assertIn("🟢", _buy_pressure(_snap(buys_5m=90, sells_5m=10)))
        self.assertIn("🔴", _buy_pressure(_snap(buys_5m=10, sells_5m=90)))
        self.assertEqual(_buy_pressure(_snap(buys_5m=None, sells_5m=None,
                                             buys_1h=None, sells_1h=None)), "")

    def test_links_row_prefers_trade_and_explorer(self):
        s = _snap()
        s.trade_url = "https://bags.fm/0xtoken"
        s.explorer_url = "https://robinhoodchain.blockscout.com/token/0xtoken"
        s.socials = {"twitter": "https://x.com/t", "telegram": "https://t.me/t"}
        row = _links_row(s)
        self.assertIn("🚀 Trade", row)
        self.assertIn("🔍 Scan", row)
        self.assertIn("𝕏", row)

    def test_smart_line(self):
        s = _snap(smart_money_wallets=["0xaaaa1111", "0xbbbb2222", "0xcccc3333", "0xdddd4444"])
        line = _smart_line(s)
        self.assertIn("4", line)
        self.assertIn("+1", line)   # shows first 3 + remainder


class TestRenders(unittest.TestCase):
    def test_maturity_alert_has_sections(self):
        s = _snap(launchpad="flap", curve_progress_pct=62)
        s.trade_url = "https://bags.fm/0xtoken"
        r = score_token(s, Config.load("rhl2_scanner/config/robinhood.example.yaml"),
                        strict_safety=False, pragmatic=True)
        html = to_telegram_html(s, r)
        self.assertIn("<code>0xtoken</code>", html)     # tap-copy CA
        self.assertIn("curve ▰", html)                  # progress bar
        self.assertIn("🛡", html)                        # safety row present
        self.assertIn("💰 MCAP", html)
        self.assertIn("🚀 Trade", html)

    def test_early_alert_has_bar_and_links(self):
        s = _snap(launchpad="bags", curve_progress_pct=40)
        s.explorer_url = "https://robinhoodchain.blockscout.com/token/0xtoken"
        r = score_token(s, Config(), strict_safety=False, pragmatic=True)
        html = format_early_launch_html(s, r)
        self.assertIn("EARLY LAUNCH", html)
        self.assertIn("▰", html)          # curve bar
        self.assertIn("🔍 Scan", html)    # explorer link

    def test_consolidated_alert_has_all_intel(self):
        # Everything the system knows must land in ONE alert.
        s = _snap(launchpad="flap", smart_money_wallets=["0xaaaa1111"],
                  core_alpha_wallets=["0xaaaa1111"], narrative="dog",
                  narrative_hot=True, kol_labels=["Ansem"])
        s.smart_money_labels = {"0xaaaa1111": "Ansem"}
        s.conviction = 82.0
        s.conviction_factors = [("base", 35.0), ("core-alpha", 18.0), ("memelab sig", 15.0)]
        s.contract_risk = "🟢 no contract red flags found"
        s.exit_target = "TP ~4x–7x · trail −55%"
        s.trade_url = "https://flap.sh/0xtoken"
        r = score_token(s, Config(), strict_safety=False, pragmatic=True)
        html = to_telegram_html(s, r)
        self.assertIn("Conviction 82", html)      # conviction headline
        self.assertIn("core-alpha", html)         # factor + smart tag
        self.assertIn("dog", html)                # narrative
        self.assertIn("KOL in", html)             # KOL among buyers
        self.assertIn("Contract", html)           # contract audit
        self.assertIn("Sell check", html)         # honeypot/sellability line
        self.assertIn("Exit plan", html)          # learned exit
        self.assertIn("🚀 Trade", html)           # links
        self.assertIn("💎", html)                 # core-alpha marker


if __name__ == "__main__":
    unittest.main()
