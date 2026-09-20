"""Small helpers shared across the admin API and the server-rendered UI."""

from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException, Request, status
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from timekpr_hub.db.models import User

# One Jinja2Templates instance (one template cache, one loader) shared by
# api/admin_auth.py and api/ui.py -- constructing it per-module gave each
# router its own cache over the identical directory.
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "web" / "templates"))


def client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


async def get_user_or_404(session: AsyncSession, username: str) -> User:
    result = await session.execute(select(User).where(User.canonical_username == username))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown user")
    return user
