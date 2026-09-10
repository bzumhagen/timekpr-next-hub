# Implementation Checklist

Tracks execution against the design plan at
`/home/bzumhagen/.claude/plans/help-me-make-a-twinkling-penguin.md` (referenced below as **PLAN**).
Check items off as completed; leave a one-line note if something deviated from PLAN and why.

---

## Phase 0 — Spike (PLAN: "Phase 0 — spike (1–2 days). Do not skip.")

Goal: empirically confirm the linchpin invariant before building anything on top of it.

- [x] Confirm `timekpr` Python package location on this machine (Arch/CachyOS →
      `/usr/lib/python3.*/site-packages`) — PLAN "Agent — Python 3.11+"
      **Finding:** hardcoded to `/usr/lib/python3/dist-packages/timekpr`, invoked via explicit
      path by `timekprd`'s shebang wrapper, NOT on default `sys.path`. Agent must insert it
      explicitly. See `docs/phase0-findings.md` §4.
- [x] Confirm `timekprd` + `timekpra` installed and daemon running (v0.5.10)
- [x] Confirm current user (or a throwaway service user) can be added to group `timekpr`
      and gains `com.timekpr.server.user.admin` access without root — PLAN "Context" bullet 1
      **Finding:** `usermod -aG` + `newgrp timekpr` (or new login) is sufficient, no root
      needed for calls. See §5.
- [x] Write throwaway script: call `getUserConfigurationAndInformation(user, 'F')` via
      `timekprAdminConnector`, using `initTimekprConnection(pTryOnce=True)` — PLAN "Repo layout and stack"
      → `scratchpad/phase0_spike.py`. **Correction to plan:** signature is
      `(pTryOnce, pRescheduleConnection=False, pCLI=None)`, no `pIsClient` kwarg. See §6.
- [x] Call `setTimeLeft(user, '-', 60)` and confirm `TIME_SPENT_BALANCE` moves while
      `TIME_SPENT_DAY` does NOT — PLAN "The core mechanism" linchpin
      **CONFIRMED against live daemon.** See `docs/phase0-findings.md` §1.
- [x] Measure write→visible latency (DBUS call to daemon reflecting new balance)
      **~2ms**, negligible vs. any planned sync interval. §3.
- [x] Confirm the `'='` op regression is ≤30s (write `'='`, observe `TIME_SPENT_DAY` dip) —
      PLAN "⚠ The '=' regression trap"
      **CONFIRMED, more precisely than expected**: regression is visible in
      `ACTUAL_TIME_SPENT_DAY` (live counter, dropped 192→180, i.e. 12s lost) rather than the
      persisted `TIME_SPENT_DAY` in a short test window. See §2 — informs how the agent should
      audit `'='` writes.
- [x] Write up observed invariants as a short note in `docs/phase0-findings.md`
- [x] Restored test account (`bzumhagen`) balance back to its pre-test value (180s) after
      the spike — no lasting effect on the real machine's timekpr state.

**Phase 0 verdict: COMPLETE.** All linchpin assumptions hold against the real daemon.
Proceeding to Phase 1.

---

## Phase 1 — MVP: pooled daily budget (PLAN: "Phase 1 — MVP")

**This is the actual deliverable for the original ask — stop and validate here before continuing.**

### Repo scaffolding (PLAN: "Repo layout and stack")
- [x] `core/timekpr_hub_core/` package (models.py, calendar.py, convergence.py, interval_union.py)
- [x] `hub/timekpr_hub/` package + Alembic migrations set up, `.venv` dev environment
      (`requirements-dev.txt` frozen)
