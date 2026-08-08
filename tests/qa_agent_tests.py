#!/usr/bin/env python3
"""
QA Test Suite for the Vida agent package (agent_api / agent_tools / agent_cli).

These tests cover the framework-agnostic layer: tool manifest shape, dispatch
validation, the no-password guarantee, and secret redaction. They run WITHOUT
the kaspa SDK or network access, so they pass anywhere Python 3.11 runs.
Full session round-trip tests (wallet + session + spend policy) additionally
require the kaspa SDK and are skipped with a notice when it is absent.
"""

import asyncio
import inspect
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "vida"))

from agent_api import AgentWallet, AgentWalletError, _assert_no_secrets, SECRET_FIELD_PATTERNS
from agent_tools import TOOLS, TOOL_NAMES, dispatch


class Colors:
    GREEN = "\033[92m"
    RED = "\033[91m"
    YELLOW = "\033[93m"
    RESET = "\033[0m"


passed, failed = 0, 0


def check(name: str, condition: bool, detail: str = ""):
    global passed, failed
    if condition:
        passed += 1
        print(f"{Colors.GREEN}✔ PASS{Colors.RESET} {name}")
    else:
        failed += 1
        print(f"{Colors.RED}✘ FAIL{Colors.RESET} {name} {detail}")


# ── Manifest shape ──────────────────────────────────────────────────────────

def test_manifest():
    check("manifest is non-empty", len(TOOLS) >= 5)
    check("tool names are unique", len(TOOL_NAMES) == len(set(TOOL_NAMES)))
    check("tool names are vida_-prefixed", all(n.startswith("vida_") for n in TOOL_NAMES))
    for t in TOOLS:
        schema = t.get("input_schema", {})
        ok = (
            schema.get("type") == "object"
            and isinstance(schema.get("properties"), dict)
            and isinstance(schema.get("required"), list)
            and set(schema["required"]) <= set(schema["properties"])
        )
        check(f"{t['name']} schema well-formed", ok)
        check(f"{t['name']} has a description", len(t.get("description", "")) > 20)


# ── Dispatch validation (no wallet needed for these paths) ──────────────────

def test_dispatch_validation():
    unknown = asyncio.run(dispatch(None, "vida_teleport", {}))
    check("unknown tool rejected", unknown.get("success") is False
          and "Unknown tool" in unknown.get("error", ""))

    missing = asyncio.run(dispatch(None, "vida_send", {"amount_kas": 1.0}))
    check("missing required arg rejected", missing.get("success") is False
          and "to_address" in missing.get("error", ""))


# ── The no-password guarantee ───────────────────────────────────────────────

def test_no_password_surface():
    params = inspect.signature(AgentWallet.__init__).parameters
    check("AgentWallet takes no password", "password" not in params)
    check("AgentWallet unlocks via session file only",
          set(params) == {"self", "wallet_path", "session_path"})


# ── Secret redaction ────────────────────────────────────────────────────────

def test_redaction():
    clean = {"address": "kaspa:qq...", "balance_kas": 5.0, "nested": [{"txid": "ab"}]}
    check("clean payload passes redaction", _assert_no_secrets(clean) is clean)

    for bad_key in ("private_key", "mnemonic", "machine_key", "enc_schnorr", "owner_password"):
        try:
            _assert_no_secrets({"ok": 1, "deep": {bad_key: "x"}})
            check(f"redaction catches '{bad_key}'", False)
        except AgentWalletError:
            check(f"redaction catches '{bad_key}'", True)

    check("redaction patterns cover the wallet file's secret fields",
          all(any(p in f for p in SECRET_FIELD_PATTERNS)
              for f in ("enc_seed", "enc_schnorr", "enc_pq_sk")))


# ── CLI contract ────────────────────────────────────────────────────────────

def test_cli():
    cli = REPO / "scripts" / "agent_cli.py"
    r = subprocess.run([sys.executable, str(cli), "--list-tools"],
                       capture_output=True, text=True)
    manifest_ok = False
    if r.returncode == 0:
        try:
            listed = json.loads(r.stdout)
            manifest_ok = [t["name"] for t in listed] == TOOL_NAMES
        except json.JSONDecodeError:
            pass
    check("cli --list-tools prints the manifest", manifest_ok, r.stderr.strip()[:120])

    r2 = subprocess.run([sys.executable, str(cli), "vida_balance"],
                        capture_output=True, text=True)
    check("cli rejects missing --wallet/--session with exit 2", r2.returncode == 2)


# ── Full round trip (requires kaspa SDK) ────────────────────────────────────

def test_session_round_trip():
    try:
        import kaspa  # noqa: F401
    except ImportError:
        print(f"{Colors.YELLOW}⚠ SKIP{Colors.RESET} session round-trip (kaspa SDK not installed)")
        return
    import tempfile
    from secure_wallet import create_secure_wallet, grant_agent_session

    with tempfile.TemporaryDirectory() as tmp:
        wallet_path = Path(tmp) / "w.json"
        session_path = Path(tmp) / "s.json"
        create_secure_wallet(wallet_path, "correct horse battery", network="testnet")
        grant_agent_session(wallet_path, "correct horse battery", session_path,
                            hours=1, max_kas_per_tx=2.0, max_kas_per_day=5.0)

        aw = AgentWallet(wallet_path, session_path)
        desc = aw.describe()
        check("describe returns address + session", desc["address"].startswith("kaspatest:")
              and desc["session"]["active"])
        check("per-tx cap surfaced", desc["session"]["max_kas_per_tx"] == 2.0)
        try:
            aw._enforce_policy(3.0)
            check("per-tx cap enforced", False)
        except AgentWalletError:
            check("per-tx cap enforced", True)
        aw._enforce_policy(1.5)  # within caps — must not raise
        check("in-policy amount accepted", True)


if __name__ == "__main__":
    print("── Vida agent package QA ──")
    test_manifest()
    test_dispatch_validation()
    test_no_password_surface()
    test_redaction()
    test_cli()
    test_session_round_trip()
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
