"""Full-loop integration: stored snapshots → relabel → backtest → screen.

Proves the glue between every layer works on real stored data — feature
extraction from time-series, engine derivation, signature (de)serialisation via
the Store, and the screener loading + scoring.
"""

from __future__ import annotations

import time
import unittest

from memelab.models import Chain, TokenSnapshot
from memelab.storage import Store
from memelab.backtest.labeler import relabel_all
from memelab.backtest.engine import run_backtest
from memelab.screener.engine import Screener


def _insert(store, addr, first, winner):
    """3 snapshots: entry, +10min (early signal), +2h (outcome)."""
    if winner:
        early = dict(buys_5m=45, sells_5m=5, smart_money_wallets=["0xa", "0xb"],
                     holder_count=45, top10_supply_pct=25, volume_5m=2000, volume_1h=12000,
                     liquidity_usd=9000, market_cap_usd=60000)
        peak_price = 4.0
    else:
        early = dict(buys_5m=10, sells_5m=15, smart_money_wallets=[],
                     holder_count=8, top10_supply_pct=60, volume_5m=200, volume_1h=9000,
                     liquidity_usd=9000, market_cap_usd=60000)
        peak_price = 1.0
    store.record_snapshot(TokenSnapshot(chain=Chain.BASE, token_address=addr, ts=first,
                                        price_usd=1.0, liquidity_usd=8000, market_cap_usd=40000,
                                        holder_count=8))
    store.record_snapshot(TokenSnapshot(chain=Chain.BASE, token_address=addr, ts=first + 600,
                                        price_usd=1.3, **early))
    store.record_snapshot(TokenSnapshot(chain=Chain.BASE, token_address=addr, ts=first + 7200,
                                        price_usd=peak_price, liquidity_usd=9000))


class TestFullLoop(unittest.TestCase):
    def test_collect_to_screen(self):
        store = Store(":memory:")
        try:
            first = time.time() - 50 * 3600      # old enough to be labeled
            # interleave winners/duds so the time-split carries both classes
            for i in range(12):
                _insert(store, f"0x{2*i:040x}", first + i, winner=True)
                _insert(store, f"0x{2*i+1:040x}", first + i, winner=False)
                _insert(store, f"0x{100+i:040x}", first + i, winner=False)

            counts = relabel_all(store, win_multiple=3.0)
            self.assertGreaterEqual(counts["winner"], 10)
            self.assertGreaterEqual(counts["neutral"] + counts["rug"], 20)

            sig = run_backtest(store, win_multiple=3.0, chains=[Chain.BASE])
            self.assertGreater(sig.trained_on, 0)
            self.assertTrue(sig.rules, "backtest should derive rules from the signal")
            self.assertTrue(store.active_signature(), "signature persisted")

            # Screener loads the saved signature and scores a winner-shaped live
            # token above a dud-shaped one.
            sc = Screener(store)
            self.assertTrue(sc.reload_signature())
            win_live = [TokenSnapshot(chain=Chain.BASE, token_address="0xW", ts=1.0,
                        price_usd=1.0, buys_5m=45, sells_5m=5, holder_count=45,
                        top10_supply_pct=25, volume_5m=2000, volume_1h=12000,
                        smart_money_wallets=["0xa", "0xb"], liquidity_usd=9000,
                        market_cap_usd=60000)]
            dud_live = [TokenSnapshot(chain=Chain.BASE, token_address="0xD", ts=1.0,
                        price_usd=1.0, buys_5m=8, sells_5m=20, holder_count=6,
                        top10_supply_pct=65, volume_5m=100, volume_1h=8000,
                        smart_money_wallets=[], liquidity_usd=9000, market_cap_usd=60000)]
            win = sc.screen(Chain.BASE, "0xW", win_live)
            dud = sc.screen(Chain.BASE, "0xD", dud_live)
            self.assertGreater(win.score, dud.score)
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
