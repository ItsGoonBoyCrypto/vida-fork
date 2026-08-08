"""
Vida Agent API — the ONLY wallet surface an AI agent should touch.

This module frames the boundary between "the wallet" and "the agent":

  - Agents unlock with a time-boxed SESSION FILE (owner-granted via
    scripts/grant_session.py). There is deliberately NO password parameter
    anywhere in this module — an agent holding this API can never be handed
    the owner password by accident.
  - Every method returns a plain JSON-serializable dict, so results can be
    dropped straight into an LLM tool-result block, an MCP response, or a
    Dagger function return value without further massaging.
  - Session policy (expiry, max KAS per tx, max KAS per day) is enforced
    HERE, before the transaction engine is invoked. See the honesty note in
    the README: policy is process-enforced, not cryptographic.
  - No secret material ever appears in a return value. `qa_agent_tests.py`
    asserts this against SECRET_FIELD_PATTERNS.

Runtime-agnostic by design: the same AgentWallet drives
  - direct in-process use (Hermes or any local agent),
  - scripts/agent_cli.py (uniform subprocess surface),
  - the Dagger module in dagger/ (containerized, cache-friendly, LLM-bindable).

Imports of the kaspa SDK are deferred to construction time so this module —
and the tool manifest in agent_tools.py — can be imported and inspected on
machines without the SDK (e.g. an LLM host that only needs the schemas).
"""

import time
from pathlib import Path
from typing import Optional

# Field-name fragments that must never appear in any tool result.
# Kept public so tests (and future surfaces) can enforce the same guarantee.
SECRET_FIELD_PATTERNS = (
    "private", "secret", "seed", "mnemonic", "password", "machine_key",
    "enc_schnorr", "enc_seed", "enc_pq",
)


class AgentWalletError(Exception):
    """Raised for agent-facing failures. Message is safe to show an LLM."""


def _assert_no_secrets(result: dict) -> dict:
    """Defense-in-depth: refuse to return a payload with secret-ish keys."""
    def scan(obj, path=""):
        if isinstance(obj, dict):
            for k, v in obj.items():
                lk = str(k).lower()
                if any(p in lk for p in SECRET_FIELD_PATTERNS):
                    raise AgentWalletError(
                        f"Refusing to emit secret-like field '{path}{k}' in a tool result"
                    )
                scan(v, f"{path}{k}.")
        elif isinstance(obj, (list, tuple)):
            for i, v in enumerate(obj):
                scan(v, f"{path}{i}.")
    scan(result)
    return result


class AgentWallet:
    """
    Session-scoped wallet handle for autonomous agents.

    Usage:
        wallet = AgentWallet("vida_secure.json", "agent_session.json")
        info    = wallet.describe()                     # sync, no network
        status  = wallet.session_status()               # sync, no network
        balance = await wallet.balance()                # network
        result  = await wallet.send("kaspa:...", 1.5)   # network, policy-gated
        await wallet.close()
    """

    def __init__(self, wallet_path: str | Path, session_path: str | Path):
        # Deferred imports: these pull in the kaspa SDK.
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from secure_wallet import SecureVida
        from transactions import VidaTransactor

        self._session_path = Path(session_path)
        self._vida = SecureVida(wallet_path, _session_file=self._session_path)
        self._tx = VidaTransactor(self._vida)

        # In-memory daily-spend tracker for the max_kas_per_day cap.
        # TODO(frame): persist this ledger (0600 JSON next to the session file)
        # so the cap survives process restarts. Tracked for the covenant module
        # to eventually make cryptographic.
        self._spent_today_kas = 0.0
        self._spend_day = time.strftime("%Y-%m-%d")

    # ── Read-only, no network ─────────────────────────────────────────────

    def describe(self) -> dict:
        """Capability card: who am I, on what network, under what limits."""
        return _assert_no_secrets({
            "address": self._vida.address,
            "network": self._vida.network,
            "pq_ready": self._vida.pq_public_key is not None,
            "session": self.session_status(),
            "custody": "owner-held seed; agent operates inside a time-boxed session",
        })

    def session_status(self) -> dict:
        """Expiry + limits of the current session. Agents should check this
        before planning spends instead of discovering rejections mid-task."""
        expires_at = getattr(self._vida, "session_expires_at", None)
        limits = getattr(self._vida, "session_limits", {}) or {}
        remaining_s = max(0.0, expires_at - time.time()) if expires_at else 0.0
        return _assert_no_secrets({
            "active": self._session_path.exists() and remaining_s > 0,
            "expires_at": expires_at,
            "remaining_seconds": round(remaining_s, 1),
            "max_kas_per_tx": limits.get("max_kas_per_tx", 0.0),
            "max_kas_per_day": limits.get("max_kas_per_day", 0.0),
            "spent_today_kas": self._spent_today_kas,
        })

    # ── Policy gate ───────────────────────────────────────────────────────

    def _enforce_policy(self, amount_kas: float):
        """
        Session policy checks, applied BEFORE the transaction engine runs.

        Note: SecureVida sessions carry limits in the session file (AAD-bound,
        tamper-evident) but the transaction engine's session gate covers the
        legacy wallet.py path only — so the secure-session limits are enforced
        here, at the agent boundary. A limit of 0.0 means "no cap set".
        """
        status = self.session_status()
        if not status["active"]:
            raise AgentWalletError("Session expired or revoked — ask the owner for a new grant")
        cap_tx = status["max_kas_per_tx"]
        if cap_tx and amount_kas > cap_tx:
            raise AgentWalletError(
                f"Amount {amount_kas} KAS exceeds per-transaction cap of {cap_tx} KAS"
            )
        today = time.strftime("%Y-%m-%d")
        if today != self._spend_day:
            self._spend_day, self._spent_today_kas = today, 0.0
        cap_day = status["max_kas_per_day"]
        if cap_day and self._spent_today_kas + amount_kas > cap_day:
            raise AgentWalletError(
                f"Daily cap reached: spent {self._spent_today_kas} of {cap_day} KAS today"
            )

    # ── Network operations ────────────────────────────────────────────────

    async def balance(self) -> dict:
        """Confirmed balance in KAS."""
        bal = await self._tx.get_balance()
        return _assert_no_secrets({
            "address": self._vida.address,
            "network": self._vida.network,
            "balance_kas": bal,
        })

    async def send(self, to_address: str, amount_kas: float) -> dict:
        """Policy-gated send. Returns txid + explorer URL on success."""
        self._enforce_policy(amount_kas)
        result = await self._tx.send(to_address=to_address, amount_kas=amount_kas)
        if result.success:
            self._spent_today_kas += amount_kas
        return _assert_no_secrets({
            "success": result.success,
            "txid": result.txid,
            "amount_kas": result.amount_kas,
            "to_address": result.to_address,
            "fee_kas": result.fee_kas,
            "verified_on_network": result.verified_on_network,
            "explorer_url": result.explorer_url,
            "error": result.error,
        })

    def sign_message(self, message: str) -> dict:
        """Schnorr-sign an arbitrary message (proof of address control)."""
        sig = self._vida.sign(message)
        return _assert_no_secrets({
            "address": self._vida.address,
            "message": message,
            "signature": sig,
        })

    async def close(self):
        """Disconnect from the network and scrub key material."""
        await self._tx.disconnect()
        self._vida.lock()
