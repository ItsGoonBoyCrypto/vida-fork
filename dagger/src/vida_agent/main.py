"""
Vida 🗡️ Dagger module — the agent wallet as sandboxed, composable functions.

Why Dagger for an agent wallet:
  - SANDBOX: every wallet operation runs in a fresh container. The agent's
    LLM loop never holds key material in its own process — it calls a
    function, gets JSON back.
  - SECRETS DONE RIGHT: the owner-granted session file is passed as a
    dagger.Secret, so it is never written into image layers, never cached,
    and never appears in logs. The encrypted wallet file is ciphertext and
    rides along as a plain File.
  - LLM-NATIVE: Dagger binds module functions to LLMs (dagger.Env / dag.llm),
    so any agent framework that speaks Dagger can discover and call these
    functions freely — which is the point.
  - REPRODUCIBLE: pinned base image + pip install from requirements.txt,
    cached by Dagger's DAG. Same wallet behavior on every machine.

Quickstart (from the repo root, Dagger >= 0.18 installed):

    dagger develop -m dagger          # generate the client bindings
    dagger call -m dagger list-tools
    dagger call -m dagger balance --wallet=./vida_secure.json
    dagger call -m dagger send \
        --wallet=./vida_secure.json \
        --session=file:./agent_session.json \
        --to-address=kaspa:qq... --amount-kas=1.5

STATUS: full intended surface. Run `dagger develop -m dagger` once to
generate the client bindings, then `dagger call` / `dag.llm()` drive
everything below. Not yet exercised against a live engine in CI — do that
before publishing to the Daggerverse.
"""

from typing import Annotated

import dagger
from dagger import DefaultPath, Doc, dag, function, object_type

# Pinned for reproducibility. Bump deliberately, not accidentally.
BASE_IMAGE = "python:3.11-slim"

# Paths inside the sandbox container.
WALLET_PATH = "/data/wallet.json"
SESSION_PATH = "/run/secrets/vida_session.json"


