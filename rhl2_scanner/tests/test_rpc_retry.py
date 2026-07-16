"""Tests for transient RPC-error detection (rate-limit / timeout retry)."""

from __future__ import annotations

import unittest

from rhl2_scanner.sources.poollistener import _transient


class TestTransient(unittest.TestCase):
    def test_transient_markers(self):
        for msg in [
            'context deadline exceeded',
            'HTTP 429 rate limited',
            "Post \"http://10.31.82.32:8547/rpc\": context deadline exceeded",
            'too many requests',
            'server is busy',
            'request timed out',
        ]:
            self.assertTrue(_transient(msg), msg)

    def test_not_transient(self):
        for msg in [
            'invalid address: hex string without 0x prefix',
            'method not supported',
            'execution reverted',
            '',
        ]:
            self.assertFalse(_transient(msg), msg)


if __name__ == "__main__":
    unittest.main()
