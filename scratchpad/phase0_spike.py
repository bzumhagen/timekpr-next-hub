#!/usr/bin/env python3
"""
Phase 0 spike script — validates the linchpin invariant from the plan:

    setTimeLeft(user, '-'/'+' , secs) moves TIME_SPENT_BALANCE
    but does NOT move TIME_SPENT_DAY/WEEK/MONTH.

Also measures write->visible latency and the '=' op's regression window.

Run as a user in group `timekpr` (after a fresh login/newgrp so the
membership is active on this process's credentials):

    python3 scratchpad/phase0_spike.py <username>

Requires: /usr/lib/python3/dist-packages on sys.path (added below).
"""

import sys
import time

sys.path.insert(0, "/usr/lib/python3/dist-packages")

from dbus.mainloop.glib import DBusGMainLoop  # noqa: E402

DBusGMainLoop(set_as_default=True)

from timekpr.client.interface.dbus.administration import timekprAdminConnector  # noqa: E402


def dump_relevant(info, label):
    keys = [
        "TIME_SPENT_BALANCE",
        "TIME_SPENT_DAY",
        "TIME_SPENT_WEEK",
        "TIME_SPENT_MONTH",
        "TIME_LEFT_DAY",
        "ACTUAL_TIME_SPENT_BALANCE",
        "ACTUAL_TIME_SPENT_DAY",
    ]
    print(f"--- {label} ---")
    for k in keys:
        if k in info:
            print(f"  {k} = {info[k]}")
    print()


def get_info(admin, user, lvl="F"):
    result, message, info = admin.getUserConfigurationAndInformation(user, lvl)
    if result != 0:
        raise RuntimeError(f"getUserConfigurationAndInformation failed: {message}")
    return info


def main():
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} <username>")
        sys.exit(1)
    user = sys.argv[1]

    admin = timekprAdminConnector()
    admin.initTimekprConnection(pTryOnce=True, pCLI=True)

    print("=== BEFORE ===")
    before = get_info(admin, user)
    dump_relevant(before, "before any write")

    # --- Test 1: relative '-' op should move BALANCE only, not DAY/WEEK/MONTH ---
    t0 = time.monotonic()
    result, msg = admin.setTimeLeft(user, "-", 60)
    t1 = time.monotonic()
    print(f"setTimeLeft('-', 60) -> result={result} msg={msg!r} (call took {t1 - t0:.3f}s)")

    after_minus = get_info(admin, user)
    dump_relevant(after_minus, "after '-' 60s")

    balance_moved = after_minus.get("TIME_SPENT_BALANCE") != before.get("TIME_SPENT_BALANCE")
    day_unmoved = after_minus.get("TIME_SPENT_DAY") == before.get("TIME_SPENT_DAY")
    week_unmoved = after_minus.get("TIME_SPENT_WEEK") == before.get("TIME_SPENT_WEEK")
    month_unmoved = after_minus.get("TIME_SPENT_MONTH") == before.get("TIME_SPENT_MONTH")

    print(f"[CHECK] BALANCE moved:        {balance_moved}")
    print(f"[CHECK] TIME_SPENT_DAY unmoved:   {day_unmoved}")
    print(f"[CHECK] TIME_SPENT_WEEK unmoved:  {week_unmoved}")
    print(f"[CHECK] TIME_SPENT_MONTH unmoved: {month_unmoved}")
    print()

    # --- Test 2: '+' op should give time back, again without touching DAY ---
    before2 = after_minus
    result, msg = admin.setTimeLeft(user, "+", 60)
    print(f"setTimeLeft('+', 60) -> result={result} msg={msg!r}")
    after_plus = get_info(admin, user)
    dump_relevant(after_plus, "after '+' 60s (should restore balance)")

    # --- Test 3: '=' op regression window ---
    print("=== '=' op regression test ===")
    before_eq = get_info(admin, user)
    dump_relevant(before_eq, "before '=' op")
    result, msg = admin.setTimeLeft(user, "=", 120)
    print(f"setTimeLeft('=', 120) -> result={result} msg={msg!r}")
    after_eq = get_info(admin, user)
    dump_relevant(after_eq, "after '=' op (watch TIME_SPENT_DAY for regression)")

    day_before = before_eq.get("TIME_SPENT_DAY")
    day_after = after_eq.get("TIME_SPENT_DAY")
    print(
        f"[CHECK] TIME_SPENT_DAY before={day_before} after={day_after} "
        f"delta={(day_after or 0) - (day_before or 0)}"
    )
    print(
        "(A negative delta of up to ~30s here is the expected '=' regression "
        "described in the plan; a large negative delta would indicate an actual "
        "day rollover, not the flush artifact.)"
    )


if __name__ == "__main__":
    main()
