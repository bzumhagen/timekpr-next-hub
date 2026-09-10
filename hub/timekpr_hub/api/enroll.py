"""POST /enroll -- PLAN "API": device enrollment via a one-time code.

No device auth on this endpoint (there's no token yet); the enrollment code
itself is the credential, single-use and short-lived.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from timekpr_hub_core.models import EnrollRequest, EnrollResponse

from timekpr_hub.db.models import Device, EnrollmentCode, User, UserAlias
from timekpr_hub.db.session import get_session

router = APIRouter()

TOKEN_PREFIX = "tkh_"


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@router.post("/enroll", response_model=EnrollResponse, status_code=status.HTTP_201_CREATED)
async def enroll(req: EnrollRequest, session: AsyncSession = Depends(get_session)) -> EnrollResponse:
    now = datetime.now(UTC)

    result = await session.execute(select(EnrollmentCode).where(EnrollmentCode.code == req.enrollment_code))
    code_row = result.scalar_one_or_none()
    if code_row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown enrollment code")
    if code_row.used_at is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "enrollment code already used")
    if code_row.expires_at < now:
        raise HTTPException(status.HTTP_410_GONE, "enrollment code expired")

    raw_token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    device = Device(
        id=uuid.uuid4(),
        name=req.hostname,
        hostname=req.hostname,
        machine_id=req.machine_id,
        token_hash=_hash_token(raw_token),
        token_prefix=raw_token[:12],
        status="pending",
        enforcement="enforce",
        agent_version=req.agent_version,
        os_info=req.os,
        tz=req.tz,
        enrolled_at=now,
    )
    session.add(device)
    await session.flush()

    # Auto-suggest aliases by exact local-username match against existing
    # canonical users (PLAN "Enrollment"); a parent still has to approve the
    # device before it's usable (status stays 'pending').
    for local_username in req.local_users:
        existing = await session.execute(select(User).where(User.canonical_username == local_username))
        user = existing.scalar_one_or_none()
        if user is not None:
            session.add(
                UserAlias(
                    id=uuid.uuid4(), user_id=user.id, device_id=device.id, local_username=local_username
                )
            )

    code_row.used_at = now
    code_row.used_by_device_id = device.id

    await session.commit()

    return EnrollResponse(
        device_id=str(device.id),
        device_token=raw_token,
        hub_time=now.isoformat(),
        next_poll_ms=20000,
    )
