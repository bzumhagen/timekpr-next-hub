"""Merging a one-day allowed-hours override onto a standing `PolicyPayload`,
plus the revision token that decides whether `/sync` needs to push it.

Unlike every other per-date exception (grants, day overrides, the chore
gate), `allowed_hours` is part of `PolicyPayload` and reaches the device
over DBUS (`setAllowedHours`), keyed by ISO weekday. timekpr has no
date-scoped window, so a one-day override must be explicitly un-pushed on
the following weekday boundary, or it runs forever. Two things follow:

1. The payload must carry all seven weekday keys, not just the overridden
   one -- `_apply_allowed_hours` (agent/timekpr_hub_agent/policy_push.py)
   leaves an absent weekday untouched, so a policy whose
   `allowed_hours_json` is `{}` (a brand-new user's default) would have no
   key to revert the overridden day to. `materialize_allowed_hours` fills
   in the rest as unrestricted so a revert is always representable.
2. The `/sync` push gate can't be `PolicyPayload.version` alone, since the
   effective payload now varies by date with no policy edit. `policy_revision`
   hashes the full effective payload instead, so it changes exactly when
   what belongs on the device changes.
"""

from __future__ import annotations

import hashlib
import json

from timekpr_hub_core.allowed_hours import intervals_to_hours, unrestricted
from timekpr_hub_core.models import WEEKDAY_TOKENS, AllowedHourInterval, PolicyPayload


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
    unrestricted -- see module docstring for why an absent key can never be
    reverted by `_apply_allowed_hours`."""
    unrestricted_wire = _unrestricted_wire_intervals()
    return {day: list(allowed_hours.get(day) or unrestricted_wire) for day in WEEKDAY_TOKENS}


def with_day_hour_override(
    payload: PolicyPayload, *, weekday: str, intervals: list[AllowedHourInterval]
) -> PolicyPayload:
    """Returns `payload` with weekday `weekday`'s `allowed_hours` entry
    replaced by `intervals`, after materializing every other day so the
    result is always fully revertible.

    Silently skips the substitution (materializes, but leaves `weekday`
    alone) if it's not one of `payload.allowed_weekdays` -- an hours
    override for a day the user can't log in at all would misrepresent the
    policy if applied. Callers that need to *reject* this case should check
    `allowed_weekdays` themselves; this only guarantees it never takes
    effect."""
    materialized = materialize_allowed_hours(payload.allowed_hours)
    if weekday in (payload.allowed_weekdays or WEEKDAY_TOKENS):
        materialized = {**materialized, weekday: list(intervals)}
    return payload.model_copy(update={"allowed_hours": materialized})


def policy_revision(payload: PolicyPayload) -> str:
    """An opaque token that changes exactly when the payload the agent
    should apply changes -- the `/sync` push gate alongside
    `PolicyPayload.version` (see module docstring).

    Hashes the payload itself, not the date: a future-dated override yields
    the same revision as no override (no push today), and an
    already-applied override doesn't get needlessly re-pushed at midnight.

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
