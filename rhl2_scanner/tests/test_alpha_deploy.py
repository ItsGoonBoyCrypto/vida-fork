"""Pre-index alpha trigger: a labelled deployer's new-token creation fires once."""

from __future__ import annotations

import unittest

from rhl2_scanner.config import Config
from rhl2_scanner.storage import Storage
from rhl2_scanner.walletwatch import WalletWatcher, format_deploy_html

DEV = "0x" + "d" * 40
NEW_TOKEN = "0x" + "a" * 40


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
    def __init__(self, txlist):
        self._txlist = txlist

    def get(self, url, params=None):
        if params and params.get("action") == "txlist":
            return _Resp({"status": "1", "result": self._txlist})
        return _Resp({"status": "1", "result": []})


def _cfg():
    cfg = Config()
    cfg.chain.explorer_api_url = "https://exp/api"
    cfg.wallet_watch.enabled = True
    cfg.wallet_watch.labels = {DEV: "USDG deployer"}
    return cfg


class TestDeployPoll(unittest.IsolatedAsyncioTestCase):
    async def test_new_deploy_fires_once_after_seed(self):
        creation = {"hash": "0xtx1", "to": "", "contractAddress": NEW_TOKEN}
        s = Storage(":memory:")
        try:
            ww = WalletWatcher(_cfg(), s, session=_Session([creation]))
            # first poll SEEDS history (no alert on pre-existing deploys)
            first = await ww.poll_deploys()
            self.assertEqual(first, [])
            # a genuinely new creation on the next poll fires
            ww2 = WalletWatcher(_cfg(), s, session=_Session([
                {"hash": "0xtx2", "to": "", "contractAddress": NEW_TOKEN}]))
            events = await ww2.poll_deploys()
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].token_address, NEW_TOKEN)
            self.assertEqual(events[0].label, "USDG deployer")
            # dedup: same creation again -> nothing
            ww3 = WalletWatcher(_cfg(), s, session=_Session([
                {"hash": "0xtx2", "to": "", "contractAddress": NEW_TOKEN}]))
            self.assertEqual(await ww3.poll_deploys(), [])
        finally:
            s.close()

    async def test_non_creation_txs_ignored(self):
        s = Storage(":memory:")
        try:
            # a normal tx (has `to`, no contractAddress) is not a deploy
            ww = WalletWatcher(_cfg(), s, session=_Session([
                {"hash": "0xtx", "to": "0x" + "9" * 40, "contractAddress": ""}]))
            await ww.poll_deploys()      # seed
            ww2 = WalletWatcher(_cfg(), s, session=_Session([
                {"hash": "0xtxb", "to": "0x" + "9" * 40, "contractAddress": ""}]))
            self.assertEqual(await ww2.poll_deploys(), [])
        finally:
            s.close()

    def test_format(self):
        from rhl2_scanner.walletwatch import DeployEvent
        html = format_deploy_html(DeployEvent(DEV, "USDG deployer", NEW_TOKEN, "0xtx"),
                                  explorer="https://exp")
        self.assertIn("ALPHA DEPLOY", html)
        self.assertIn("USDG deployer", html)
        self.assertIn(NEW_TOKEN, html)


if __name__ == "__main__":
    unittest.main()
