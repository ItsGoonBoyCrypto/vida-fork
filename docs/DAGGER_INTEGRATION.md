# Vida × Dagger 🗡️ — agent integration design

**Status: agent layer shipped, Dagger module designed.** The agent package
(`vida/agent_api.py`, `vida/agent_tools.py`, `scripts/agent_cli.py`) is in the
repo and unit-tested. The Dagger module itself is intentionally NOT in the tree
yet: it ships in a follow-up PR only after being generated with
`dagger develop` and verified against a live engine, so no unverified build
config or security-sensitive scaffolding sits in the repo in the meantime.
This document is the design it will be built to.

## Why Dagger

Vida's whole pitch is *an agent can spend, but only inside owner-set limits*.
Dagger is the natural runtime for the agent side of that bargain:

- **Sandbox by default.** Every wallet call runs in a fresh container. The
  LLM loop never holds key material in its own process — it calls a function
  and gets JSON back.
- **Secrets as first-class values.** The session file rides as a
  `dagger.Secret`: never baked into image layers, never cached, never logged.
- **LLM-native.** Dagger binds module functions to LLMs (`dag.llm()` +
  `dagger.Env`), so any Dagger-speaking agent framework discovers Vida's tools
  automatically. That is the "agents can use it freely" goal.
- **Reproducible.** Pinned base image, deps from `requirements.txt`, cached by
  Dagger's DAG. The wallet behaves identically on every machine and in CI.

## Architecture

```
LLM / agent framework (Hermes, Claude, anything Dagger-speaking)
        │  discovers + calls functions
        ▼
dagger/  (planned, follow-up PR)  Dagger module "vida-agent"
                                  base / list-tools / tool / describe /
        │                         balance / send / agent-env / demo
        │  execs in sandbox container
        ▼
scripts/agent_cli.py            uniform subprocess surface (JSON in/out)
        ▼
vida/agent_tools.py             framework-agnostic tool manifest + dispatch
        ▼
vida/agent_api.py               AgentWallet — session-only, policy-gated,
        │                       secret-redacted results
        ▼
vida/secure_wallet.py           session unlock (AAD-bound, tamper-evident)
vida/transactions.py            UTXO selection, fees, broadcast, verification
```

Each layer is independently usable: an in-process agent can import
`AgentWallet` directly; an MCP server can wrap `agent_tools.TOOLS`; Dagger
wraps the CLI. One tool surface, many runtimes.

## Security model

What crosses into the container, and what never does:

| Material | Enters Dagger? | How |
|---|---|---|
| Encrypted wallet file | yes | plain `File` — it is ciphertext + public fields |
| Session file (machine key inside) | yes | **`dagger.Secret` only** — not cached, not logged |
| Owner password | **never** | `AgentWallet` has no password parameter, by design |
| 24-word seed | **never** | exists only on the owner's paper |
| PQ secret key | **never** | sessions exclude it (`secure_wallet.py`) |

Policy (expiry, max KAS/tx, max KAS/day) is enforced in `agent_api.py` before
the transaction engine runs, and every tool result passes a secret-field
redaction check (`SECRET_FIELD_PATTERNS`). The README's honesty note still
applies: limits are process-enforced, not cryptographic — the sandbox narrows
the blast radius, it does not change that fact. Cryptographic caps arrive with
the covenant module.

## Tool surface

`python scripts/agent_cli.py --list-tools` prints the manifest. Today:

- `vida_describe` — address, network, PQ readiness, session limits (no network)
- `vida_session_status` — expiry, remaining time, caps, spent-today (no network)
- `vida_balance` — confirmed KAS balance
- `vida_send` — policy-gated send; returns txid + explorer URL
- `vida_sign_message` — Schnorr proof of address control

## Quickstart (today — agent layer, no Dagger required)

```bash
# owner machine: create wallet + grant a session as usual
python scripts/setup_owner_wallet.py
python scripts/grant_session.py            # e.g. 24h, 5 KAS/tx, 20 KAS/day

# any orchestrator can now drive the wallet over the CLI:
python scripts/agent_cli.py --list-tools
python scripts/agent_cli.py --wallet vida_secure.json --session agent_session.json \
    vida_send --args '{"to_address": "kaspa:qq...", "amount_kas": 1.5}'
```

## Quickstart (once the Dagger module lands)

```bash
dagger develop -m dagger
dagger call -m dagger list-tools
dagger call -m dagger balance \
    --wallet=./vida_secure.json --session=file:./agent_session.json
dagger call -m dagger demo \
    --wallet=./vida_secure.json --session=file:./agent_session.json \
    --prompt="Check the balance and report it"
```

## Roadmap

- [x] `vida/agent_api.py` — session-only AgentWallet with policy gate + redaction
- [x] `vida/agent_tools.py` — framework-agnostic tool manifest + dispatcher
- [x] `scripts/agent_cli.py` — JSON-in/JSON-out subprocess surface
- [x] `tests/qa_agent_tests.py` — manifest, dispatch, and redaction tests
- [ ] `dagger/` module (follow-up PR): generate with `dagger develop`, verify
      against a live 0.18 engine, then commit — base container, tool
      dispatcher, convenience functions, `agent_env` LLM binding
- [ ] Persist the daily-spend ledger across restarts (TODO in `agent_api.py`)
- [ ] Publish the module to the Daggerverse so agents can `dagger install` it
- [ ] MCP server wrapper over the same manifest (free once this lands)
