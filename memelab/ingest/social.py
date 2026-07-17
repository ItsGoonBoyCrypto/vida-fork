"""Social sentiment ingest — LunarCrush (optional, API-key gated).

Social traction is a distinct, strong meme predictor we don't otherwise capture.
LunarCrush tracks mentions/interactions/sentiment per topic (symbol). Coverage of
brand-new micro-caps is sparse — most fresh tokens return nothing, which is fine
(the feature is simply absent and never penalises). For the ones with real
buzz, it adds signal.

Enable by setting MEMELAB_LUNARCRUSH_KEY. Without it, this is a no-op.

API (v4): GET https://lunarcrush.com/api4/public/topic/{topic}/v1
          Authorization: Bearer <key>
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

log = logging.getLogger("memelab.social")

_BASE = "https://lunarcrush.com/api4/public/topic"


def _f(x) -> Optional[float]:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


class SocialFeed:
    def __init__(self, api_key: str = "", session=None):
        self.api_key = api_key
        self._session = session

    @property
    def enabled(self) -> bool:
        return bool(self.api_key and self._session)

    async def metrics_for(self, symbol: str) -> Optional[dict]:
        """{'social_volume', 'social_sentiment', 'social_score'} for a symbol, or None."""
        if not self.enabled or not symbol:
            return None
        topic = symbol.strip().lower().lstrip("$")
        data = await self._get(f"{_BASE}/{topic}/v1")
        d = (data or {}).get("data") if isinstance(data, dict) else None
        if not isinstance(d, dict):
            return None
        vol = _f(d.get("interactions_24h")) or _f(d.get("social_volume_24h")) or _f(d.get("num_posts"))
        sent = _f(d.get("sentiment")) or _f(d.get("types_sentiment", {}).get("tweet") if isinstance(d.get("types_sentiment"), dict) else None)
        score = _f(d.get("social_score")) or _f(d.get("galaxy_score")) or _f(d.get("topic_rank"))
        if vol is None and sent is None and score is None:
            return None
        return {"social_volume": vol, "social_sentiment": sent, "social_score": score}

    async def _get(self, url: str):
        headers = {"Authorization": f"Bearer {self.api_key}"}
        for attempt in range(3):
            try:
                async with self._session.get(url, headers=headers) as r:
                    if r.status in (429, 502, 503, 504):
                        await asyncio.sleep(min(2.0, 0.4 * (2 ** attempt)))
                        continue
                    return await r.json() if r.status == 200 else None
            except Exception:  # noqa: BLE001
                if attempt < 2:
                    await asyncio.sleep(0.4 * (2 ** attempt))
                    continue
                return None
        return None
