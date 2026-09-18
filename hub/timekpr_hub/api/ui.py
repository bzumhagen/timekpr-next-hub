"""Hub-UI routes (PLAN: "One UI page... (Jinja2 + HTMX)", extended with a
full per-user policy editor and usage-statistics view -- see the plan's
"Full policy management + usage statistics in the hub UI").

Deliberately thin: reuses the same service functions as the JSON parent API
(`api/parent.py`) rather than duplicating logic, and renders server-side
HTML fragments/pages -- no client-side JS beyond htmx.min.js, the dashboard's
small inline ticker, and the policy editor's own "check/clear a whole day"
convenience buttons.

Every route here (and every /api/v1/* parent route) requires an
authenticated parent session -- see `get_current_parent_ui` in
`api/parent_auth.py`, applied router-level in `app.py`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
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

from timekpr_hub.api.parent_auth import get_current_parent_ui
from timekpr_hub.db.models import Device, Grant, Parent, User
from timekpr_hub.db.session import get_session
from timekpr_hub.services.audit import record_audit_event
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
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "web" / "templates"))

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


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "index.html", {"poll_ms": settings.default_next_poll_ms})


@router.get("/devices", response_class=HTMLResponse)
async def devices_page(request: Request) -> HTMLResponse:
    """Device sync status and enrollment-code generation, split out from
    the dashboard onto its own page -- device state is hub-wide (not
    per-kid), so unlike the per-user policy/stats/settings pages it doesn't
    belong nested under a user card; it's just noise on the dashboard most
    of the time and only wanted when actually managing a device."""
    return templates.TemplateResponse(request, "devices.html", {"poll_ms": settings.default_next_poll_ms})


async def _user_summaries(session: AsyncSession, *, usernames: list[str] | None = None) -> list[dict]:
    """Template-shaped view of `compute_user_summaries` (services/
    summaries.py, shared with the JSON parent API), plus two UI-only
    additions: today's gate state (for the dashboard badge/Release button)
    and whether *tomorrow* already carries a `DayOverride` (so a parent who
    cancelled tomorrow sees a heads-up today rather than being surprised)."""
    rows = await compute_user_summaries(session, usernames=usernames)
    if not rows:
        return []

    now = datetime.now(UTC)
    tomorrow = canonical_stamp(now, settings.tz).day + timedelta(days=1)
    tomorrow_overrides = await day_overrides_batch(
        session, user_ids=[row.user.id for row in rows], day=tomorrow
    )

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
            "tomorrow": tomorrow.isoformat(),
            "tomorrow_override_s": tomorrow_overrides.get(row.user.id),
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
    parent: Parent = Depends(get_current_parent_ui),
) -> HTMLResponse:
    result = await session.execute(select(User).where(User.canonical_username == username))
    user = result.scalar_one_or_none()
    if user is not None:
        now = datetime.now(UTC)
        stamp = canonical_stamp(now, settings.tz)
        minutes = seconds / 60
        grant = Grant(
            id=uuid.uuid4(),
            user_id=user.id,
            day=stamp.day,
            seconds=seconds,
            reason=f"{minutes:+g} min (UI)",
            source="parent",
            granted_by="ui",
        )
        session.add(grant)
        await record_audit_event(
            session,
            actor_type="parent",
            actor_id=str(parent.id),
            action="grant.create",
            target_type="user",
            target_id=username,
            after={"seconds": grant.seconds, "day": stamp.day_str, "reason": grant.reason},
            ip=_client_ip(request),
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
    parent: Parent = Depends(get_current_parent_ui),
) -> HTMLResponse:
    """`mode="none"` is a full moratorium (limit_seconds=0); `mode="limit"`
    sets the h/m pair instead. Either way this REPLACES that date's base
    limit rather than adding to it -- see services/limits.py::DayOverride's
    docstring for why that's not just a large negative grant."""
    user = await _get_user_or_404(session, username)
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
        actor_type="parent",
        actor_id=str(parent.id),
        action="override.set",
        target_type="user",
        target_id=username,
        after={"day": day, "limit_seconds": limit_seconds, "reason": reason},
        ip=_client_ip(request),
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
    parent: Parent = Depends(get_current_parent_ui),
) -> HTMLResponse:
    user = await _get_user_or_404(session, username)
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

    users = await _user_summaries(session, usernames=[username])
    return templates.TemplateResponse(request, "_users_fragment.html", {"users": users})


