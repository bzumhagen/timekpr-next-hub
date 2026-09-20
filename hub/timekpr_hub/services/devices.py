"""Device mutation operations shared by the JSON admin API (api/admin/)
and the server-rendered UI (api/ui/) -- each used to reimplement these
independently, and neither recorded an audit event for an observe/enforce
mode change."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from timekpr_hub.db.models import Device
from timekpr_hub.services.audit import record_audit_event


async def get_device(session: AsyncSession, device_id: uuid.UUID) -> Device | None:
    result = await session.execute(select(Device).where(Device.id == device_id))
    return result.scalar_one_or_none()


async def revoke_device(
    session: AsyncSession, *, device_id: uuid.UUID, actor_id: uuid.UUID, ip: str | None
) -> Device | None:
    """Sets status='revoked' -- get_current_device (api/auth.py) 403s this
    device's very next sync. History is untouched, and its machine_id is
    freed for a later re-enroll to bind a new row to. The reversible,
    default action; see delete_device for the destructive alternative.
    Returns None (nothing to do) if device_id doesn't exist."""
    device = await get_device(session, device_id)
    if device is None:
        return None
    before_status = device.status
    device.status = "revoked"
    await record_audit_event(
        session,
        actor_type="admin",
        actor_id=str(actor_id),
        action="device.revoke",
        target_type="device",
        target_id=str(device.id),
        before={"status": before_status},
        after={"status": device.status},
        ip=ip,
    )
    return device


async def set_device_enforcement(
    session: AsyncSession, *, device_id: uuid.UUID, enforcement: str, actor_id: uuid.UUID, ip: str | None
) -> Device | None:
    """Toggles between normal enforcement and dry-run "observe" mode, where
    the agent keeps syncing and computing what it would write but never
    calls into DBUS -- see timekpr_hub_agent.tick.run_tick's
    `resp_user.get("enforcement") == "observe"` branch, and api/sync.py,
    which reports EnforcementMode.OBSERVE for this device until it's
    flipped back. Returns None if device_id doesn't exist."""
    device = await get_device(session, device_id)
    if device is None:
        return None
    before = device.enforcement
    device.enforcement = enforcement
    await record_audit_event(
        session,
        actor_type="admin",
        actor_id=str(actor_id),
        action=f"device.{enforcement}",
        target_type="device",
        target_id=str(device.id),
        before={"enforcement": before},
        after={"enforcement": enforcement},
        ip=ip,
    )
    return device


async def delete_device(
    session: AsyncSession, *, device_id: uuid.UUID, actor_id: uuid.UUID, ip: str | None
) -> Device | None:
    """Hard delete -- FK cascades (ondelete='CASCADE' on user_aliases,
    usage_counters, activity_intervals) drop this device's contribution
    entirely, which *rewrites* any day it reported usage for -- unlike
    revoke, not reversible. Offered for "enrolled the wrong thing" cleanup;
    revoke is the routine action. Returns None if device_id doesn't exist."""
    device = await get_device(session, device_id)
    if device is None:
        return None
    before = {"name": device.name, "status": device.status}
    await record_audit_event(
        session,
        actor_type="admin",
        actor_id=str(actor_id),
        action="device.delete",
        target_type="device",
        target_id=str(device_id),
        before=before,
        ip=ip,
    )
    await session.delete(device)
    return device
