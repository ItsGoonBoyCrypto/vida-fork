"""Query/serve layer — backend for the analytics UI (and programmatic access).

FastAPI stub. Surfaces what the platform knows: the live screen (ranked
candidates per chain), the active signature + its backtest stats, and ad-hoc
metric queries over the snapshot store. Keep it read-only over `Store`; the
ingest/backtest loops write.

    uvicorn memelab.api.app:app --reload

Endpoints (planned):
  GET /screen?chain=&min_score=      → ranked live candidates (Screen[])
  GET /signature                     → active Signature + precision/recall/lift
  GET /backtest/run                  → trigger/inspect a backtest run
  GET /token/{chain}/{addr}          → time-series + features + outcome
  GET /winners?chain=&min_mult=      → historical winners (the training set)
  GET /stats                         → coverage: tokens tracked, labeled, by chain
"""

from __future__ import annotations

# from fastapi import FastAPI, Query
# from ..storage import Store
# from ..models import Chain

# app = FastAPI(title="memelab")
# store = Store()

# @app.get("/screen")
# async def screen(chain: str | None = None, min_score: float = 60):
#     ...  # rank live Screens from the screener, filtered by chain/min_score

# @app.get("/signature")
# async def signature():
#     ...  # store.active_signature() + its validation metrics

# TODO: implement once storage + screener land. A lightweight React/HTML
# dashboard (or an Artifact) can consume these endpoints.
