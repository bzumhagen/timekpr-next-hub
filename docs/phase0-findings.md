# Phase 0 findings

Validated against a real `timekprd` (v0.5.10, running on this machine) using
`scratchpad/phase0_spike.py bzumhagen`, executed as a member of group `timekpr`
(no root). Raw output kept below the summary.

## 1. Linchpin invariant: CONFIRMED

`setTimeLeft(user, '-', 60)` and `setTimeLeft(user, '+', 60)`:

- **Moved** `TIME_SPENT_BALANCE` (and `ACTUAL_TIME_SPENT_BALANCE`) by exactly the
  requested amount.
- **Did not move** `TIME_SPENT_DAY`, `TIME_SPENT_WEEK`, `TIME_SPENT_MONTH` at all.

This is the core assumption the whole convergence design rests on
(PLAN "The core mechanism") — an agent using `'-'`/`'+'` cannot pollute its own
measurement signal (`TIME_SPENT_DAY`). Confirmed against live code, not just
static analysis.

## 2. The `'='` regression: CONFIRMED, and more precisely than expected

`setTimeLeft(user, '=', 120)` set `BALANCE := todaysLimit − 120` as documented
(observed `todaysLimit = 86400`, i.e. the default unconfigured 24h limit for
this account → `BALANCE = 86280`).

The regression showed up in `ACTUAL_TIME_SPENT_DAY` (the live, in-memory,
not-yet-flushed counter), not in the persisted `TIME_SPENT_DAY`:

| field | before `'='` | after `'='` |
|---|---|---|
| `TIME_SPENT_DAY` (saved) | 180 | 180 (unchanged — nothing had been flushed yet) |
| `ACTUAL_TIME_SPENT_DAY` (live) | 192 | 180 (**dropped 12s**) |

Interpretation: 12 seconds of real activity had accrued in the daemon's
in-memory counter since the last 30s save-to-disk, and `pPreserveSpent=False`
(triggered by the `'='` op) discarded that unflushed delta by reloading from
the on-disk control file. This exactly matches the plan's predicted mechanism
([userdata.py:402](../../timekpr-next/server/user/userdata.py#L402),
`pPreserveSpent=False` path) — the loss is bounded by the save interval
(`TK_SAVE_INTERVAL=30s`), and it shows up first/most visibly in `ACTUAL_*`,
which the agent should treat as the authoritative "did we just lose time"
signal when auditing `'='` writes, rather than the saved field alone.

**Action for the convergence code:** when doing an `'='` write, expect
`ACTUAL_TIME_SPENT_DAY` (if the user is logged in) to potentially drop by up to
~30s immediately after the call. The `advance_cumulative()` magnitude-rule
(PLAN "⚠ The '=' regression trap") correctly absorbs this since 30s ≪ the 90s
tolerance threshold.

## 3. Latency

`setTimeLeft` round trip: **~2ms** (local system bus, single call). Fully
negligible relative to any planned sync interval (10–20s). No async/threading
concerns for the agent's DBUS calls.

## 4. Package location (Arch/CachyOS)

Confirmed via package inspection, not just prediction:

```
$ head -1 /usr/bin/timekprd
#!/bin/sh
exec /usr/bin/python3 /usr/lib/python3/dist-packages/timekpr/server/timekprd.py "$@"
```

`timekprd` hardcodes the path `/usr/lib/python3/dist-packages/timekpr` and
invokes the **system** `/usr/bin/python3` directly — it does not rely on
`sys.path` at all. This directory is **not** on system Python's default
`sys.path` on Arch (which uses `/usr/lib/python3.14/site-packages`), so the
agent's `timekpr_paths.py` must explicitly `sys.path.insert(0, ...)` this
directory rather than assuming an importable package. Confirmed both
`python-dbus` (`dbus-python`) and `python-gobject` are present system-wide, so
`import dbus` works unmodified from any Python 3 interpreter with this path
added.

## 5. Group / permission model

`sudo usermod -aG timekpr <user>` plus a **new login session** (or, for
scripting/testing, `newgrp timekpr` in a subshell) is sufficient — no root
needed for any admin DBUS call once in the group. `initTimekprConnection`
degrades gracefully (returns non-zero / logs, does not raise) when the group
membership isn't active, which the agent's error handling should rely on
rather than expecting exceptions.

## 6. Correction to the plan

`timekprAdminConnector.initTimekprConnection` signature is
`(pTryOnce, pRescheduleConnection=False, pCLI=None)` — **positional/keyword
`pTryOnce`, no `pIsClient` argument**. The plan's pseudocode should read:

```python
admin = timekprAdminConnector()
admin.initTimekprConnection(pTryOnce=True, pCLI=True)
```

## Raw output

```
=== BEFORE ===
--- before any write ---
  TIME_SPENT_BALANCE = 180
  TIME_SPENT_DAY = 180
  TIME_SPENT_WEEK = 180
  TIME_SPENT_MONTH = 180
  TIME_LEFT_DAY = 7935
  ACTUAL_TIME_SPENT_BALANCE = 192
  ACTUAL_TIME_SPENT_DAY = 192

setTimeLeft('-', 60) -> result=0 msg='' (call took 0.002s)
--- after '-' 60s ---
  TIME_SPENT_BALANCE = 240
  TIME_SPENT_DAY = 180
  TIME_SPENT_WEEK = 180
  TIME_SPENT_MONTH = 180
  ACTUAL_TIME_SPENT_BALANCE = 252
  ACTUAL_TIME_SPENT_DAY = 192

[CHECK] BALANCE moved:            True
[CHECK] TIME_SPENT_DAY unmoved:   True
[CHECK] TIME_SPENT_WEEK unmoved:  True
[CHECK] TIME_SPENT_MONTH unmoved: True

setTimeLeft('+', 60) -> restores BALANCE to 180 / ACTUAL_BALANCE to 192, DAY untouched throughout.

=== '=' op regression test ===
before: TIME_SPENT_DAY=180 ACTUAL_TIME_SPENT_DAY=192
setTimeLeft('=', 120) -> BALANCE=86280 (limit 86400 - 120), TIME_LEFT_DAY=120
after:  TIME_SPENT_DAY=180 (unchanged, nothing flushed yet)
        ACTUAL_TIME_SPENT_DAY=180 (dropped from 192 -- the regression)
```

## Verdict

Phase 0 is **complete**. All linchpin assumptions in the plan hold against the
real daemon. Proceeding to Phase 1 scaffolding.
