"""Per-date overrides ("Tuesday is 30 minutes" / "no time tomorrow"), the
one-day allowed-hours override, and the approval gate's per-date release --
all deliberately separate from the policy editor (policy.py): none of them
bump a policy version, and only the hours override reaches a device, see
services/limits.py's module docstring and timekpr_hub_core.effective_policy.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from timekpr_hub_core.allowed_hours import (
    IntervalConflictError,
    TimeInterval,
    intervals_to_hours,
    unrestricted,
    validate_intervals,
)
from timekpr_hub_core.calendar import canonical_stamp
from timekpr_hub_core.models import WEEKDAY_TOKENS

from timekpr_hub.api.admin_auth import get_current_admin_ui
from timekpr_hub.api.ui.dashboard import _user_summaries
from timekpr_hub.api.ui.policy import _parse_time_str
from timekpr_hub.api.util import client_ip, get_user_or_404, templates, wire_intervals
from timekpr_hub.db.models import Admin
from timekpr_hub.db.session import get_session
from timekpr_hub.services.audit import record_audit_event
from timekpr_hub.services.day_hours import clear_day_hour_override, set_day_hour_override
from timekpr_hub.services.limits import clear_day_override, release_gate, set_day_override, unrelease_gate
from timekpr_hub.services.policy import get_or_create_policy
from timekpr_hub.settings import settings

router = APIRouter()


@router.post("/ui/users/{username}/day-override", response_class=HTMLResponse)
async def set_day_override_ui(
    request: Request,
    username: str,
    day: str = Form(...),
    mode: str = Form("none"),
    limit_h: int = Form(0, ge=0, le=24),
    limit_m: int = Form(0, ge=0, le=59),
    reason: str = Form(""),
    session: AsyncSession = Depends(get_session),
    admin: Admin = Depends(get_current_admin_ui),
) -> HTMLResponse:
    """`mode="none"` is a full moratorium (limit_seconds=0); `mode="limit"`
    sets the h/m pair instead. Either way this REPLACES that date's base
    limit rather than adding to it -- see services/limits.py::DayOverride's
    docstring for why that's not just a large negative grant."""
    user = await get_user_or_404(session, username)
    override_day = date.fromisoformat(day)
    limit_seconds = 0 if mode == "none" else limit_h * 3600 + limit_m * 60

    await set_day_override(
        session,
        user_id=user.id,
        day=override_day,
        limit_seconds=limit_seconds,
        reason=reason,
        created_by="ui",
    )
    await record_audit_event(
        session,
        actor_type="admin",
        actor_id=str(admin.id),
        action="override.set",
        target_type="user",
        target_id=username,
        after={"day": day, "limit_seconds": limit_seconds, "reason": reason},
        ip=client_ip(request),
    )
    await session.commit()

    users = await _user_summaries(session, usernames=[username])
    return templates.TemplateResponse(request, "_users_fragment.html", {"users": users})


@router.post("/ui/users/{username}/day-override/clear", response_class=HTMLResponse)
async def clear_day_override_ui(
    request: Request,
    username: str,
    day: str = Form(...),
    session: AsyncSession = Depends(get_session),
    admin: Admin = Depends(get_current_admin_ui),
) -> HTMLResponse:
    user = await get_user_or_404(session, username)
    cleared = await clear_day_override(session, user_id=user.id, day=date.fromisoformat(day))
    if cleared:
        await record_audit_event(
            session,
            actor_type="admin",
            actor_id=str(admin.id),
            action="override.clear",
            target_type="user",
            target_id=username,
            before={"day": day},
            ip=client_ip(request),
        )
        await session.commit()

    users = await _user_summaries(session, usernames=[username])
    return templates.TemplateResponse(request, "_users_fragment.html", {"users": users})


