"""Parent-facing login/logout/first-run-setup, and the two dependencies that
gate every other parent/UI route.

PLAN: "Parent auth" -- until this landed, every route in `api/parent.py` and
`api/ui.py` was reachable by anyone who could reach the hub at all (the one
open HIGH finding in docs/best-practices-review.md, which blocked pointing
`deploy/Caddyfile`'s HUB_DOMAIN at a real public domain). First account is
created via a first-run `/setup` page rather than a CLI command or an env
var: `/setup` 404s once a parent exists, so the window during which an
unclaimed hub is reachable is exactly "before the first parent visits it and
claims it" -- log a warning at startup naming that state.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import APIKeyCookie
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from timekpr_hub.db.models import Parent
from timekpr_hub.db.session import get_session
from timekpr_hub.services.audit import record_audit_event
from timekpr_hub.services.parent_auth import (
    SESSION_COOKIE_NAME,
    any_parent_exists,
    create_session,
    delete_session,
    get_parent_by_session_token,
    hash_password,
    verify_password,
)

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "web" / "templates"))

_cookie_scheme = APIKeyCookie(name=SESSION_COOKIE_NAME, auto_error=False)


class RequireLoginRedirect(Exception):
    """Raised by `get_current_parent_ui` for an HTML request with no valid
    session -- caught by an app-level exception handler (app.py) that
    redirects to /login, since a raw 401 is the wrong UX for a browser tab."""


def _set_session_cookie(response, request: Request, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        max_age=30 * 24 * 3600,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
    )


async def get_current_parent_api(
    token: str | None = Depends(_cookie_scheme), session: AsyncSession = Depends(get_session)
) -> Parent:
    if token:
        parent = await get_parent_by_session_token(session, token)
        if parent is not None:
            return parent
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not authenticated -- log in at /login")


async def get_current_parent_ui(
    token: str | None = Depends(_cookie_scheme), session: AsyncSession = Depends(get_session)
) -> Parent:
    if token:
        parent = await get_parent_by_session_token(session, token)
        if parent is not None:
            return parent
    raise RequireLoginRedirect()


@router.get("/setup", response_class=HTMLResponse)
async def setup_form(request: Request, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    if await any_parent_exists(session):
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return templates.TemplateResponse(request, "setup.html", {})


@router.post("/setup")
async def setup_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(..., min_length=8),
    session: AsyncSession = Depends(get_session),
):
    if await any_parent_exists(session):
        raise HTTPException(status.HTTP_404_NOT_FOUND)

    parent = Parent(email=email, password_hash=hash_password(password))
    session.add(parent)
    await session.flush()
    token = await create_session(
        session,
        parent_id=parent.id,
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    await session.commit()

    response = RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    _set_session_cookie(response, request, token)
    return response


@router.get("/login", response_class=HTMLResponse, response_model=None)
async def login_form(
    request: Request, session: AsyncSession = Depends(get_session)
) -> HTMLResponse | RedirectResponse:
    if not await any_parent_exists(session):
        return RedirectResponse("/setup", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(request, "login.html", {"error": request.query_params.get("error")})


@router.post("/login")
async def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(select(Parent).where(Parent.email == email))
    parent = result.scalar_one_or_none()
    if parent is None or not verify_password(password, parent.password_hash):
        return RedirectResponse("/login?error=1", status_code=status.HTTP_303_SEE_OTHER)

    ip = request.client.host if request.client else None
    token = await create_session(
        session, parent_id=parent.id, ip=ip, user_agent=request.headers.get("user-agent")
    )
    await record_audit_event(
        session,
        actor_type="parent",
        actor_id=str(parent.id),
        action="parent.login",
        target_type="parent",
        target_id=str(parent.id),
        ip=ip,
    )
    await session.commit()

    response = RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    _set_session_cookie(response, request, token)
    return response


@router.post("/logout")
async def logout(
    request: Request,
    token: str | None = Depends(_cookie_scheme),
    session: AsyncSession = Depends(get_session),
):
    if token:
        await delete_session(session, token)
        await session.commit()
    response = RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(SESSION_COOKIE_NAME)
    return response