@object_type
class VidaAgent:
    """Vida wallet operations for AI agents, each one sandboxed in a container."""

    @function
    def base(
        self,
        source: Annotated[
            dagger.Directory,
            DefaultPath("/"),
            Doc("The Vida repository root (defaults to the module's repo)"),
        ],
    ) -> dagger.Container:
        """Build the sandbox: pinned Python + Vida source + dependencies."""
        return (
            dag.container()
            .from_(BASE_IMAGE)
            .with_directory(
                "/app",
                source,
                # Never let local wallet/session files leak into the image.
                exclude=["**/*.json", "dagger/", ".git/", "venv/", "**/__pycache__/"],
            )
            .with_workdir("/app")
            .with_exec(["pip", "install", "--no-cache-dir", "-r", "requirements.txt"])
        )

    @function
    async def list_tools(
        self,
        source: Annotated[dagger.Directory, DefaultPath("/")],
    ) -> str:
        """Return the JSON tool manifest — what an agent is allowed to call."""
        return await (
            self.base(source)
            .with_exec(["python", "scripts/agent_cli.py", "--list-tools"])
            .stdout()
        )

    @function
    async def tool(
        self,
        source: Annotated[dagger.Directory, DefaultPath("/")],
        wallet: Annotated[dagger.File, Doc("Encrypted wallet JSON (ciphertext — safe to pass)")],
        session: Annotated[dagger.Secret, Doc("Owner-granted session file (sensitive — passed as a Secret)")],
        name: Annotated[str, Doc("Tool name from list-tools, e.g. vida_balance")],
        args_json: Annotated[str, Doc("Tool arguments as a JSON object")] = "{}",
    ) -> str:
        """
        Generic dispatcher: run any manifest tool in the sandbox, return its JSON.

        This is the one choke point every wallet call goes through — the
        convenience functions below all delegate here.
        """
        ctr = self.sandbox(source, wallet, session)
        return await ctr.with_exec(
            [
                "python", "scripts/agent_cli.py",
                "--wallet", WALLET_PATH,
                "--session", SESSION_PATH,
                name,
                "--args", args_json,
            ]
        ).stdout()

    # ── Convenience wrappers (nicer `dagger call` / LLM ergonomics) ───────

    @function
    async def describe(
        self,
        source: Annotated[dagger.Directory, DefaultPath("/")],
        wallet: dagger.File,
        session: dagger.Secret,
    ) -> str:
        """Wallet capability card: address, network, session limits."""
        return await self.tool(source, wallet, session, "vida_describe")

    @function
    async def balance(
        self,
        source: Annotated[dagger.Directory, DefaultPath("/")],
        wallet: dagger.File,
        session: dagger.Secret,
    ) -> str:
        """Confirmed KAS balance from the Kaspa network."""
        return await self.tool(source, wallet, session, "vida_balance")

    @function
    async def send(
        self,
        source: Annotated[dagger.Directory, DefaultPath("/")],
        wallet: dagger.File,
        session: dagger.Secret,
        to_address: Annotated[str, Doc("Destination kaspa:/kaspatest: address")],
        amount_kas: Annotated[float, Doc("Amount in KAS (> 0.02 dust threshold)")],
    ) -> str:
        """Policy-gated KAS send. Returns txid + explorer URL as JSON."""
        import json

        return await self.tool(
            source, wallet, session, "vida_send",
            json.dumps({"to_address": to_address, "amount_kas": amount_kas}),
        )

    # ── LLM binding (the "agents can use Dagger fully" part) ──────────────

    @function
    def sandbox(
        self,
        source: Annotated[dagger.Directory, DefaultPath("/")],
        wallet: dagger.File,
        session: dagger.Secret,
    ) -> dagger.Container:
        """A ready-to-use wallet sandbox: source + deps + wallet + session
        mounted. Every wallet capability is one `agent_cli.py` exec away."""
        return (
            self.base(source)
            .with_file(WALLET_PATH, wallet)
            .with_mounted_secret(SESSION_PATH, session)
        )

    @function
    def agent_env(
        self,
        source: Annotated[dagger.Directory, DefaultPath("/")],
        wallet: dagger.File,
        session: dagger.Secret,
    ) -> dagger.Env:
        """
        An LLM environment with the wallet sandbox bound as a tool surface.

        The sandbox Container is passed as a typed Env input, so the model
        gets the container's functions (with_exec, stdout, ...) as tools and
        can run any manifest tool via scripts/agent_cli.py — inside the
        sandbox, inside session limits, and nothing else. Compose it like:

            dag.llm().with_env(vida.agent_env(...)).with_prompt("...")
        """
        return (
            dag.env()
            .with_container_input(
                "wallet_sandbox",
                self.sandbox(source, wallet, session),
                "Sandbox with the Vida wallet mounted. Run tools with: "
                f"python scripts/agent_cli.py --wallet {WALLET_PATH} "
                f"--session {SESSION_PATH} <tool> --args '<json>'. "
                "Discover tools with: python scripts/agent_cli.py --list-tools. "
                "Call vida_describe first to learn your session limits. "
                "Never retry a policy rejection; report it to the owner.",
            )
        )

    @function
    async def demo(
        self,
        source: Annotated[dagger.Directory, DefaultPath("/")],
        wallet: dagger.File,
        session: dagger.Secret,
        prompt: Annotated[str, Doc("What the agent should do, in plain language")],
    ) -> str:
        """
        End-to-end demo: hand an LLM the wallet env and a task.

        Example:
            dagger call -m dagger demo \
                --wallet=./vida_secure.json --session=file:./agent_session.json \
                --prompt="Check the balance and report it with the address"
        """
        return await (
            dag.llm()
            .with_env(self.agent_env(source, wallet, session))
            .with_prompt(prompt)
            .last_reply()
        )
