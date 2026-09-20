"""Per-user settings (hub-only knobs that never reach `PolicyPayload` or a
device: which weekdays are chore-gated, the accounting mode), rename,
delete, and usage statistics -- everything about one user that isn't the
policy editor (policy.py) or the dashboard card (dashboard.py).

Settings is a separate page with its own single save button, deliberately
not a second card on the policy editor -- that page has exactly one Save,
and a second one would reintroduce the tab-scoped-Apply confusion that
makes `timekpra`'s own settings window easy to get wrong.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from timekpr_hub_core.models import WEEKDAY_TOKENS

from timekpr_hub.api.admin_auth import get_current_admin_ui
from timekpr_hub.api.ui.policy import _checkbox, _hours_display
from timekpr_hub.api.util import client_ip, get_user_or_404, templates
from timekpr_hub.db.models import Admin
from timekpr_hub.db.session import get_session
from timekpr_hub.services.audit import record_audit_event
from timekpr_hub.services.policy import get_or_create_policy
from timekpr_hub.services.summaries import compute_usage_history
from timekpr_hub.settings import settings

router = APIRouter()

_WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


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
            "weekday_tokens": WEEKDAY_TOKENS,
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
    gated_weekdays = [d for d in WEEKDAY_TOKENS if _checkbox(form, f"gated_weekday_{d}")]

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
