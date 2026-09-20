"""Admin accounts: invites, deletion, password change. See services/
admin_auth.py for the underlying invite/session logic and
api/admin_auth.py's GET/POST /invite/{token} for the redemption page.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from timekpr_hub.api.admin_auth import get_current_admin_ui
from timekpr_hub.api.util import client_ip, templates
from timekpr_hub.db.models import Admin
from timekpr_hub.db.session import get_session
from timekpr_hub.services.admin_auth import (
    LastAdminError,
    WrongPasswordError,
    change_admin_own_password,
    delete_admin_account,
    mint_admin_invite,
)

router = APIRouter()


async def _admins_fragment_context(session: AsyncSession, admin: Admin) -> dict:
    result = await session.execute(select(Admin))
    admins = [{"id": str(a.id), "email": a.email, "you": a.id == admin.id} for a in result.scalars().all()]
    return {"admins": admins, "can_delete": len(admins) > 1}


@router.get("/admins", response_class=HTMLResponse)
async def admins_page(
    request: Request,
    session: AsyncSession = Depends(get_session),
    admin: Admin = Depends(get_current_admin_ui),
) -> HTMLResponse:
    context = await _admins_fragment_context(session, admin)
    return templates.TemplateResponse(request, "admins.html", context)


@router.post("/ui/admin-invites", response_class=HTMLResponse)
async def create_admin_invite_ui(
    request: Request,
    session: AsyncSession = Depends(get_session),
    admin: Admin = Depends(get_current_admin_ui),
) -> HTMLResponse:
    token = await mint_admin_invite(session, actor_admin_id=admin.id, ip=client_ip(request))
    await session.commit()
    invite_url = f"{str(request.base_url).rstrip('/')}/invite/{token}"
    return HTMLResponse(
        f"<p>Invite link (expires in 24h, single-use):</p><pre>{invite_url}</pre>"
        "<p>Send it to the person you're inviting -- they'll set their own password.</p>"
    )


@router.post("/ui/admins/{target_admin_id}/delete", response_class=HTMLResponse)
async def delete_admin_ui(
    request: Request,
    target_admin_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    admin: Admin = Depends(get_current_admin_ui),
) -> HTMLResponse:
    try:
        await delete_admin_account(
            session, target_admin_id=target_admin_id, actor_admin_id=admin.id, ip=client_ip(request)
        )
        await session.commit()
    except LastAdminError:
        pass  # guard -- the fragment re-render below just shows them all still present
    context = await _admins_fragment_context(session, admin)
    return templates.TemplateResponse(request, "_admins_fragment.html", context)


@router.post("/ui/admin/password", response_class=HTMLResponse)
async def change_own_password_ui(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(..., min_length=8),
    session: AsyncSession = Depends(get_session),
    admin: Admin = Depends(get_current_admin_ui),
) -> HTMLResponse:
    token = request.cookies.get("tkh_session", "")
    try:
        await change_admin_own_password(
            session,
            admin=admin,
            current_password=current_password,
            new_password=new_password,
            session_token=token,
            ip=client_ip(request),
        )
    except WrongPasswordError as exc:
        return HTMLResponse(f'<p style="color: var(--tk-danger);">{exc}</p>')
    await session.commit()
    return HTMLResponse("<p>Password changed.</p>")
