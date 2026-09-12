# Implementation Checklist

Tracks execution against the design plan at
`/home/bzumhagen/.claude/plans/help-me-make-a-twinkling-penguin.md` (referenced below as **PLAN**).
Check items off as completed; leave a one-line note if something deviated from PLAN and why.

See also [`docs/best-practices-review.md`](docs/best-practices-review.md)
for a general Python/FastAPI/SQLAlchemy best-practices pass done after
Phase 1 — several of its findings (no parent auth, pending devices can
sync, the agent's offline-grace clock) sharpen items already listed below
as deferred or open here.

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
- [x] `hub/timekpr_hub/` FastAPI app itself (routers, app.py) — `app.py` +
      6 routers under `hub/timekpr_hub/api/` (enroll, sync, parent,
      parent_auth, ui, auth)
- [x] `agent/timekpr_hub_agent/timekpr_paths.py` — probes distro package
      locations, validated against the real install (see Phase 0 §4 above)
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
- [x] `POST /enroll` — `hub/timekpr_hub/api/enroll.py`, single-use enrollment codes. Each reported
      `local_users` entry is provisioned as a new canonical `User` if the hub doesn't already know
      that username, or aliased into the existing one if it does (a savepoint absorbs the
      unique-constraint race from two devices enrolling the same brand-new username
      concurrently) — no separate "create a user" step needed before enrolling. Covered by
      `test_enroll_provisions_a_new_user_when_none_exists` and
      `test_enroll_merges_into_an_existing_user_of_the_same_username` in `tests/integration/test_hub_api.py`.
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
- [x] systemd unit + sysusers.d + tmpfiles.d + PKGBUILD — `agent/packaging/`. **No venv**: the
      agent's only third-party dependency is `httpx` (it never imports `pydantic` itself), so
      the package now `depends=` on `python-httpx`/`python-pydantic` directly and installs
      straight into `/usr/lib/timekpr-hub-agent/` (a fixed, Python-version-independent path) via
      a thin `/usr/bin/timekpr-hub-agent` launcher, replacing the earlier
      `--system-site-packages` venv under `/opt` — an Arch Python minor upgrade no longer
      requires rebuilding this package. `Restart=always` (was `on-failure`) with
      `StartLimitIntervalSec=0`, `Type=notify` + `WatchdogSec=120` (agent sends `READY=1`/
      `WATCHDOG=1` via a ~15-line stdlib `notify.py`, no dependency), and deliberately no
      `After=`/`Requires=` on `timekprd`/`dbus` (the old `After=timekpr.service` combined with
      `WantedBy=multi-user.target` forms a boot-ordering cycle with timekpr's own
      `After=multi-user.target` unit). `makepkg -f` run and verified in this session: produced a
      correctly-laid-out `.pkg.tar.zst` (`usr/bin/timekpr-hub-agent` present,
      `tar -tf … | grep python3\.` empty, systemd unit +
      sysusers.d/tmpfiles.d/agent.env at their expected paths). **`pacman -U` + a real
      enrollment has since been run and verified on a separate machine** (deliberately
      left for the user rather than done unattended here, since it creates a system user
      and a running service).
- [x] `enroll`/`run`/`status` CLI subcommands, single-command enrollment (`main.py`,
      `config.py`). `enroll` now: preflights the local timekpr install and DBUS connectivity;
      prompts for which local users to manage (from timekpr's own `getUserList()`) when
      `--users` is omitted, validating any explicitly-given names the same way; reads each
      user's own configured limits to seed a brand-new hub user's policy (Phase 5a, see below)
      instead of always defaulting to 1h/day; writes the device token `chown`-ed to the
      service's own user (`config.chown_to_service_user` — a plain `sudo … enroll` used to leave
      a root-owned token the service can't read, working the *first* time only because
      systemd's `StateDirectory=` re-owns the directory once, not on a re-enroll); writes
      `/etc/timekpr-hub-agent/agent.env` itself instead of printing it for a parent to
      copy-paste; and runs `systemctl enable --now` (skippable with `--no-start`). Friendly
      errors (`hubclient.EnrollError`) for an unknown/used/expired code or an unreachable hub,
      no traceback. The hub UI's own enrollment snippet was also fixed — it previously printed
      `--hub` (not a real flag) and omitted the required `--users`
      (`docs/best-practices-review.md`). `status` reports one ✓/✗ line per link in the chain
      (timekpr installed, DBUS reachable, config present, token readable, service
      enabled/active, hub reachable) plus each managed user's last sync/balance.
- [x] **Reboot/offline correctness fixes**, all caught or confirmed by new tests in
      `tests/unit/test_agent_state.py`/`test_agent_run_tick.py`:
      - Offline grace now keyed on wall-clock time (`last_hub_contact_utc`), not
        `time.monotonic()` (whose epoch resets on reboot and used to leave the agent silently
        unenforced — `docs/best-practices-review.md`).
      - `state.load()` drops unknown/legacy fields instead of `TypeError`-crashing on an old
        `state.json` after an upgrade.
      - Offline `capped` policy now estimates today's spend as
        `last_global_spent_s + (local activity since contact)`, so local usage while offline
        is never silently refunded back to the stale synced value — the *cap itself* stays
        anchored to the frozen `last_global_spent_s`, not the live estimate (a first attempt at
        this used the live estimate for both, which the new
        `test_offline_capped_policy_still_enforces_the_cap` caught: it made the cap always
        trail the estimate by a fixed margin and never actually bind).
      - A device's first tick for a user now credits whatever timekpr already shows as spent
        today instead of resetting to 0 (Phase 5f — pre-enrollment usage used to be silently
        forgiven).
- [x] **Hub-authoritative limits (Phase 5, partial)** — previously each device converged its
      local balance toward the pooled *spend* total but enforced against its *own* locally
      configured limit, so an unconfigured device (timekpr's 24h/day default) was effectively
      unlimited regardless of the hub's policy, and a hub-side grant never actually granted
      anything on a device whose local limit hadn't been separately pushed.
      `core/timekpr_hub_core/convergence.py`'s `plan()` now converges to
      `target_balance = G + (L_dev - L_eff)`, making time left equal `L_eff - G` (the hub's
      effective limit minus its global spent total) regardless of the device's own configured
      limit — see the new property test in `test_convergence.py` and the updated
      `test_absolute_write_uses_device_limit_not_hub_target_limit` regression test. The agent
      now actually pushes `setTimeLimitForDays`/`Week`/`Month`/`setAllowedDays` when the hub
      sends a policy payload (previously nothing called these, and a `policy_version_applied`
      bug marked the push as done without ever writing anything, so the hub stopped resending
      it after the very first tick). `/enroll` seeds a brand-new hub user's policy from the
      first device's own configured limits rather than always defaulting to 1h/day, and returns
      each user's effective policy so `enroll` can print a diff. **Not yet done**: a hub-side
      policy editor UI/endpoint (there's still no way to *change* a policy through the hub
      itself beyond the existing +time grants), pushing `allowed_hours`/lockout
      type/PlayTime, and a hub UI indicator for a device whose reported limit still disagrees
      with policy after a push (see `docs/best-practices-review.md`).
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
- [x] Two devices, user with 1h limit: burn 40min on A, log into B, confirm ~20min left
      within one sync interval — `tests/e2e/test_acceptance.py::test_sequential_two_device_handoff`,
      driving the real agent tick loop + real convergence math against a real hub over real HTTP
      (see `tests/e2e/harness.py`)
- [x] Burn remaining 20min on B, confirm both lock —
      `test_both_devices_lock_at_the_shared_limit`, also checks the PLAN overshoot bound
- [x] Wall-clock test: both active simultaneously for 30min, confirm ~30min consumed not 60 —
      `test_wallclock_accounting_counts_overlapping_use_once`, the one scenario that drives real
      overlapping `activity_intervals` through the real `range_agg` union

---

## Phase 1.5 — Accuracy, device lifecycle, policy editor, parent auth

Prompted by live dogfooding after Phase 1: the hub UI read noticeably less
elapsed time than timekpr's own UI, uninstalling/reinstalling the agent
silently forked a duplicate device, `enroll` couldn't be run fully
interactively, and the two largest remaining UI/security gaps (no limit
editor beyond additive grants, no parent auth at all) were closed together.

- [x] **Time-accuracy fix** — `global_spent_wallclock` (`services/
      aggregate.py`) now floors the `range_agg` union at
      `MAX(usage_counters.spent_seconds)` across devices: that absolute,
      idempotently MAX-merged counter is a hard lower bound on the true
      total and self-heals anything the union under-counts, without
      disturbing the union's authority for the genuinely-simultaneous
      multi-device case. The agent (`main.py`) now clamps each tick's
      `active_span` start to the *previous* tick's own emitted end
      (`UserState.last_tick_utc`) instead of fabricating `now - burned_s`
      from scratch, which used to reach backwards past the prior span
      whenever a tick was delayed (the union then silently swallowed the
      overlap — the dominant source of the discrepancy). A sync that fails
      to reach the hub now buffers its span (`UserState.pending_spans`,
      capped) and resends it with the next successful tick instead of
      losing that activity from the union forever. Wire format changed
      `active_span: ActiveSpan | None` → `active_spans: list[ActiveSpan]` to
      carry replayed spans alongside the current tick's own.
- [x] **Active/idle/offline indicator** — `UserObservation.active` used to
      be a bare alias for `logged_in`; ground truth is now the tick-over-
      tick burn delta (`draining` when it moved, `idle` when logged in but
      not burning, `logged_out` otherwise) — no new DBUS field needed. Hub
      stores `usage_counters.activity_state` and serves the most recent
      non-stale device's value; the UI shows a badge per user and ticks the
      displayed elapsed time live (extrapolating from `as_of`) while
      draining, rather than only ever showing a value up to one 20s poll old.
- [x] **Device rebind on re-enroll** (`api/enroll.py`) — a known,
      non-revoked `machine_id` now rotates that device's token and reuses
      its row (history intact) instead of creating a duplicate; `enroll`
      prints `↻ re-bound to existing device …`. A partial unique index
      (`uq_devices_machine_id_live`, migration `8f3c2a1e9b04`) enforces this
      at the DB level while still letting a *revoked* device's machine_id
      be reused by a genuinely new row. Added `revoke` (reversible, kills
      the token immediately) and `delete` (hard, erases history) device
      actions to both the parent API and the hub UI — previously `approve`
      was the only device action available.
- [x] **Fully interactive `enroll`** — `--code` is no longer
      argparse-required and `--hub-url` is prompted for exactly like
      `--users` already was (shared via a new `_prompt_or_die` helper);
      typing a scheme-less host (`hub.local:8000`) is normalized to
      `http://` (`config.normalize_hub_url`), and a malformed URL that
      reaches `HubClient._post` anyway now raises a friendly `EnrollError`
      instead of an uncaught `ValueError` traceback.
- [x] **Daily-limit / policy editor** — `PUT /api/v1/users/{u}/policy` and
      an inline hub-UI form insert a new append-only `Policy` version and
      repoint `current_policy_id` (`services/policy.py::update_policy`,
      `SELECT … FOR UPDATE`-guarded against the version race); the agent
      already had the push/apply path built (Phase 1's Phase 5 work), so
      this was the missing write path. This is now the only way to change a
      child's *limit* through the hub — grants stay additive/day-scoped.
- [x] **Parent authentication** — every route under `api/parent.py` and
      `api/ui.py` now requires a logged-in parent (`api/parent_auth.py`'s
      `get_current_parent_api`/`_ui`, applied router-level in `app.py`):
      argon2id password hash, session cookie mirroring the device-token
      pattern (`api/auth.py`) rather than a second scheme, first account
      created via a first-run `/setup` page that 404s once claimed. Closes
      the one open HIGH finding in `docs/best-practices-review.md`.
- [x] Folded in three smaller `docs/best-practices-review.md` MEDIUM
      findings while touching this code: `EnrollRequest`/`GrantCreate`/the
      new `PolicyUpdate` now bound every field (previously an oversized
      value could 500 as a raw asyncpg error); a malformed
      `start >= end` span is now skipped rather than reaching `tstzrange`;
      added the missing `(user_id, day)` indexes on `usage_counters`,
      `activity_intervals`, and `grants`.
- [x] **Closed**: `tests/e2e/harness.py` wires `FakeTimekprDaemon` + the real
      agent tick loop (`run_tick`) + the real convergence math + a real hub
      (uvicorn on an ephemeral port) + real Postgres into one process. The
      Phase 1 acceptance boxes above and Verification Layer 5 below are now
      checked from it (`tests/e2e/test_acceptance.py`), plus a Layer 6 chaos
      starter (`tests/e2e/test_chaos.py`: a multi-tick hub outage with
      buffer/replay, a backwards clock jump). `run_tick` gained an optional
      `now`/`debug_clock` pair (default off, gated so a stray `now=` alone
      is inert) to let the harness compress tens of minutes of simulated
      activity into milliseconds without touching the real agent's clock
      path. `make test-e2e` runs just this directory; `make test-all`
      includes it. Layer 3 (a real-daemon `TK_DEV_ACTIVE` harness) is still
      open — this closes the *simulated-daemon* end-to-end gap, not that one.

## Phase 2 — Robustness & centralized policy (PLAN: "Phase 2")
- [x] **Observe/dry-run enforcement mode** — the agent already skipped
      convergence and only logged intent when `/sync` reported
      `EnforcementMode.OBSERVE` (`main.py`'s `resp_user.get("enforcement")
      == "observe"` branch, originally built for the unmapped-user case);
      what was missing was any way for a parent to actually *set* it. Added
      `POST /api/v1/devices/{id}/observe` / `.../enforce` (`api/parent.py`)
      and matching hub-UI buttons + an "(observe-only)" badge
      (`api/ui.py`, `_devices_fragment.html`), toggling the existing
      `devices.enforcement` column `/sync` already reads. Verified end to
      end: `tests/integration/test_hub_api.py::
      test_device_observe_toggle_flips_enforcement_reported_by_sync`
      enrolls a device, flips it to observe, and confirms the next `/sync`
      reports `enforcement: "observe"`.
- [x] **`audit_log` wired** for the security/change-relevant actions named
      in the PLAN's "alerts, audit_log tables fully wired" item: grant
      creation, policy edits, device revoke/delete, and parent login
      (`services/audit.py`'s `record_audit_event`, called from
      `api/parent.py` and `api/parent_auth.py`). Approve/observe/enforce
      toggles and `alerts` itself are not wired yet — left for a future
      pass rather than expanding this one further. Verified against real
      Postgres: `test_grant_policy_update_and_device_revoke_are_all_audit_logged`
      and `test_login_is_audit_logged`.
- [x] **Hub logging configured** — nothing configured the `timekpr_hub`
      logger before this (`log.debug` calls were silently dropped,
      `log.warning` only reached stderr via Python's unformatted "last
      resort" handler); `logging_config.py`'s `configure_logging()`, called
      once from `app.py`, adds a formatted stream handler at a level
      controlled by `TIMEKPR_HUB_LOG_LEVEL` (default `INFO`).
- [ ] Full policy push w/ per-field change detection
- [ ] Drift detection + adopt/ignore workflow (local `timekpra` edits)
- [ ] Local-grant capture & auto-promotion (`unexplained` offset detection)
- [ ] Clock-skew + NTP detection (`org.freedesktop.timedate1`)
- [ ] Silent-device alerting + ntfy push
- [ ] `alerts` table wired (audit_log now is, see above)
- [ ] History charts (Chart.js)
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

## Multi-distro support (not started — Arch/CachyOS only today)

timekpr-next itself supports Ubuntu & derivatives (PPA), Debian (native),
Arch/Manjaro (AUR), Fedora (COPR), and openSUSE (native) — see its own
README. The agent should eventually run on all of them; today only the
Arch PKGBUILD exists. Systemd distros only (timekpr-next itself depends on
logind for session tracking).

- [x] Make the agent stdlib-only: replace `httpx` (the agent's only
      third-party dependency — it never imports `pydantic` itself) with
      `urllib.request`. The two POST calls it makes (enroll, sync) don't
      need more than the stdlib gives, and this drops the agent's runtime
      dependencies to just `python3`/`python3-dbus`/`python3-gi`, which
      timekpr-next already requires on every supported distro. Done —
      `agent/timekpr_hub_agent/hubclient.py` is built on `urllib.request`,
      and `agent/pyproject.toml` declares no `httpx` dependency (only
      `timekpr-hub-core`, for its pure convergence/calendar logic).
- [ ] Build `.deb`/`.rpm`/Arch packages from one payload (e.g.
      [nfpm](https://nfpm.goreleaser.com/)) in CI, attached to GitHub
      Releases — this also finally gives the Arch PKGBUILD a real
      `source=()` tarball instead of building from `$startdir/../..`.
- [ ] Per-format post-install scripts (`systemd-sysusers`,
      `systemd-tmpfiles --create`, `systemctl daemon-reload`) for
      `.deb`/`.rpm` — Arch's own pacman hooks already cover this
      (`timekpr-hub-agent.install` only prints the next step).
- [ ] Publish per distro as demand warrants: AUR first (closest to today's
      PKGBUILD), then a PPA/COPR/OBS submission.
- [ ] Confirm each distro's timekpr-next package actually creates the
      `timekpr` group the agent's `sysusers.d` config joins, and that
      `timekpr_paths.py`'s search (`dist-packages`/`site-packages`) finds
      that distro's install layout.
- [ ] Log the detected timekpr-next version at startup, and re-run the
      live checks from `docs/agent-live-test-findings.md` on each
      distro — distro timekpr-next versions differ (Debian's own README
      calls its packages "usually outdated"), and both real bugs that
      writeup found were invisible to every synthetic test.
- [ ] CI smoke tests: install the built package in
      `debian`/`ubuntu`/`fedora`/`opensuse`/`archlinux` containers and run
      `timekpr-hub-agent status`; a real reboot-survival check still needs
      an actual VM per distro, not a container.

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
      `tests/integration/test_aggregate_postgres.py` (7 tests: idempotent MAX-merge,
      out-of-order arrival, wall-clock union via real `range_agg`, the `GREATEST` absolute-
      counter floor self-healing a union that lost spans and NOT overriding a larger genuine
      union) and `tests/integration/test_hub_api.py` (23 tests: full enroll/approve/sync flow,
      single-use codes, auth required, revoked-device fail-closed, two-device burn-once via
      real HTTP, machine_id rebind/revoke-then-reenroll, policy PUT + push-on-next-sync, the
      full parent-auth login/setup/logout flow). DST/ISO-week boundary coverage is in Layer 1
      (`test_calendar.py`) since that logic is pure and doesn't need Postgres. **119/119 tests
      passing** across all layers implemented so far (skips gracefully if Postgres isn't
      reachable) — see `make check`/`make test-all`.
- [x] Layer 5: `tests/e2e/` (see Phase 1 acceptance test above) — the real agent tick loop
      against a real hub (uvicorn) + real Postgres, not docker-compose packaging specifically
      (a real systemd agent + real timekprd containers, still open) but the same logical gap
      Layers 2 and 4 individually left unclosed
- [x] Layer 6 (starter): `tests/e2e/test_chaos.py` — a multi-tick hub outage with span
      buffer/replay, a backwards clock jump. Partitions, daemon restarts mid-tick, and a wider
      sweep of corrupted-state variants are still open (some corrupted-state cases are already
      covered without a DB in `tests/unit/test_agent_state.py`)
- [ ] Layer 7: `--dry-run` against real machines for a week before enforcing
