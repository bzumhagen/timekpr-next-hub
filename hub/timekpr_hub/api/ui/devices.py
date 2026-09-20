"""Devices: sync status, enrollment-code generation, and per-device
revoke/observe/enforce/delete.

Split out from the dashboard onto its own page -- device state is hub-wide
(not per-user), so unlike the per-user policy/stats/settings pages it
doesn't belong nested under a user card; it's just noise on the dashboard
most of the time and only wanted when actually managing a device.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from timekpr_hub.api.admin_auth import get_current_admin_ui
from timekpr_hub.api.util import client_ip, templates
from timekpr_hub.db.models import Admin, Device
from timekpr_hub.db.session import get_session
from timekpr_hub.services import devices as device_ops
from timekpr_hub.services.enrollment import create_enrollment_code
from timekpr_hub.settings import settings

router = APIRouter()


@router.get("/devices", response_class=HTMLResponse)
async def devices_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "devices.html", {"poll_ms": settings.default_next_poll_ms})


@router.get("/ui/devices-fragment", response_class=HTMLResponse)
async def devices_fragment(request: Request, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    result = await session.execute(select(Device))
    now = datetime.now(UTC)
    devices = []
    for d in result.scalars().all():
        if d.last_seen_at is None:
            seen_label, stale = "never synced", True
        else:
            age_s = (now - d.last_seen_at).total_seconds()
            # "Stale" at 3x the poll interval -- a couple of missed ticks is
            # normal jitter, three in a row means the device is actually
            # unreachable, asleep, or the agent has stopped.
            stale = age_s > 3 * (settings.default_next_poll_ms / 1000)
            seen_label = _relative_time(age_s)
        devices.append(
            {
                "id": str(d.id),
                "name": d.name,
                "status": d.status,
                "enforcement": d.enforcement,
                "agent_version": d.agent_version or "?",
                "last_seen": seen_label,
                "stale": stale,
            }
        )
    return templates.TemplateResponse(request, "_devices_fragment.html", {"devices": devices})


def _relative_time(age_s: float) -> str:
    if age_s < 90:
        return f"{int(age_s)}s ago"
    if age_s < 5400:
        return f"{int(age_s / 60)}m ago"
    if age_s < 172800:
        return f"{int(age_s / 3600)}h ago"
    return f"{int(age_s / 86400)}d ago"


@router.post("/ui/devices/{device_id}/revoke", response_class=HTMLResponse)
async def revoke_device_ui(
    request: Request,
    device_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    admin: Admin = Depends(get_current_admin_ui),
) -> HTMLResponse:
    """Kills the device's token immediately (get_current_device 403s a
    revoked device on its very next sync) without touching any history it
    already contributed -- the reversible, default action. See
    /ui/devices/{id} (DELETE) for the destructive alternative."""
    await device_ops.revoke_device(session, device_id=device_id, actor_id=admin.id, ip=client_ip(request))
    await session.commit()
    return await devices_fragment(request, session)


@router.post("/ui/devices/{device_id}/observe", response_class=HTMLResponse)
async def set_device_observe_mode_ui(
    request: Request,
    device_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    admin: Admin = Depends(get_current_admin_ui),
) -> HTMLResponse:
    """Dry-run mode: the agent keeps syncing but never writes to DBUS --
    see services/devices.py's `set_device_enforcement` for the JSON-API
    twin this shares."""
    await device_ops.set_device_enforcement(
        session, device_id=device_id, enforcement="observe", actor_id=admin.id, ip=client_ip(request)
    )
    await session.commit()
    return await devices_fragment(request, session)


@router.post("/ui/devices/{device_id}/enforce", response_class=HTMLResponse)
async def set_device_enforce_mode_ui(
    request: Request,
    device_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    admin: Admin = Depends(get_current_admin_ui),
) -> HTMLResponse:
    """Reverses set_device_observe_mode_ui -- back to normal enforcement."""
    await device_ops.set_device_enforcement(
        session, device_id=device_id, enforcement="enforce", actor_id=admin.id, ip=client_ip(request)
    )
    await session.commit()
    return await devices_fragment(request, session)


@router.post("/ui/devices/{device_id}/delete", response_class=HTMLResponse)
async def delete_device_ui(
    request: Request,
    device_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    admin: Admin = Depends(get_current_admin_ui),
) -> HTMLResponse:
    """Hard delete -- FK cascades drop this device's usage_counters,
    activity_intervals and user_aliases too, which *rewrites* that user's
    historical totals for any day this device contributed to. Revoke is the
    button offered by default; this one sits behind the template's own
    confirm() and is for "I enrolled the wrong thing" cleanup, not routine
    device retirement."""
    await device_ops.delete_device(session, device_id=device_id, actor_id=admin.id, ip=client_ip(request))
    await session.commit()
    return await devices_fragment(request, session)


@router.post("/ui/enrollment-codes", response_class=HTMLResponse)
async def create_enrollment_code_ui(
    request: Request, session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    row = await create_enrollment_code(session)
    await session.commit()
    # The real flag is --hub-url (not --hub), so this line can be
    # copy-pasted straight into a terminal. Built from the request's own
    # host:port so it works for a LAN hostname or Tailscale address too, not
    # just whatever URL happened to be typed into a README example.
    command = f"sudo timekpr-hub-agent enroll --hub-url {request.base_url} --code {row.code}"
    return HTMLResponse(
        f"<p>Code: <code>{row.code}</code> (expires {row.expires_at.isoformat()}). "
        f"Run on the new device:</p><pre>{command}</pre>"
        "<p>Or just run <code>sudo timekpr-hub-agent enroll</code> with no flags at all -- "
        "it prompts for the hub URL, the code, and which local users to manage.</p>"
    )
