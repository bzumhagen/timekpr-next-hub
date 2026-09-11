"""Minimal Phase 1 UI (PLAN: "One UI page: per-user usage bar split by
device, +30 min button, daily-limit editor, device list (Jinja2 + HTMX)").

Deliberately thin: reuses the same service functions as the JSON parent API
(`api/parent.py`) rather than duplicating logic, and renders server-side
HTML fragments that HTMX swaps in on a poll interval -- no client-side JS
beyond htmx.min.js itself, matching PLAN's "no npm, no build step" choice.

Daily-limit editing is not wired yet (still todo -- tracked in
CHECKLIST.md); the usage bar, device list/approval, and +30 min grant are.
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

from timekpr_hub.db.models import Device, Grant, User
from timekpr_hub.db.session import get_session
from timekpr_hub.services.aggregate import global_spent_parallel, global_spent_wallclock
from timekpr_hub.services.limits import effective_daily_limit
from timekpr_hub.services.policy import get_current_policy
from timekpr_hub.settings import settings

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "web" / "templates"))


@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "index.html", {})


async def _user_summaries(session: AsyncSession) -> list[dict]:
    now = datetime.now(UTC)
    stamp = canonical_stamp(now, settings.tz)

    result = await session.execute(select(User))
    summaries = []
    for user in result.scalars().all():
        policy = await get_current_policy(session, user)
        if policy is None:
            summaries.append(
                {
                    "username": user.canonical_username,
                    "display_name": user.display_name,
                    "today_global_spent_s": 0,
                    "today_effective_limit_s": 0,
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
    seconds: int = Form(...),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    result = await session.execute(select(User).where(User.canonical_username == username))
    user = result.scalar_one_or_none()
    if user is not None:
        now = datetime.now(UTC)
        stamp = canonical_stamp(now, settings.tz)
        session.add(
            Grant(
                id=uuid.uuid4(),
                user_id=user.id,
                day=stamp.day,
                seconds=seconds,
                reason="+30 min (UI)",
                source="parent",
                granted_by="ui",
            )
        )
        await session.commit()

    users = await _user_summaries(session)
    this_user = next((u for u in users if u["username"] == username), None)
    return templates.TemplateResponse(
        request, "_users_fragment.html", {"users": [this_user] if this_user else []}
    )


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


@router.post("/ui/enrollment-codes", response_class=HTMLResponse)
async def create_enrollment_code_ui(
    request: Request, session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    from timekpr_hub.api.parent import create_enrollment_code

    result = await create_enrollment_code(session)
    # The real flag is --hub-url (not --hub), and --users is required --
    # both were wrong here before (docs/best-practices-review.md), which
    # meant copy-pasting this line straight into a terminal failed. Built
    # from the request's own host:port so it works for a LAN hostname or
    # Tailscale address too, not just whatever URL happened to be typed
    # into a README example.
    command = f"sudo timekpr-hub-agent enroll --hub-url {request.base_url} --code {result['code']}"
    return HTMLResponse(
        f"<p>Code: <code>{result['code']}</code> (expires {result['expires_at']}). "
        f"Run on the new device:</p><pre>{command}</pre>"
        "<p>(prompts for which local users to manage if you don't pass <code>--users</code>)</p>"
    )
