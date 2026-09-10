"""Minimal parent-facing API -- PLAN "API" parent endpoints, Phase 1 subset.

No auth wired yet in Phase 1 (PLAN's parent auth -- email + argon2id + TOTP
session cookies -- is worth its own pass; tracked in CHECKLIST.md Phase 2).
These endpoints are deliberately usable today for local development and the
Phase 1 acceptance test, and are the extension point once auth lands.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from timekpr_hub_core.calendar import canonical_stamp
from timekpr_hub_core.models import GrantCreate, UserSummary

from timekpr_hub.db.models import Device, EnrollmentCode, User
from timekpr_hub.db.session import get_session
from timekpr_hub.services.aggregate import global_spent_parallel, global_spent_wallclock
from timekpr_hub.services.limits import effective_daily_limit
from timekpr_hub.services.policy import get_current_policy
from timekpr_hub.settings import settings

router = APIRouter()


@router.get("/users", response_model=list[UserSummary])
async def list_users(session: AsyncSession = Depends(get_session)) -> list[UserSummary]:
    now = datetime.now(UTC)
    stamp = canonical_stamp(now, settings.tz)

    result = await session.execute(select(User))
    users = result.scalars().all()

    summaries = []
    for user in users:
        policy = await get_current_policy(session, user)
        if policy is None:
            summaries.append(
                UserSummary(
                    username=user.canonical_username,
                    display_name=user.display_name,
                    accounting_mode=user.accounting_mode,
                    today_global_spent_s=0,
                    today_effective_limit_s=0,
                )
            )
            continue

        if user.accounting_mode == "wallclock":
            spent = await global_spent_wallclock(session, user_id=user.id, day=stamp.day)
        else:
            spent = await global_spent_parallel(session, user_id=user.id, day=stamp.day)
        limit_today = await effective_daily_limit(session, policy=policy, user_id=user.id, day=stamp.day)

        summaries.append(
            UserSummary(
                username=user.canonical_username,
                display_name=user.display_name,
                accounting_mode=user.accounting_mode,
                today_global_spent_s=spent,
                today_effective_limit_s=limit_today,
            )
        )
    return summaries


@router.post("/users/{username}/grants", status_code=status.HTTP_201_CREATED)
async def create_grant(
    username: str, body: GrantCreate, session: AsyncSession = Depends(get_session)
) -> dict:
    from timekpr_hub.db.models import Grant

    result = await session.execute(select(User).where(User.canonical_username == username))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown user")

    now = datetime.now(UTC)
    stamp = canonical_stamp(now, settings.tz)

    grant = Grant(
        id=uuid.uuid4(),
        user_id=user.id,
        day=stamp.day,
        seconds=body.seconds,
        reason=body.reason,
        source="parent",
        granted_by="parent-api",
    )
    session.add(grant)
    await session.commit()
    return {"id": str(grant.id), "seconds": grant.seconds, "day": stamp.day_str}


@router.post("/enrollment-codes", status_code=status.HTTP_201_CREATED)
async def create_enrollment_code(session: AsyncSession = Depends(get_session)) -> dict:
    code = secrets.token_urlsafe(6).upper().replace("_", "A").replace("-", "B")[:8]
    now = datetime.now(UTC)
    row = EnrollmentCode(code=code, expires_at=now + timedelta(minutes=15))
    session.add(row)
    await session.commit()
    return {"code": code, "expires_at": row.expires_at.isoformat()}


@router.get("/devices")
async def list_devices(session: AsyncSession = Depends(get_session)) -> list[dict]:
    result = await session.execute(select(Device))
    return [
        {
            "id": str(d.id),
            "name": d.name,
            "status": d.status,
            "last_sync_at": d.last_sync_at.isoformat() if d.last_sync_at else None,
        }
        for d in result.scalars().all()
    ]


@router.post("/devices/{device_id}/approve")
async def approve_device(device_id: uuid.UUID, session: AsyncSession = Depends(get_session)) -> dict:
    result = await session.execute(select(Device).where(Device.id == device_id))
    device = result.scalar_one_or_none()
    if device is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown device")
    device.status = "active"
    await session.commit()
    return {"id": str(device.id), "status": device.status}
