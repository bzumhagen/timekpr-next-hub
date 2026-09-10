"""FastAPI app entrypoint.

PLAN: "https://hub.example.com/api/v1". Run with:
    uvicorn timekpr_hub.app:app --host 0.0.0.0 --port 8000
(see deploy/docker-compose.yml for the containerized version, fronted by Caddy).
"""

from __future__ import annotations

from fastapi import FastAPI

from timekpr_hub.api import enroll, parent, sync, ui

app = FastAPI(title="timekpr-next-hub", version="0.1.0")

app.include_router(enroll.router, prefix="/api/v1", tags=["enrollment"])
app.include_router(sync.router, prefix="/api/v1", tags=["sync"])
app.include_router(parent.router, prefix="/api/v1", tags=["parent"])
app.include_router(ui.router, tags=["ui"])


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}
