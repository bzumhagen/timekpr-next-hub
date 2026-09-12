"""Minimal parent-facing API -- PLAN "API" parent endpoints, Phase 1 subset.

No auth wired yet in Phase 1 (PLAN's parent auth -- email + argon2id + TOTP
session cookies -- is worth its own pass; tracked in CHECKLIST.md Phase 2).
These endpoints are deliberately usable today for local development and the
Phase 1 acceptance test, and are the extension point once auth lands.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from timekpr_hub_core.calendar import canonical_stamp
from timekpr_hub_core.models import (
    DayOverrideCreate,
    GateReleaseCreate,
    GrantCreate,
    PolicyPayload,
    PolicyUpdate,
    UserSettingsUpdate,
    UserSummary,
)

from timekpr_hub.api.parent_auth import get_current_parent_api
from timekpr_hub.db.models import Device, EnrollmentCode, Parent, User
from timekpr_hub.db.session import get_session
from timekpr_hub.services.audit import record_audit_event
from timekpr_hub.services.limits import clear_day_override, release_gate, set_day_override, unrelease_gate
from timekpr_hub.services.policy import get_current_policy, policy_to_payload, update_policy
from timekpr_hub.services.summaries import compute_user_summaries
from timekpr_hub.settings import settings


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


router = APIRouter()


@router.get("/users", response_model=list[UserSummary])
async def list_users(session: AsyncSession = Depends(get_session)) -> list[UserSummary]:
    rows = await compute_user_summaries(session)
    return [row.to_user_summary() for row in rows]


@router.post("/users/{username}/grants", status_code=status.HTTP_201_CREATED)
async def create_grant(
    username: str,
    body: GrantCreate,
    request: Request,
    session: AsyncSession = Depends(get_session),
    parent: Parent = Depends(get_current_parent_api),
) -> dict:
    from timekpr_hub.db.models import Grant

    result = await session.execute(select(User).where(User.canonical_username == username))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown user")

    # `body.day` lets a grant target a future date ("you lose 30 minutes
    # tomorrow") without touching the standing policy -- None (the default,
    # and every caller before dated grants existed) means today in the
    # hub's own timezone, exactly the prior behavior.
    grant_day = (
        date.fromisoformat(body.day) if body.day else canonical_stamp(datetime.now(UTC), settings.tz).day
    )
    grant_day_str = grant_day.isoformat()

    grant = Grant(
        id=uuid.uuid4(),
        user_id=user.id,
        day=grant_day,
        seconds=body.seconds,
        reason=body.reason,
        source="parent",
        granted_by="parent-api",
    )
    session.add(grant)
    await record_audit_event(
        session,
        actor_type="parent",
        actor_id=str(parent.id),
        action="grant.create",
        target_type="user",
        target_id=username,
        after={"seconds": grant.seconds, "day": grant_day_str, "reason": grant.reason},
        ip=_client_ip(request),
    )
    await session.commit()
    return {"id": str(grant.id), "seconds": grant.seconds, "day": grant_day_str}


@router.put("/users/{username}/policy", response_model=PolicyPayload)
async def update_user_policy(
    username: str,
    body: PolicyUpdate,
    request: Request,
    session: AsyncSession = Depends(get_session),
    parent: Parent = Depends(get_current_parent_api),
) -> PolicyPayload:
    """The only way to change a child's *limit* (as opposed to grant
    additive bonus time) through the hub -- see services/policy.py::
    update_policy. The agent already applies whatever this returns on its
    next tick (sync.py pushes the payload whenever policy_version_applied
    disagrees, and the agent already calls setTimeLimitForDays/Week/Month +
    setAllowedDays -- CHECKLIST.md Phase 5)."""
    result = await session.execute(select(User).where(User.canonical_username == username))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown user")

    before_policy = await get_current_policy(session, user)
    before = policy_to_payload(before_policy).model_dump() if before_policy else None

    policy = await update_policy(session, user=user, update=body, created_by="parent-api")
    await record_audit_event(
        session,
        actor_type="parent",
        actor_id=str(parent.id),
        action="policy.update",
        target_type="user",
        target_id=username,
        before=before,
        after=policy_to_payload(policy).model_dump(),
        ip=_client_ip(request),
    )
    await session.commit()
    return policy_to_payload(policy)


@router.put("/users/{username}/day-override")
async def set_user_day_override(
    username: str,
    body: DayOverrideCreate,
    request: Request,
    session: AsyncSession = Depends(get_session),
    parent: Parent = Depends(get_current_parent_api),
) -> dict:
    """Sets (or replaces) an absolute per-date limit -- "instead of" the
    standing policy, not "in addition to" like a grant. See
    services/limits.py::set_day_override for why this exists as its own
    concept rather than a large negative grant."""
    result = await session.execute(select(User).where(User.canonical_username == username))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown user")

    day = date.fromisoformat(body.day)
    override = await set_day_override(
        session,
        user_id=user.id,
        day=day,
        limit_seconds=body.limit_seconds,
        reason=body.reason,
        created_by="parent-api",
    )
    await record_audit_event(
        session,
        actor_type="parent",
        actor_id=str(parent.id),
        action="override.set",
        target_type="user",
        target_id=username,
        after={"day": body.day, "limit_seconds": body.limit_seconds, "reason": body.reason},
        ip=_client_ip(request),
    )
    await session.commit()
    return {"id": str(override.id), "day": body.day, "limit_seconds": override.limit_seconds}


@router.delete("/users/{username}/day-override/{day}")
async def clear_user_day_override(
    username: str,
    day: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
    parent: Parent = Depends(get_current_parent_api),
) -> dict:
    result = await session.execute(select(User).where(User.canonical_username == username))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown user")

    cleared = await clear_day_override(session, user_id=user.id, day=date.fromisoformat(day))
    if cleared:
        await record_audit_event(
            session,
            actor_type="parent",
            actor_id=str(parent.id),
            action="override.clear",
            target_type="user",
            target_id=username,
            before={"day": day},
            ip=_client_ip(request),
        )
    await session.commit()
    return {"day": day, "cleared": cleared}


@router.post("/users/{username}/gate-release", status_code=status.HTTP_201_CREATED)
async def release_user_gate(
    username: str,
    body: GateReleaseCreate,
    request: Request,
    session: AsyncSession = Depends(get_session),
    parent: Parent = Depends(get_current_parent_api),
) -> dict:
    """Records that a gated day's precondition was met for one date --
    the exception to `users.gated_weekdays_json`'s recurring rule. Releasing
    an already-released day just refreshes who/why (see
    services/limits.py::release_gate)."""
    result = await session.execute(select(User).where(User.canonical_username == username))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown user")

    release = await release_gate(
        session, user_id=user.id, day=date.fromisoformat(body.day), released_by="parent-api", note=body.note
    )
    await record_audit_event(
        session,
        actor_type="parent",
        actor_id=str(parent.id),
        action="gate.release",
        target_type="user",
        target_id=username,
        after={"day": body.day, "note": body.note},
        ip=_client_ip(request),
    )
    await session.commit()
    return {"id": str(release.id), "day": body.day}


@router.delete("/users/{username}/gate-release/{day}")
async def unrelease_user_gate(
    username: str,
    day: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
    parent: Parent = Depends(get_current_parent_api),
) -> dict:
    """Reverses `release_user_gate` -- deletes the release row, re-gating
    that date (absence of a row IS the gate)."""
    result = await session.execute(select(User).where(User.canonical_username == username))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown user")

    unreleased = await unrelease_gate(session, user_id=user.id, day=date.fromisoformat(day))
    if unreleased:
        await record_audit_event(
            session,
            actor_type="parent",
            actor_id=str(parent.id),
            action="gate.unrelease",
            target_type="user",
            target_id=username,
            before={"day": day},
            ip=_client_ip(request),
        )
    await session.commit()
    return {"day": day, "unreleased": unreleased}


@router.put("/users/{username}/settings")
async def update_user_settings(
    username: str,
    body: UserSettingsUpdate,
    request: Request,
    session: AsyncSession = Depends(get_session),
    parent: Parent = Depends(get_current_parent_api),
) -> dict:
    """The hub-only per-user knobs (which weekdays are chore-gated, the
    accounting mode) -- deliberately NOT part of PolicyUpdate/update_policy:
    no policy version bump, no device push, its own save action. See
    core/timekpr_hub_core/models.py::UserSettingsUpdate."""
    result = await session.execute(select(User).where(User.canonical_username == username))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown user")

    before = {
        "gated_weekdays": user.gated_weekdays_json,
        "accounting_mode": user.accounting_mode,
    }
    user.gated_weekdays_json = body.gated_weekdays
    user.accounting_mode = body.accounting_mode.value
    await record_audit_event(
        session,
        actor_type="parent",
        actor_id=str(parent.id),
        action="user_settings.update",
        target_type="user",
        target_id=username,
        before=before,
        after={"gated_weekdays": body.gated_weekdays, "accounting_mode": body.accounting_mode.value},
        ip=_client_ip(request),
    )
    await session.commit()
    return {"gated_weekdays": user.gated_weekdays_json, "accounting_mode": user.accounting_mode}


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
            "enforcement": d.enforcement,
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


@router.post("/devices/{device_id}/revoke")
async def revoke_device(
    device_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    parent: Parent = Depends(get_current_parent_api),
) -> dict:
    """Kills the device's token immediately -- get_current_device 403s a
    revoked device on its very next sync (auth.py's "never fail open").
    History (usage_counters/activity_intervals/user_aliases) is untouched,
    and the device's machine_id is freed for a later re-enroll to bind a
    *new* row to (the partial unique index on devices.machine_id only
    applies to non-revoked rows) -- the reversible, non-destructive action;
    see delete_device for the alternative that also erases history."""
    result = await session.execute(select(Device).where(Device.id == device_id))
    device = result.scalar_one_or_none()
    if device is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown device")
    before_status = device.status
    device.status = "revoked"
    await record_audit_event(
        session,
        actor_type="parent",
        actor_id=str(parent.id),
        action="device.revoke",
        target_type="device",
        target_id=str(device.id),
        before={"status": before_status},
        after={"status": device.status},
        ip=_client_ip(request),
    )
    await session.commit()
    return {"id": str(device.id), "status": device.status}


@router.post("/devices/{device_id}/observe")
async def set_device_observe_mode(device_id: uuid.UUID, session: AsyncSession = Depends(get_session)) -> dict:
    """Dry-run mode (PLAN "Layer 7 -- household safety net"): the agent
    keeps syncing and computing what it *would* write, logging it, but
    never actually calls into DBUS -- see `main.py`'s
    `resp_user.get("enforcement") == "observe"` branch, which already
    existed for the unmapped-user case and now also serves this per-device
    toggle. `/sync` (api/sync.py) reads this column and reports
    `EnforcementMode.OBSERVE` for every user on this device until
    `set_device_enforce_mode` flips it back."""
    result = await session.execute(select(Device).where(Device.id == device_id))
    device = result.scalar_one_or_none()
    if device is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown device")
    device.enforcement = "observe"
    await session.commit()
    return {"id": str(device.id), "enforcement": device.enforcement}


@router.post("/devices/{device_id}/enforce")
async def set_device_enforce_mode(device_id: uuid.UUID, session: AsyncSession = Depends(get_session)) -> dict:
    """Reverses `set_device_observe_mode` -- back to normal enforcement."""
    result = await session.execute(select(Device).where(Device.id == device_id))
    device = result.scalar_one_or_none()
    if device is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown device")
    device.enforcement = "enforce"
    await session.commit()
    return {"id": str(device.id), "enforcement": device.enforcement}


@router.delete("/devices/{device_id}")
async def delete_device(
    device_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    parent: Parent = Depends(get_current_parent_api),
) -> dict:
    """Hard delete. FK cascades (ondelete='CASCADE' on user_aliases,
    usage_counters, activity_intervals) drop this device's contribution
    entirely, which *rewrites* any day it reported usage for -- unlike
    revoke, this is not reversible and changes past totals. Offered for
    "enrolled the wrong thing" cleanup; revoke is the routine action."""
    result = await session.execute(select(Device).where(Device.id == device_id))
    device = result.scalar_one_or_none()
    if device is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown device")
    before = {"name": device.name, "status": device.status}
    await record_audit_event(
        session,
        actor_type="parent",
        actor_id=str(parent.id),
        action="device.delete",
        target_type="device",
        target_id=str(device_id),
        before=before,
        ip=_client_ip(request),
    )
    await session.delete(device)
    await session.commit()
    return {"id": str(device_id), "status": "deleted"}