@router.post("/ui/users/{username}/gate-release", response_class=HTMLResponse)
async def release_gate_ui(
    request: Request,
    username: str,
    session: AsyncSession = Depends(get_session),
    parent: Parent = Depends(get_current_parent_ui),
) -> HTMLResponse:
    """Releases *today* specifically -- the dashboard badge only ever shows
    for the current day, so there's no date to pick here (see
    /users/{username}/settings for the recurring gated_weekdays rule)."""
    user = await _get_user_or_404(session, username)
    today = canonical_stamp(datetime.now(UTC), settings.tz).day
    await release_gate(session, user_id=user.id, day=today, released_by="ui")
    await record_audit_event(
        session,
        actor_type="parent",
        actor_id=str(parent.id),
        action="gate.release",
        target_type="user",
        target_id=username,
        after={"day": today.isoformat()},
        ip=_client_ip(request),
    )
    await session.commit()

    users = await _user_summaries(session, usernames=[username])
    return templates.TemplateResponse(request, "_users_fragment.html", {"users": users})


@router.post("/ui/users/{username}/gate-unrelease", response_class=HTMLResponse)
async def unrelease_gate_ui(
    request: Request,
    username: str,
    session: AsyncSession = Depends(get_session),
    parent: Parent = Depends(get_current_parent_ui),
) -> HTMLResponse:
    """Reverses `release_gate_ui` for today -- re-gates the day (absence of
    a release row IS the gate)."""
    user = await _get_user_or_404(session, username)
    today = canonical_stamp(datetime.now(UTC), settings.tz).day
    unreleased = await unrelease_gate(session, user_id=user.id, day=today)
    if unreleased:
        await record_audit_event(
            session,
            actor_type="parent",
            actor_id=str(parent.id),
            action="gate.unrelease",
            target_type="user",
            target_id=username,
            before={"day": today.isoformat()},
            ip=_client_ip(request),
        )
        await session.commit()

    users = await _user_summaries(session, usernames=[username])
    return templates.TemplateResponse(request, "_users_fragment.html", {"users": users})


# --------------------------------------------------------------------------
# Full policy editor
# --------------------------------------------------------------------------


async def _get_user_or_404(session: AsyncSession, username: str) -> User:
    result = await session.execute(select(User).where(User.canonical_username == username))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown user")
    return user


def _split_hm(total_s: int) -> dict:
    total_m = total_s // 60
    return {"h": total_m // 60, "m": total_m % 60}


_FULL_DAY = (0, 24 * 60)

# Seed value for the "Between" mode's two <input type=time> fields when the
# day is actually in "All day" mode (i.e. these are never the *stored*
# from/to -- only what appears if a parent switches that day to "Between").
# NOT (0, 24*60): an <input type=time> only accepts 00:00-23:59, so "24:00"
# (this module's own internal end-exclusive representation of midnight) is
# an invalid attribute value a browser silently rejects, leaving the field
# blank the first time anyone switches modes. A plain daytime range is a
# far more useful starting point to edit from anyway.
_BETWEEN_SEED = (9 * 60, 17 * 60)


def _classify_day_hours(intervals: list[AllowedHourInterval] | None) -> dict:
    """Turns one day's stored `AllowedHourInterval`s into what the editor
    actually shows: an "all day / between / custom" mode, so a parent almost
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
    whole-hour granularity, same as this editor's first version."""
    if intervals is None:
        return {
            "mode": "all",
            "from_min": _BETWEEN_SEED[0],
            "to_min": _BETWEEN_SEED[1],
            "hours": set(range(24)),
        }

    records = [HourRecord(iv.hour, iv.start_min, iv.end_min, iv.unaccounted) for iv in intervals]
    merged = hours_to_intervals(records)
    hours = {iv.hour for iv in intervals}

    if not merged:
        return {"mode": "custom", "from_min": _BETWEEN_SEED[0], "to_min": _BETWEEN_SEED[1], "hours": hours}
    if len(merged) == 1 and (merged[0].start_min, merged[0].end_min) == _FULL_DAY:
        return {
            "mode": "all",
            "from_min": _BETWEEN_SEED[0],
            "to_min": _BETWEEN_SEED[1],
            "hours": set(range(24)),
        }
    if len(merged) == 1:
        return {
            "mode": "between",
            "from_min": merged[0].start_min,
            "to_min": merged[0].end_min,
            "hours": hours,
        }
    return {"mode": "custom", "from_min": 0, "to_min": 0, "hours": hours}


def _fmt_hm(total_min: int) -> str:
    return f"{total_min // 60:02d}:{total_min % 60:02d}"


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
    user = await _get_user_or_404(session, username)
    policy = await get_or_create_policy(session, user)
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
            status.HTTP_422_UNPROCESSABLE_ENTITY, f"invalid time for {field}: {value!r}"
        ) from exc
    if not (0 <= total <= 24 * 60):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"invalid time for {field}: {value!r}")
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
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"{day_name}: {exc}") from exc
        records = intervals_to_hours([interval])
        return [AllowedHourInterval(hour=r.hour, start_min=r.start_min, end_min=r.end_min) for r in records]

    # mode == "custom"
    checked_hours = sorted(h for h in range(24) if _checkbox(form, f"hh_{day}_{h}"))
    if not checked_hours:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"No allowed hours are set for {day_name}. To block a whole day, uncheck it under "
            "'Days allowed to log in' in Advanced settings instead -- an empty hour list can't be "
            "applied on the device.",
        )
    return [AllowedHourInterval(hour=h, start_min=0, end_min=60, unaccounted=False) for h in checked_hours]


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
            status.HTTP_422_UNPROCESSABLE_ENTITY, "At least one day must be allowed to log in at all."
        )

    lockout_type_raw = form.get("lockout_type", "lock")
    try:
        lockout_type = LockoutType(lockout_type_raw)
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, f"unknown lockout type {lockout_type_raw!r}"
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
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc


