# Live agent dogfooding: findings

Recorded while running the actual `timekpr_hub_agent.main.run_tick` against
this machine's real `timekprd` (via `newgrp timekpr`) and a real hub server
backed by Postgres, for the first time — after all 47 synthetic tests
(property tests, `FakeTimekprDaemon` parity, multi-device simulation,
Postgres-backed hub API tests) were already green. Two real bugs surfaced
here that no synthetic test caught, both in how `Observation.limit_today_s`
was computed/used. This is exactly the value of PLAN "Verification" Layer 7
("household safety net... run `--dry-run` against the real machines") —
recorded here a step early, during initial development rather than a
pre-launch dry run, but the same principle: live dogfooding finds what
synthetic modeling cannot, because the model can only be as correct as its
author's understanding of the real system.

## Bug 1: absolute writes used the hub's target limit instead of the device's own limit

See the fix and full explanation in `convergence.py`'s `Observation.limit_today_s`
docstring and the `needs_absolute`/`seconds` computation in `plan()`. Summary:
`setTimeLeft(user, '=', secs)` computes `BALANCE := DEVICE's own configured
limit - secs`. The original code computed `secs` (and the overspend guard)
against `target.limit_today_s` (the hub's belief), which is only correct
once a policy push has actually landed on the device. Before that (which is
the case for every newly-enrolled device in Phase 1, since policy push is
Phase 2), this silently misdirected every `'='` write.

Caught on the very first live tick: `bzumhagen`'s real, unconfigured account
has a full-day limit (86400s), while the hub's freshly-autocreated default
policy has a 1h limit (3600s). The buggy code produced `BALANCE = 82800`
where it should have produced `BALANCE = 0`.

Regression test: `tests/unit/test_convergence.py::test_absolute_write_uses_device_limit_not_hub_target_limit`.

## Bug 2: `limit_today_s` was derived from the wrong field (more fundamental)

Fixing bug 1 required an actual value for "the device's own configured
limit" in `Observation`. The first attempt derived it as
`TIME_LEFT_DAY + balance` — reasoning that `TIME_LEFT_DAY = limit - balance`
by the same arithmetic verified in Phase 0. That reasoning is wrong:
`TIME_LEFT_DAY` is a **dynamically recomputed runtime value**
(`recalculateTimeLeft()` in
[userdata.py:145](../../timekpr-next/server/user/userdata.py#L145)), which
takes `min(LEFTD, LEFTW, LEFTM)` and *also* folds in `ALLOWED_HOURS`
restrictions. It is not simply "static configured limit minus balance" the
moment any hour-based restriction is configured.

Caught live on the same account, which happens to have `ALLOWED_HOURS`
configured (inherited from earlier testing in this session): raw dump of
`getUserConfigurationAndInformation(user, 'F')` showed

```
LIMITS_PER_WEEKDAYS = [86400, 86400, 86400, 86400, 86400, 86400, 86400]   # the STATIC configured limit
TIME_LEFT_DAY = 6196
TIME_SPENT_BALANCE = 79793
```

`TIME_LEFT_DAY + BALANCE = 85989 ≠ 86400`. The 411-second gap is exactly the
hour-restriction effect baked into `TIME_LEFT_DAY`.

**Fix:** read `LIMITS_PER_WEEKDAYS` directly and index it by
`isoweekday() - 1`, mirroring exactly what
`configprocessor.py:718`'s own `checkAndSetTimeLeft` does internally:

```python
today_idx = datetime.now().isoweekday() - 1
limit_today = [int(x) for x in info["LIMITS_PER_WEEKDAYS"]][today_idx]
```

This is the only value that is guaranteed to agree with what `setTimeLeft`
will actually use, because it's identically the same lookup.

## Verification after both fixes

Ran the corrected `run_tick` against the live daemon with a fresh
enrollment: `BEFORE: balance=393, spent_day=393` (offset 0, natural state)
→ tick → `AFTER: balance=0, spent_day=393` (offset -393). The hub's
`global_spent_s` was 0 (fresh device, nothing reported yet), so the target
offset is `G - s = 0 - 393 = -393` — an exact match. Confirmed the fix is
correct against the real system, not just the synthetic models.

## Takeaway for the rest of the project

Both bugs lived in the same 15 lines of "derive a limit value" code, and
both were invisible to:
- Hypothesis property tests (they never modeled a device/hub limit
  *mismatch*, or `ALLOWED_HOURS`-driven `TIME_LEFT_DAY` dynamics, because
  `FakeTimekprDaemon` doesn't model hour restrictions at all — deliberately
  out of its stated scope, see its module docstring),
- the multi-device simulation (`hub.limit_today_s` and
  `dev.daemon.limit_today_s` were always constructed equal, by harness
  design, so the mismatch this bug depends on could never occur there),
- the Postgres-backed hub API tests (they never touch DBUS or a real
  daemon at all).

None of that is a flaw in those tests — each was doing its job within its
stated scope. It's a reminder that Phase 0's live spike de-risks the
*linchpin invariant*, but does not substitute for periodically running the
real agent against a real, even slightly unusual (hour-restricted) account
before trusting the implementation. `FakeTimekprDaemon`'s docstring should
be read as "verified for the accounting semantics it explicitly models" —
`ALLOWED_HOURS` is out of scope for it and remains a real-daemon-only check
for now; worth a `FakeTimekprDaemon` extension if hour restrictions become
load-bearing for the hub's own policy (they currently are represented in
`PolicyPayload.allowed_hours` but not yet enforced by the agent beyond
passing them through — tracked as Phase 2 policy-push work).

## Open item: does a widened `ALLOWED_HOURS` release a currently locked-out session?

Introduced with the one-day allowed-hours override (CHECKLIST.md's "Phase 2"
entry). The feature lets a parent widen (or narrow) today's allowed window --
e.g. "let them start at noon instead of 15:00" -- and pushes it to the device
via `setAllowedHours` on the device's next `/sync`. What's untested against a
real daemon: if the account is *already logged in and locked out* (timekprd
enforcing a lockout because the current time falls outside the old window)
when the widened window lands, does `timekprd` re-evaluate and release the
session immediately, or does the child have to log out and back in for the
new window to take effect?

`FakeTimekprDaemon` can't answer this -- its own module docstring says hour
restrictions are out of its modeled scope entirely (the same gap Bug 2 above
ran into), so every unit/integration/e2e test for this feature necessarily
asserts only that the *correct DBUS payload* was sent (`tests/unit/
test_apply_policy_push.py`'s revert test, `tests/e2e/test_acceptance.py::
test_day_hour_override_is_pushed_and_reverted`), never what the real daemon
does with it once applied. Worth a live dogfooding pass the same way Bugs 1
and 2 above were found: lock an account out under the standing schedule,
push a widened override, and watch whether the session unlocks without any
action from the child.
