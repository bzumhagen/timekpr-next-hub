"""Hub-UI routes: the dashboard, the per-user policy editor, and the
usage-statistics view.

Deliberately thin: reuses the same service functions as the JSON admin API
(`api/admin.py`) rather than duplicating logic, and renders server-side
HTML fragments/pages -- no client-side JS beyond `_base.html`'s small
form/poll helper, the dashboard's inline ticker, and the policy editor's
own "check/clear a whole day"
convenience buttons.

Every route here (and every /api/v1/* admin route) requires an
authenticated admin session -- see `get_current_admin_ui` in
`api/admin_auth.py`, applied router-level in `app.py`.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, date, datetime, timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from timekpr_hub_core.allowed_hours import (
    HourRecord,
    IntervalConflictError,
    TimeInterval,
    hours_to_intervals,
    intervals_to_hours,
    unrestricted,
    validate_intervals,
)
from timekpr_hub_core.calendar import canonical_stamp
from timekpr_hub_core.models import (
    AllowedHourInterval,
    LockoutType,
    PlayTimeActivity,
    PlayTimePayload,
    PolicyUpdate,
)

from timekpr_hub.api.admin_auth import get_current_admin_ui
from timekpr_hub.api.util import client_ip, get_user_or_404, templates
from timekpr_hub.db.models import Admin, Device, Grant, User
from timekpr_hub.db.session import get_session
from timekpr_hub.services.audit import list_audit_events, record_audit_event
from timekpr_hub.services.day_hours import (
    clear_day_hour_override,
    day_hour_override,
    day_hour_overrides_batch,
    set_day_hour_override,
)
from timekpr_hub.services.limits import (
    clear_day_override,
    day_overrides_batch,
    release_gate,
    set_day_override,
    unrelease_gate,
)
from timekpr_hub.services.policy import get_or_create_policy, policy_to_payload, update_policy
from timekpr_hub.services.summaries import compute_usage_history, compute_user_summaries
from timekpr_hub.settings import settings

router = APIRouter()

_WEEKDAY_TOKENS = ["1", "2", "3", "4", "5", "6", "7"]
_WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# (value, short label, one-line consequence), ordered least to most
# aggressive -- shown as the "At the limit" option-list rather than the bare
# enum values timekpr itself uses.
_LOCKOUT_OPTIONS = [
    ("lock", "Lock the screen", "Session keeps running in the background"),
    ("terminate", "Log out of the session", "Unsaved work may be lost"),
    ("suspend", "Suspend the computer", "Resumes instantly when woken"),
    ("suspendwake", "Suspend, but wake for a window", "Wakes during the hours below, then suspends again"),
    ("shutdown", "Shut the computer down", "A full power-off, not just a suspend"),
    ("kill", "Force-kill the session", "Last resort -- nothing is saved"),
]


@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "index.html", {"poll_ms": settings.default_next_poll_ms})


@router.get("/devices", response_class=HTMLResponse)
async def devices_page(request: Request) -> HTMLResponse:
    """Device sync status and enrollment-code generation, split out from
    the dashboard onto its own page -- device state is hub-wide (not
    per-user), so unlike the per-user policy/stats/settings pages it doesn't
    belong nested under a user card; it's just noise on the dashboard most
    of the time and only wanted when actually managing a device."""
    return templates.TemplateResponse(request, "devices.html", {"poll_ms": settings.default_next_poll_ms})


_AUDIT_PAGE_SIZE = 50


@router.get("/audit", response_class=HTMLResponse)
async def audit_page(
    request: Request, offset: int = 0, session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    """Every admin action, newest first -- the read side of
    services/audit.py's `record_audit_event`, which every mutating route in
    this file and api/admin.py already calls. Rendered once per request
    (no live poll, unlike the dashboard/devices pages): audit history
    doesn't change out from under the page the way live usage does, and
    `?offset=` pages back through it with plain links."""
    events = await list_audit_events(session, limit=_AUDIT_PAGE_SIZE + 1, offset=offset)
    has_more = len(events) > _AUDIT_PAGE_SIZE
    events = events[:_AUDIT_PAGE_SIZE]
    rows = [
        {
            "ts": e.ts.strftime("%Y-%m-%d %H:%M:%S UTC"),
            "actor": f"{e.actor_type} {e.actor_id}" if e.actor_id else e.actor_type,
            "action": e.action,
            "target": f"{e.target_type} {e.target_id}" if e.target_type else None,
            "before": json.dumps(e.before_json, indent=2, sort_keys=True) if e.before_json else None,
            "after": json.dumps(e.after_json, indent=2, sort_keys=True) if e.after_json else None,
            "ip": e.ip,
        }
        for e in events
    ]
    return templates.TemplateResponse(
        request,
        "audit.html",
        {
            "events": rows,
            "offset": offset,
            "page_size": _AUDIT_PAGE_SIZE,
            "has_more": has_more,
            "prev_offset": max(0, offset - _AUDIT_PAGE_SIZE),
        },
    )


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


# --------------------------------------------------------------------------
# Per-date overrides ("Tuesday is 30 minutes" / "no time tomorrow") and the
# approval gate's per-date release -- both deliberately separate from the
# policy editor: neither bumps a policy version or pushes anything to a
# device, see services/limits.py's module docstring.
# --------------------------------------------------------------------------


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


# --------------------------------------------------------------------------
# One-day allowed-hours override ("today, 12:00-20:00 instead of the usual
# 15:00-20:00" / "any time today"). Separate control from the day-override
# above: that replaces the day's LIMIT (seconds), this replaces WHEN it may
# be used -- and unlike every override on this page so far, this one DOES
# reach the device on its next /sync (see timekpr_hub_core.effective_policy).
# --------------------------------------------------------------------------


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
    checked -- `<input type=time>` can't submit "24:00" itself (see the
    comment on `_BETWEEN_SEED` above for why)."""
    user = await get_user_or_404(session, username)
    override_day = date.fromisoformat(day)

    if str(override_day.isoweekday()) not in (
        (await get_or_create_policy(session, user)).allowed_weekdays_json or _WEEKDAY_TOKENS
    ):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"{day} isn't one of {user.display_name}'s allowed login days, so an hours window "
            "would have no effect -- change 'Days allowed to log in' in the policy first",
        )

    if mode == "any":
        records = intervals_to_hours(unrestricted())
        intervals = [
            AllowedHourInterval(hour=r.hour, start_min=r.start_min, end_min=r.end_min) for r in records
        ]
    else:
        from_min = _parse_time_str(from_, "start time")
        to_min = 24 * 60 if to_midnight is not None else _parse_time_str(to, "end time")
        try:
            interval = TimeInterval(from_min, to_min)
            validate_intervals([interval])
        except (ValueError, IntervalConflictError) as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
        records = intervals_to_hours([interval])
        intervals = [
            AllowedHourInterval(hour=r.hour, start_min=r.start_min, end_min=r.end_min) for r in records
        ]

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