- [x] `agent/timekpr_hub_agent/` package skeleton (fake_timekpr.py written so far)
- [ ] `hub/timekpr_hub/` FastAPI app itself (routers, app.py) — in progress
- [ ] `agent/timekpr_hub_agent/timekpr_paths.py` — probe distro package locations
- [x] `deploy/docker-compose.yml` + `Dockerfile` + `Caddyfile` + `.env.example` — **built and
      ran the actual stack** (postgres + hub containers) via `docker compose` (podman backend):
      image builds cleanly, migrations auto-apply on container start, `/healthz` responds `ok`
      inside the container. Caddyfile validated separately with `caddy validate` (rootless
      podman in this sandbox can't bind privileged ports 80/443 to prove the reverse-proxy path
      live end-to-end — a real Docker host or rootful podman doesn't have that restriction). A
      nightly `pg_dump` backup service is included per PLAN "Hardening", not yet exercised.
- [x] `tests/{unit,integration,e2e}/` dirs
- [ ] `third_party/timekpr-next` git submodule (read-only, tests only) — deferred; Phase 0/2
      validation so far has been done directly against `/home/bzumhagen/source/timekpr-next`
      and a locally installed package, which was sufficient and faster to iterate on

### Core (shared, pure, no IO) — PLAN "The core mechanism"
- [x] `convergence.py`: `plan(...) -> Plan(op, seconds, new_applied_offset, local_grant_s, reason)`
- [x] Deadband (5s), `'='` vs `'+/-'` op selection logic (clamp guard, |correction|>300s guard)
- [x] `advance_cumulative()` — the `'='` regression magnitude-rule (≤90s tolerance)
- [x] `calendar.py` — canonical day/ISO-week/month boundary helpers
- [x] `interval_union.py` — wall-clock union (pure Python), `snap_to_grid`, brute-force oracle
- [x] `models.py` — shared Pydantic wire types (Sync/Enroll/Policy/Event/parent API)
- [x] Layer 1 property tests written & passing: `tests/unit/test_convergence.py` (12 tests),
      `tests/unit/test_interval_union.py` (9 tests, incl. burn-once acceptance property),
      `tests/unit/test_calendar.py` (9 tests, incl. ISO-week year boundary) — **30/30 passing**

  **⚠ Bug caught during implementation (worth recording):** my first cut of `plan()`
  computed `correction = global_spent_s − observed_offset` (i.e. `G − O`), which carries a
  spurious `+s` term. The correct formula, matching the PLAN's own pseudocode
  (`correction = remote_spent_s − (B − s)`, where `remote_spent_s = R = G − s`), reduces to
  `correction = G − B` directly. The bug was caught by
  `tests/integration/test_multi_device_simulation.py` (a two-device convergence scenario
  diverged immediately) rather than by the unit-level property tests, which is exactly why
  the plan calls the multi-device simulation "the highest-ROI artifact in the project" — see
  `convergence.py`'s module docstring for the corrected derivation.

### Hub — Phase 1 scope only (PLAN "Data model", "API")
- [x] Postgres schema (Alembic): `parents`, `parent_sessions`, `users`, `user_aliases`,
      `devices`, `policies`, `usage_counters`, `activity_intervals`, `grants`,
      `enrollment_codes` — `alerts`/`audit_log` tables also created now (empty of app logic,
      cheaper than a later migration) — `hub/timekpr_hub/db/models.py`,
      `hub/migrations/versions/fafdbdc4fd31_initial_schema.py`. **Applied against a real
      Postgres 16 container and validated**: server-side column defaults (fixed a bug where
      they were Python-only and broke raw SQL inserts), the `range_agg` wall-clock union query
      (returns exactly 2700s for two overlapping 30-min windows), and idempotent MAX-merge on
      `usage_counters`. See `hub/timekpr_hub/services/aggregate.py` and
      `tests/integration/test_aggregate_postgres.py` (3 tests, skips gracefully with no DB
      reachable).
- [x] `POST /enroll` — `hub/timekpr_hub/api/enroll.py`, single-use enrollment codes, auto-suggests
      user aliases by exact local-username match
- [x] `POST /sync` (daily budget only; wall-clock union via `activity_intervals` + range_agg) —
      `hub/timekpr_hub/api/sync.py`. Supports both `wallclock` and `parallel` accounting modes
      per-user (user's choice was wallclock; parallel implemented too since it's nearly free —
      `services/aggregate.py::global_spent_parallel`)
- [x] Device bearer-token auth (`hub/timekpr_hub/api/auth.py`) — revoked token → immediate 403,
      never fails open (PLAN pitfall)
- [x] Minimal parent API: `GET /users`, `POST /users/{u}/grants`, `GET /devices`,
      `POST /devices/{id}/approve`, `POST /enrollment-codes` — `hub/timekpr_hub/api/parent.py`.
      **No parent authentication wired yet** (email/argon2id/TOTP is real work, deferred to
      Phase 2 — these endpoints are open on the local network for now, fine for a
      single-household dev/LAN deployment but flagged here so it isn't forgotten before any
      wider exposure)
- [x] One UI page: per-user usage bar, **+30 min** button, device list + approve action
      (Jinja2 + HTMX, poll-refreshed) — `hub/timekpr_hub/api/ui.py` +
      `hub/timekpr_hub/web/templates/`. **Daily-limit editor not yet built** (still todo).
      Verified live: usage bar renders real data, +30 min grant visibly moves the limit
      (60→90 min in a live test), device list + approve both round-trip through real Postgres.
- [x] Verified end-to-end against real Postgres + real HTTP (not mocks): enroll → approve →
      sync flow, single-use enrollment codes, revoked-device 403, and the two-device wall-clock
      "burn once" property, all via `tests/integration/test_hub_api.py` (6 tests) using FastAPI
      `dependency_overrides` to give each test its own DB session bound to pytest-asyncio's
      event loop (see that file's top-of-file comment for why — this was a real footgun:
      pytest-asyncio's default per-test event loop scope conflicts with a module-level asyncpg
      engine created once at import time; fixed via
      `asyncio_default_fixture_loop_scope = "session"` +
      `asyncio_default_test_loop_scope = "session"` in `pyproject.toml`, plus overriding
      `get_session` per test rather than sharing the app's global engine)

### Agent — Phase 1 scope only (PLAN "Each agent tick", "Offline behavior")
- [x] `hubclient.py` — httpx client, enrollment flow (token stored 0600); revoked-token and
      hub-unreachable raise distinct exceptions the tick loop handles differently
- [x] `enforcer.py` — reuse `timekprAdminConnector`, `pTryOnce=True`, no GLib loop. **Validated
      against the real daemon** (read + write + restore round trip on a live account)
- [x] `timekpr_paths.py` — probes `/usr/lib/python3/dist-packages` (confirmed this is where
      `timekprd` itself loads from, via its shebang wrapper) and the `site-packages` glob as a
      fallback; validated it actually locates and imports the real installed package
- [x] `state.py` — atomic state.json (write+fsync+rename), corrupted-file-safe load
- [x] `main.py` — plain `while True`-style `run_tick()` (no loop itself yet, testable per-tick),
      canonical-day rollover detection, offline degraded-mode dispatch
- [x] Daily convergence per PLAN pseudocode (deadband, `'='` cases, regression-safe cumulative)
- [x] Offline grace + `open`/`capped`/`closed` policies (default `capped`, grace/cap constants
      from PLAN defaults) — `_apply_offline_policy()` in `main.py`
- [x] systemd unit + sysusers.d + tmpfiles.d + PKGBUILD — `agent/packaging/`. Unit file syntax
      checked with `systemd-analyze verify` (only complaint: the venv path doesn't exist yet in
      this dev sandbox, which is expected — it's created by the PKGBUILD's `package()` step).
      **The PKGBUILD itself has not been run through `makepkg`** — it's a solid draft following
      the `--system-site-packages` venv approach validated manually earlier in this session,
      but an actual build+install cycle on this machine is still open work.
- [x] **Agent venv packaging validated**: `python3 -m venv --system-site-packages agent/.venv`
      + `pip install httpx pydantic` gives one interpreter with both `dbus` (system) and
      `httpx`/`pydantic` (pip) importable together, exactly per PLAN's packaging
      recommendation — confirmed by actually creating it and running the agent through it.
- [x] **Full live dogfooding against the real daemon + real hub** (not mocks): enrolled a real
      device, ran `run_tick()` against `timekprd` on this machine via `newgrp timekpr`, and
      against the live FastAPI+Postgres hub. This is the single most valuable testing done in
      Phase 1 — it found **two real bugs that all 47 synthetic tests had missed**:
      1. Absolute (`'='`) writes computed `seconds` against the *hub's* target limit rather
         than the *device's own configured* limit — wrong whenever they disagree (true for
         every device before Phase 2's policy push exists). Fixed in `convergence.py`; see its
         `Observation.limit_today_s` docstring and the new regression test
         `test_absolute_write_uses_device_limit_not_hub_target_limit`.
      2. The device's configured limit was itself being derived incorrectly, as
         `TIME_LEFT_DAY + balance` — but `TIME_LEFT_DAY` is a *dynamically recomputed* runtime
         value (folds in `ALLOWED_HOURS` restrictions and week/month `min()`s), not simply
         `static_limit - balance`. Fixed in `enforcer.py` to read `LIMITS_PER_WEEKDAYS[today]`
         directly — the exact same lookup `configprocessor.py:718` itself uses.
      Both fixes verified by re-running the live tick and confirming the resulting balance
      lands exactly where the math predicts. **Full writeup:
      `docs/agent-live-test-findings.md`** — read this; it's short and explains why neither bug
      was (or could have been) caught by the synthetic test suite given each test's stated
      scope, which is itself a useful note for what to dogfood again before Phase 2.

### Explicitly deferred to later phases (do not build now)
- Week/month pooling, full policy push/diffing, drift detection, PlayTime, charts,
  one-active-device leases, per-device weighting

### Phase 1 acceptance test (PLAN "Layer 5 — end-to-end acceptance")
- [ ] Two devices, user with 1h limit: burn 40min on A, log into B, confirm ~20min left
      within one sync interval
- [ ] Burn remaining 20min on B, confirm both lock
- [ ] Wall-clock test: both active simultaneously for 30min, confirm ~30min consumed not 60

---

## Phase 2 — Robustness & centralized policy (PLAN: "Phase 2")
- [ ] Full policy push w/ per-field change detection
- [ ] Drift detection + adopt/ignore workflow (local `timekpra` edits)
- [ ] Local-grant capture & auto-promotion (`unexplained` offset detection)
- [ ] Clock-skew + NTP detection (`org.freedesktop.timedate1`)
- [ ] Silent-device alerting + ntfy push
- [ ] `alerts`, `audit_log` tables fully wired
- [ ] History charts (Chart.js)
- [ ] Observe/dry-run enforcement mode
- [ ] Overshoot carryover to next day

## Phase 3 — Pooled week/month (PLAN: "Phase 3")
- [ ] Effective week/month limit computation (shrink `LIMIT_PER_WEEK`/`_MONTH`, quantized
      writes ≤1/min, excluded from drift detection)
- [ ] Hub-side aggregation from daily counters
- [ ] Weekly-budget UI

## Phase 4 — Later / optional (PLAN: "Phase 4")
- [ ] Kid-initiated bonus-time requests
- [ ] Calendar overrides
- [ ] One-active-device leases
- [ ] PlayTime pooling
- [ ] Per-device weighting

---

## Verification carried out alongside implementation (PLAN "Verification")
- [x] Layer 1: Hypothesis property tests on `convergence.plan()` and interval union —
      `tests/unit/test_convergence.py`, `tests/unit/test_interval_union.py`,
      `tests/unit/test_calendar.py` (30 tests)
- [x] Layer 2: `FakeTimekprDaemon` — `agent/timekpr_hub_agent/fake_timekpr.py`, pinned to the
      exact numbers observed in `docs/phase0-findings.md`
      (`tests/integration/test_fake_timekpr_parity.py`, 5 tests), plus a 3-device/30-day
      random-activity simulation asserting the overshoot bound
      (`tests/integration/test_multi_device_simulation.py`, 2 tests) — **this simulation is
      what caught the `G−O` vs `G−B` correction-formula bug above.**
- [ ] Layer 3: `TK_DEV_ACTIVE` real-daemon integration harness (session bus, dev paths)
- [x] Layer 4: FastAPI + httpx `AsyncClient` + real throwaway Postgres 16 (podman container) —
      `tests/integration/test_aggregate_postgres.py` (3 tests: idempotent MAX-merge,
      out-of-order arrival, wall-clock union via real `range_agg`) and
      `tests/integration/test_hub_api.py` (6 tests: full enroll/approve/sync flow, single-use
      codes, auth required, revoked-device fail-closed, two-device burn-once via real HTTP).
      DST/ISO-week boundary coverage is in Layer 1 (`test_calendar.py`) since that logic is
      pure and doesn't need Postgres. **46/46 tests passing** across all layers implemented so
      far (skips gracefully to 40/40 if Postgres isn't reachable).
- [ ] Layer 5: docker-compose e2e acceptance (see Phase 1 acceptance test above) — the
      two-device scenarios in Layers 2 and 4 above already cover this logically; the
      docker-compose packaging (real systemd agent + real timekprd containers) is still open
- [ ] Layer 6: chaos tests (hub 500s, partitions, clock jumps, daemon restarts, corrupted state)
- [ ] Layer 7: `--dry-run` against real machines for a week before enforcing
