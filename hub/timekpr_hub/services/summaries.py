"""Per-user usage summary computation, shared by the parent JSON API
(`api/parent.py`'s `GET /users`) and the HTML UI (`api/ui.py`'s users
fragment) -- previously two near-identical copies of the same
per-user-in-a-loop logic (docs/best-practices-review.md), which also ran
~3 queries per user shown. Batched here into a handful of queries total
regardless of how many users are shown, via the `*_batch` aggregate/limits
helpers.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from timekpr_hub_core.calendar import canonical_stamp
from timekpr_hub_core.models import UserSummary

from timekpr_hub.db.models import Policy, User
from timekpr_hub.services.aggregate import (
    global_spent_parallel_batch,
    global_spent_wallclock_batch,
    latest_activity_states_batch,
)
from timekpr_hub.services.limits import base_daily_limit, grants_totals_batch
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

    def to_user_summary(self) -> UserSummary:
        return UserSummary(
            username=self.user.canonical_username,
            display_name=self.user.display_name,
            accounting_mode=self.user.accounting_mode,
            today_global_spent_s=self.today_global_spent_s,
            today_effective_limit_s=self.today_effective_limit_s,
            activity_state=self.activity_state,
            as_of=self.as_of,
        )


async def compute_user_summaries(
    session: AsyncSession, *, usernames: list[str] | None = None
) -> list[UserSummaryRow]:
    """One row per user (optionally filtered to `usernames`): today's global
    spend, effective limit, and activity state -- degraded to "logged_out"
    once no device has reported in for 3 poll intervals, the same rule used
    everywhere else a device's last-seen state is shown.

    A handful of batched queries total, not one (or four) per user: one for
    the users themselves, one for their current policies, one each for the
    wallclock/parallel accounting-mode groups' global spend, one for
    activity state, and one for today's grants -- versus the ~3-4 queries
    *per user* this used to run before both `api/parent.py::list_users` and
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

    rows = []
    for user in users:
        activity_state, activity_as_of = activity_by_user.get(user.id, ("logged_out", None))
        if activity_as_of is None or (now - activity_as_of).total_seconds() > staleness_s:
            activity_state = "logged_out"
        as_of_str = activity_as_of.isoformat() if activity_as_of else None

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
                )
            )
            continue

        spent = spent_by_user.get(user.id, 0)
        limit_today = base_daily_limit(policy, stamp.day) + grants_by_user.get(user.id, 0)

        rows.append(
            UserSummaryRow(
                user=user,
                policy=policy,
                today_global_spent_s=spent,
                today_effective_limit_s=limit_today,
                activity_state=activity_state,
                as_of=as_of_str,
            )
        )
    return rows
