"""Learned-signature → scanner score bonus (dormant until trained)."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest

from rhl2_scanner.models import SafetyReport, TokenSnapshot
from rhl2_scanner.signature_bonus import (
    load_signature, match_fraction, signature_bonus,
)


def _snap(**kw):
    base = dict(chain="robinhood", pair_address="0xp", token_address="0xt",
                market_cap_usd=30000, liquidity_usd=3000, top10_supply_pct=30,
                holder_growth_1h=5, buys_5m=40, sells_5m=10,
                smart_money_wallets=["0xa", "0xb"])
    base.update(kw)
    s = TokenSnapshot(**base)
    s.safety = SafetyReport(is_honeypot=False, dev_holdings_pct=3)
    return s


_SIG = {
    "trained_on": 40, "precision": 0.7,
    "rules": [
        {"feature": "smart_money_count", "op": ">=", "value": 1},
        {"feature": "buy_ratio_5m", "op": ">=", "value": 0.62},
        {"feature": "liq_to_mcap", "op": ">=", "value": 0.06},
        {"feature": "top10_pct", "op": "<=", "value": 45},
        {"feature": "dev_holdings_pct", "op": "<=", "value": 15},
        {"feature": "is_sellable", "op": ">=", "value": 1},
    ],
}


class TestSignatureBonus(unittest.TestCase):
    def test_matching_token_gets_bonus(self):
        frac, judged = match_fraction(_snap(), _SIG)
        self.assertEqual(judged, 6)
        self.assertGreaterEqual(frac, 0.9)
        self.assertGreater(signature_bonus(_snap(), _SIG), 0)

    def test_non_matching_token_no_bonus(self):
        # fails buy_ratio, top10, sellable
        s = _snap(buys_5m=5, sells_5m=95, top10_supply_pct=80)
        s.safety = SafetyReport(is_honeypot=True, dev_holdings_pct=40)
        self.assertEqual(signature_bonus(s, _SIG), 0.0)

    def test_dormant_when_untrained(self):
        # a bootstrap prior (trained_on 0) must contribute nothing
        with tempfile.NamedTemporaryFile(suffix=".db") as f:
            conn = sqlite3.connect(f.name)
            conn.execute("CREATE TABLE signatures (created_ts REAL, json TEXT)")
            conn.execute("INSERT INTO signatures VALUES (1, ?)",
                         (json.dumps({"trained_on": 0, "rules": _SIG["rules"]}),))
            conn.commit()
            conn.close()
            self.assertIsNone(load_signature(f.name, min_trained=20))   # dormant

    def test_loads_trained_signature(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as f:
            conn = sqlite3.connect(f.name)
            conn.execute("CREATE TABLE signatures (created_ts REAL, json TEXT)")
            conn.execute("INSERT INTO signatures VALUES (1, ?)", (json.dumps(_SIG),))
            conn.commit()
            conn.close()
            sig = load_signature(f.name, min_trained=20)
            self.assertIsNotNone(sig)
            self.assertEqual(sig["trained_on"], 40)

    def test_missing_db_dormant(self):
        self.assertIsNone(load_signature("/nonexistent/path.db"))
        self.assertIsNone(load_signature(""))
        self.assertEqual(signature_bonus(_snap(), None), 0.0)


if __name__ == "__main__":
    unittest.main()
