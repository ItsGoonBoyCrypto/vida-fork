"""Guard the /perf-driven rebalance: momentum & discovery are anti-predictive,
so they must stay minority weight vs safety + distribution."""

from __future__ import annotations

import unittest

from rhl2_scanner.config import Config


class TestWeightBalance(unittest.TestCase):
    def test_live_config_deprioritises_momentum_and_discovery(self):
        w = Config.load("rhl2_scanner/config/robinhood.example.yaml").weights
        # each anti-predictive signal weighs less than each protective one
        self.assertLess(w.momentum, w.safety)
        self.assertLess(w.momentum, w.distribution)
        self.assertLess(w.discovery, w.safety)
        self.assertLess(w.discovery, w.distribution)
        # and together they're a clear minority of the total weight
        self.assertLess(w.momentum + w.discovery, w.safety + w.distribution)


if __name__ == "__main__":
    unittest.main()
