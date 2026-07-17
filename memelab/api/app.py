"""Read-only query/serve layer — backend for the analytics dashboard.

FastAPI, import-safe: if fastapi isn't installed, importing this module still
works; `create_app()` raises only when you actually try to serve. Endpoints are
read-only over the Store; the collector/backtest loops are the writers.

    pip install fastapi uvicorn
    uvicorn "memelab.api.app:app" --reload      # app built on import if fastapi present
"""

from __future__ import annotations

from ..models import Chain, signature_from_json
from ..storage import Store


def create_app(db: str = "memelab.db"):
    try:
        from fastapi import FastAPI, Query
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("pip install fastapi uvicorn to serve the API") from exc

    api = FastAPI(title="memelab", version="0.1")
    store = Store(db)

    @api.get("/stats")
    def stats():
        return store.coverage()

    @api.get("/signature")
    def signature():
        raw = store.active_signature()
        if not raw:
            return {"active": False}
        sig = signature_from_json(raw)
        return {"active": True, "chains": [c.value for c in sig.chains],
                "win_multiple": sig.win_multiple, "trained_on": sig.trained_on,
                "precision": sig.precision, "recall": sig.recall,
                "rules": sig.rules, "notes": sig.notes}

    @api.get("/token/{chain}/{address}")
    def token(chain: str, address: str):
        ts = store.time_series(Chain(chain), address)
        return {"chain": chain, "token": address, "outcome": ts.outcome.value,
                "peak_multiple": ts.peak_multiple, "snapshots": len(ts.snapshots),
                "entry_price": ts.entry_price}

    @api.get("/winners")
    def winners(chain: str | None = Query(default=None), min_mult: float = 3.0):
        ch = Chain(chain) if chain else None
        out = []
        for ts in store.labeled_tokens(chain=ch):
            if ts.peak_multiple >= min_mult:
                out.append({"chain": ts.chain.value, "token": ts.token_address,
                            "peak_multiple": round(ts.peak_multiple, 2)})
        out.sort(key=lambda x: x["peak_multiple"], reverse=True)
        return out[:200]

    return api


# Built on import when fastapi is available (uvicorn "memelab.api.app:app").
try:  # pragma: no cover
    app = create_app()
except Exception:  # noqa: BLE001 — fine; import stays safe without fastapi
    app = None