# --------------------------------------------------------------------------
# Full policy editor
# --------------------------------------------------------------------------


def _split_hm(total_s: int) -> dict:
    total_m = total_s // 60
    return {"h": total_m // 60, "m": total_m % 60}


_FULL_DAY = (0, 24 * 60)

# Seed value for the "Between" mode's two <input type=time> fields when the
# day is actually in "All day" mode (i.e. these are never the *stored*
# from/to -- only what appears if a admin switches that day to "Between").
# NOT (0, 24*60): an <input type=time> only accepts 00:00-23:59, so "24:00"
# (this module's own internal end-exclusive representation of midnight) is
# an invalid attribute value a browser silently rejects, leaving the field
# blank the first time anyone switches modes. A plain daytime range is a
# far more useful starting point to edit from anyway.
_BETWEEN_SEED = (9 * 60, 17 * 60)


def _classify_day_hours(intervals: list[AllowedHourInterval] | None) -> dict:
    """Turns one day's stored `AllowedHourInterval`s into what the editor
    actually shows: an "all day / between / custom" mode, so a admin almost
    never has to meet the raw per-hour checkbox grid.

    `intervals` absent entirely (day never set, e.g. a brand-new user) is
    "all day" -- matching `core.allowed_hours.unrestricted()`'s convention
    that "no restriction" must be an explicit all-24-hours entry, never an
    empty one (see that module's own docstring: an absent hour means
    *forbidden* to timekpr, not *allowed*).

    A single interval spanning the whole day is also "all day". A single
    partial interval is "between" (rendered as two <input type=time>
    fields, so it gets minute precision the old whole-hour checkbox grid
    could not express). Anything else (zero intervals, or more than one --
    i.e. a genuinely split day) is "custom", rendered as the paint track at
    whole-hour granularity, same as this editor's first version.

    `unaccounted` (custom mode only) is True when every checked hour that
    day is timekpr's "!" unaccounted -- allowed, but not counted against
    the daily limit (e.g. a standing homework hour). It's a single
    day-level toggle rather than a per-hour one: real per-hour granularity
    would need a second paint track, and a day that mixes accounted and
    unaccounted hours is rare enough not to justify that UI cost yet."""
    if intervals is None:
        return {
            "mode": "all",
            "from_min": _BETWEEN_SEED[0],
            "to_min": _BETWEEN_SEED[1],
            "hours": set(range(24)),
            "unaccounted": False,
        }

    records = [HourRecord(iv.hour, iv.start_min, iv.end_min, iv.unaccounted) for iv in intervals]
    merged = hours_to_intervals(records)
    hours = {iv.hour for iv in intervals}
    unaccounted = bool(intervals) and all(iv.unaccounted for iv in intervals)

    if not merged:
        return {
            "mode": "custom",
            "from_min": _BETWEEN_SEED[0],
            "to_min": _BETWEEN_SEED[1],
            "hours": hours,
            "unaccounted": unaccounted,
        }
    if len(merged) == 1 and (merged[0].start_min, merged[0].end_min) == _FULL_DAY:
        return {
            "mode": "all",
            "from_min": _BETWEEN_SEED[0],
            "to_min": _BETWEEN_SEED[1],
            "hours": set(range(24)),
            "unaccounted": False,
        }
    # A single merged interval is normally "between" (minute precision, no
    # unaccounted checkbox in that mode's UI) -- but if it's unaccounted,
    # showing it as "between" would silently lose that flag on the next
    # save, since "between" mode has nowhere to display or resubmit it.
    # Two adjacent unaccounted hours in custom mode merge into exactly one
    # interval here, so this is a real, reachable case, not a theoretical
    # one.
    if len(merged) == 1 and not merged[0].unaccounted:
        return {
            "mode": "between",
            "from_min": merged[0].start_min,
            "to_min": merged[0].end_min,
            "hours": hours,
            "unaccounted": False,
        }
    return {"mode": "custom", "from_min": 0, "to_min": 0, "hours": hours, "unaccounted": unaccounted}


def _fmt_hm(total_min: int) -> str:
    return f"{total_min // 60:02d}:{total_min % 60:02d}"


def _to_wire_intervals(raw: list[dict] | None) -> list[AllowedHourInterval] | None:
    """`Policy.allowed_hours_json[day]` is a plain JSON list of dicts (raw
    storage); this is the wire-model form `_classify_day_hours` and
    `_hours_display` expect."""
    if raw is None:
        return None
    return [AllowedHourInterval.model_validate(iv) for iv in raw]


def _hours_display(intervals: list[AllowedHourInterval] | None) -> str:
    """A short human string for one day's allowed-hours -- "any time",
    "12:00-20:00", or "custom hours" -- reusing `_classify_day_hours`'s
    tested inversion rather than re-deriving a mode from raw intervals."""
    classified = _classify_day_hours(intervals)
    if classified["mode"] == "all":
        return "any time"
    if classified["mode"] == "between":
        return f"{_fmt_hm(classified['from_min'])}–{_fmt_hm(classified['to_min'])}"
    return "custom hours"


def _policy_view_model(payload) -> dict:
    """Shapes a `PolicyPayload` for the editor template. Time values are
    handed over as `{h, m}` pairs (never bare minutes) so the template never
    has to do arithmetic, and allowed-hours are pre-classified into the
    all/between/custom modes `_classify_day_hours` picks."""
    same_daily = len(set(payload.daily_limits_s)) == 1
    hours_by_day: dict[str, dict] = {}
    for day in _WEEKDAY_TOKENS:
        hours_by_day[day] = _classify_day_hours(payload.allowed_hours.get(day))
        hours_by_day[day]["from_str"] = _fmt_hm(hours_by_day[day]["from_min"])
        hours_by_day[day]["to_str"] = _fmt_hm(hours_by_day[day]["to_min"])

    lockout_type = (
        payload.lockout_type.value if hasattr(payload.lockout_type, "value") else payload.lockout_type
    )

    return {
        "version": payload.version,
        "daily_same": same_daily,
        "daily_master": _split_hm(payload.daily_limits_s[0]),
        "daily_per_day": [_split_hm(s) for s in payload.daily_limits_s],
        "hours_by_day": hours_by_day,
        "weekly_enabled": payload.weekly_limit_s < 7 * 86400,
        "weekly": _split_hm(payload.weekly_limit_s),
        "monthly_enabled": payload.monthly_limit_s < 31 * 86400,
        "monthly": _split_hm(payload.monthly_limit_s),
        "allowed_weekdays": set(payload.allowed_weekdays or _WEEKDAY_TOKENS),
        "lockout_type": lockout_type,
        "wake_from": payload.wake_from or "",
        "wake_to": payload.wake_to or "",
        "track_inactive": payload.track_inactive,
        "hide_tray_icon": payload.hide_tray_icon,
        "note": payload.note or "",
        "playtime": payload.playtime,
        "pt_allowed_weekdays": set(payload.playtime.allowed_weekdays or []),
        "pt_daily": [_split_hm(s) for s in payload.playtime.daily_limits_s],
        "pt_activities_text": "\n".join(f"{a.mask}|{a.description}" for a in payload.playtime.activities),
    }


@router.get("/users/{username}", response_class=HTMLResponse)
async def user_policy_page(
    request: Request, username: str, session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    user = await get_user_or_404(session, username)
    policy = await get_or_create_policy(session, user)
    today = canonical_stamp(datetime.now(UTC), settings.tz).day
    today_override = await day_hour_override(session, user_id=user.id, day=today)
    await session.commit()
    payload = policy_to_payload(policy)
    return templates.TemplateResponse(
        request,
        "user_policy.html",
        {
            "username": username,
            "display_name": user.display_name,
            "weekday_names": _WEEKDAY_NAMES,
            "weekday_tokens": _WEEKDAY_TOKENS,
            "lockout_options": _LOCKOUT_OPTIONS,
            "p": _policy_view_model(payload),
            # "Today is temporarily overridden" banner -- without it the
            # editor would render this weekday's stored hours while the
            # device is actually running something else (see
            # timekpr_hub_core.effective_policy). Saving this form does
            # NOT clear the override; the two are deliberately independent.
            "today": today.isoformat(),
            "today_hours_override": _hours_display(_to_wire_intervals(today_override.intervals_json))
            if today_override is not None
            else None,
        },
    )


def _checkbox(form, name: str) -> bool:
    return form.get(name) is not None


def _hm_seconds(form, h_name: str, m_name: str) -> int:
    h = int(form.get(h_name, 0) or 0)
    m = int(form.get(m_name, 0) or 0)
    return max(0, h) * 3600 + max(0, min(m, 59)) * 60


def _parse_time_str(value: str, field: str) -> int:
    """Parses an `<input type=time>` value ("HH:MM") into minutes since
    midnight, raising a 422 naming the offending field rather than a bare
    ValueError/500 on a malformed or empty submission."""
    try:
        hh, mm = value.split(":")
        total = int(hh) * 60 + int(mm)
    except (ValueError, AttributeError) as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"invalid time for {field}: {value!r}"
        ) from exc
    if not (0 <= total <= 24 * 60):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"invalid time for {field}: {value!r}")
    return total


