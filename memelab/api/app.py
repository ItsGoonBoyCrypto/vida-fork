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
        from fastapi import Body, FastAPI, Header, Query
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("pip install fastapi uvicorn to serve the API") from exc

    from fastapi.responses import HTMLResponse
    import os

    api = FastAPI(title="memelab", version="0.1")
    store = Store(db)

    @api.get("/", response_class=HTMLResponse)
    def dashboard():
        here = os.path.dirname(__file__)
        with open(os.path.join(here, "dashboard.html"), encoding="utf-8") as fh:
            return fh.read()

    @api.get("/stats")
    def stats():
        cov = store.coverage()
        cov["smart_wallets"] = store.smart_wallet_count()
        return cov

    @api.get("/screen")
    def screen(chain: str | None = Query(default=None), min_score: float = 0.0,
               limit: int = 50):
        from ..screener.engine import Screener, rank_live
        sc = Screener(store)
        if not sc.reload_signature():
            return {"ready": False, "items": []}
        ch = Chain(chain) if chain else None
        return {"ready": True, "items": rank_live(store, sc, ch, min_score, limit)}

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

    @api.get("/smart-wallets")
    def smart_wallets_list():
        return {"items": store.smart_wallets_detailed(),
                "locked": bool(os.environ.get("MEMELAB_ADMIN_KEY"))}

    @api.post("/smart-wallets")
    def smart_wallets_add(payload: dict = Body(default={}),
                          x_admin_key: str = Header(default="")):
        # Optional guard: if MEMELAB_ADMIN_KEY is set, require it (header or body).
        key = os.environ.get("MEMELAB_ADMIN_KEY", "")
        if key and (x_admin_key or payload.get("key")) != key:
            return {"ok": False, "error": "unauthorized"}
        chain_s = str(payload.get("chain") or "robinhood").strip().lower()
        wallet = str(payload.get("wallet") or "").strip()
        remove = bool(payload.get("remove"))
        try:
            chain = Chain(chain_s)
        except ValueError:
            return {"ok": False, "error": f"unknown chain '{chain_s}'"}
        # Basic address sanity (EVM 0x… or Solana base58) — just non-empty + length.
        if not wallet or len(wallet) < 32:
            return {"ok": False, "error": "invalid wallet address"}
        if remove:
            gone = store.remove_smart_wallet(chain, wallet)
            return {"ok": True, "removed": gone, "count": store.smart_wallet_count()}
        added = store.add_smart_wallet(chain, wallet, source="dashboard")
        return {"ok": True, "added": added, "count": store.smart_wallet_count()}

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
# DB path from MEMELAB_DB so the dashboard service reads the collector's volume.
try:  # pragma: no cover
    import os as _os
    app = create_app(_os.environ.get("MEMELAB_DB", "memelab.db"))
except Exception:  # noqa: BLE001 — fine; import stays safe without fastapi
    app = None
