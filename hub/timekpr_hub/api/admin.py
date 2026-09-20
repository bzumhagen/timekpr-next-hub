"""Admin-facing API: the JSON endpoints behind the hub UI.

Every route here is gated on a logged-in admin session -- see
`api/admin_auth.py`, which wires the dependency in `app.py`.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from timekpr_hub_core.allowed_hours import (
    IntervalConflictError,
    TimeInterval,
    intervals_to_hours,
    unrestricted,
)
from timekpr_hub_core.allowed_hours import validate_intervals as _validate_intervals
from timekpr_hub_core.calendar import canonical_stamp
from timekpr_hub_core.models import (
    AllowedHourInterval,
    DayHourOverrideCreate,
    DayOverrideCreate,
    GateReleaseCreate,
    GrantCreate,
    PolicyPayload,
    PolicyUpdate,
    UserSettingsUpdate,
    UserSummary,
)

from timekpr_hub.api.admin_auth import get_current_admin_api
from timekpr_hub.db.models import Admin, Device, EnrollmentCode, User
from timekpr_hub.db.session import get_session
from timekpr_hub.services.admin_auth import (
    change_password,
    count_admins,
    create_invite,
    delete_other_sessions,
    verify_password,
)
from timekpr_hub.services.audit import list_audit_events, record_audit_event
from timekpr_hub.services.day_hours import clear_day_hour_override, set_day_hour_override
from timekpr_hub.services.limits import clear_day_override, release_gate, set_day_override, unrelease_gate
from timekpr_hub.services.policy import get_current_policy, policy_to_payload, update_policy
from timekpr_hub.services.summaries import compute_user_summaries
from timekpr_hub.settings import settings


def _wire_intervals(records) -> list[AllowedHourInterval]:
    return [
        AllowedHourInterval(hour=r.hour, start_min=r.start_min, end_min=r.end_min, unaccounted=r.unaccounted)
        for r in records
    ]


def _day_hour_override_intervals(body: DayHourOverrideCreate) -> list[AllowedHourInterval]:
    """`DayHourOverrideCreate`'s two modes -> the wire `AllowedHourInterval`
    list `set_day_hour_override` stores. "unrestricted" always writes the
    explicit all-24-hours form (never `[]` -- see
    `timekpr_hub_core.allowed_hours.unrestricted`'s docstring); "window"
    goes through the same validate/expand chain the policy editor's
    "between" mode uses (`api/ui.py::_parse_day_hours`), so a caller gets
    the identical one-clock-hour-per-interval error message either way."""
    if body.mode == "unrestricted":
        return _wire_intervals(intervals_to_hours(unrestricted()))
    interval = TimeInterval(body.from_min, body.to_min, unaccounted=body.unaccounted)
    try:
        _validate_intervals([interval])
    except IntervalConflictError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    return _wire_intervals(intervals_to_hours([interval]))


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


router = APIRouter()


@router.get("/users", response_model=list[UserSummary])
async def list_users(session: AsyncSession = Depends(get_session)) -> list[UserSummary]:
    rows = await compute_user_summaries(session)
    return [row.to_user_summary() for row in rows]


@router.post("/users/{username}/grants", status_code=status.HTTP_201_CREATED)
async def create_grant(
    username: str,
    body: GrantCreate,
    request: Request,
    session: AsyncSession = Depends(get_session),
    admin: Admin = Depends(get_current_admin_api),
) -> dict:
    from timekpr_hub.db.models import Grant

    result = await session.execute(select(User).where(User.canonical_username == username))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown user")

    # `body.day` lets a grant target a future date ("you lose 30 minutes
    # tomorrow") without touching the standing policy -- None (the default,
    # and every caller before dated grants existed) means today in the
    # hub's own timezone, exactly the prior behavior.
    grant_day = (
        date.fromisoformat(body.day) if body.day else canonical_stamp(datetime.now(UTC), settings.tz).day
    )
    grant_day_str = grant_day.isoformat()

    grant = Grant(
        id=uuid.uuid4(),
        user_id=user.id,
        day=grant_day,
        seconds=body.seconds,
        reason=body.reason,
        source="admin",
        granted_by="admin-api",
    )
    session.add(grant)
    await record_audit_event(
        session,
        actor_type="admin",
        actor_id=str(admin.id),
        action="grant.create",
        target_type="user",
        target_id=username,
        after={"seconds": grant.seconds, "day": grant_day_str, "reason": grant.reason},
        ip=_client_ip(request),
    )
    await session.commit()
    return {"id": str(grant.id), "seconds": grant.seconds, "day": grant_day_str}


@router.put("/users/{username}/policy", response_model=PolicyPayload)
async def update_user_policy(
    username: str,
    body: PolicyUpdate,
    request: Request,
    session: AsyncSession = Depends(get_session),
    admin: Admin = Depends(get_current_admin_api),
) -> PolicyPayload:
    """The only way to change a user's *limit* (as opposed to grant
    additive bonus time) through the hub -- see services/policy.py::
    update_policy. The agent already applies whatever this returns on its
    next tick (sync.py pushes the payload whenever policy_version_applied
    disagrees, and the agent already calls setTimeLimitForDays/Week/Month +
    setAllowedDays)."""
    result = await session.execute(select(User).where(User.canonical_username == username))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown user")

    before_policy = await get_current_policy(session, user)
    before = policy_to_payload(before_policy).model_dump() if before_policy else None

    policy = await update_policy(session, user=user, update=body, created_by="admin-api")
    await record_audit_event(
        session,
        actor_type="admin",
        actor_id=str(admin.id),
        action="policy.update",
        target_type="user",
        target_id=username,
        before=before,
        after=policy_to_payload(policy).model_dump(),
        ip=_client_ip(request),
    )
    await session.commit()
    return policy_to_payload(policy)


@router.put("/users/{username}/day-override")
async def set_user_day_override(
    username: str,
    body: DayOverrideCreate,
    request: Request,
    session: AsyncSession = Depends(get_session),
    admin: Admin = Depends(get_current_admin_api),
) -> dict:
    """Sets (or replaces) an absolute per-date limit -- "instead of" the
    standing policy, not "in addition to" like a grant. See
    services/limits.py::set_day_override for why this exists as its own
    concept rather than a large negative grant."""
    result = await session.execute(select(User).where(User.canonical_username == username))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown user")

    day = date.fromisoformat(body.day)
    override = await set_day_override(
        session,
        user_id=user.id,
        day=day,
        limit_seconds=body.limit_seconds,
        reason=body.reason,
        created_by="admin-api",
    )
    await record_audit_event(
        session,
        actor_type="admin",
        actor_id=str(admin.id),
        action="override.set",
        target_type="user",
        target_id=username,
        after={"day": body.day, "limit_seconds": body.limit_seconds, "reason": body.reason},
        ip=_client_ip(request),
    )
    await session.commit()
    return {"id": str(override.id), "day": body.day, "limit_seconds": override.limit_seconds}


@router.delete("/users/{username}/day-override/{day}")
async def clear_user_day_override(
    username: str,
    day: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
    admin: Admin = Depends(get_current_admin_api),
) -> dict:
    result = await session.execute(select(User).where(User.canonical_username == username))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown user")

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
            ip=_client_ip(request),
        )
    await session.commit()
    return {"day": day, "cleared": cleared}


@router.put("/users/{username}/day-hours")
async def set_user_day_hour_override(
    username: str,
    body: DayHourOverrideCreate,
    request: Request,
    session: AsyncSession = Depends(get_session),
    admin: Admin = Depends(get_current_admin_api),
) -> dict:
    """Sets (or replaces) a one-day replacement for the policy's standing
    allowed time-of-day window. Kept separate from `day-override` above,
    which replaces the day's *limit* -- this replaces *when* the limit may
    be used. Unlike every other per-date exception, this one does reach the
    device (see `timekpr_hub_core.effective_policy`'s module docstring), on
    the device's next `/sync`.

    422s if `day`'s weekday isn't one of the policy's `allowed_weekdays`:
    an hours window on a day the user can't log in at all would silently
    have no effect (see `effective_policy_payload`'s read-path skip for the
    same case, which covers a policy edited *after* this override is set)."""
    result = await session.execute(select(User).where(User.canonical_username == username))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown user")

    day = date.fromisoformat(body.day)
    policy = await get_current_policy(session, user)
    allowed_weekdays = (policy.allowed_weekdays_json if policy else None) or [
        "1",
        "2",
        "3",
        "4",
        "5",
        "6",
        "7",
    ]
    if str(day.isoweekday()) not in allowed_weekdays:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"{body.day} isn't one of this user's allowed login days, so an hours window "
            "would have no effect -- change 'Days allowed to log in' in the policy first",
        )

    intervals = _day_hour_override_intervals(body)
    override = await set_day_hour_override(
        session,
        user_id=user.id,
        day=day,
        intervals=intervals,
        reason=body.reason,
        created_by="admin-api",
    )
    await record_audit_event(
        session,
        actor_type="admin",
        actor_id=str(admin.id),
        action="day_hours.set",
        target_type="user",
        target_id=username,
        after={"day": body.day, "mode": body.mode, "reason": body.reason},
        ip=_client_ip(request),
    )
    await session.commit()
    return {"id": str(override.id), "day": body.day, "mode": body.mode}


@router.delete("/users/{username}/day-hours/{day}")
async def clear_user_day_hour_override(
    username: str,
    day: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
    admin: Admin = Depends(get_current_admin_api),
) -> dict:
    result = await session.execute(select(User).where(User.canonical_username == username))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown user")

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
            ip=_client_ip(request),
        )
    await session.commit()
    return {"day": day, "cleared": cleared}


@router.post("/users/{username}/gate-release", status_code=status.HTTP_201_CREATED)
async def release_user_gate(
    username: str,
    body: GateReleaseCreate,
    request: Request,
    session: AsyncSession = Depends(get_session),
    admin: Admin = Depends(get_current_admin_api),
) -> dict:
    """Records that a gated day's precondition was met for one date --
    the exception to `users.gated_weekdays_json`'s recurring rule. Releasing
    an already-released day just refreshes who/why (see
    services/limits.py::release_gate)."""
    result = await session.execute(select(User).where(User.canonical_username == username))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown user")

    release = await release_gate(
        session, user_id=user.id, day=date.fromisoformat(body.day), released_by="admin-api", note=body.note
    )
    await record_audit_event(
        session,
        actor_type="admin",
        actor_id=str(admin.id),
        action="gate.release",
        target_type="user",
        target_id=username,
        after={"day": body.day, "note": body.note},
        ip=_client_ip(request),
    )
    await session.commit()
    return {"id": str(release.id), "day": body.day}


@router.delete("/users/{username}/gate-release/{day}")
async def unrelease_user_gate(
    username: str,
    day: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
    admin: Admin = Depends(get_current_admin_api),
) -> dict:
    """Reverses `release_user_gate` -- deletes the release row, re-gating
    that date (absence of a row IS the gate)."""
    result = await session.execute(select(User).where(User.canonical_username == username))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown user")

    unreleased = await unrelease_gate(session, user_id=user.id, day=date.fromisoformat(day))
    if unreleased:
        await record_audit_event(
            session,
            actor_type="admin",
            actor_id=str(admin.id),
            action="gate.unrelease",
            target_type="user",
            target_id=username,
            before={"day": day},
            ip=_client_ip(request),
        )
    await session.commit()
    return {"day": day, "unreleased": unreleased}


@router.put("/users/{username}/settings")
async def update_user_settings(
    username: str,
    body: UserSettingsUpdate,
    request: Request,
    session: AsyncSession = Depends(get_session),
    admin: Admin = Depends(get_current_admin_api),
) -> dict:
    """The hub-only per-user knobs (which weekdays are approval-gated, the
    accounting mode, the offline-grace policy) -- deliberately NOT part of
    PolicyUpdate/update_policy: no policy version bump, no device push, its
    own save action. See core/timekpr_hub_core/models.py::UserSettingsUpdate."""
    result = await session.execute(select(User).where(User.canonical_username == username))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown user")

    before = {
        "gated_weekdays": user.gated_weekdays_json,
        "accounting_mode": user.accounting_mode,
        "offline_policy": user.offline_policy,
        "offline_grace_s": user.offline_grace_s,
        "offline_cap_s": user.offline_cap_s,
    }
    user.gated_weekdays_json = body.gated_weekdays
    user.accounting_mode = body.accounting_mode.value
    user.offline_policy = body.offline_policy.value
    user.offline_grace_s = body.offline_grace_s
    user.offline_cap_s = body.offline_cap_s
    after = {
        "gated_weekdays": body.gated_weekdays,
        "accounting_mode": body.accounting_mode.value,
        "offline_policy": body.offline_policy.value,
        "offline_grace_s": body.offline_grace_s,
        "offline_cap_s": body.offline_cap_s,
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
        ip=_client_ip(request),
    )
    await session.commit()
    return after


@router.post("/enrollment-codes", status_code=status.HTTP_201_CREATED)
async def create_enrollment_code(session: AsyncSession = Depends(get_session)) -> dict:
    code = secrets.token_urlsafe(6).upper().replace("_", "A").replace("-", "B")[:8]
    now = datetime.now(UTC)
    row = EnrollmentCode(code=code, expires_at=now + timedelta(minutes=15))
    session.add(row)
    await session.commit()
    return {"code": code, "expires_at": row.expires_at.isoformat()}


@router.get("/audit")
async def list_audit(
    limit: int = 50,
    offset: int = 0,
    actor_id: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    """Every admin action, newest first -- see services/audit.py's
    `record_audit_event`, which every mutating endpoint in this file and
    api/ui.py already calls. `before`/`after` are the full JSON diffs those
    call sites recorded (e.g. a policy edit's whole payload before and
    after), not a summary."""
    limit = min(max(limit, 1), 200)
    events = await list_audit_events(
        session,
        limit=limit,
        offset=max(offset, 0),
        actor_id=actor_id,
        target_type=target_type,
        target_id=target_id,
    )
    return [
        {
            "id": str(e.id),
            "ts": e.ts.isoformat(),
            "actor_type": e.actor_type,
            "actor_id": e.actor_id,
            "action": e.action,
            "target_type": e.target_type,
            "target_id": e.target_id,
            "before": e.before_json,
            "after": e.after_json,
            "ip": e.ip,
        }
        for e in events
    ]


@router.get("/devices")
async def list_devices(session: AsyncSession = Depends(get_session)) -> list[dict]:
    result = await session.execute(select(Device))
    return [
        {
            "id": str(d.id),
            "name": d.name,
            "status": d.status,
            "enforcement": d.enforcement,
            "last_sync_at": d.last_sync_at.isoformat() if d.last_sync_at else None,
        }
        for d in result.scalars().all()
    ]


@router.post("/devices/{device_id}/revoke")
async def revoke_device(
    device_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    admin: Admin = Depends(get_current_admin_api),
) -> dict:
    """Kills the device's token immediately -- get_current_device 403s a
    revoked device on its very next sync (auth.py's "never fail open").
    History (usage_counters/activity_intervals/user_aliases) is untouched,
    and the device's machine_id is freed for a later re-enroll to bind a
    *new* row to (the partial unique index on devices.machine_id only
    applies to non-revoked rows) -- the reversible, non-destructive action;
    see delete_device for the alternative that also erases history."""
    result = await session.execute(select(Device).where(Device.id == device_id))
    device = result.scalar_one_or_none()
    if device is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown device")
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
        ip=_client_ip(request),
    )
    await session.commit()
    return {"id": str(device.id), "status": device.status}


@router.post("/devices/{device_id}/observe")
async def set_device_observe_mode(device_id: uuid.UUID, session: AsyncSession = Depends(get_session)) -> dict:
    """Dry-run mode: the agent keeps syncing and computing what it *would*
    write, logging it, but
    never actually calls into DBUS -- see `main.py`'s
    `resp_user.get("enforcement") == "observe"` branch, which already
    existed for the unmapped-user case and now also serves this per-device
    toggle. `/sync` (api/sync.py) reads this column and reports
    `EnforcementMode.OBSERVE` for every user on this device until
    `set_device_enforce_mode` flips it back."""
    result = await session.execute(select(Device).where(Device.id == device_id))
    device = result.scalar_one_or_none()
    if device is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown device")
    device.enforcement = "observe"
    await session.commit()
    return {"id": str(device.id), "enforcement": device.enforcement}


@router.post("/devices/{device_id}/enforce")
async def set_device_enforce_mode(device_id: uuid.UUID, session: AsyncSession = Depends(get_session)) -> dict:
    """Reverses `set_device_observe_mode` -- back to normal enforcement."""
    result = await session.execute(select(Device).where(Device.id == device_id))
    device = result.scalar_one_or_none()
    if device is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown device")
    device.enforcement = "enforce"
    await session.commit()
    return {"id": str(device.id), "enforcement": device.enforcement}


@router.delete("/devices/{device_id}")
async def delete_device(
    device_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    admin: Admin = Depends(get_current_admin_api),
) -> dict:
    """Hard delete. FK cascades (ondelete='CASCADE' on user_aliases,
    usage_counters, activity_intervals) drop this device's contribution
    entirely, which *rewrites* any day it reported usage for -- unlike
    revoke, this is not reversible and changes past totals. Offered for
    "enrolled the wrong thing" cleanup; revoke is the routine action."""
    result = await session.execute(select(Device).where(Device.id == device_id))
    device = result.scalar_one_or_none()
    if device is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown device")
    before = {"name": device.name, "status": device.status}
    await record_audit_event(
        session,
        actor_type="admin",
        actor_id=str(admin.id),
        action="device.delete",
        target_type="device",
        target_id=str(device_id),
        before=before,
        ip=_client_ip(request),
    )
    await session.delete(device)
    await session.commit()
    return {"id": str(device_id), "status": "deleted"}


# --------------------------------------------------------------------------
# Admin accounts: invites, deletion, password change. See services/
# admin_auth.py for the underlying invite/session logic and
# api/admin_auth.py's GET/POST /invite/{token} for the redemption page.
# --------------------------------------------------------------------------


@router.post("/admin-invites", status_code=status.HTTP_201_CREATED)
async def create_admin_invite(
    request: Request,
    session: AsyncSession = Depends(get_session),
    admin: Admin = Depends(get_current_admin_api),
) -> dict:
    token = await create_invite(session, created_by_admin_id=admin.id)
    await record_audit_event(
        session,
        actor_type="admin",
        actor_id=str(admin.id),
        action="admin.invite_created",
        ip=_client_ip(request),
    )
    await session.commit()
    invite_url = f"{str(request.base_url).rstrip('/')}/invite/{token}"
    return {"token": token, "url": invite_url}


@router.get("/admins")
async def list_admins(
    session: AsyncSession = Depends(get_session), admin: Admin = Depends(get_current_admin_api)
) -> list[dict]:
    result = await session.execute(select(Admin))
    return [
        {"id": str(p.id), "email": p.email, "created_at": p.created_at.isoformat(), "you": p.id == admin.id}
        for p in result.scalars().all()
    ]


@router.delete("/admins/{admin_id}")
async def delete_admin(
    admin_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    admin: Admin = Depends(get_current_admin_api),
) -> dict:
    """The last remaining admin can never be deleted -- including
    themselves -- since that would permanently lock the hub's own admin UI
    (there is no other way back in; /setup only ever fires once)."""
    if await count_admins(session) <= 1:
        raise HTTPException(status.HTTP_409_CONFLICT, "cannot delete the last remaining admin account")
    result = await session.execute(select(Admin).where(Admin.id == admin_id))
    target = result.scalar_one_or_none()
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown admin")
    await record_audit_event(
        session,
        actor_type="admin",
        actor_id=str(admin.id),
        action="admin.deleted",
        target_type="admin",
        target_id=str(target.id),
        before={"email": target.email},
        ip=_client_ip(request),
    )
    await session.delete(target)
    await session.commit()
    return {"id": str(admin_id), "status": "deleted"}


class PasswordChange(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8)


@router.post("/admin/password")
async def change_own_password(
    request: Request,
    body: PasswordChange,
    session: AsyncSession = Depends(get_session),
    admin: Admin = Depends(get_current_admin_api),
) -> dict:
    if not verify_password(body.current_password, admin.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "current password is incorrect")
    change_password(admin=admin, new_password=body.new_password)
    token = request.cookies.get("tkh_session", "")
    await delete_other_sessions(session, admin_id=admin.id, keep_token=token)
    await record_audit_event(
        session,
        actor_type="admin",
        actor_id=str(admin.id),
        action="admin.password_changed",
        target_type="admin",
        target_id=str(admin.id),
        ip=_client_ip(request),
    )
    await session.commit()
    return {"status": "ok"}
