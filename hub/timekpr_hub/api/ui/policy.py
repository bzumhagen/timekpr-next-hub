"""The full policy editor: `GET/POST /users/{username}` and its helpers.

`_hours_display`/`_to_wire_intervals`/`_checkbox`/`_parse_time_str` are also
used by sibling ui/ modules (dashboard's summary cards, overrides' one-day
hours window, users' settings/stats pages) -- imported from here rather
than duplicated.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError
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
    WEEKDAY_TOKENS,
    AllowedHourInterval,
    LockoutType,
    PlayTimeActivity,
    PlayTimePayload,
    PolicyUpdate,
)

from timekpr_hub.api.admin_auth import get_current_admin_ui
from timekpr_hub.api.util import client_ip, get_user_or_404, templates, wire_intervals
from timekpr_hub.db.models import Admin
from timekpr_hub.db.session import get_session
from timekpr_hub.services.audit import record_audit_event
from timekpr_hub.services.day_hours import day_hour_override
from timekpr_hub.services.policy import get_or_create_policy, policy_to_payload, update_policy
from timekpr_hub.settings import settings

router = APIRouter()

_WEEKDAY_TOKENS = WEEKDAY_TOKENS
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
        return wire_intervals(intervals_to_hours(unrestricted()))

    if mode == "between":
        from_min = _parse_time_str(form.get(f"hours_from_{day}", ""), f"{day_name} start time")
        to_min = _parse_time_str(form.get(f"hours_to_{day}", ""), f"{day_name} end time")
        try:
            interval = TimeInterval(from_min, to_min)
            validate_intervals([interval])
        except (ValueError, IntervalConflictError) as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"{day_name}: {exc}") from exc
        return wire_intervals(intervals_to_hours([interval]))

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
