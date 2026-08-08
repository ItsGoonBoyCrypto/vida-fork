"""
Vida agent tool manifest — framework-agnostic LLM tool definitions.

One manifest, many bindings:
  - Anthropic tool-use: pass TOOLS straight into the `tools` parameter.
  - MCP: each entry maps 1:1 onto an MCP tool (name / description / inputSchema).
  - Dagger 🗡️: the module in dagger/ exposes the same names as Dagger
    functions, so `dagger call` and Dagger's LLM bindings hit the same surface.
  - CLI: scripts/agent_cli.py dispatches by tool name for subprocess callers.

Schemas here are plain JSON Schema dicts with no SDK dependency, so an LLM
host can import this file to advertise the tools without installing kaspa.
Execution happens in `dispatch`, which is the single audited entry point.
"""

import json
from typing import Any

from agent_api import AgentWallet, AgentWalletError

TOOLS: list[dict] = [
    {
        "name": "vida_describe",
        "description": (
            "Describe this Vida wallet: Kaspa address, network, post-quantum "
            "readiness, and the current session's limits. No network calls. "
            "Call this first to learn what you are allowed to do."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "vida_session_status",
        "description": (
            "Check the agent session: active or expired, time remaining, "
            "per-transaction and per-day KAS caps, and KAS spent today. "
            "Check before planning spends."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "vida_balance",
        "description": "Get the wallet's confirmed KAS balance from the Kaspa network.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "vida_send",
        "description": (
            "Send KAS to an address. Enforced limits: session expiry, "
            "max KAS per transaction, max KAS per day, dust threshold "
            "(0.02 KAS). Returns the txid and an explorer URL on success. "
            "Failures return an explanatory error — do not retry a policy "
            "rejection; report it to the owner instead."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "to_address": {
                    "type": "string",
                    "description": "Destination address (kaspa: on mainnet, kaspatest: on testnet)",
                },
                "amount_kas": {
                    "type": "number",
                    "exclusiveMinimum": 0,
                    "description": "Amount in KAS (must exceed the 0.02 KAS dust threshold)",
                },
            },
            "required": ["to_address", "amount_kas"],
        },
    },
    {
        "name": "vida_sign_message",
        "description": (
            "Schnorr-sign an arbitrary text message with the wallet key, "
            "proving control of the address. Costs nothing; moves no funds."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "Message text to sign"},
            },
            "required": ["message"],
        },
    },
]

TOOL_NAMES = [t["name"] for t in TOOLS]


def _validate_args(tool: dict, args: dict):
    """Minimal required-field validation (full JSON Schema left to the host)."""
    for field in tool["input_schema"]["required"]:
        if field not in args:
            raise AgentWalletError(f"{tool['name']}: missing required argument '{field}'")


async def dispatch(wallet: AgentWallet, name: str, args: dict[str, Any] | None = None) -> dict:
    """
    Execute one tool call against an unlocked AgentWallet.

    Never raises for expected failures — policy rejections and bad input come
    back as {"success": False, "error": ...} so an LLM loop can read them.
    """
    args = args or {}
    tool = next((t for t in TOOLS if t["name"] == name), None)
    if tool is None:
        return {"success": False, "error": f"Unknown tool '{name}'. Available: {TOOL_NAMES}"}

    try:
        _validate_args(tool, args)
        if name == "vida_describe":
            return wallet.describe()
        if name == "vida_session_status":
            return wallet.session_status()
        if name == "vida_balance":
            return await wallet.balance()
        if name == "vida_send":
            return await wallet.send(str(args["to_address"]), float(args["amount_kas"]))
        if name == "vida_sign_message":
            return wallet.sign_message(str(args["message"]))
        return {"success": False, "error": f"Tool '{name}' is declared but not routed"}
    except AgentWalletError as e:
        return {"success": False, "error": str(e)}
    except (TypeError, ValueError) as e:
        return {"success": False, "error": f"Bad arguments for {name}: {e}"}


def manifest_json() -> str:
    """The manifest as JSON — what the Dagger module and MCP servers advertise."""
    return json.dumps(TOOLS, indent=2)