@router.post("/users/{username}/policy")
async def update_policy_ui(
    request: Request,
    username: str,
    session: AsyncSession = Depends(get_session),
    parent: Parent = Depends(get_current_parent_ui),
):
    user = await _get_user_or_404(session, username)
    form = await request.form()
    update = await _parse_policy_form(form)

    before_policy = await get_or_create_policy(session, user)
    before = policy_to_payload(before_policy).model_dump(mode="json")

    policy = await update_policy(session, user=user, update=update, created_by="ui")
    await record_audit_event(
        session,
        actor_type="parent",
        actor_id=str(parent.id),
        action="policy.update",
        target_type="user",
        target_id=username,
        before=before,
        after=policy_to_payload(policy).model_dump(mode="json"),
        ip=_client_ip(request),
    )
    await session.commit()
    return RedirectResponse(f"/users/{username}", status_code=status.HTTP_303_SEE_OTHER)


# --------------------------------------------------------------------------
# Per-user settings: the hub-only knobs that never reach `PolicyPayload` or
# a device (which weekdays are chore-gated, the accounting mode). A separate
# page with its own single save button, deliberately not a second card on
# the policy editor -- that page has exactly one Save, and a second one
# would reintroduce the tab-scoped-Apply confusion this project's own
# `timekpra` prior-art review called out (CHECKLIST.md).
# --------------------------------------------------------------------------


@router.get("/users/{username}/settings", response_class=HTMLResponse)
async def user_settings_page(
    request: Request, username: str, session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    user = await _get_user_or_404(session, username)
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
        },
    )


@router.post("/users/{username}/settings")
async def update_user_settings_ui(
    request: Request,
    username: str,
    accounting_mode: str = Form("wallclock"),
    session: AsyncSession = Depends(get_session),
    parent: Parent = Depends(get_current_parent_ui),
):
    user = await _get_user_or_404(session, username)
    form = await request.form()
    gated_weekdays = [d for d in _WEEKDAY_TOKENS if _checkbox(form, f"gated_weekday_{d}")]

    before = {"gated_weekdays": user.gated_weekdays_json, "accounting_mode": user.accounting_mode}
    user.gated_weekdays_json = gated_weekdays
    user.accounting_mode = accounting_mode
    await record_audit_event(
        session,
        actor_type="parent",
        actor_id=str(parent.id),
        action="user_settings.update",
        target_type="user",
        target_id=username,
        before=before,
        after={"gated_weekdays": gated_weekdays, "accounting_mode": accounting_mode},
        ip=_client_ip(request),
    )
    await session.commit()
    return RedirectResponse(f"/users/{username}/settings", status_code=status.HTTP_303_SEE_OTHER)


# --------------------------------------------------------------------------
# Usage statistics
# --------------------------------------------------------------------------


@router.get("/users/{username}/stats", response_class=HTMLResponse)
async def user_stats_page(
    request: Request, username: str, days: int = 30, session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    user = await _get_user_or_404(session, username)
    policy = await get_or_create_policy(session, user)
    await session.commit()
    num_days = days if days in (7, 30, 90) else 30
    history = await compute_usage_history(
        session, user=user, policy=policy, num_days=num_days, tz=settings.tz
    )
    max_s = max([d.limit_s for d in history.days] + [d.spent_s for d in history.days] + [1])
    return templates.TemplateResponse(
        request,
        "user_stats.html",
        {
            "username": username,
            "display_name": user.display_name,
            "days": num_days,
            "history": history,
            "max_s": max_s,
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
    request: Request,
    device_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    parent: Parent = Depends(get_current_parent_ui),
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
    request: Request,
    device_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    parent: Parent = Depends(get_current_parent_ui),
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
