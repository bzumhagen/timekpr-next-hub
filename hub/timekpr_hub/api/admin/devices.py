"""Enrollment codes, the audit log, and device management -- the
hub-wide (not per-user) half of the admin API."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from timekpr_hub.api.admin_auth import get_current_admin_api
from timekpr_hub.api.util import client_ip
from timekpr_hub.db.models import Admin, Device
from timekpr_hub.db.session import get_session
from timekpr_hub.services import devices as device_ops
from timekpr_hub.services import enrollment as enrollment_ops
from timekpr_hub.services.audit import list_audit_events

router = APIRouter()


@router.post("/enrollment-codes", status_code=status.HTTP_201_CREATED)
async def create_enrollment_code(session: AsyncSession = Depends(get_session)) -> dict:
    row = await enrollment_ops.create_enrollment_code(session)
    await session.commit()
    return {"code": row.code, "expires_at": row.expires_at.isoformat()}


@router.get("/audit")
async def list_audit(
    limit: int = 50,
    offset: int = 0,
    actor_id: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    """Every admin action, newest first -- see services/audit.py's
    `record_audit_event`, which every mutating endpoint in api/admin/ and
    api/ui/ already calls. `before`/`after` are the full JSON diffs those
    call sites recorded (e.g. a policy edit's whole payload before and
    after), not a summary."""
    limit = min(max(limit, 1), 200)
    events = await list_audit_events(
        session,
        limit=limit,
        offset=max(offset, 0),
        actor_id=actor_id,
        target_type=target_type,
        target_id=target_id,
    )
    return [
        {
            "id": str(e.id),
            "ts": e.ts.isoformat(),
            "actor_type": e.actor_type,
            "actor_id": e.actor_id,
            "action": e.action,
            "target_type": e.target_type,
            "target_id": e.target_id,
            "before": e.before_json,
            "after": e.after_json,
            "ip": e.ip,
        }
        for e in events
    ]


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


@router.post("/devices/{device_id}/revoke")
async def revoke_device(
    device_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    admin: Admin = Depends(get_current_admin_api),
) -> dict:
    """Kills the device's token immediately -- get_current_device 403s a
    revoked device on its very next sync (auth.py's "never fail open").
    History (usage_counters/activity_intervals/user_aliases) is untouched,
    and the device's machine_id is freed for a later re-enroll to bind a
    *new* row to (the partial unique index on devices.machine_id only
    applies to non-revoked rows) -- the reversible, non-destructive action;
    see delete_device for the alternative that also erases history."""
    device = await device_ops.revoke_device(
        session, device_id=device_id, actor_id=admin.id, ip=client_ip(request)
    )
    if device is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown device")
    await session.commit()
    return {"id": str(device.id), "status": device.status}


@router.post("/devices/{device_id}/observe")
async def set_device_observe_mode(
    device_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    admin: Admin = Depends(get_current_admin_api),
) -> dict:
    """Dry-run mode: the agent keeps syncing and computing what it *would*
    write, logging it, but never actually calls into DBUS -- see
    `timekpr_hub_agent.tick.run_tick`'s `resp_user.get("enforcement") ==
    "observe"` branch, which already existed for the unmapped-user case and
    also serves this per-device toggle. `/sync` (api/sync.py) reads this
    column and reports `EnforcementMode.OBSERVE` for every user on this
    device until `set_device_enforce_mode` flips it back."""
    device = await device_ops.set_device_enforcement(
        session, device_id=device_id, enforcement="observe", actor_id=admin.id, ip=client_ip(request)
    )
    if device is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown device")
    await session.commit()
    return {"id": str(device.id), "enforcement": device.enforcement}


@router.post("/devices/{device_id}/enforce")
async def set_device_enforce_mode(
    device_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    admin: Admin = Depends(get_current_admin_api),
) -> dict:
    """Reverses `set_device_observe_mode` -- back to normal enforcement."""
    device = await device_ops.set_device_enforcement(
        session, device_id=device_id, enforcement="enforce", actor_id=admin.id, ip=client_ip(request)
    )
    if device is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown device")
    await session.commit()
    return {"id": str(device.id), "enforcement": device.enforcement}


@router.delete("/devices/{device_id}")
async def delete_device(
    device_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    admin: Admin = Depends(get_current_admin_api),
) -> dict:
    """Hard delete. FK cascades (ondelete='CASCADE' on user_aliases,
    usage_counters, activity_intervals) drop this device's contribution
    entirely, which *rewrites* any day it reported usage for -- unlike
    revoke, this is not reversible and changes past totals. Offered for
    "enrolled the wrong thing" cleanup; revoke is the routine action."""
    device = await device_ops.delete_device(
        session, device_id=device_id, actor_id=admin.id, ip=client_ip(request)
    )
    if device is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown device")
    await session.commit()
    return {"id": str(device_id), "status": "deleted"}
