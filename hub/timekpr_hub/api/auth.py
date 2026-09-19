"""Device bearer-token authentication.

Device tokens are `secrets.token_urlsafe(32)`, prefixed `tkh_`, sha256 at
rest, scoped so a device can only touch users mapped to it, and revocable.
"""

from __future__ import annotations

import hashlib

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from timekpr_hub.db.models import Device
from timekpr_hub.db.session import get_session

bearer_scheme = HTTPBearer(auto_error=False)


async def get_current_device(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    session: AsyncSession = Depends(get_session),
) -> Device:
    if credentials is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing device token")

    token_hash = hashlib.sha256(credentials.credentials.encode("utf-8")).hexdigest()
    result = await session.execute(select(Device).where(Device.token_hash == token_hash))
    device = result.scalar_one_or_none()

    if device is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid device token")
    if device.status == "revoked":
        # Never fail open on an auth error -- 403, not a
        # silent pass-through, so the agent's offline handling treats this
        # as an immediate `closed` enforcement mode.
        raise HTTPException(status.HTTP_403_FORBIDDEN, "device token revoked")

    return device