@router.post("/ui/users/{username}/day-hours", response_class=HTMLResponse)
async def set_day_hour_override_ui(
    request: Request,
    username: str,
    day: str = Form(...),
    mode: str = Form("window"),
    from_: str = Form("", alias="from"),
    to: str = Form(""),
    to_midnight: str | None = Form(None),
    reason: str = Form(""),
    session: AsyncSession = Depends(get_session),
    admin: Admin = Depends(get_current_admin_ui),
) -> HTMLResponse:
    """`mode="any"` writes the explicit unrestricted map (never `[]` -- see
    `timekpr_hub_core.allowed_hours.unrestricted`'s docstring); `mode="window"`
    parses `from`/`to` the same way the policy editor's "between" mode does
    (`_parse_day_hours`), except `to_midnight` stands in for `to` when
    checked -- `<input type=time>` can't submit "24:00" itself (see
    policy.py's `_BETWEEN_SEED` comment for why)."""
    user = await get_user_or_404(session, username)
    override_day = date.fromisoformat(day)

    if str(override_day.isoweekday()) not in (
        (await get_or_create_policy(session, user)).allowed_weekdays_json or WEEKDAY_TOKENS
    ):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"{day} isn't one of {user.display_name}'s allowed login days, so an hours window "
            "would have no effect -- change 'Days allowed to log in' in the policy first",
        )

    if mode == "any":
        intervals = wire_intervals(intervals_to_hours(unrestricted()))
    else:
        from_min = _parse_time_str(from_, "start time")
        to_min = 24 * 60 if to_midnight is not None else _parse_time_str(to, "end time")
        try:
            interval = TimeInterval(from_min, to_min)
            validate_intervals([interval])
        except (ValueError, IntervalConflictError) as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
        intervals = wire_intervals(intervals_to_hours([interval]))

    await set_day_hour_override(
        session,
        user_id=user.id,
        day=override_day,
        intervals=intervals,
        reason=reason,
        created_by="ui",
    )
    await record_audit_event(
        session,
        actor_type="admin",
        actor_id=str(admin.id),
        action="day_hours.set",
        target_type="user",
        target_id=username,
        after={"day": day, "mode": mode, "reason": reason},
        ip=client_ip(request),
    )
    await session.commit()

    users = await _user_summaries(session, usernames=[username])
    return templates.TemplateResponse(request, "_users_fragment.html", {"users": users})


@router.post("/ui/users/{username}/day-hours/clear", response_model=None)
async def clear_day_hour_override_ui(
    request: Request,
    username: str,
    day: str = Form(...),
    redirect_to: str = Form(""),
    session: AsyncSession = Depends(get_session),
    admin: Admin = Depends(get_current_admin_ui),
) -> HTMLResponse | RedirectResponse:
    """`redirect_to` is set only by the policy editor's banner (see
    user_policy.html) -- a full page, not a dashboard `.user-card` fragment,
    so it can't use the usual `data-post`/outerHTML-swap flow: there's no
    `.user-card` on that page for the JS in `_base.html` to swap into. A
    plain (non-AJAX) form submit here gets an ordinary 303 back to that
    page instead. The dashboard card's own Clear button leaves this blank
    and gets the fragment swap exactly as every other per-date control
    does."""
    user = await get_user_or_404(session, username)
    cleared = await clear_day_hour_override(session, user_id=user.id, day=date.fromisoformat(day))
    if cleared:
        await record_audit_event(
            session,
            actor_type="admin",
            actor_id=str(admin.id),
            action="day_hours.clear",
            target_type="user",
            target_id=username,
            before={"day": day},
            ip=client_ip(request),
        )
        await session.commit()

    if redirect_to:
        return RedirectResponse(redirect_to, status_code=status.HTTP_303_SEE_OTHER)

    users = await _user_summaries(session, usernames=[username])
    return templates.TemplateResponse(request, "_users_fragment.html", {"users": users})


@router.post("/ui/users/{username}/gate-release", response_class=HTMLResponse)
async def release_gate_ui(
    request: Request,
    username: str,
    session: AsyncSession = Depends(get_session),
    admin: Admin = Depends(get_current_admin_ui),
) -> HTMLResponse:
    """Releases *today* specifically -- the dashboard badge only ever shows
    for the current day, so there's no date to pick here (see
    /users/{username}/settings for the recurring gated_weekdays rule)."""
    user = await get_user_or_404(session, username)
    today = canonical_stamp(datetime.now(UTC), settings.tz).day
    await release_gate(session, user_id=user.id, day=today, released_by="ui")
    await record_audit_event(
        session,
        actor_type="admin",
        actor_id=str(admin.id),
        action="gate.release",
        target_type="user",
        target_id=username,
        after={"day": today.isoformat()},
        ip=client_ip(request),
    )
    await session.commit()

    users = await _user_summaries(session, usernames=[username])
    return templates.TemplateResponse(request, "_users_fragment.html", {"users": users})


@router.post("/ui/users/{username}/gate-unrelease", response_class=HTMLResponse)
async def unrelease_gate_ui(
    request: Request,
    username: str,
    session: AsyncSession = Depends(get_session),
    admin: Admin = Depends(get_current_admin_ui),
) -> HTMLResponse:
    """Reverses `release_gate_ui` for today -- re-gates the day (absence of
    a release row IS the gate)."""
    user = await get_user_or_404(session, username)
    today = canonical_stamp(datetime.now(UTC), settings.tz).day
    unreleased = await unrelease_gate(session, user_id=user.id, day=today)
    if unreleased:
        await record_audit_event(
            session,
            actor_type="admin",
            actor_id=str(admin.id),
            action="gate.unrelease",
            target_type="user",
            target_id=username,
            before={"day": today.isoformat()},
            ip=client_ip(request),
        )
        await session.commit()

    users = await _user_summaries(session, usernames=[username])
    return templates.TemplateResponse(request, "_users_fragment.html", {"users": users})
