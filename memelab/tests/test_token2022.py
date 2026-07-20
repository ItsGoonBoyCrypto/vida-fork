"""Solana Token-2022 danger-extension audit — RugCheck + jsonParsed parsing."""

from __future__ import annotations

import unittest

from memelab.token2022 import scan_extensions, scan_rugcheck


class TestScanExtensions(unittest.TestCase):
    def test_permanent_delegate_is_hard(self):
        out = scan_extensions([{"extension": "permanentDelegate", "state": {}}])
        self.assertTrue(out["honeypot"])
        self.assertTrue(any("permanent delegate" in f for f in out["flags"]))

    def test_transfer_hook_and_non_transferable_are_hard(self):
        for name in ("transferHook", "nonTransferable"):
            out = scan_extensions([{"extension": name, "state": {}}])
            self.assertTrue(out["honeypot"], name)

    def test_default_frozen_only_when_frozen(self):
        frozen = scan_extensions([{"extension": "defaultAccountState",
                                   "state": {"accountState": "frozen"}}])
        self.assertTrue(frozen["honeypot"])
        thawed = scan_extensions([{"extension": "defaultAccountState",
                                   "state": {"accountState": "initialized"}}])
        self.assertFalse(thawed["honeypot"])
        self.assertEqual(thawed["flags"], [])

    def test_transfer_fee_is_settable_not_hard_with_pct(self):
        out = scan_extensions([{"extension": "transferFeeConfig", "state": {
            "newerTransferFee": {"transferFeeBasisPoints": 500}}}])   # 5%
        self.assertFalse(out["honeypot"])
        self.assertTrue(out["settable_fee"])
        self.assertAlmostEqual(out["fee_pct"], 5.0, places=2)

    def test_clean_token_is_blank(self):
        out = scan_extensions([{"extension": "metadataPointer", "state": {}}])
        self.assertFalse(out["honeypot"])
        self.assertFalse(out["settable_fee"])
        self.assertEqual(out["flags"], [])

    def test_junk_is_safe(self):
        for junk in (None, "x", [1, 2], [{"no": "ext"}]):
            self.assertFalse(scan_extensions(junk)["honeypot"])


class TestScanRugcheck(unittest.TestCase):
    def test_named_risks_classified(self):
        report = {"risks": [
            {"name": "Permanent Delegate", "level": "danger"},
            {"name": "Transfer Fee", "level": "warn"},
        ]}
        out = scan_rugcheck(report)
        self.assertTrue(out["honeypot"])          # permanent delegate
        self.assertTrue(out["settable_fee"])      # transfer fee
        self.assertTrue(any("permanent delegate" in f for f in out["flags"]))

    def test_transfer_fee_field_gives_pct(self):
        report = {"transferFee": {"transferFeeBasisPoints": 1000}}   # 10%
        out = scan_rugcheck(report)
        self.assertTrue(out["settable_fee"])
        self.assertAlmostEqual(out["fee_pct"], 10.0, places=2)

    def test_extensions_list_echoed(self):
        report = {"tokenExtensions": [{"extension": "transferHook", "state": {}}]}
        self.assertTrue(scan_rugcheck(report)["honeypot"])

    def test_clean_report(self):
        out = scan_rugcheck({"risks": [{"name": "Low liquidity"}]})
        self.assertFalse(out["honeypot"])
        self.assertEqual(out["flags"], [])


if __name__ == "__main__":
    unittest.main()
