"""Per-user usage summary computation, shared by the parent JSON API
(`api/parent.py`'s `GET /users`) and the HTML UI (`api/ui.py`'s users
fragment) -- previously two near-identical copies of the same
per-user-in-a-loop logic (docs/best-practices-review.md), which also ran
~3 queries per user shown. Batched here into a handful of queries total
regardless of how many users are shown, via the `*_batch` aggregate/limits
helpers.

Both `compute_user_summaries` and `compute_usage_history` route their limit
math through `services.limits.combine_limit` -- the one place overrides and
the chore gate are applied -- rather than each recomputing
`base + grants` on its own, which is exactly the drift that made a gated or
overridden day disagree between the dashboard, the stats page, and `/sync`'s
actual enforcement before this collapse.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from timekpr_hub_core.calendar import canonical_stamp
from timekpr_hub_core.models import AllowedHourInterval, UserSummary

from timekpr_hub.db.models import Device, Policy, User
from timekpr_hub.services.aggregate import (
    device_spent_for_day_by_device,
    global_spent_parallel_batch,
    global_spent_wallclock_batch,
    global_spent_wallclock_history,
    latest_activity_states_batch,
)
from timekpr_hub.services.day_hours import day_hour_overrides_history
from timekpr_hub.services.limits import (
    base_daily_limit,
    combine_limit,
    day_overrides_batch,
    day_overrides_history,
    gate_releases_batch,
    gate_releases_history,
    grants_totals_batch,
    grants_totals_history,
    is_gated_weekday,
)
from timekpr_hub.settings import settings


@dataclass
class UserSummaryRow:
    """Everything computed per user -- `user`/`policy` are kept alongside
    the wire-shaped fields so a caller needing something `UserSummary`
    doesn't carry (e.g. the UI's `daily_limit_minutes`) doesn't have to
    re-fetch the policy itself."""

    user: User
    policy: Policy | None
    today_global_spent_s: int
    today_effective_limit_s: int
    activity_state: str
    as_of: str | None
    gated_today: bool = False
    gate_released_today: bool = False

    def to_user_summary(self) -> UserSummary:
        return UserSummary(
            username=self.user.canonical_username,
            display_name=self.user.display_name,
            accounting_mode=self.user.accounting_mode,
            today_global_spent_s=self.today_global_spent_s,
            today_effective_limit_s=self.today_effective_limit_s,
            activity_state=self.activity_state,
            as_of=self.as_of,
            gated_today=self.gated_today,
            gate_released_today=self.gate_released_today,
        )


async def compute_user_summaries(
    session: AsyncSession, *, usernames: list[str] | None = None
) -> list[UserSummaryRow]:
    """One row per user (optionally filtered to `usernames`): today's global
    spend, effective limit, and activity state -- degraded to "logged_out"
    once no device has reported in for 3 poll intervals, the same rule used
    everywhere else a device's last-seen state is shown.

    A handful of batched queries total, not one (or several) per user: one
    for the users themselves, one for their current policies, one each for
    the wallclock/parallel accounting-mode groups' global spend, one for
    activity state, one for today's grants, one for today's day overrides,
    and one for today's gate releases -- versus the ~3-4 queries *per user*
    this used to run before both `api/parent.py::list_users` and
    `api/ui.py::_user_summaries` were collapsed into this one function
    (docs/best-practices-review.md)."""
    now = datetime.now(UTC)
    stamp = canonical_stamp(now, settings.tz)
    staleness_s = 3 * (settings.default_next_poll_ms / 1000)

    query = select(User)
    if usernames is not None:
        query = query.where(User.canonical_username.in_(usernames))
    users = list((await session.execute(query)).scalars().all())
    if not users:
        return []

    policy_ids = [u.current_policy_id for u in users if u.current_policy_id is not None]
    policies_by_id: dict = {}
    if policy_ids:
        policy_rows = await session.execute(select(Policy).where(Policy.id.in_(policy_ids)))
        policies_by_id = {p.id: p for p in policy_rows.scalars().all()}

    user_ids = [u.id for u in users]
    wallclock_ids = [u.id for u in users if u.accounting_mode == "wallclock"]
    parallel_ids = [u.id for u in users if u.accounting_mode != "wallclock"]

    spent_by_user: dict = {}
    if wallclock_ids:
        spent_by_user.update(
            await global_spent_wallclock_batch(session, user_ids=wallclock_ids, day=stamp.day)
        )
    if parallel_ids:
        spent_by_user.update(await global_spent_parallel_batch(session, user_ids=parallel_ids, day=stamp.day))

    activity_by_user = await latest_activity_states_batch(session, user_ids=user_ids, day=stamp.day)
    grants_by_user = await grants_totals_batch(session, user_ids=user_ids, day=stamp.day)
    overrides_by_user = await day_overrides_batch(session, user_ids=user_ids, day=stamp.day)
    released_users = await gate_releases_batch(session, user_ids=user_ids, day=stamp.day)

    rows = []
    for user in users:
        activity_state, activity_as_of = activity_by_user.get(user.id, ("logged_out", None))
        if activity_as_of is None or (now - activity_as_of).total_seconds() > staleness_s:
            activity_state = "logged_out"
        as_of_str = activity_as_of.isoformat() if activity_as_of else None

        gated = is_gated_weekday(user, stamp.day)
        released = user.id in released_users

        policy = policies_by_id.get(user.current_policy_id) if user.current_policy_id else None
        if policy is None:
            rows.append(
                UserSummaryRow(
                    user=user,
                    policy=None,
                    today_global_spent_s=0,
                    today_effective_limit_s=0,
                    activity_state=activity_state,
                    as_of=as_of_str,
                    gated_today=gated and not released,
                    gate_released_today=released,
                )
            )
            continue

        spent = spent_by_user.get(user.id, 0)
        base_s = overrides_by_user.get(user.id, base_daily_limit(policy, stamp.day))
        limit_today = combine_limit(
            base_s=base_s, grants_s=grants_by_user.get(user.id, 0), gated=gated, released=released
        )

        rows.append(
            UserSummaryRow(
                user=user,
                policy=policy,
                today_global_spent_s=spent,
                today_effective_limit_s=limit_today,
                activity_state=activity_state,
                as_of=as_of_str,
                gated_today=gated and not released,
                gate_released_today=released,
            )
        )
    return rows


@dataclass
class DayUsage:
    day: date
    spent_s: int
    limit_s: int
    grants_s: int
    overridden: bool = False
    gated: bool = False
    gate_released: bool = False
    hours_overridden: bool = False
    hours_override_intervals: list[AllowedHourInterval] | None = None
    """The stored override for this day, if `hours_overridden` -- carried
    as intervals rather than a pre-formatted string so the display wording
    stays in one place (`api/ui.py::_hours_display`, the same function the
    dashboard card and policy editor banner use) instead of being re-derived
    here, which would risk the three disagreeing on phrasing."""


@dataclass
class DeviceUsage:
    device_id: str
    device_name: str
    spent_s: int


@dataclass
class UsageHistory:
    days: list[DayUsage]
    selected_day: date
    selected_day_devices: list[DeviceUsage]


async def compute_usage_history(
    session: AsyncSession, *, user: User, policy: Policy | None, num_days: int, tz: ZoneInfo
) -> UsageHistory:
    """`num_days` of daily spend vs. limit (+ grants applied) ending today,
    plus a per-device split for the most recent day -- one query per
    dimension (spend, grants, overrides, gate releases, device split), not
    one per day, following the same batched-query discipline as
    `compute_user_summaries` (docs/best-practices-review.md's N+1 finding)."""
    now = datetime.now(UTC)
    stamp = canonical_stamp(now, tz)
    end_day = stamp.day
    start_day = end_day - timedelta(days=num_days - 1)

    spent_by_day = await global_spent_wallclock_history(
        session, user_id=user.id, start_day=start_day, end_day=end_day
    )
    grants_by_day = await grants_totals_history(
        session, user_id=user.id, start_day=start_day, end_day=end_day
    )
    overrides_by_day = await day_overrides_history(
        session, user_id=user.id, start_day=start_day, end_day=end_day
    )
    released_days = await gate_releases_history(
        session, user_id=user.id, start_day=start_day, end_day=end_day
    )
    hours_overrides_by_day = await day_hour_overrides_history(
        session, user_id=user.id, start_day=start_day, end_day=end_day
    )

    days: list[DayUsage] = []
    for offset in range(num_days):
        day = start_day + timedelta(days=offset)
        grants_s = grants_by_day.get(day, 0)
        overridden = day in overrides_by_day
        base_s = overrides_by_day.get(day, base_daily_limit(policy, day) if policy else 0)
        gated = is_gated_weekday(user, day)
        released = day in released_days
        limit_s = combine_limit(base_s=base_s, grants_s=grants_s, gated=gated, released=released)
        days.append(
            DayUsage(
                day=day,
                spent_s=spent_by_day.get(day, 0),
                limit_s=limit_s,
                grants_s=grants_s,
                overridden=overridden,
                gated=gated,
                gate_released=released,
                hours_overridden=day in hours_overrides_by_day,
                hours_override_intervals=hours_overrides_by_day.get(day),
            )
        )

    device_spent = await device_spent_for_day_by_device(session, user_id=user.id, day=end_day)
    device_names: dict = {}
    if device_spent:
        result = await session.execute(select(Device).where(Device.id.in_(device_spent.keys())))
        device_names = {d.id: d.name for d in result.scalars().all()}

    selected_day_devices = [
        DeviceUsage(device_id=str(device_id), device_name=device_names.get(device_id, "?"), spent_s=spent)
        for device_id, spent in sorted(device_spent.items(), key=lambda kv: -kv[1])
    ]

    return UsageHistory(days=days, selected_day=end_day, selected_day_devices=selected_day_devices)
