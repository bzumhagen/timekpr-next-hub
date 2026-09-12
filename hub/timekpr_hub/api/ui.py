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
from timekpr_hub.services.aggregate import (
    global_spent_parallel,
    global_spent_wallclock,
    latest_activity_state,
)
from timekpr_hub.services.limits import effective_daily_limit
from timekpr_hub.services.policy import get_current_policy, update_policy
from timekpr_hub.settings import settings

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "web" / "templates"))


@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "index.html", {})


async def _user_summaries(session: AsyncSession, *, usernames: list[str] | None = None) -> list[dict]:
    now = datetime.now(UTC)
    stamp = canonical_stamp(now, settings.tz)

    query = select(User)
    if usernames is not None:
        query = query.where(User.canonical_username.in_(usernames))
    result = await session.execute(query)

    summaries = []
    for user in result.scalars().all():
        policy = await get_current_policy(session, user)
        activity_state, activity_as_of = await latest_activity_state(session, user_id=user.id, day=stamp.day)
        # Degrade a stale device's last-reported state to "logged_out" using
        # the same 3x-poll-interval rule as the devices fragment below --
        # otherwise a device that stopped syncing hours ago would leave the
        # badge stuck on whatever it last reported (often "draining").
        if activity_as_of is None or (now - activity_as_of).total_seconds() > 3 * (
            settings.default_next_poll_ms / 1000
        ):
            activity_state = "logged_out"

        if policy is None:
            summaries.append(
                {
                    "username": user.canonical_username,
                    "display_name": user.display_name,
                    "today_global_spent_s": 0,
                    "today_effective_limit_s": 0,
                    "activity_state": activity_state,
                    "as_of": activity_as_of.isoformat() if activity_as_of else None,
                    "daily_limit_minutes": 0,
                }
            )
            continue
        if user.accounting_mode == "wallclock":
            spent = await global_spent_wallclock(session, user_id=user.id, day=stamp.day)
        else:
            spent = await global_spent_parallel(session, user_id=user.id, day=stamp.day)
        limit_today = await effective_daily_limit(session, policy=policy, user_id=user.id, day=stamp.day)
        summaries.append(
            {
                "username": user.canonical_username,
                "display_name": user.display_name,
                "today_global_spent_s": spent,
                "today_effective_limit_s": limit_today,
                "activity_state": activity_state,
                "as_of": activity_as_of.isoformat() if activity_as_of else None,
                # Seed value for the editor's "minutes/day" field -- the
                # policy's Monday entry, converted for display only; the
                # editor always writes all seven days at once (Phase 1
                # scope, see services/policy.py::update_policy).
                "daily_limit_minutes": policy.daily_limits_json[0] // 60,
            }
        )
    return summaries


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
