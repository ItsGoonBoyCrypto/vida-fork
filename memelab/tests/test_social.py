"""Tests for the LunarCrush social feed mapping + social features."""

from __future__ import annotations

import unittest

from memelab.models import Chain, TokenSnapshot, TokenTimeSeries
from memelab.ingest.social import SocialFeed
from memelab.metrics.features import extract


class TestSocialFeed(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_without_key(self):
        f = SocialFeed(api_key="", session=object())
        self.assertFalse(f.enabled)
        self.assertIsNone(await f.metrics_for("WOW"))

    async def test_maps_topic_response(self):
        f = SocialFeed(api_key="k", session=object())
        async def fake_get(url):
            self.assertIn("/wow/", url)          # symbol lowercased into topic
            return {"data": {"interactions_24h": 12000, "sentiment": 72, "galaxy_score": 55}}
        f._get = fake_get  # type: ignore
        m = await f.metrics_for("$WOW")
        self.assertEqual(m["social_volume"], 12000)
        self.assertEqual(m["social_sentiment"], 72)
        self.assertEqual(m["social_score"], 55)

    async def test_none_when_no_metrics(self):
        f = SocialFeed(api_key="k", session=object())
        async def fake_get(url):
            return {"data": {}}
        f._get = fake_get  # type: ignore
        self.assertIsNone(await f.metrics_for("WOW"))


class TestSocialFeatures(unittest.TestCase):
    def test_social_features_extracted(self):
        first = 1_000_000.0
        snap = TokenSnapshot(chain=Chain.SOLANA, token_address="M", ts=first,
                             price_usd=1.0, social_volume=8000, social_sentiment=68,
                             social_score=61)
        ts = TokenTimeSeries(chain=Chain.SOLANA, token_address="M",
                             first_seen_ts=first, entry_price=1.0, snapshots=[snap])
        fv = extract(ts)
        self.assertEqual(fv.get("social_volume"), 8000)
        self.assertEqual(fv.get("social_sentiment"), 68)
        self.assertEqual(fv.get("social_score"), 61)


if __name__ == "__main__":
    unittest.main()
