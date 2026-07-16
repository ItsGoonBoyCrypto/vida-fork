"""Bonding-curve launchpad listener — earliest catch for flap.sh-style launches.

flap.sh launches a token onto a constant-product BONDING CURVE, not straight
into a DEX pool. The token trades on the curve until ~80% of supply is bought,
then "graduates" (liquidity is moved to a DEX). Because of that, flap tokens
only show up on DexScreener *after* graduation — minutes-to-hours late for a
runner. To catch them at (or just after) launch we watch the curve manager
contract's logs directly and yield stubs the moment a token is created.

Design mirrors ``poollistener.py``: poll ``eth_getLogs`` on the manager
address, track the last scanned block, yield ``TokenSnapshot`` stubs that flow
into the same enrichment/scoring pipeline.

The exact event ABI for a given launchpad is not hardcoded — launchpads churn
and their docs aren't always reachable. Instead each launchpad entry carries a
``create_topic`` (topic0 of the creation event) and a ``token_arg`` telling us
where the new token address sits (``topic1``/``topic2`` for indexed args, or
``data0``/``data1``… for a data word). When ``create_topic`` is empty the
listener runs in DISCOVERY mode: it logs every distinct topic0 the manager
emits (with topic/data-word counts and candidate addresses) so the real
signature can be confirmed from the running host's logs, then pinned in config.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import aiohttp

from ..config import Config
from ..models import TokenSnapshot
from .poollistener import _backoff, _transient

log = logging.getLogger("rhl2.launchpad_curve")


def _addr_from_word(word_hex: str) -> str:
    """Last 20 bytes of a 32-byte hex word -> address (0x-prefixed)."""
    w = word_hex[2:] if word_hex.startswith("0x") else word_hex
    return "0x" + w[-40:]


def _norm_addr(addr: str) -> str:
    """Ensure a 0x prefix + lowercase for RPC log filters (see poollistener)."""
    a = (addr or "").strip()
    if a and not a.startswith("0x") and not a.startswith("0X"):
        a = "0x" + a
    return a.lower()


def _data_words(data_hex: str) -> list[str]:
    d = data_hex[2:] if data_hex.startswith("0x") else data_hex
    return [d[i:i + 64] for i in range(0, len(d), 64)]


def _extract_token(entry: dict, token_arg: str) -> Optional[str]:
    """Pull the new-token address from a creation log per ``token_arg``.

    token_arg forms: ``topic1``/``topic2``/``topic3`` (indexed) or
    ``data0``/``data1``/… (nth 32-byte data word). Defaults to ``topic1``.
    """
    topics = entry.get("topics") or []
    data = entry.get("data") or "0x"
    arg = (token_arg or "topic1").lower()
    try:
        if arg.startswith("topic"):
            idx = int(arg[len("topic"):] or "1")
            if idx < len(topics):
                return _addr_from_word(topics[idx])
        elif arg.startswith("data"):
            idx = int(arg[len("data"):] or "0")
            words = _data_words(data)
            if idx < len(words):
                return _addr_from_word(words[idx])
    except (ValueError, TypeError):
        return None
    return None


def _candidate_addresses(entry: dict) -> list[str]:
    """All plausible addresses in a log — for discovery-mode logging."""
    out: list[str] = []
    for t in (entry.get("topics") or [])[1:]:
        a = _addr_from_word(t)
        if int(a, 16) != 0:
            out.append(a)
    for w in _data_words(entry.get("data") or "0x"):
        # a data word is an address only if the top 12 bytes are zero
        if w[:24] == "0" * 24 and int(w, 16) != 0:
            out.append(_addr_from_word(w))
    return out


def _tokens_by_suffix(entry: dict, suffixes: list[str]) -> list[str]:
    """Candidate addresses ending in a flap vanity suffix (8888 / 7777).

    A deployed flap token's address ends in the configured suffix, which no
    trader/creator address or uint-leakage realistically matches — so this
    cleanly isolates the new token from the noise in a Portal event.
    """
    if not suffixes:
        return []
    out: list[str] = []
    for cand in _candidate_addresses(entry):
        low = cand.lower()
        if any(low.endswith(s) for s in suffixes) and low not in out:
            out.append(low)
    return out


class LaunchpadCurveListener:
    def __init__(self, cfg: Config, session: Optional[aiohttp.ClientSession] = None):
        self.cfg = cfg
        self.chain = cfg.chain
        self._session = session
        self._owns_session = session is None
        self._last_block: Optional[int] = None
        self._rpc_id = 0
        self._seen_topics: set[str] = set()   # discovery-mode dedupe
        self._emitted: set[str] = set()       # tokens already yielded this run

    async def __aenter__(self) -> "LaunchpadCurveListener":
        if self._session is None:
            timeout = aiohttp.ClientTimeout(total=self.cfg.runtime.request_timeout_seconds)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self

    async def __aexit__(self, *exc) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()

    def _curve_launchpads(self) -> list[dict]:
        return [lp for lp in self.cfg.configured_launchpads()
                if lp.get("kind", "curve") == "curve"]

    def enabled(self) -> bool:
        return bool(self.chain.rpc_url and self._curve_launchpads())

    async def poll_new_launches(self) -> list[TokenSnapshot]:
        """Return TokenSnapshot stubs for tokens created since the last poll."""
        if not self.enabled() or self._session is None:
            return []

        head = await self._block_number()
        if head is None:
            return []
        if self._last_block is None:
            self._last_block = max(0, head - self.chain.pool_scan_block_lookback)
        from_block = self._last_block + 1
        if from_block > head:
            return []

        snaps: dict[str, TokenSnapshot] = {}
        for lp in self._curve_launchpads():
            await self._poll_one(lp, from_block, head, snaps)

        self._last_block = head
        if snaps:
            log.info("curve listener found %d new launches (blocks %d-%d)",
                     len(snaps), from_block, head)
        return list(snaps.values())

    async def _poll_one(self, lp: dict, from_block: int, head: int,
                        snaps: dict[str, TokenSnapshot]) -> None:
        manager = lp.get("manager")
        if not manager:
            return
        create_topic = (lp.get("create_topic") or "").strip()
        token_arg = lp.get("token_arg") or "topic1"
        suffixes = [str(s).lower() for s in (lp.get("token_suffixes") or [])]
        name = lp.get("name", "launchpad")
        skip = {manager.lower(), (self.chain.weth_address or "").lower()}

        def _emit(token: str) -> None:
            low = token.lower()
            if not low or int(low, 16) == 0 or low in skip or low in self._emitted:
                return
            self._emitted.add(low)
            snaps[low] = TokenSnapshot(
                chain=self.chain.dexscreener_chain,
                pair_address="",              # no pool yet — still on the curve
                token_address=token,
                age_minutes=0.0,
                launchpad=name,
            )
            log.info("curve [%s]: new token %s", name, token)

        start = from_block
        while start <= head:
            end = min(start + self.chain.pool_scan_max_range - 1, head)
            topics = [create_topic] if create_topic else None
            logs = await self._get_logs(manager, start, end, topics)
            for entry in logs:
                if create_topic:                       # explicit event pinned
                    tok = _extract_token(entry, token_arg)
                    if tok:
                        _emit(tok)
                elif suffixes:                         # vanity-suffix matching
                    for tok in _tokens_by_suffix(entry, suffixes):
                        _emit(tok)
                else:                                  # pure discovery logging
                    self._discover(name, entry)
            start = end + 1

    def _discover(self, name: str, entry: dict) -> None:
        """Log a never-before-seen event shape so we can pin the real ABI."""
        topics = entry.get("topics") or []
        if not topics:
            return
        t0 = topics[0]
        if t0 in self._seen_topics:
            return
        self._seen_topics.add(t0)
        cands = _candidate_addresses(entry)
        log.info("curve discovery [%s]: topic0=%s topics=%d data_words=%d candidates=%s",
                 name, t0, len(topics), len(_data_words(entry.get("data") or "0x")),
                 ",".join(cands[:4]) or "none")

    # -- rpc -------------------------------------------------------------

    async def _rpc(self, method: str, params: list) -> Any:
        assert self._session is not None
        self._rpc_id += 1
        payload = {"jsonrpc": "2.0", "id": self._rpc_id, "method": method, "params": params}
        # Retry transient rate-limits/timeouts (see poollistener for rationale).
        for attempt in range(4):
            try:
                async with self._session.post(self.chain.rpc_url, json=payload) as resp:
                    if resp.status in (429, 503, 504):
                        await _backoff(attempt)
                        continue
                    if resp.status != 200:
                        log.warning("curve listener: RPC %s HTTP %s", method, resp.status)
                        return None
                    data = await resp.json()
                    if isinstance(data, dict) and data.get("error"):
                        if _transient(str(data["error"])) and attempt < 3:
                            await _backoff(attempt)
                            continue
                        log.warning("curve listener: RPC %s error: %s", method, data["error"])
                        return None
                    return data.get("result")
            except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
                if attempt < 3:
                    await _backoff(attempt)
                    continue
                log.warning("curve listener: RPC %s failed: %s", method, exc)
                return None
        return None

    async def _block_number(self) -> Optional[int]:
        res = await self._rpc("eth_blockNumber", [])
        try:
            return int(res, 16) if res else None
        except (ValueError, TypeError):
            return None

    async def _get_logs(self, address: str, from_block: int, to_block: int,
                        topics: Optional[list]) -> list[dict]:
        flt: dict[str, Any] = {
            "fromBlock": hex(from_block),
            "toBlock": hex(to_block),
            "address": _norm_addr(address),
        }
        if topics:
            flt["topics"] = topics
        res = await self._rpc("eth_getLogs", [flt])
        return res if isinstance(res, list) else []
