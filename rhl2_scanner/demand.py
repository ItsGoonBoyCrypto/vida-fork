"""Demand-quality signal — real buyers vs wash/distribution.

Median memecoin rug life is under an hour, and the tell is almost always the
same: heavy VOLUME with no matching HOLDER growth. Real demand adds holders;
wash trading and distribution are the same hands (or bots) ping-ponging supply
while the operator dumps into the churn. So we cross-check turnover against
holder growth:

  · lots of turnover but few holders for the token's age  -> wash/distribution
    (a rug forming) -> demote + warn;
  · healthy holder base growing with the volume           -> real demand -> boost.

Pure + dependency-free; every branch only fires when the inputs are known, so a
genuinely data-poor fresh token stays neutral.
"""

from __future__ import annotations

from typing import Optional


def demand_signal(holder_count: Optional[int], volume_1h: Optional[float],
                  mcap: Optional[float], age_minutes: Optional[float],
                  holder_growth_1h: Optional[int] = None) -> tuple[float, str]:
    """(conviction_delta, note). Neutral (0, "") when inputs are too thin."""
    if not mcap or not volume_1h or holder_count is None:
        return 0.0, ""
    turnover = volume_1h / mcap                 # 1h volume as a fraction of mcap
    age = age_minutes if age_minutes is not None else 999.0

    # WASH / DISTRIBUTION: churny turnover but a thin holder base. Strongest tell
    # when the token isn't brand-new (a 5-min-old token legitimately has few).
    if turnover >= 1.0 and holder_count < 60 and age >= 20:
        return -12.0, (f"🩸 {turnover:.1f}x mcap traded/1h but only {holder_count} "
                       "holders — wash/distribution (rug-forming tell)")
    if holder_growth_1h is not None and holder_growth_1h <= 1 and turnover >= 1.5 and age >= 20:
        return -8.0, (f"🩸 heavy volume, +{holder_growth_1h} holders/1h — churn, not demand")

    # REAL DEMAND: a healthy, growing holder base relative to age.
    if holder_growth_1h is not None and holder_growth_1h >= 40:
        return 6.0, f"📈 +{holder_growth_1h} holders/1h — real demand"
    if holder_count >= 150 and turnover >= 0.3:
        return 4.0, f"📈 {holder_count} holders with live volume — real demand"
    return 0.0, ""
