"""The dashboard: `/`, the per-user summary cards, and quick grants."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from timekpr_hub_core.calendar import canonical_stamp

from timekpr_hub.api.admin_auth import get_current_admin_ui
from timekpr_hub.api.ui.policy import _hours_display, _to_wire_intervals
from timekpr_hub.api.util import client_ip, templates
from timekpr_hub.db.models import Admin, Grant, User
from timekpr_hub.db.session import get_session
from timekpr_hub.services.audit import record_audit_event
from timekpr_hub.services.day_hours import day_hour_overrides_batch
from timekpr_hub.services.limits import day_overrides_batch
from timekpr_hub.services.summaries import compute_user_summaries
from timekpr_hub.settings import settings

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "index.html", {"poll_ms": settings.default_next_poll_ms})


async def _user_summaries(session: AsyncSession, *, usernames: list[str] | None = None) -> list[dict]:
    """Template-shaped view of `compute_user_summaries` (services/
    summaries.py, shared with the JSON admin API), plus two UI-only
    additions: today's gate state (for the dashboard badge/Release button)
    and whether *tomorrow* already carries a `DayOverride` (so a admin who
    cancelled tomorrow sees a heads-up today rather than being surprised)."""
    rows = await compute_user_summaries(session, usernames=usernames)
    if not rows:
        return []

    now = datetime.now(UTC)
    today = canonical_stamp(now, settings.tz).day
    tomorrow = today + timedelta(days=1)
    tomorrow_overrides = await day_overrides_batch(
        session, user_ids=[row.user.id for row in rows], day=tomorrow
    )
    user_ids = [row.user.id for row in rows]
    today_hours_overrides = await day_hour_overrides_batch(session, user_ids=user_ids, day=today)
    tomorrow_hours_overrides = await day_hour_overrides_batch(session, user_ids=user_ids, day=tomorrow)

    return [
        {
            "username": row.user.canonical_username,
            "display_name": row.user.display_name,
            "today_global_spent_s": row.today_global_spent_s,
            "today_effective_limit_s": row.today_effective_limit_s,
            "activity_state": row.activity_state,
            "as_of": row.as_of,
            "gated_today": row.gated_today,
            "gate_released_today": row.gate_released_today,
            "devices_active_today": row.devices_active_today,
            "today": today.isoformat(),
            "tomorrow": tomorrow.isoformat(),
            "tomorrow_override_s": tomorrow_overrides.get(row.user.id),
            "standing_hours_today": _hours_display(
                _to_wire_intervals(
                    (row.policy.allowed_hours_json or {}).get(str(today.isoweekday())) if row.policy else None
                )
            ),
            "today_hours_override": _hours_display(today_hours_overrides.get(row.user.id))
            if row.user.id in today_hours_overrides
            else None,
            "tomorrow_hours_override": _hours_display(tomorrow_hours_overrides.get(row.user.id))
            if row.user.id in tomorrow_hours_overrides
            else None,
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
    day: str = Form(""),
    session: AsyncSession = Depends(get_session),
    admin: Admin = Depends(get_current_admin_ui),
) -> HTMLResponse:
    """`day` defaults to today (unchanged behavior for the +/-30min quick
    actions); passing a future date is the "you lose 30 minutes tomorrow"
    flow GrantCreate.day's docstring describes -- see the dashboard's own
    "-30 min tomorrow" button."""
    result = await session.execute(select(User).where(User.canonical_username == username))
    user = result.scalar_one_or_none()
    if user is not None:
        now = datetime.now(UTC)
        stamp = canonical_stamp(now, settings.tz)
        grant_day = date.fromisoformat(day) if day else stamp.day
        minutes = seconds / 60
        grant = Grant(
            id=uuid.uuid4(),
            user_id=user.id,
            day=grant_day,
            seconds=seconds,
            reason=f"{minutes:+g} min ({grant_day.isoformat()}, UI)"
            if grant_day != stamp.day
            else f"{minutes:+g} min (UI)",
            source="admin",
            granted_by="ui",
        )
        session.add(grant)
        await record_audit_event(
            session,
            actor_type="admin",
            actor_id=str(admin.id),
            action="grant.create",
            target_type="user",
            target_id=username,
            after={"seconds": grant.seconds, "day": grant_day.isoformat(), "reason": grant.reason},
            ip=client_ip(request),
        )
        await session.commit()

    users = await _user_summaries(session, usernames=[username])
    return templates.TemplateResponse(request, "_users_fragment.html", {"users": users})
