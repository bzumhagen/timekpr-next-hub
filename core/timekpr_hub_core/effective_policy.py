"""Merging a one-day allowed-hours override onto a standing `PolicyPayload`,
plus the revision token that decides whether `/sync` needs to push it.

Every *other* per-date exception (grants, day overrides, the chore gate) never
leaves the hub -- it only changes a number (`effective_limit_today_s`) in the
`/sync` response. `allowed_hours` is different: it is part of `PolicyPayload`
and gets pushed to the device over DBUS (`setAllowedHours`), keyed by ISO
weekday. timekpr has no notion of a date-scoped window, so a one-day override
must be pushed *and explicitly un-pushed* on the following weekday-boundary,
or the device just keeps running it forever.

Two things fall out of that:

1. The payload handed to the agent must carry all seven weekday keys, not
   just the overridden one. `_apply_allowed_hours`
   (`agent/timekpr_hub_agent/main.py`) deliberately leaves an *absent*
   weekday untouched -- so a payload keyed only on the overridden day, with
   the standing policy's `allowed_hours_json` left at `{}` (which is exactly
   what `services/policy.py::create_initial_policy` writes for a brand-new
   user), would never revert: there is no key for that weekday in the
   "back to normal" push for the agent to apply. `materialize_allowed_hours`
   exists to make that revert always representable.

2. The `/sync` push gate can no longer be `PolicyPayload.version` alone,
   because the effective payload now varies by *date* without any policy
   edit at all. `policy_revision` produces an opaque token derived from the
   full effective payload (not from the date), so it changes exactly when
   what we want on the device changes -- a future-dated override changes
   nothing today, and midnight doesn't force a pointless re-push for every
   user who has no override in play.
"""

from __future__ import annotations

import hashlib
import json

from timekpr_hub_core.allowed_hours import intervals_to_hours, unrestricted
from timekpr_hub_core.models import AllowedHourInterval, PolicyPayload

_ALL_WEEKDAYS = ["1", "2", "3", "4", "5", "6", "7"]


def _unrestricted_wire_intervals() -> list[AllowedHourInterval]:
    """The wire-model equivalent of `allowed_hours.unrestricted()`, expanded
    to per-hour records the same way `PolicyPayload.allowed_hours` stores
    them (see that field's own docstring: an absent day means *forbidden*
    to timekpr, never *allowed*, so "no restriction" must always be this
    explicit all-24-hours form)."""
    return [
        AllowedHourInterval(hour=r.hour, start_min=r.start_min, end_min=r.end_min, unaccounted=r.unaccounted)
        for r in intervals_to_hours(unrestricted())
    ]


def materialize_allowed_hours(
    allowed_hours: dict[str, list[AllowedHourInterval]],
) -> dict[str, list[AllowedHourInterval]]:
    """Fill in every one of the 7 weekday keys, defaulting an absent one to
    unrestricted. This is what makes reverting a one-day override possible
    at all: the "back to normal" payload must carry a real value for the
    overridden weekday, not leave it absent (an absent key is never rewritten
    by `_apply_allowed_hours`, so an already-pushed override would stick
    forever on a policy whose `allowed_hours_json` is `{}`, the standard
    default for a policy nobody has edited)."""
    unrestricted_wire = _unrestricted_wire_intervals()
    return {day: list(allowed_hours.get(day) or unrestricted_wire) for day in _ALL_WEEKDAYS}


def with_day_hour_override(
    payload: PolicyPayload, *, weekday: str, intervals: list[AllowedHourInterval]
) -> PolicyPayload:
    """Returns a copy of `payload` with weekday `weekday`'s `allowed_hours`
    entry replaced by `intervals`, after materializing every other day so
    the result is always fully revertible (see module docstring).

    Returns `payload` unchanged (materialized, but with no substitution) if
    `weekday` is not one of `payload.allowed_weekdays` -- an hours override
    for a day the user isn't allowed to log in at all would be meaningless,
    and worse, would misrepresent the standing policy if silently applied
    anyway. Callers that need to *reject* this case at write time should
    check `allowed_weekdays` themselves; this function only guarantees it
    never takes effect."""
    materialized = materialize_allowed_hours(payload.allowed_hours)
    if weekday in (payload.allowed_weekdays or _ALL_WEEKDAYS):
        materialized = {**materialized, weekday: list(intervals)}
    return payload.model_copy(update={"allowed_hours": materialized})


def policy_revision(payload: PolicyPayload) -> str:
    """An opaque token that changes exactly when the payload the agent
    should apply changes -- used as the `/sync` push gate instead of (in
    addition to) `PolicyPayload.version`, because a per-date hours override
    changes what should be on the device without bumping the policy version.

    Hashes the payload itself, not the date: a future-dated override (which
    does not affect today's effective payload) yields the same revision as
    no override at all, so it causes no push, and an override that has
    already been applied and is still in effect doesn't get needlessly
    re-pushed at midnight.

    `sort_keys=True` is load-bearing -- `allowed_hours` is a dict rebuilt
    from JSONB on every load, so its key order is insertion-dependent and
    must not leak into the hash."""
    canonical = json.dumps(payload.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    digest = hashlib.blake2s(canonical.encode("utf-8"), digest_size=8).hexdigest()
    return f"{payload.version}-{digest}"


__all__ = [
    "materialize_allowed_hours",
    "policy_revision",
    "with_day_hour_override",
]