def _parse_day_hours(form, day: str) -> list[AllowedHourInterval]:
    """One day's allowed-hours submission, in whichever of the three modes
    `_classify_day_hours` can render: "all" writes the explicit unrestricted
    map (never an empty one -- an absent hour means *forbidden* to timekpr,
    not *allowed*); "between" is the common case and gets real minute
    precision by going through `validate_intervals`/`intervals_to_hours`
    (the pure, property-tested module the first version of this editor
    bypassed entirely); "custom" keeps the original discrete-hour checkbox
    semantics for the rare day that genuinely needs more than one window."""
    mode = form.get(f"hours_mode_{day}", "all")
    day_name = _WEEKDAY_NAMES[int(day) - 1]

    if mode == "all":
        records = intervals_to_hours(unrestricted())
        return [AllowedHourInterval(hour=r.hour, start_min=r.start_min, end_min=r.end_min) for r in records]

    if mode == "between":
        from_min = _parse_time_str(form.get(f"hours_from_{day}", ""), f"{day_name} start time")
        to_min = _parse_time_str(form.get(f"hours_to_{day}", ""), f"{day_name} end time")
        try:
            interval = TimeInterval(from_min, to_min)
            validate_intervals([interval])
        except (ValueError, IntervalConflictError) as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"{day_name}: {exc}") from exc
        records = intervals_to_hours([interval])
        return [AllowedHourInterval(hour=r.hour, start_min=r.start_min, end_min=r.end_min) for r in records]

    # Remaining case: mode == "custom".
    checked_hours = sorted(h for h in range(24) if _checkbox(form, f"hh_{day}_{h}"))
    if not checked_hours:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"No allowed hours are set for {day_name}. To block a whole day, uncheck it under "
            "'Days allowed to log in' in Advanced settings instead -- an empty hour list can't be "
            "applied on the device.",
        )
    unaccounted = _checkbox(form, f"unaccounted_{day}")
    return [
        AllowedHourInterval(hour=h, start_min=0, end_min=60, unaccounted=unaccounted) for h in checked_hours
    ]


