#!/usr/bin/env python3
"""
Vida agent CLI — one uniform subprocess surface for every tool call.

This is the entry point the Dagger 🗡️ module execs inside its container,
and it works identically for any orchestrator that shells out:

    python scripts/agent_cli.py --wallet vida_secure.json \
        --session agent_session.json \
        vida_send --args '{"to_address": "kaspa:...", "amount_kas": 1.5}'

Output contract: exactly one JSON object on stdout. Exit code 0 when the
tool executed (even if it returned success=False — a policy rejection is a
valid answer, not a crash); 2 for usage errors; 1 for unexpected failures.

    python scripts/agent_cli.py --list-tools   # print the manifest, no wallet needed
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "vida"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Vida agent tool dispatcher")
    parser.add_argument("--wallet", help="Path to the encrypted wallet JSON")
    parser.add_argument("--session", help="Path to the owner-granted session file")
    parser.add_argument("--list-tools", action="store_true",
                        help="Print the tool manifest as JSON and exit")
    parser.add_argument("tool", nargs="?", help="Tool name (see --list-tools)")
    parser.add_argument("--args", default="{}", help="Tool arguments as a JSON object")
    opts = parser.parse_args()

    if opts.list_tools:
        from agent_tools import manifest_json
        print(manifest_json())
        return 0

    if not (opts.wallet and opts.session and opts.tool):
        parser.print_usage(sys.stderr)
        print("error: --wallet, --session, and a tool name are required", file=sys.stderr)
        return 2

    try:
        args = json.loads(opts.args)
        if not isinstance(args, dict):
            raise ValueError("--args must be a JSON object")
    except ValueError as e:
        print(json.dumps({"success": False, "error": f"Bad --args JSON: {e}"}))
        return 2

    from agent_api import AgentWallet
    from agent_tools import dispatch

    async def run() -> dict:
        wallet = AgentWallet(opts.wallet, opts.session)
        try:
            return await dispatch(wallet, opts.tool, args)
        finally:
            await wallet.close()

    try:
        result = asyncio.run(run())
    except Exception as e:
        # Session expired/tampered, wallet missing, network down, etc.
        print(json.dumps({"success": False, "error": f"{type(e).__name__}: {e}"}))
        return 1

    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
