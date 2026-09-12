"""Minimal Phase 1+ UI (PLAN: "One UI page: per-user usage bar split by
device, +30 min button, daily-limit editor, device list (Jinja2 + HTMX)").

Deliberately thin: reuses the same service functions as the JSON parent API
(`api/parent.py`) rather than duplicating logic, and renders server-side
HTML fragments that HTMX swaps in on a poll interval -- no client-side JS
beyond htmx.min.js itself and a small inline ticker (see _users_fragment.html),
matching PLAN's "no npm, no build step" choice.

Every route here (and every /api/v1/* parent route) requires an
authenticated parent session -- see `get_current_parent` in
`api/parent_auth.py`, applied router-level in `app.py`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from timekpr_hub_core.calendar import canonical_stamp
from timekpr_hub_core.models import PolicyUpdate

from timekpr_hub.db.models import Device, Grant, User
from timekpr_hub.db.session import get_session
from timekpr_hub.services.policy import update_policy
from timekpr_hub.services.summaries import compute_user_summaries
from timekpr_hub.settings import settings

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "web" / "templates"))


@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "index.html", {})


async def _user_summaries(session: AsyncSession, *, usernames: list[str] | None = None) -> list[dict]:
    """Template-shaped view of `compute_user_summaries` (services/
    summaries.py, shared with the JSON parent API) plus one UI-only field:
    `daily_limit_minutes`, the editor's seed value -- the policy's Monday
    entry, converted for display only; the editor always writes all seven
    days at once (Phase 1 scope, see services/policy.py::update_policy)."""
    rows = await compute_user_summaries(session, usernames=usernames)
    return [
        {
            "username": row.user.canonical_username,
            "display_name": row.user.display_name,
            "today_global_spent_s": row.today_global_spent_s,
            "today_effective_limit_s": row.today_effective_limit_s,
            "activity_state": row.activity_state,
            "as_of": row.as_of,
            "daily_limit_minutes": (row.policy.daily_limits_json[0] // 60) if row.policy else 0,
        }
        for row in rows
    ]


@router.get("/ui/users-fragment", response_class=HTMLResponse)
async def users_fragment(request: Request, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    users = await _user_summaries(session)
    return templates.TemplateResponse(request, "_users_fragment.html", {"users": users})


@router.post("/ui/users/{username}/grants", response_class=HTMLResponse)
async def grant_from_ui(
    request: Request,
    username: str,
    seconds: int = Form(..., ge=-86400, le=86400),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    result = await session.execute(select(User).where(User.canonical_username == username))
    user = result.scalar_one_or_none()
    if user is not None:
        now = datetime.now(UTC)
        stamp = canonical_stamp(now, settings.tz)
        minutes = seconds / 60
        session.add(
            Grant(
                id=uuid.uuid4(),
                user_id=user.id,
                day=stamp.day,
                seconds=seconds,
                reason=f"{minutes:+g} min (UI)",
                source="parent",
                granted_by="ui",
            )
        )
        await session.commit()

    users = await _user_summaries(session, usernames=[username])
    return templates.TemplateResponse(request, "_users_fragment.html", {"users": users})


@router.post("/ui/users/{username}/policy", response_class=HTMLResponse)
async def update_policy_ui(
    request: Request,
    username: str,
    minutes_per_day: int = Form(..., ge=0, le=1440),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """The simple case of the policy editor: one minutes/day value applied
    to all seven days. Per-day overrides go through the JSON API
    (PUT /api/v1/users/{username}/policy) until there's demand for a richer
    per-day form here."""
    result = await session.execute(select(User).where(User.canonical_username == username))
    user = result.scalar_one_or_none()
    if user is not None:
        seconds_per_day = minutes_per_day * 60
        body = PolicyUpdate(
            daily_limits_s=[seconds_per_day] * 7,
            weekly_limit_s=seconds_per_day * 7,
            monthly_limit_s=seconds_per_day * 30,
            allowed_weekdays=["1", "2", "3", "4", "5", "6", "7"],
        )
        await update_policy(session, user=user, update=body, created_by="ui")
        await session.commit()

    users = await _user_summaries(session, usernames=[username])
    return templates.TemplateResponse(request, "_users_fragment.html", {"users": users})


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
            # "Stale" at 3x the poll interval (docs/best-practices-review.md
            # / Phase 3 "hub device health") -- a couple of missed ticks is
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


@router.post("/ui/devices/{device_id}/approve", response_class=HTMLResponse)
async def approve_device_ui(
    request: Request, device_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    result = await session.execute(select(Device).where(Device.id == device_id))
    device = result.scalar_one_or_none()
    if device is not None:
        device.status = "active"
        await session.commit()
    return await devices_fragment(request, session)


@router.post("/ui/devices/{device_id}/revoke", response_class=HTMLResponse)
async def revoke_device_ui(
    request: Request, device_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    """Kills the device's token immediately (get_current_device 403s a
    revoked device on its very next sync) without touching any history it
    already contributed -- the reversible, default action. See
    /ui/devices/{id} (DELETE) for the destructive alternative."""
    result = await session.execute(select(Device).where(Device.id == device_id))
    device = result.scalar_one_or_none()
    if device is not None:
        device.status = "revoked"
        await session.commit()
    return await devices_fragment(request, session)


@router.post("/ui/devices/{device_id}/observe", response_class=HTMLResponse)
async def set_device_observe_mode_ui(
    request: Request, device_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    """Dry-run mode (PLAN "Layer 7 -- household safety net"): the agent
    keeps syncing but never writes to DBUS -- see api/parent.py's
    `set_device_observe_mode` for the JSON-API twin this wraps."""
    result = await session.execute(select(Device).where(Device.id == device_id))
    device = result.scalar_one_or_none()
    if device is not None:
        device.enforcement = "observe"
        await session.commit()
    return await devices_fragment(request, session)


@router.post("/ui/devices/{device_id}/enforce", response_class=HTMLResponse)
async def set_device_enforce_mode_ui(
    request: Request, device_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    """Reverses set_device_observe_mode_ui -- back to normal enforcement."""
    result = await session.execute(select(Device).where(Device.id == device_id))
    device = result.scalar_one_or_none()
    if device is not None:
        device.enforcement = "enforce"
        await session.commit()
    return await devices_fragment(request, session)


@router.post("/ui/devices/{device_id}/delete", response_class=HTMLResponse)
async def delete_device_ui(
    request: Request, device_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    """Hard delete -- FK cascades drop this device's usage_counters,
    activity_intervals and user_aliases too, which *rewrites* that user's
    historical totals for any day this device contributed to. Revoke is the
    button offered by default; this one sits behind the template's own
    confirm() and is for "I enrolled the wrong thing" cleanup, not routine
    device retirement."""
    result = await session.execute(select(Device).where(Device.id == device_id))
    device = result.scalar_one_or_none()
    if device is not None:
        await session.delete(device)
        await session.commit()
    return await devices_fragment(request, session)


@router.post("/ui/enrollment-codes", response_class=HTMLResponse)
async def create_enrollment_code_ui(
    request: Request, session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    from timekpr_hub.api.parent import create_enrollment_code

    result = await create_enrollment_code(session)
    # The real flag is --hub-url (not --hub) -- previously wrong here
    # (docs/best-practices-review.md), which meant copy-pasting this line
    # straight into a terminal failed. Built from the request's own
    # host:port so it works for a LAN hostname or Tailscale address too, not
    # just whatever URL happened to be typed into a README example.
    command = f"sudo timekpr-hub-agent enroll --hub-url {request.base_url} --code {result['code']}"
    return HTMLResponse(
        f"<p>Code: <code>{result['code']}</code> (expires {result['expires_at']}). "
        f"Run on the new device:</p><pre>{command}</pre>"
        "<p>Or just run <code>sudo timekpr-hub-agent enroll</code> with no flags at all -- "
        "it prompts for the hub URL, the code, and which local users to manage.</p>"
    )
