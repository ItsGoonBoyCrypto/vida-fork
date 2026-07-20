"""Per-chain safety coverage report + Solana mint-authority disambiguation."""

from __future__ import annotations

import os
import unittest
from unittest import mock

from memelab.chains.registry import safety_coverage
from memelab.chains.solana import SolanaAdapter
from memelab.chains.registry import REGISTRY
from memelab.models import Chain, TokenSnapshot


class TestCoverage(unittest.TestCase):
    def test_robinhood_goplus_off(self):
        cov = safety_coverage(Chain.ROBINHOOD)
        self.assertTrue(any("goplus" in m and "OFF" in m for m in cov["missing"]))

    def test_bnb_needs_etherscan_key(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            cov = safety_coverage(Chain.BNB)
            self.assertTrue(any("ETHERSCAN_API_KEY" in m for m in cov["missing"]))
        with mock.patch.dict(os.environ, {"ETHERSCAN_API_KEY": "k"}):
            cov = safety_coverage(Chain.BNB)
            self.assertTrue(any("etherscan" in a for a in cov["active"]))

    def test_solana_rugcheck_active(self):
        cov = safety_coverage(Chain.SOLANA)
        self.assertTrue(any("rugcheck" in a for a in cov["active"]))


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
    def __init__(self, payload):
        self._p = payload

    def get(self, url, params=None):
        return _Resp(self._p)

    def post(self, url, json=None):
        return _Resp({})


class TestSolanaMintAuthority(unittest.IsolatedAsyncioTestCase):
    async def _assess(self, report):
        ad = SolanaAdapter(REGISTRY[Chain.SOLANA], session=_Session(report))
        snap = TokenSnapshot(chain=Chain.SOLANA, token_address="MINT")
        await ad.enrich_safety(snap)
        return snap

    async def test_absent_field_stays_unknown(self):
        snap = await self._assess({"score": 100})     # no mintAuthority key
        self.assertIsNone(snap.mint_authority_revoked)   # NOT falsely "revoked"

    async def test_null_authority_is_revoked(self):
        snap = await self._assess({"mintAuthority": None})
        self.assertIs(snap.mint_authority_revoked, True)

    async def test_present_authority_is_not_revoked(self):
        snap = await self._assess({"mintAuthority": "SomeAuthorityPubkey"})
        self.assertIs(snap.mint_authority_revoked, False)


if __name__ == "__main__":
    unittest.main()
