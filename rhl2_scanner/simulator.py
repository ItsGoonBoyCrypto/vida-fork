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
# Uniswap V3 SwapRouter02.exactInputSingle((address,address,uint24,address,uint256,uint256,uint160))
# — derived at runtime so it can't drift from the ABI.
_SEL_V3_EXACT_IN_SINGLE = keccak256(
    b"exactInputSingle((address,address,uint24,address,uint256,uint256,uint160))"
)[:4].hex()


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
        self._slot_cache: dict[tuple, Optional[int]] = {}

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
        if chain.dex_router_kind == "univ3":
            await self._contract_sim_v3(snap, report)
        else:
            await self._contract_sim_v2(snap, report)

    async def _contract_sim_v2(self, snap: TokenSnapshot, report: SafetyReport) -> None:
        chain = self.cfg.chain
        code = _hexcode(chain.honeypot_simulator_bytecode)
        sim_addr = chain.honeypot_simulator_address
        amount = int(chain.honeypot_sim_amount_wei)
        selector = keccak256(b"simulate(address,address,address,uint256)")[:4].hex()
        data = (
            "0x" + selector + _addr(snap.token_address) + _addr(chain.dex_router_address)
            + _addr(chain.weth_address) + _w(amount)
        )
        overrides = {
            sim_addr.lower(): {"code": code},
            BURNER: {"balance": "0x" + _w(amount * 4)},
        }
        status, result = await self._call_status_raw(sim_addr, data, overrides, frm=BURNER, value=amount)
        if status == "revert":
            report.is_honeypot = True
            return
        if status != "ok" or not result:
            return
        words = _decode_words(result, 5)
        if words is None:
            return
        buy_bps, sell_bps, bought, sold_eth, ok = words
        report.buy_tax_pct = round(buy_bps / 100.0, 2)
        report.sell_tax_pct = round(sell_bps / 100.0, 2)
        report.is_honeypot = not (bool(ok) and bought > 0 and sold_eth > 0)

    async def _contract_sim_v3(self, snap: TokenSnapshot, report: SafetyReport) -> None:
        """V3 simulate: buy tax + round-trip loss + sellability, per fee tier.

        Reports buy_tax_pct (cleanly isolated) and is_honeypot (not sellable).
        Sell tax isn't isolatable on V3 in one call, so it's left None; the
        round-trip loss guards against punitive-tax/honeypot tokens.
        """
        chain = self.cfg.chain
        code = _hexcode(chain.honeypot_simulator_bytecode)
        sim_addr = chain.honeypot_simulator_address
        amount = int(chain.honeypot_sim_amount_wei)
        selector = keccak256(b"simulateV3(address,address,address,uint24,uint256)")[:4].hex()
        overrides = {
            sim_addr.lower(): {"code": code},
            BURNER: {"balance": "0x" + _w(amount * 4)},
        }
        for fee in chain.dex_v3_fee_tiers:
            data = (
                "0x" + selector + _addr(snap.token_address) + _addr(chain.dex_router_address)
                + _addr(chain.weth_address) + _w(int(fee)) + _w(amount)
            )
            status, result = await self._call_status_raw(sim_addr, data, overrides, frm=BURNER, value=amount)
            if status != "ok" or not result:
                continue
            words = _decode_words(result, 5)
            if words is None:
                continue
            buy_bps, roundtrip_bps, bought, sold_eth, ok = words
            if not (bool(ok) and bought > 0):
                continue   # no pool at this fee tier / not tradeable — try next
            report.buy_tax_pct = round(buy_bps / 100.0, 2)
            report.is_honeypot = sold_eth == 0 or roundtrip_bps >= 5000  # >=50% round-trip loss
            return
        # No tier produced a tradeable result -> leave facts None (sell-sim/fallback covers it)

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

        if chain.dex_router_kind == "univ3":
            # V3: a non-reverting sell is NOT proof of sellability — a honeypot
            # (near-100% sell tax, or proceeds siphoned by a transfer hook) lets
            # the swap execute with amountOutMinimum=0 and returns dust. So we
            # measure a value-matched round-trip (buy WETH->token, then sell the
            # tokens back) and only credit "sellable" when real proceeds come
            # back. Unmeasurable => None (unconfirmed), never a false "Sellable".
            report.is_honeypot = await self._v3_roundtrip(
                token, weth, router, bal_slot, report=report, snap=snap)
            return

        # V2: single pool per pair, so a revert is a meaningful "can't sell".
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

    async def _v3_out(self, router: str, token_in: str, token_out: str, fee: int,
                      amount_in: int, overrides: dict) -> tuple[str, Optional[int]]:
        """One V3 exactInputSingle leg. Returns (status, amountOut).

        exactInputSingle returns the realised output amount, so we can measure
        how much actually came back (not just that the call didn't revert).
        """
        data = (
            "0x" + _SEL_V3_EXACT_IN_SINGLE
            + _addr(token_in)      # tokenIn
            + _addr(token_out)     # tokenOut
            + _w(int(fee))         # fee tier
            + _addr(BURNER)        # recipient
            + _w(amount_in)        # amountIn
            + _w(0)                # amountOutMinimum (0 -> we judge the output ourselves)
            + _w(0)                # sqrtPriceLimitX96 (0 = no limit)
        )
        status, result = await self._call_status(router, data, overrides, frm=BURNER)
        if status != "ok" or not result:
            return status, None
        words = _decode_words(result, 1)
        return status, (words[0] if words else None)

    async def _v3_out_from(self, router: str, token_in: str, token_out: str, fee: int,
                           amount_in: int, overrides: dict, frm: str) -> tuple[str, Optional[int]]:
        """Like _v3_out but the swap originates FROM `frm` (a real holder), so an
        address-keyed blacklist that spares the burner still trips here."""
        data = (
            "0x" + _SEL_V3_EXACT_IN_SINGLE
            + _addr(token_in) + _addr(token_out) + _w(int(fee))
            + _addr(frm)           # recipient = the impersonated holder
            + _w(amount_in) + _w(0) + _w(0)
        )
        status, result = await self._call_status(router, data, overrides, frm=frm)
        if status != "ok" or not result:
            return status, None
        words = _decode_words(result, 1)
        return status, (words[0] if words else None)

    async def _v3_roundtrip(self, token: str, weth: str, router: str,
                            token_bal_slot: int, report=None, snap=None) -> Optional[bool]:
        """Value-matched round-trip honeypot check via eth_call + stateOverride.

        For each fee tier: buy (WETH->token) with an injected WETH balance, then
        sell those tokens back (token->WETH). Verdict:
          * buy works but sell reverts / returns 0  -> honeypot (True)
          * round-trip loses more than the sell-tax cap -> honeypot (True)
          * round-trip recovers most of the value     -> sellable  (False)
          * no tradeable pool / can't fund the probe  -> unconfirmed (None)
        Never asserts "sellable" without measured proceeds.
        """
        chain = self.cfg.chain
        probe = int(chain.honeypot_sim_amount_wei)          # WETH spent on the test buy
        max_loss_bps = int(round(self.cfg.runtime.honeypot_sell_tax_pct * 100))  # 50% -> 5000

        weth_bal_slot = await self._cached_slot(weth, "bal")
        if weth_bal_slot is None:
            return None                                     # can't fund a buy -> unconfirmed
        weth_allow_slot = await self._cached_slot(weth, "allow", router)
        token_allow_slot = await self._find_allowance_slot(token, router)

        weth_ov = self._bal_allow_override(weth, weth_bal_slot, weth_allow_slot, router)
        token_ov = self._bal_allow_override(token, token_bal_slot, token_allow_slot, router)

        saw_pool = False
        for fee in chain.dex_v3_fee_tiers:
            st_buy, tok_out = await self._v3_out(router, weth, token, int(fee), probe, weth_ov)
            if st_buy != "ok" or not tok_out:
                continue                                    # no pool at this tier
            saw_pool = True
            st_sell, eth_back = await self._v3_out(router, token, weth, int(fee), tok_out, token_ov)
            if st_sell == "revert" or not eth_back:
                return True                                 # bought fine, can't cash out -> honeypot
            loss_bps = int((1.0 - (eth_back / probe)) * 10000) if probe else 0
            if loss_bps >= max_loss_bps:
                return True                                 # punitive round-trip -> effective honeypot
            # Sellable at the test size — now run the extra scenarios that a
            # single round-trip misses (max-sell traps + selective blacklists).
            if report is not None:
                blacklisted = await self._extra_sell_scenarios(
                    token, weth, router, int(fee), tok_out, token_bal_slot,
                    token_allow_slot, report, snap)
                if blacklisted:
                    return True                             # real holders can't sell
            return False                                    # measured, real proceeds -> sellable
        return None if not saw_pool else False

    async def _extra_sell_scenarios(self, token, weth, router, fee, base_tokens,
                                    token_bal_slot, token_allow_slot, report, snap) -> bool:
        """Two scenarios beyond the base round-trip:

        1. MAX-SELL trap — retry the sell at a much larger size. If the small
           sell clears but a 25x sell reverts, the token caps sell size so you
           can't exit a real position (max-tx / max-sell). A caution, not a hard
           honeypot (you *can* sell small).
        2. SELECTIVE BLACKLIST — impersonate real top holders (inject their
           balance + allowance at THEIR address, not the burner's). An
           address-keyed blacklist reverts on those addresses even though the
           fresh burner sold fine — a can't-sell trap for actual buyers.
        """
        # 1) larger-size sell probe
        big = base_tokens * 25
        big_ov = self._bal_allow_override(token, token_bal_slot, token_allow_slot, router)
        st_big, out_big = await self._v3_out(router, token, weth, fee, big, big_ov)
        if st_big == "revert" or (out_big is not None and out_big == 0):
            if "max-sell limit (can't exit a full position)" not in report.high_risk_flags:
                report.high_risk_flags.append("max-sell limit (can't exit a full position)")

        # 2) real-holder impersonation (needs the token's actual top holders)
        holders = [h for h in (getattr(snap, "top_holders", None) or [])
                   if isinstance(h, str) and h.lower() != BURNER]
        blocked = 0
        for holder in holders[:3]:
            diff = {mapping_slot(holder, token_bal_slot): "0x" + _w(_BIG)}
            if token_allow_slot is not None:
                diff[nested_mapping_slot(holder, router, token_allow_slot)] = "0x" + _w(_BIG)
            ov = {token.lower(): {"stateDiff": diff}}
            st, out = await self._v3_out_from(router, token, weth, fee, base_tokens, ov, holder)
            if st == "revert" or (out is not None and out == 0):
                blocked += 1
        if blocked and blocked >= min(2, len(holders[:3])):
            if "selective blacklist (real holders can't sell)" not in report.high_risk_flags:
                report.high_risk_flags.append("selective blacklist (real holders can't sell)")
            return True                     # real holders can't sell -> honeypot
        return False

    def _bal_allow_override(self, token: str, bal_slot: int,
                            allow_slot: Optional[int], spender: str) -> dict:
        diff = {mapping_slot(BURNER, bal_slot): "0x" + _w(_BIG)}
        if allow_slot is not None:
            diff[nested_mapping_slot(BURNER, spender, allow_slot)] = "0x" + _w(_BIG)
        return {token.lower(): {"stateDiff": diff}}

    async def _cached_slot(self, token: str, kind: str,
                           spender: Optional[str] = None) -> Optional[int]:
        key = (token.lower(), kind, (spender or "").lower())
        if key in self._slot_cache:
            return self._slot_cache[key]
        if kind == "bal":
            slot = await self._find_balance_slot(token)
        else:
            slot = await self._find_allowance_slot(token, spender or "")
        self._slot_cache[key] = slot
        return slot

    async def _transfer_restricted(self, token: str, bal_slot: int) -> Optional[bool]:
        """Simulate a plain transfer; revert => restricted/honeypot-ish.

        Safety-first: a *working* plain transfer is NOT proof you can sell into
        the pool (most honeypots allow wallet-to-wallet transfers and only block
        sells), so this only ever escalates to True — it never asserts sellable.
        """
        overrides = {
            token.lower(): {"stateDiff": {mapping_slot(BURNER, bal_slot): "0x" + _w(_BIG)}}
        }
        # transfer(0x...dead, 1)
        data = "0xa9059cbb" + _addr("0x000000000000000000000000000000000000dEaD") + _w(1)
        status, _ = await self._call_status(token, data, overrides, frm=BURNER)
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
        err = (body.get("error") or {}).get("message", "").lower()
        # A true contract revert = "can't sell". Do NOT treat AMM-side
        # "INSUFFICIENT_LIQUIDITY / INSUFFICIENT_OUTPUT_AMOUNT" (no/thin pool) as
        # a revert — that misreads a token with no pool at this router as a
        # honeypot, which then permanently poisons the deployer/funder blocklist.
        insufficient_pool = ("insufficient_liquidity" in err
                             or "insufficient_output" in err
                             or "insufficient liquidity" in err)
        if not insufficient_pool and ("revert" in err or "execution reverted" in err):
            return "revert", err
        return "error", None


def _hexcode(code: str) -> str:
    return code if code.startswith("0x") else "0x" + code


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
