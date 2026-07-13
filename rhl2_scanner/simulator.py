"""Honeypot / tax detection via on-chain simulation.

The single most important rug signal is: *can you actually sell?* This module
answers that without owning the token, using ``eth_call`` with ``stateOverride``:

  1. Detect the ERC-20 balance storage slot by brute-forcing candidate slots
     (override slot -> read balanceOf -> match) using real Solidity mapping-slot
     math (keccak256(pad(holder) . pad(slot))).
  2. Detect the allowance slot the same way (nested mapping).
  3. Override our burner's balance + router allowance, then simulate
     ``swapExactTokensForETHSupportingFeeOnTransferTokens`` against the DEX
     router. If it reverts -> not sellable (honeypot). If it succeeds -> sellable.

This is exactly the technique open-source honeypot checkers use, and it needs
only an RPC endpoint + the DEX router address — no third-party service and no
deployed contract. It is therefore the right fallback for a brand-new L2 (like
RH L2) that GoPlus / honeypot.is don't cover yet.

Exact buy/sell *tax magnitude* still needs either the external API strategy or
a deployed simulator contract that performs buy+sell in one call — that hook is
left explicit (``honeypot_api_url``). When nothing can determine a fact it stays
``None`` (=> the strict safety gate treats it as unconfirmed => not tradeable).
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import aiohttp

from .config import Config
from .keccak import keccak256, mapping_slot, nested_mapping_slot
from .models import SafetyReport, TokenSnapshot

log = logging.getLogger("rhl2.simulator")

BURNER = "0x000000000000000000000000000000000000babe"
_BIG = 10 ** 30
_MAX_SLOT = 30

# Function selectors (keccak-derived, well-known).
_SEL_BALANCEOF = "70a08231"
_SEL_ALLOWANCE = "dd62ed3e"
_SEL_DECIMALS = "313ce567"
_SEL_SWAP_FEE = "791ac947"   # swapExactTokensForETHSupportingFeeOnTransferTokens
_SEL_GET_AMOUNTS_OUT = "d06ca61f"


def _w(x: int | str) -> str:
    if isinstance(x, str):
        x = int(x, 16) if x.startswith("0x") else int(x)
    return f"{x:064x}"


def _addr(a: str) -> str:
    return a.lower().replace("0x", "").rjust(64, "0")


class HoneypotSimulator:
    def __init__(self, cfg: Config, session: Optional[aiohttp.ClientSession] = None):
        self.cfg = cfg
        self._session = session
        self._owns_session = session is None
        self._rpc_id = 0

    async def __aenter__(self) -> "HoneypotSimulator":
        if self._session is None:
            timeout = aiohttp.ClientTimeout(total=self.cfg.runtime.request_timeout_seconds)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self

    async def __aexit__(self, *exc) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()

    # -- public ----------------------------------------------------------

    async def check(self, snap: TokenSnapshot) -> SafetyReport:
        """Return a partial SafetyReport with honeypot/tax facts only.

        Strategy precedence (first that yields a honeypot verdict wins):
          1. deployed simulator contract (exact buy+sell tax, via stateOverride),
          2. external honeypot.is-style API (exact taxes),
          3. on-chain sell-simulation boolean (RPC-only, sellability).
        """
        report = SafetyReport()

        # 1) Buy+sell simulator contract -> exact tax %.
        if self.cfg.chain.honeypot_simulator_bytecode:
            await self._check_contract_sim(snap, report)
            if report.is_honeypot is not None:
                return report

        # 2) External honeypot API.
        if self.cfg.chain.honeypot_api_url:
            await self._check_api(snap, report)
            if report.is_honeypot is not None:
                return report

        # 3) On-chain sell simulation (works without any third party).
        await self._check_onchain(snap, report)
        return report

    # -- deployed simulator contract (exact taxes) -----------------------

    async def _check_contract_sim(self, snap: TokenSnapshot, report: SafetyReport) -> None:
        chain = self.cfg.chain
        if not (chain.rpc_url and chain.dex_router_address and chain.weth_address):
            return
        code = chain.honeypot_simulator_bytecode
        code = code if code.startswith("0x") else "0x" + code
        sim_addr = chain.honeypot_simulator_address
        amount = int(chain.honeypot_sim_amount_wei)

        selector = keccak256(b"simulate(address,address,address,uint256)")[:4].hex()
        data = (
            "0x" + selector
            + _addr(snap.token_address)
            + _addr(chain.dex_router_address)
            + _addr(chain.weth_address)
            + _w(amount)
        )
        overrides = {
            sim_addr.lower(): {"code": code},
            BURNER: {"balance": "0x" + _w(amount * 4)},   # fund the caller to send `value`
        }
        status, result = await self._call_status_raw(
            sim_addr, data, overrides, frm=BURNER, value=amount
        )
        if status == "revert":
            # A revert inside the simulator's sell path = not sellable.
            report.is_honeypot = True
            return
        if status != "ok" or not result:
            return   # couldn't determine -> leave None
        words = _decode_words(result, 5)
        if words is None:
            return
        buy_bps, sell_bps, bought, sold_eth, ok = words
        report.buy_tax_pct = round(buy_bps / 100.0, 2)
        report.sell_tax_pct = round(sell_bps / 100.0, 2)
        report.is_honeypot = not (bool(ok) and bought > 0 and sold_eth > 0)

    # -- external API strategy -------------------------------------------

    async def _check_api(self, snap: TokenSnapshot, report: SafetyReport) -> None:
        assert self._session is not None
        url = self.cfg.chain.honeypot_api_url
        params = {"address": snap.token_address, "chainID": str(self.cfg.chain.chain_id)}
        try:
            async with self._session.get(url, params=params) as resp:
                if resp.status != 200:
                    return
                data = await resp.json()
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            log.debug("honeypot api failed: %s", exc)
            return
        # honeypot.is-compatible shape: {"honeypotResult":{"isHoneypot":bool},
        #  "simulationResult":{"buyTax":..,"sellTax":..}}
        hp = (data or {}).get("honeypotResult") or {}
        sim = (data or {}).get("simulationResult") or {}
        if "isHoneypot" in hp:
            report.is_honeypot = bool(hp["isHoneypot"])
        if "buyTax" in sim:
            report.buy_tax_pct = _to_float(sim["buyTax"])
        if "sellTax" in sim:
            report.sell_tax_pct = _to_float(sim["sellTax"])

    # -- on-chain simulation ---------------------------------------------

    async def _check_onchain(self, snap: TokenSnapshot, report: SafetyReport) -> None:
        chain = self.cfg.chain
        if not chain.rpc_url or self._session is None:
            return
        token = snap.token_address
        router = chain.dex_router_address
        weth = chain.weth_address

        bal_slot = await self._find_balance_slot(token)
        if bal_slot is None:
            log.debug("balance slot not found for %s; cannot simulate", token)
            return

        # Without a router/weth we can still detect transfer-restriction honeypots.
        if not router or not weth:
            report.is_honeypot = await self._transfer_restricted(token, bal_slot)
            return

        allow_slot = await self._find_allowance_slot(token, router)
        decimals = await self._decimals(token) or 18
        amount_in = 10 ** decimals  # 1 whole token: minimal price impact

        overrides = {
            token.lower(): {
                "stateDiff": {
                    mapping_slot(BURNER, bal_slot): "0x" + _w(_BIG),
                }
            }
        }
        if allow_slot is not None:
            overrides[token.lower()]["stateDiff"][
                nested_mapping_slot(BURNER, router, allow_slot)
            ] = "0x" + _w(_BIG)

        data = (
            "0x" + _SEL_SWAP_FEE
            + _w(amount_in)            # amountIn
            + _w(0)                    # amountOutMin
            + _w(160)                  # offset to path (5 words)
            + _addr(BURNER)            # to
            + _w(10 ** 18)             # deadline (far future)
            + _w(2)                    # path length
            + _addr(token)
            + _addr(weth)
        )
        status, _ = await self._call_status(router, data, overrides, frm=BURNER)
        if status == "ok":
            report.is_honeypot = False
        elif status == "revert":
            report.is_honeypot = True
        # status "error" (rpc/no-node) -> leave None (unconfirmed)

    async def _transfer_restricted(self, token: str, bal_slot: int) -> Optional[bool]:
        """Fallback: simulate a plain transfer; revert => restricted/honeypot-ish."""
        overrides = {
            token.lower(): {"stateDiff": {mapping_slot(BURNER, bal_slot): "0x" + _w(_BIG)}}
        }
        # transfer(0x...dead, 1)
        data = "0xa9059cbb" + _addr("0x000000000000000000000000000000000000dEaD") + _w(1)
        status, _ = await self._call_status(token, data, overrides, frm=BURNER)
        if status == "ok":
            return False
        if status == "revert":
            return True
        return None

    # -- slot detection --------------------------------------------------

    async def _find_balance_slot(self, token: str) -> Optional[int]:
        for slot in range(_MAX_SLOT):
            key = mapping_slot(BURNER, slot)
            overrides = {token.lower(): {"stateDiff": {key: "0x" + _w(_BIG)}}}
            data = "0x" + _SEL_BALANCEOF + _addr(BURNER)
            res = await self._eth_call(token, data, overrides)
            if res and _hex_to_int(res) == _BIG:
                return slot
        return None

    async def _find_allowance_slot(self, token: str, spender: str) -> Optional[int]:
        for slot in range(_MAX_SLOT):
            key = nested_mapping_slot(BURNER, spender, slot)
            overrides = {token.lower(): {"stateDiff": {key: "0x" + _w(_BIG)}}}
            data = "0x" + _SEL_ALLOWANCE + _addr(BURNER) + _addr(spender)
            res = await self._eth_call(token, data, overrides)
            if res and _hex_to_int(res) == _BIG:
                return slot
        return None

    async def _decimals(self, token: str) -> Optional[int]:
        res = await self._eth_call(token, "0x" + _SEL_DECIMALS, None)
        return _hex_to_int(res) if res else None

    # -- rpc plumbing ----------------------------------------------------

    async def _eth_call(self, to: str, data: str, overrides: Optional[dict]) -> Optional[str]:
        status, result = await self._call_status_raw(to, data, overrides, None)
        return result if status == "ok" else None

    async def _call_status(self, to: str, data: str, overrides: Optional[dict], frm: Optional[str]):
        return await self._call_status_raw(to, data, overrides, frm)

    async def _call_status_raw(self, to, data, overrides, frm, value=None):
        """Return ('ok', result) | ('revert', msg) | ('error', None)."""
        if not self.cfg.chain.rpc_url or self._session is None:
            return "error", None
        tx: dict[str, Any] = {"to": to, "data": data}
        if frm:
            tx["from"] = frm
        if value is not None:
            tx["value"] = hex(value)
        params: list = [tx, "latest"]
        if overrides:
            params.append(overrides)
        self._rpc_id += 1
        payload = {"jsonrpc": "2.0", "id": self._rpc_id, "method": "eth_call", "params": params}
        try:
            async with self._session.post(self.cfg.chain.rpc_url, json=payload) as resp:
                if resp.status != 200:
                    return "error", None
                body = await resp.json()
        except (aiohttp.ClientError, TimeoutError, ValueError):
            return "error", None
        if "result" in body:
            return "ok", body["result"]
        err = (body.get("error") or {}).get("message", "")
        if "revert" in err.lower() or "execution reverted" in err.lower() or "insufficient" in err.lower():
            return "revert", err
        return "error", None


def _decode_words(result: str, n: int) -> Optional[tuple[int, ...]]:
    """Decode the first ``n`` 32-byte words of an ABI-encoded return blob."""
    h = result[2:] if result.startswith("0x") else result
    if len(h) < n * 64:
        return None
    try:
        return tuple(int(h[i * 64:(i + 1) * 64], 16) for i in range(n))
    except ValueError:
        return None


def _hex_to_int(x: Optional[str]) -> Optional[int]:
    if not x or x == "0x":
        return None
    try:
        return int(x, 16)
    except (ValueError, TypeError):
        return None


def _to_float(x: Any) -> Optional[float]:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None