async def _parse_policy_form(form) -> PolicyUpdate:
    """Builds a full `PolicyUpdate` from the editor's raw form fields.
    Raises `HTTPException(422)` with a specific message for anything the
    pure `allowed_hours` model itself doesn't already validate."""
    if form.get("daily_mode", "same") == "same":
        master_s = _hm_seconds(form, "daily_h", "daily_m")
        daily_limits_s = [master_s] * 7
    else:
        daily_limits_s = [_hm_seconds(form, f"daily_h_{i}", f"daily_m_{i}") for i in range(7)]

    allowed_hours: dict[str, list[AllowedHourInterval]] = {
        day: _parse_day_hours(form, day) for day in _WEEKDAY_TOKENS
    }

    allowed_weekdays = [d for d in _WEEKDAY_TOKENS if _checkbox(form, f"allowed_weekday_{d}")]
    if not allowed_weekdays:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "At least one day must be allowed to log in at all."
        )

    lockout_type_raw = form.get("lockout_type", "lock")
    try:
        lockout_type = LockoutType(lockout_type_raw)
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"unknown lockout type {lockout_type_raw!r}"
        ) from exc

    # An unchecked cap writes timekpr's own "no cap" maximum (7/31 days'
    # worth of seconds) rather than 0 -- unchecked must mean "uncapped", not
    # "instantly out of time".
    weekly_limit_s = (
        _hm_seconds(form, "weekly_h", "weekly_m") if _checkbox(form, "weekly_cap_enabled") else 7 * 86400
    )
    monthly_limit_s = (
        _hm_seconds(form, "monthly_h", "monthly_m") if _checkbox(form, "monthly_cap_enabled") else 31 * 86400
    )

    pt_daily_limits_s = [_hm_seconds(form, f"pt_daily_h_{i}", f"pt_daily_m_{i}") for i in range(7)]

    pt_activities: list[PlayTimeActivity] = []
    for line in (form.get("pt_activities") or "").splitlines():
        line = line.strip()
        if not line:
            continue
        mask, _, description = line.partition("|")
        if not mask.strip():
            continue
        pt_activities.append(PlayTimeActivity(mask=mask.strip(), description=description.strip()))

    try:
        return PolicyUpdate(
            daily_limits_s=daily_limits_s,
            weekly_limit_s=weekly_limit_s,
            monthly_limit_s=monthly_limit_s,
            allowed_weekdays=allowed_weekdays,
            allowed_hours=allowed_hours,
            lockout_type=lockout_type,
            wake_from=(form.get("wake_from") or None),
            wake_to=(form.get("wake_to") or None),
            track_inactive=_checkbox(form, "track_inactive"),
            hide_tray_icon=_checkbox(form, "hide_tray_icon"),
            playtime=PlayTimePayload(
                enabled=_checkbox(form, "pt_enabled"),
                override_enabled=_checkbox(form, "pt_override"),
                unaccounted_intervals_enabled=_checkbox(form, "pt_unaccounted"),
                allowed_weekdays=[d for d in _WEEKDAY_TOKENS if _checkbox(form, f"pt_allowed_weekday_{d}")],
                daily_limits_s=pt_daily_limits_s,
                activities=pt_activities,
            ),
            note=(form.get("note") or ""),
        )
    except ValidationError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc


