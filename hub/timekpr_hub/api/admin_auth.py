"""Admin-facing login/logout/first-run-setup, and the two dependencies that
gate every other admin/UI route.

Without this, every route in `api/admin/` and `api/ui/` would be
reachable by anyone who could reach the hub at all. The first account is
created via a first-run `/setup` page rather than a CLI command or an env
var: `/setup` 404s once an admin exists, so the window during which an
unclaimed hub is reachable is exactly "before the first admin visits it and
claims it" -- log a warning at startup naming that state.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import APIKeyCookie
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from timekpr_hub.api.util import client_ip, templates
from timekpr_hub.db.models import Admin
from timekpr_hub.db.session import get_session
from timekpr_hub.services.admin_auth import (
    SESSION_COOKIE_NAME,
    InviteError,
    any_admin_exists,
    create_session,
    delete_session,
    get_admin_by_session_token,
    hash_password,
    redeem_invite,
    verify_password,
)
from timekpr_hub.services.audit import record_audit_event

router = APIRouter()

_cookie_scheme = APIKeyCookie(name=SESSION_COOKIE_NAME, auto_error=False)


class RequireLoginRedirect(Exception):
    """Raised by `get_current_admin_ui` for an HTML request with no valid
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


async def get_current_admin_api(
    token: str | None = Depends(_cookie_scheme), session: AsyncSession = Depends(get_session)
) -> Admin:
    if token:
        admin = await get_admin_by_session_token(session, token)
        if admin is not None:
            return admin
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not authenticated -- log in at /login")


async def get_current_admin_ui(
    token: str | None = Depends(_cookie_scheme), session: AsyncSession = Depends(get_session)
) -> Admin:
    if token:
        admin = await get_admin_by_session_token(session, token)
        if admin is not None:
            return admin
    raise RequireLoginRedirect()


@router.get("/setup", response_class=HTMLResponse)
async def setup_form(request: Request, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    if await any_admin_exists(session):
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return templates.TemplateResponse(request, "setup.html", {})


@router.post("/setup")
async def setup_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(..., min_length=8),
    session: AsyncSession = Depends(get_session),
):
    if await any_admin_exists(session):
        raise HTTPException(status.HTTP_404_NOT_FOUND)

    password_hash = await run_in_threadpool(hash_password, password)
    admin = Admin(email=email, password_hash=password_hash)
    session.add(admin)
    await session.flush()
    token = await create_session(
        session,
        admin_id=admin.id,
        ip=client_ip(request),
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
    if not await any_admin_exists(session):
        return RedirectResponse("/setup", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(request, "login.html", {"error": request.query_params.get("error")})


@router.post("/login")
async def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(select(Admin).where(Admin.email == email))
    admin = result.scalar_one_or_none()
    if admin is None or not await run_in_threadpool(verify_password, password, admin.password_hash):
        return RedirectResponse("/login?error=1", status_code=status.HTTP_303_SEE_OTHER)

    ip = client_ip(request)
    token = await create_session(
        session, admin_id=admin.id, ip=ip, user_agent=request.headers.get("user-agent")
    )
    await record_audit_event(
        session,
        actor_type="admin",
        actor_id=str(admin.id),
        action="admin.login",
        target_type="admin",
        target_id=str(admin.id),
        ip=ip,
    )
    await session.commit()

    response = RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    _set_session_cookie(response, request, token)
    return response


@router.post("/logout")
async def logout(
    token: str | None = Depends(_cookie_scheme),
    session: AsyncSession = Depends(get_session),
):
    if token:
        await delete_session(session, token)
        await session.commit()
    response = RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(SESSION_COOKIE_NAME)
    return response


@router.get("/invite/{token}", response_class=HTMLResponse)
async def invite_form(request: Request, token: str) -> HTMLResponse:
    """No auth required -- the token itself is the credential, single-use
    and expiring, same as an enrollment code. Doesn't check the token's
    validity up front (that happens atomically on submit, in
    `redeem_invite`) so a page reload can't burn it just by being viewed."""
    return templates.TemplateResponse(request, "invite.html", {"token": token, "error": None})


@router.post("/invite/{token}")
async def invite_submit(
    request: Request,
    token: str,
    email: str = Form(...),
    password: str = Form(..., min_length=8),
    session: AsyncSession = Depends(get_session),
):
    try:
        await redeem_invite(session, token=token)
    except InviteError as exc:
        return templates.TemplateResponse(
            request, "invite.html", {"token": token, "error": str(exc)}, status_code=status.HTTP_410_GONE
        )

    password_hash = await run_in_threadpool(hash_password, password)
    admin = Admin(email=email, password_hash=password_hash)
    session.add(admin)
    try:
        await session.flush()
    except IntegrityError:
        # A second admin claiming this same invite with a duplicate email
        # (racing the check below, or just reusing an existing address) --
        # the invite is already burned by redeem_invite above, so this is a
        # clean failure rather than a half-created account.
        await session.rollback()
        return templates.TemplateResponse(
            request,
            "invite.html",
            {"token": token, "error": "an account with that email already exists"},
            status_code=status.HTTP_409_CONFLICT,
        )
    await record_audit_event(
        session,
        actor_type="admin",
        actor_id=str(admin.id),
        action="admin.created",
        target_type="admin",
        target_id=str(admin.id),
        ip=client_ip(request),
    )
    session_token = await create_session(
        session,
        admin_id=admin.id,
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    await session.commit()

    response = RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    _set_session_cookie(response, request, session_token)
    return response
