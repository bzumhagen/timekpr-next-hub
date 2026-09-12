"""FastAPI app entrypoint.

PLAN: "https://hub.example.com/api/v1". Run with:
    uvicorn timekpr_hub.app:app --host 0.0.0.0 --port 8000
(see deploy/docker-compose.yml for the containerized version, fronted by Caddy).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.metadata import version

from fastapi import Depends, FastAPI, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from timekpr_hub.api import enroll, parent, parent_auth, sync, ui
from timekpr_hub.api.parent_auth import RequireLoginRedirect, get_current_parent_api, get_current_parent_ui
from timekpr_hub.db.models import Parent
from timekpr_hub.db.session import SessionLocal, engine
from timekpr_hub.logging_config import configure_logging

configure_logging()
log = logging.getLogger("timekpr_hub")


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    # Best-effort only: this uses the module-level SessionLocal (the app's
    # own configured DATABASE_URL), not whatever a test has overridden
    # get_session to -- so under a test harness (or if the DB simply isn't
    # up yet at boot) this must never block startup. Worst case, the
    # warning is silently skipped once and reappears on the next restart.
    try:
        async with SessionLocal() as session:
            result = await session.execute(select(Parent.id).limit(1))
            if result.first() is None:
                log.warning(
                    "no parent account exists yet -- this hub is reachable by anyone who can "
                    "reach it until the first account is created at /setup. Claim it before "
                    "exposing this hub beyond your LAN."
                )
    except Exception:
        log.debug("skipped the unclaimed-hub startup check (database not reachable yet)", exc_info=True)
    yield
    # Best-effort here too: tests/e2e/harness.py disposes this same
    # module-level engine itself around each run, so a second dispose() on
    # an already-disposed engine (or one bound to a loop that's already
    # closing) must never raise and take the process down with it.
    try:
        await engine.dispose()
    except Exception:
        log.debug("engine.dispose() raised during shutdown", exc_info=True)


app = FastAPI(title="timekpr-next-hub", version=version("timekpr-hub"), lifespan=_lifespan)

# Device- and code-authenticated endpoints: no parent session involved.
app.include_router(enroll.router, prefix="/api/v1", tags=["enrollment"])
app.include_router(sync.router, prefix="/api/v1", tags=["sync"])

# Login/logout/first-run-setup: deliberately unauthenticated (that's the point).
app.include_router(parent_auth.router, tags=["parent-auth"])

# Everything else requires a logged-in parent (PLAN "Parent auth") -- a JSON
# 401 for the API, a redirect to /login for the HTML UI (RequireLoginRedirect
# below).
app.include_router(
    parent.router, prefix="/api/v1", tags=["parent"], dependencies=[Depends(get_current_parent_api)]
)
app.include_router(ui.router, tags=["ui"], dependencies=[Depends(get_current_parent_ui)])


@app.exception_handler(RequireLoginRedirect)
async def _redirect_unauthenticated_ui_request_to_login(request: Request, exc: RequireLoginRedirect):
    return RedirectResponse("/login", status_code=303)


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}