@router.post("/users/{username}/policy")
async def update_policy_ui(
    request: Request,
    username: str,
    session: AsyncSession = Depends(get_session),
    admin: Admin = Depends(get_current_admin_ui),
):
    user = await get_user_or_404(session, username)
    form = await request.form()
    update = await _parse_policy_form(form)

    before_policy = await get_or_create_policy(session, user)
    before = policy_to_payload(before_policy).model_dump(mode="json")

    policy = await update_policy(session, user=user, update=update, created_by="ui")
    await record_audit_event(
        session,
        actor_type="admin",
        actor_id=str(admin.id),
        action="policy.update",
        target_type="user",
        target_id=username,
        before=before,
        after=policy_to_payload(policy).model_dump(mode="json"),
        ip=client_ip(request),
    )
    await session.commit()
    return RedirectResponse(f"/users/{username}", status_code=status.HTTP_303_SEE_OTHER)


# --------------------------------------------------------------------------
# Per-user settings: the hub-only knobs that never reach `PolicyPayload` or
# a device (which weekdays are chore-gated, the accounting mode). A separate
# page with its own single save button, deliberately not a second card on
# the policy editor -- that page has exactly one Save, and a second one
# would reintroduce the tab-scoped-Apply confusion that makes `timekpra`'s
# own settings window easy to get wrong.
# --------------------------------------------------------------------------


