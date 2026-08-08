# Vida 🗡️ Dagger module

The Vida agent wallet, exposed as sandboxed [Dagger](https://dagger.io) functions
that AI agents can discover and call freely.

Full design + security model: [`docs/DAGGER_INTEGRATION.md`](../docs/DAGGER_INTEGRATION.md)

```bash
# from the repo root
dagger develop -m dagger        # generate client bindings (run once, and after SDK bumps)
dagger call -m dagger list-tools
dagger call -m dagger balance \
    --wallet=./vida_secure.json \
    --session=file:./agent_session.json
```

The wallet file is ciphertext and travels as a plain `File`. The session file
is sensitive and always travels as a `dagger.Secret` — never cached, never in
image layers, never logged. The owner seed and password never enter Dagger at all.
