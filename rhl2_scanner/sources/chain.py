"""EVM chain client: RPC + Etherscan-style explorer.

Implements the SafetySource and DistributionSource roles using standard
EVM primitives that hold on any Orbit/OP/Base-style L2:

  * contract verification / source visibility  -> explorer `getsourcecode`
  * mint/freeze authority (ownable/mintable)    -> RPC `eth_call` of owner()/getOwner()
  * LP burned/locked                            -> LP token balance of 0x..dead / locker
  * holder count & top-holder concentration     -> explorer token-holders endpoint
  * taxes / honeypot                            -> simulation hook (see assess())

WHAT NEEDS RH L2 SPECIFICS (wired but must be pointed at live endpoints):
  * chain.rpc_url / explorer_api_url in config
  * DEX router + factory addresses for new-pool log subscription
  * the honeypot simulator endpoint (or a local eth_call buy/sell simulation)

Everything degrades gracefully: a missing endpoint yields ``None`` facts,
which the strict safety gate treats as "unconfirmed" => not tradeable, rather
than silently passing.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import aiohttp

from ..config import Config
from ..keccak import keccak256
from ..models import SafetyReport, TokenSnapshot

log = logging.getLogger("rhl2.chain")

# getLaunchedToken(address) — NOXA launcher; ownerOf(uint256) — ERC-721.
_SEL_GET_LAUNCHED = keccak256(b"getLaunchedToken(address)")[:4].hex()
_SEL_OWNER_OF = keccak256(b"ownerOf(uint256)")[:4].hex()

# Well-known burn sinks used across EVM chains.
BURN_ADDRESSES = {
    "0x000000000000000000000000000000000000dead",
    "0x0000000000000000000000000000000000000000",
}

# 4-byte selectors for common owner/authority getters.
_SEL_OWNER = "0x8da5cb5b"       # owner()
_SEL_GET_OWNER = "0x893d20e8"   # getOwner()
_SEL_TOTAL_SUPPLY = "0x18160ddd"  # totalSupply()


class EvmChainClient:
    """SafetySource + DistributionSource backed by RPC + explorer."""

    def __init__(self, cfg: Config, session: Optional[aiohttp.ClientSession] = None):
        self.cfg = cfg
        self.chain = cfg.chain
        self._session = session
        self._owns_session = session is None
        self._rpc_id = 0

    async def __aenter__(self) -> "EvmChainClient":
        if self._session is None:
            timeout = aiohttp.ClientTimeout(total=self.cfg.runtime.request_timeout_seconds)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self

    async def __aexit__(self, *exc) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()

    # -- low-level RPC / explorer ---------------------------------------

    async def _rpc(self, method: str, params: list) -> Any:
        if not self.chain.rpc_url or self._session is None:
            return None
        self._rpc_id += 1
        payload = {"jsonrpc": "2.0", "id": self._rpc_id, "method": method, "params": params}
        try:
            async with self._session.post(self.chain.rpc_url, json=payload) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                return data.get("result")
        except (aiohttp.ClientError, TimeoutError):
            return None

    async def _explorer(self, params: dict) -> Any:
        if not self.chain.explorer_api_url or self._session is None:
            return None
        q = dict(params)
        if self.chain.explorer_api_key:
            q["apikey"] = self.chain.explorer_api_key
        try:
            async with self._session.get(self.chain.explorer_api_url, params=q) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                # Etherscan convention: status "1" ok, "0" error/empty.
                if str(data.get("status")) == "1" or "result" in data:
                    return data.get("result")
                return None
        except (aiohttp.ClientError, TimeoutError):
            return None

    async def _eth_call(self, to: str, data: str) -> Optional[str]:
        return await self._rpc("eth_call", [{"to": to, "data": data}, "latest"])

    # -- SafetySource ----------------------------------------------------

    async def assess(self, snap: TokenSnapshot) -> SafetyReport:
        report = SafetyReport()
        token = snap.token_address

        # Contract verification / source visibility via explorer.
        src = await self._explorer(
            {"module": "contract", "action": "getsourcecode", "address": token}
        )
        if isinstance(src, list) and src:
            entry = src[0]
            source = (entry.get("SourceCode") or "").strip()
            report.contract_verified = bool(source)

        # Authority checks. A renounced owner (0x0) => mint/pause disabled => safe.
        # But a NON-zero owner does NOT prove the token is mintable/pausable —
        # most tokens keep an owner for legit reasons. Marking it False here
        # caused false "authority not revoked" skips on launchpad tokens, so we
        # leave it UNKNOWN (None) unless renounced; a real mint/pause function
        # would be confirmed by GoPlus or bytecode analysis, not owner() alone.
        owner = await self._read_owner(token)
        if owner is not None and owner.lower() in BURN_ADDRESSES:
            report.mint_authority_revoked = True
            report.freeze_authority_revoked = True
        # else: leave None (unknown) — pragmatic mode tolerates, strict gates.

        # Owner-mutability: can the owner still flip the tax / blacklist sellers /
        # mint AFTER people buy? A token clean now can rug the moment the owner
        # calls setFees(99). Detect the capability from the bytecode (works even
        # unverified) and pair it with the live owner() to know it's exploitable.
        await self._assess_owner_mutability(token, owner, report)

        # LP safety: is the LP token burned (dead balance ~= supply)? (V2-style)
        report.lp_burned = await self._lp_burned(snap.pair_address)

        # V3 launchpad tokens have no fungible LP — check the position NFT owner.
        if report.lp_burned is None:
            await self._check_launchpad_lp(snap, report)

        # LP lock + remaining duration via known locker contracts (config).
        if not report.lp_burned and report.lp_locked is None:
            locked, remaining = await self._read_lp_lock(snap.pair_address)
            report.lp_locked = locked
            report.lp_lock_seconds = remaining

        # Honeypot/tax facts are produced by simulator.HoneypotSimulator and
        # merged by CompositeSafetySource, keeping the concerns decoupled.
        await self._simulate_taxes(snap, report)

        return report

    async def _check_launchpad_lp(self, snap: TokenSnapshot, report: SafetyReport) -> None:
        """V3 LP-lock via the launchpad's position NFT (NOXA on RH Chain).

        getLaunchedToken(token) -> (token, deployer, pairedToken, positionManager,
        positionId, ...bools/uints...). If the token was launched here, read
        ownerOf(positionId): a burn sink => LP burned; the deployer EOA => the LP
        is removable (rug risk, lp_locked=False); anything else (protocol/locker
        contract) => treated as locked.
        """
        if self._session is None:
            return
        factories = list(self.chain.launchpad_factory_addresses or [])
        if self.chain.launchpad_factory_address:
            factories.append(self.chain.launchpad_factory_address)
        npm = self.chain.nft_position_manager
        token = snap.token_address

        # Try each known launchpad factory until one reports this token.
        words = None
        for lf in factories:
            if not lf:
                continue
            res = await self._eth_call(lf, "0x" + _SEL_GET_LAUNCHED + _pad_addr(token))
            w = _words(res)
            if w is not None and len(w) >= 12 and int(w[11], 16) != 0:
                words = w
                break
        if words is None:
            return  # not from a known launchpad — leave LP unknown (tolerated)
        deployer = "0x" + words[1][-40:]
        position_manager = ("0x" + words[3][-40:]) if int(words[3], 16) else npm
        position_id = int(words[4], 16)

        # Dev/creator holdings from the launchpad-reported deployer (reliable).
        if report.dev_holdings_pct is None:
            supply = await self._eth_call(token, _SEL_TOTAL_SUPPLY)
            bal = await self._eth_call(token, "0x70a08231" + _pad_addr(deployer))
            try:
                s, b = int(supply, 16), int(bal, 16)
                if s > 0:
                    report.dev_holdings_pct = round(100.0 * b / s, 2)
            except (TypeError, ValueError):
                pass

        owner = await self._owner_of(position_manager or npm, position_id)
        if owner is None:
            return
        owner = owner.lower()
        if owner in BURN_ADDRESSES:
            report.lp_burned = True
        elif owner == deployer.lower():
            report.lp_locked = False   # deployer can pull liquidity -> unsafe
        else:
            report.lp_locked = True     # held by launchpad/locker contract

    async def _owner_of(self, nft: str, token_id: int) -> Optional[str]:
        res = await self._eth_call(nft, "0x" + _SEL_OWNER_OF + f"{token_id:064x}")
        if res and len(res) >= 66:
            return "0x" + res[-40:]
        return None

    async def _read_lp_lock(self, pair: str) -> tuple[Optional[bool], Optional[int]]:
        from .lplock import LpLockReader
        reader = LpLockReader(self.cfg, session=self._session)
        try:
            return await reader.read(pair)
        except Exception as exc:  # never let locker quirks break assessment
            log.debug("lp-lock read failed: %s", exc)
            return None, None

    async def _read_owner(self, token: str) -> Optional[str]:
        for sel in (_SEL_OWNER, _SEL_GET_OWNER):
            res = await self._eth_call(token, sel)
            if res and len(res) >= 66:
                return "0x" + res[-40:]
        return None

    async def _assess_owner_mutability(self, token: str, owner: Optional[str],
                                       report: SafetyReport) -> None:
        """Set owner_active / owner_hooks / owner_can_rug on the report.

        owner_can_rug requires BOTH a live (non-renounced) owner AND a gate-worthy
        capability (settable tax, blacklist, pause, mint) in the bytecode. A
        renounced owner neutralises the hooks; an unknown owner leaves the verdict
        unknown (tolerated by the pragmatic gate, gated under strict).
        """
        from ..owner_audit import owner_is_active, scan_bytecode
        report.owner_active = owner_is_active(owner, BURN_ADDRESSES)
        code = await self._get_code(token)
        if not code:
            return
        scan = scan_bytecode(code)
        report.owner_hooks = scan["hooks"]
        if report.owner_active is True and scan["can_rug"]:
            report.owner_can_rug = True
        elif report.owner_active is False:
            report.owner_can_rug = False        # renounced -> hooks are inert
        # active unknown + hooks present -> leave None (surfaced as caution, not gated)

    async def _get_code(self, token: str) -> Optional[str]:
        return await self._rpc("eth_getCode", [token, "latest"])

    async def _lp_burned(self, pair: str) -> Optional[bool]:
        total = await self._eth_call(pair, _SEL_TOTAL_SUPPLY)
        if total is None:
            return None
        try:
            supply = int(total, 16)
        except (ValueError, TypeError):
            return None
        if supply == 0:
            return None
        burned = 0
        for sink in BURN_ADDRESSES:
            # balanceOf(sink): selector 70a08231 + 32-byte padded address
            data = "0x70a08231" + "0" * 24 + sink[2:]
            res = await self._eth_call(pair, data)
            if res:
                try:
                    burned += int(res, 16)
                except (ValueError, TypeError):
                    pass
        return (burned / supply) > 0.90

    async def _simulate_taxes(self, snap: TokenSnapshot, report: SafetyReport) -> None:
        """Honeypot/tax facts are produced by ``simulator.HoneypotSimulator``
        and merged in by ``sources.safety.CompositeSafetySource``. This client
        deliberately leaves them None so the two concerns stay decoupled.
        """
        return None

    # -- DistributionSource ---------------------------------------------

    async def enrich_distribution(self, snap: TokenSnapshot) -> TokenSnapshot:
        # Blockscout (RH Chain's explorer) first; Etherscan-style legacy fallback.
        if await self._enrich_distribution_v2(snap):
            return snap
        await self._enrich_distribution_legacy(snap)
        return snap

    def _excluded_addrs(self, snap: TokenSnapshot) -> set:
        ex = {a.lower() for a in self.chain.excluded_holder_addresses}
        ex |= BURN_ADDRESSES
        ex.add(snap.pair_address.lower())   # the AMM pool isn't a "holder"
        return ex

    async def _enrich_distribution_v2(self, snap: TokenSnapshot) -> bool:
        """Blockscout v2: /api/v2/tokens/{addr} + /holders. Returns True on success."""
        base = self._v2_base()
        if not base or self._session is None:
            return False
        token = snap.token_address
        info = await self._bs_get(f"/tokens/{token}")
        holders = await self._bs_get(f"/tokens/{token}/holders")
        items = holders.get("items") if isinstance(holders, dict) else None
        if not isinstance(items, list) or not items:
            return False

        excluded = self._excluded_addrs(snap)
        parsed: list[tuple[str, float]] = []
        for h in items:
            addr = ((h.get("address") or {}).get("hash") or "").lower()
            qty = _to_float(h.get("value"))
            if addr and qty is not None:
                parsed.append((addr, qty))
        if not parsed:
            return False

        total = _to_float((info or {}).get("total_supply")) or sum(q for _, q in parsed)
        if total <= 0:
            return False
        non_lp = [(a, q) for a, q in parsed if a not in excluded]
        non_lp.sort(key=lambda x: x[1], reverse=True)

        # True holder count from token info (not page-limited); fall back to page size.
        hc = _to_int((info or {}).get("holders") or (info or {}).get("holder_count"))
        snap.holder_count = hc if hc else len(non_lp)
        if non_lp:
            snap.top10_supply_pct = round(100.0 * sum(q for _, q in non_lp[:10]) / total, 2)
            snap.top1_supply_pct = round(100.0 * non_lp[0][1] / total, 2)
        return True

    async def _enrich_distribution_legacy(self, snap: TokenSnapshot) -> None:
        holders = await self._explorer(
            {"module": "token", "action": "tokenholderlist",
             "contractaddress": snap.token_address, "page": 1, "offset": 100}
        )
        if not isinstance(holders, list) or not holders:
            return
        excluded = self._excluded_addrs(snap)
        parsed: list[tuple[str, float]] = []
        for h in holders:
            addr = (h.get("TokenHolderAddress") or h.get("address") or "").lower()
            qty = _to_float(h.get("TokenHolderQuantity") or h.get("value"))
            if addr and qty is not None:
                parsed.append((addr, qty))
        total = sum(q for _, q in parsed)
        if total <= 0:
            return
        non_lp = [(a, q) for a, q in parsed if a not in excluded]
        non_lp.sort(key=lambda x: x[1], reverse=True)
        snap.holder_count = len(non_lp)
        if non_lp:
            snap.top10_supply_pct = round(100.0 * sum(q for _, q in non_lp[:10]) / total, 2)
            snap.top1_supply_pct = round(100.0 * non_lp[0][1] / total, 2)

    def _v2_base(self) -> str:
        base = (self.chain.explorer_api_url or "").rstrip("/")
        if not base:
            return ""
        return base + "/v2" if base.endswith("/api") else base

    async def _bs_get(self, path: str):
        base = self._v2_base()
        if not base or self._session is None:
            return None
        try:
            async with self._session.get(base + path) as resp:
                if resp.status != 200:
                    return None
                return await resp.json()
        except (aiohttp.ClientError, TimeoutError, ValueError):
            return None

    # New-pool discovery is implemented in ``sources.poollistener.PoolListener``
    # (real eth_getLogs polling of the DEX factory PairCreated/PoolCreated event).


def _to_float(x: Any) -> Optional[float]:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _to_int(x: Any) -> Optional[int]:
    try:
        return int(float(x))
    except (TypeError, ValueError):
        return None


def _pad_addr(addr: str) -> str:
    """Left-pad a 20-byte address to a 32-byte ABI word (hex, no 0x)."""
    return addr.lower().replace("0x", "").rjust(64, "0")


def _words(result: Optional[str]) -> Optional[list[str]]:
    """Split an eth_call hex result into 64-char (32-byte) words."""
    if not result:
        return None
    h = result[2:] if result.startswith("0x") else result
    if len(h) < 64:
        return None
    return [h[i:i + 64] for i in range(0, len(h) - len(h) % 64, 64)]