@router.get("/users/{username}/settings", response_class=HTMLResponse)
async def user_settings_page(
    request: Request, username: str, session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    user = await get_user_or_404(session, username)
    return templates.TemplateResponse(
        request,
        "user_settings.html",
        {
            "username": username,
            "display_name": user.display_name,
            "weekday_names": _WEEKDAY_NAMES,
            "weekday_tokens": _WEEKDAY_TOKENS,
            "gated_weekdays": set(user.gated_weekdays_json or []),
            "accounting_mode": user.accounting_mode,
            "offline_policy": user.offline_policy,
            "offline_grace_min": user.offline_grace_s // 60,
            "offline_cap_min": user.offline_cap_s // 60,
        },
    )


@router.post("/users/{username}/settings")
async def update_user_settings_ui(
    request: Request,
    username: str,
    accounting_mode: str = Form("wallclock"),
    offline_policy: str = Form("capped"),
    offline_grace_min: int = Form(15, ge=0, le=10080),
    offline_cap_min: int = Form(30, ge=0, le=10080),
    session: AsyncSession = Depends(get_session),
    admin: Admin = Depends(get_current_admin_ui),
):
    user = await get_user_or_404(session, username)
    form = await request.form()
    gated_weekdays = [d for d in _WEEKDAY_TOKENS if _checkbox(form, f"gated_weekday_{d}")]

    before = {
        "gated_weekdays": user.gated_weekdays_json,
        "accounting_mode": user.accounting_mode,
        "offline_policy": user.offline_policy,
        "offline_grace_s": user.offline_grace_s,
        "offline_cap_s": user.offline_cap_s,
    }
    user.gated_weekdays_json = gated_weekdays
    user.accounting_mode = accounting_mode
    user.offline_policy = offline_policy
    user.offline_grace_s = offline_grace_min * 60
    user.offline_cap_s = offline_cap_min * 60
    after = {
        "gated_weekdays": gated_weekdays,
        "accounting_mode": accounting_mode,
        "offline_policy": offline_policy,
        "offline_grace_s": user.offline_grace_s,
        "offline_cap_s": user.offline_cap_s,
    }
    await record_audit_event(
        session,
        actor_type="admin",
        actor_id=str(admin.id),
        action="user_settings.update",
        target_type="user",
        target_id=username,
        before=before,
        after=after,
        ip=client_ip(request),
    )
    await session.commit()
    return RedirectResponse(f"/users/{username}/settings", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/users/{username}/rename")
async def rename_user_ui(
    request: Request,
    username: str,
    display_name: str = Form(..., min_length=1, max_length=128),
    session: AsyncSession = Depends(get_session),
    admin: Admin = Depends(get_current_admin_ui),
):
    """Changes only the display name shown in the hub UI -- `username`
    (`User.canonical_username`, the local unix account it's matched
    against) is never editable here."""
    user = await get_user_or_404(session, username)
    before = user.display_name
    user.display_name = display_name
    await record_audit_event(
        session,
        actor_type="admin",
        actor_id=str(admin.id),
        action="user.rename",
        target_type="user",
        target_id=username,
        before={"display_name": before},
        after={"display_name": display_name},
        ip=client_ip(request),
    )
    await session.commit()
    return RedirectResponse(f"/users/{username}/settings", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/users/{username}/delete")
async def delete_user_ui(
    request: Request,
    username: str,
    session: AsyncSession = Depends(get_session),
    admin: Admin = Depends(get_current_admin_ui),
):
    """Permanently removes the user and everything FK'd to it (aliases,
    usage counters, activity intervals, grants, overrides, policies) via
    ON DELETE CASCADE -- there is no revoke-only middle ground for a user
    the way there is for a device, since (unlike a device) a user has no
    ongoing artifact (a token) to revoke independently of its history.
    Devices that go on reporting this local username are unaffected: the
    next /sync for it re-provisions a fresh user via
    services/enrollment.py, exactly as if it had never been added."""
    user = await get_user_or_404(session, username)
    await record_audit_event(
        session,
        actor_type="admin",
        actor_id=str(admin.id),
        action="user.delete",
        target_type="user",
        target_id=username,
        before={"display_name": user.display_name},
        ip=client_ip(request),
    )
    await session.delete(user)
    await session.commit()
    return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)


# --------------------------------------------------------------------------
# Usage statistics
# --------------------------------------------------------------------------


@router.get("/users/{username}/stats", response_class=HTMLResponse)
async def user_stats_page(
    request: Request, username: str, days: int = 30, session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    user = await get_user_or_404(session, username)
    policy = await get_or_create_policy(session, user)
    await session.commit()
    num_days = days if days in (7, 30, 90) else 30
    history = await compute_usage_history(
        session, user=user, policy=policy, num_days=num_days, tz=settings.tz
    )
    max_s = max([d.limit_s for d in history.days] + [d.spent_s for d in history.days] + [1])
    # Precomputed here (not in the template) so the wording matches the
    # dashboard card and policy editor banner exactly -- all three go
    # through `_hours_display`.
    hours_override_display = {
        d.day: _hours_display(d.hours_override_intervals) for d in history.days if d.hours_overridden
    }
    return templates.TemplateResponse(
        request,
        "user_stats.html",
        {
            "username": username,
            "display_name": user.display_name,
            "days": num_days,
            "history": history,
            "max_s": max_s,
            "hours_override_display": hours_override_display,
        },
    )


# --------------------------------------------------------------------------
# Devices
# --------------------------------------------------------------------------


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
    result = await session.execute(select(Device).where(Device.id == device_id))
    device = result.scalar_one_or_none()
    if device is not None:
        before_status = device.status
        device.status = "revoked"
        await record_audit_event(
            session,
            actor_type="admin",
            actor_id=str(admin.id),
            action="device.revoke",
            target_type="device",
            target_id=str(device.id),
            before={"status": before_status},
            after={"status": device.status},
            ip=client_ip(request),
        )
        await session.commit()
    return await devices_fragment(request, session)


@router.post("/ui/devices/{device_id}/observe", response_class=HTMLResponse)
async def set_device_observe_mode_ui(
    request: Request, device_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    """Dry-run mode: the agent keeps syncing but never writes to DBUS --
    see api/admin.py's
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
    result = await session.execute(select(Device).where(Device.id == device_id))
    device = result.scalar_one_or_none()
    if device is not None:
        before = {"name": device.name, "status": device.status}
        await record_audit_event(
            session,
            actor_type="admin",
            actor_id=str(admin.id),
            action="device.delete",
            target_type="device",
            target_id=str(device_id),
            before=before,
            ip=client_ip(request),
        )
        await session.delete(device)
        await session.commit()
    return await devices_fragment(request, session)


@router.post("/ui/enrollment-codes", response_class=HTMLResponse)
async def create_enrollment_code_ui(
    request: Request, session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    from timekpr_hub.api.admin import create_enrollment_code

    result = await create_enrollment_code(session)
    # The real flag is --hub-url (not --hub), so this line can be
    # copy-pasted straight into a terminal. Built from the request's own
    # host:port so it works for a LAN hostname or Tailscale address too, not
    # just whatever URL happened to be typed into a README example.
    command = f"sudo timekpr-hub-agent enroll --hub-url {request.base_url} --code {result['code']}"
    return HTMLResponse(
        f"<p>Code: <code>{result['code']}</code> (expires {result['expires_at']}). "
        f"Run on the new device:</p><pre>{command}</pre>"
        "<p>Or just run <code>sudo timekpr-hub-agent enroll</code> with no flags at all -- "
        "it prompts for the hub URL, the code, and which local users to manage.</p>"
    )


# --------------------------------------------------------------------------
# Admin accounts
# --------------------------------------------------------------------------


async def _admins_fragment_context(session: AsyncSession, admin: Admin) -> dict:
    result = await session.execute(select(Admin))
    admins = [{"id": str(a.id), "email": a.email, "you": a.id == admin.id} for a in result.scalars().all()]
    return {"admins": admins, "can_delete": len(admins) > 1}


@router.get("/admins", response_class=HTMLResponse)
async def admins_page(
    request: Request,
    session: AsyncSession = Depends(get_session),
    admin: Admin = Depends(get_current_admin_ui),
) -> HTMLResponse:
    context = await _admins_fragment_context(session, admin)
    return templates.TemplateResponse(request, "admins.html", context)


@router.post("/ui/admin-invites", response_class=HTMLResponse)
async def create_admin_invite_ui(
    request: Request,
    session: AsyncSession = Depends(get_session),
    admin: Admin = Depends(get_current_admin_ui),
) -> HTMLResponse:
    from timekpr_hub.api.admin import create_admin_invite

    result = await create_admin_invite(request, session=session, admin=admin)
    return HTMLResponse(
        f"<p>Invite link (expires in 24h, single-use):</p><pre>{result['url']}</pre>"
        "<p>Send it to the person you're inviting -- they'll set their own password.</p>"
    )


@router.post("/ui/admins/{target_admin_id}/delete", response_class=HTMLResponse)
async def delete_admin_ui(
    request: Request,
    target_admin_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    admin: Admin = Depends(get_current_admin_ui),
) -> HTMLResponse:
    from timekpr_hub.api.admin import delete_admin

    try:
        await delete_admin(target_admin_id, request, session=session, admin=admin)
    except HTTPException:
        pass  # last-remaining-admin guard -- the fragment re-render below just shows them all still present
    context = await _admins_fragment_context(session, admin)
    return templates.TemplateResponse(request, "_admins_fragment.html", context)


@router.post("/ui/admin/password", response_class=HTMLResponse)
async def change_own_password_ui(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(..., min_length=8),
    session: AsyncSession = Depends(get_session),
    admin: Admin = Depends(get_current_admin_ui),
) -> HTMLResponse:
    from timekpr_hub.api.admin import PasswordChange, change_own_password

    try:
        await change_own_password(
            request,
            PasswordChange(current_password=current_password, new_password=new_password),
            session=session,
            admin=admin,
        )
    except HTTPException as exc:
        return HTMLResponse(f'<p style="color: var(--tk-danger);">{exc.detail}</p>')
    return HTMLResponse("<p>Password changed.</p>")
