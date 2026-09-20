"""Admin accounts: invites, deletion, password change. See services/
admin_auth.py for the underlying invite/session/account logic and
api/admin_auth.py's GET/POST /invite/{token} for the redemption page."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from timekpr_hub.api.admin_auth import get_current_admin_api
from timekpr_hub.api.util import client_ip
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


@router.post("/admin-invites", status_code=status.HTTP_201_CREATED)
async def create_admin_invite(
    request: Request,
    session: AsyncSession = Depends(get_session),
    admin: Admin = Depends(get_current_admin_api),
) -> dict:
    token = await mint_admin_invite(session, actor_admin_id=admin.id, ip=client_ip(request))
    await session.commit()
    invite_url = f"{str(request.base_url).rstrip('/')}/invite/{token}"
    return {"token": token, "url": invite_url}


@router.get("/admins")
async def list_admins(
    session: AsyncSession = Depends(get_session), admin: Admin = Depends(get_current_admin_api)
) -> list[dict]:
    result = await session.execute(select(Admin))
    return [
        {"id": str(p.id), "email": p.email, "created_at": p.created_at.isoformat(), "you": p.id == admin.id}
        for p in result.scalars().all()
    ]


@router.delete("/admins/{admin_id}")
async def delete_admin(
    admin_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    admin: Admin = Depends(get_current_admin_api),
) -> dict:
    """The last remaining admin can never be deleted -- including
    themselves -- since that would permanently lock the hub's own admin UI
    (there is no other way back in; /setup only ever fires once)."""
    try:
        target = await delete_admin_account(
            session, target_admin_id=admin_id, actor_admin_id=admin.id, ip=client_ip(request)
        )
    except LastAdminError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown admin")
    await session.commit()
    return {"id": str(admin_id), "status": "deleted"}


class PasswordChange(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8)


@router.post("/admin/password")
async def change_own_password(
    request: Request,
    body: PasswordChange,
    session: AsyncSession = Depends(get_session),
    admin: Admin = Depends(get_current_admin_api),
) -> dict:
    token = request.cookies.get("tkh_session", "")
    try:
        await change_admin_own_password(
            session,
            admin=admin,
            current_password=body.current_password,
            new_password=body.new_password,
            session_token=token,
            ip=client_ip(request),
        )
    except WrongPasswordError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc
    await session.commit()
    return {"status": "ok"}
