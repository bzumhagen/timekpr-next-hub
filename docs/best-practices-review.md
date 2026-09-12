# Best-practices review

A review of `timekpr-next-hub` against general Python/FastAPI/SQLAlchemy
practices, done at the point where `CHECKLIST.md` marks Phase 1 complete.
Findings are ranked by severity; each names a file and, where useful, a
line. A struck-through heading marked **fixed** or **moot** has been
addressed since this review was written (see the top-level `git log`);
everything else is reported only — deliberately, so this review doesn't
quietly expand into an app-logic rewrite. Re-run `make check` after
picking any of these up, and update this file's status when you do.

---

## High — all fixed or moot

- ~~**A rotated device token stays live-locked-out until something restarts
  the agent process.**~~ — **fixed**: diagnosed from a real report (a child
  logged into a freshly-enrolled, correctly-policied account and was
  immediately locked out) via a live diagnostic bundle (journal + `state.json`
  + timekpr's own control file). Root cause: `HubClient._token`
  (`agent/timekpr_hub_agent/hubclient.py`) is loaded once, at `__init__`, and
  a `run` process can live for the lifetime of a systemd service (in the
  reported case, 12+ hours). A re-enroll on the same machine rewrites the
  token file on disk and rotates it hub-side, and `enroll`'s own
  `systemctl restart` is supposed to replace the running process with a
  fresh one holding the new token — but if that restart didn't happen for
  any reason (`--no-start`, a failed/skipped systemctl call, or any other
  gap), the already-running process is left sending a now-stale token
  forever. Every `/sync` then 401s, which `hubclient.py` already correctly
  treats as `DeviceRevokedError` (fail-closed, "never fail open") — so the
  child is locked to zero time, indistinguishable from a genuine revoke,
  until someone happens to restart the service by hand. Confirmed live: at
  the moment of the report, `timekpr-hub-agent status` (a fresh process,
  freshly re-reading the token file) reported "✓ hub reachable" while the
  long-running `run` service kept 403ing on the very same tick.
  `HubClient.sync` (`hubclient.py`) now detects the on-disk token has
  changed and retries once with the fresh one before concluding the device
  is actually revoked — self-healing within one tick (~20s) instead of
  requiring a manual restart; a genuine revoke leaves the file untouched,
  so the retry is a no-op. **Second, design-level fix on the same code
  path**: even a *confirmed* revoke (the retry above also failed) no
  longer locks the child to zero time. An admin removing a device is a
  deliberate, authoritative "stop managing this machine" action, not an
  error condition like the hub being unreachable — treating it as a
  maximum-severity offline policy (the original behavior, reusing
  `_apply_offline_policy("closed", grace=0, cap=0)`) punished that decision
  as if it were a fault. `run_tick`'s `DeviceRevokedError` handler
  (`main.py`) now touches nothing at all: whatever limit/balance timekpr
  already has stays exactly as it is, so the machine reverts to local
  self-management — a parent can reconfigure it directly via
  timekpra/the timekpr GUI again, same as before it was ever enrolled.
  Re-enrolling (picked up by the retry above without even needing a
  restart) resumes hub management on the very next successful sync.
  Regression tests: `tests/unit/test_hubclient.py` covers both the
  rotated-token self-heal and the genuine-revoke case (confirms no wasted
  retry); `tests/unit/test_agent_run_tick.py`'s
  `test_revoked_device_relinquishes_control_instead_of_locking_out`
  confirms no DBUS write happens once revoked.

- ~~**No parent authentication, and the Caddyfile is set up for a public
  TLS domain.**~~ — **fixed**: every route in `api/parent.py` and
  `api/ui.py` now requires a logged-in parent (`get_current_parent_api` /
  `get_current_parent_ui` in `api/parent_auth.py`, applied router-level in
  `app.py`), password hashed with argon2id, session cookie mirroring the
  device-token pattern in `api/auth.py` (random token, only its sha256
  persisted, in the existing `Parent`/`ParentSession` tables). First account
  is created via a first-run `/setup` page that 404s once a parent exists —
  a startup-time warning names the unclaimed-hub window until then. The
  `deploy/Caddyfile` warning below still applies until that first account is
  actually claimed on a given deployment.
- ~~**Each device enforces its own local timekpr limit against the pooled
  total, not the hub's effective limit.**~~ — **fixed**: `plan()`
  (`core/timekpr_hub_core/convergence.py`) previously converged BALANCE to
  plain `global_spent_s`, which only produces the hub's intended time-left
  when the device's own configured limit happens to already match the
  hub's policy — never true for a freshly-enrolled device before any
  policy push, and never true after a hub-side grant (which was never
  reflected in the local limit at all). A device left at timekpr's
  unconfigured 24h/day default was, in practice, unlimited. `plan()` now
  converges to `target_balance = G + (L_dev - L_eff)`, which makes time
  left equal `L_eff - G` regardless of `L_dev`; `main.py` also now
  actually pushes the hub's daily/weekly/monthly limits down via
  `setTimeLimitForDays`/`Week`/`Month` (previously nothing called these,
  and the one line that looked like it tracked a pushed policy version
  marked it applied without ever writing anything). See
  `tests/unit/test_convergence.py`'s new property test and
  `docs/agent-live-test-findings.md`-style regression test for the
  algebra, and Phase 5 of the reboot/enrollment plan for the rest
  (policy seeding at enroll time, drift re-push, offline-cap fix).
- ~~**A `pending` (unapproved) device can sync.**~~ — **moot**: a
  parent-minted enrollment code is now itself the approval
  (`hub/timekpr_hub/api/enroll.py`, devices are created `status="active"`);
  the `pending`/`approve` machinery is kept only for any device enrolled
  before this change. This doesn't add authentication — anyone who can
  reach the hub can still mint a code, per the item above — it just
  removes a step that added friction without adding security.
- ~~**The agent's offline grace timer uses `time.monotonic()`**~~ —
  **fixed**: `agent/timekpr_hub_agent/state.py`'s `UserState` now tracks
  `last_hub_contact_utc` (wall-clock epoch seconds) instead, and
  `_apply_offline_policy` in `main.py` treats a negative or implausible
  elapsed time as grace already expired rather than staying open. Covered
  by `tests/unit/test_agent_run_tick.py`'s offline tests (which also
  caught a related bug: the offline cap itself was accidentally anchored
  to the live usage estimate instead of the last-known-good value, which
  would have made the cap never actually bind).
- ~~**The agent computes its canonical day/rollover in UTC, not
  `HUB_TZ`**~~ — **fixed**: `main.py`'s `_canonical_day_str` now takes the
  household timezone (cached in `AgentState.hub_tz`, refreshed from every
  `EnrollResponse`/`SyncResponse`) via `timekpr_hub_core.calendar.canonical_stamp`,
  falling back to UTC only when no timezone is known yet (e.g. before the
  first successful sync).

## Medium — open

- ~~**Unvalidated input reaches the database and can 500.**~~ — **fixed**:
  `EnrollRequest` now bounds every field with `max_length` matching its
  backing column; `GrantCreate.seconds`/`reason` are bounded (±86400s,
  255 chars) and the UI's grant form field carries the same bound;
  `PolicyUpdate` (new) validates each daily limit is `0..86400`; and
  `api/sync.py` now skips any `active_span`/`active_spans` entry where
  `end <= start` instead of letting a malformed one reach `tstzrange`.
- ~~**Enrollment-code and first-policy races.**~~ — **fixed**: `api/enroll.py`
  now claims a code with one atomic `UPDATE ... WHERE used_at IS NULL AND
  expires_at >= now RETURNING code`, not a plain `SELECT` followed by a
  later `UPDATE` — two concurrent enrolls with the same code now
  serialize on that row, and the loser's re-checked `WHERE` correctly sees
  it as already used. The first-policy race (two devices reaching a
  shared-but-policy-less user's first sync or enroll concurrently) is
  closed by a new `services/policy.py::get_or_create_policy`, used by both
  `/enroll` and `/sync`. Neither path has rate limiting, which is unrelated
  and still open.
  **A second, subtler bug was found and fixed alongside this**:
  `update_policy`'s existing `SELECT ... FOR UPDATE` (and the new
  `get_or_create_policy`'s) re-selects `user`, which every caller had
  already loaded once earlier in the same session — SQLAlchemy's identity
  map was handing back that same stale Python object once the lock was
  granted, rather than the just-committed row, silently defeating the lock
  entirely. Both now pass `execution_options(populate_existing=True)`.
  Regression tests for all three races (concurrent code redemption,
  concurrent first-policy creation, concurrent policy updates) are in
  `tests/integration/test_hub_api.py`; the last two failed against the
  code before `populate_existing` was added, confirming the bug was real.
  **A third bug, caught later by the same regression test under a
  different container/DB restart**: `api/enroll.py` inserted the
  FK'd-to-`user_id` `user_aliases` row *before* calling
  `get_or_create_policy`, not after. Each transaction's alias INSERT takes
  a shared (FOR KEY SHARE) lock on the referenced `users` row to validate
  the FK; `get_or_create_policy`'s `SELECT ... FOR UPDATE` then tries to
  upgrade that same row to an exclusive lock. Two concurrent enrolls of the
  same existing user both holding the shared lock and both waiting on the
  other's to release before their own upgrade could proceed is a textbook
  Postgres deadlock (`DeadlockDetectedError`, one transaction killed) — not
  a flaky test, a real latent bug in the original fix, reproducible on
  demand. Moving the alias insert to after the FOR UPDATE lock (so only one
  transaction ever holds any lock on the row at a time) fixed it; confirmed
  with 15 repeated runs of the deadlocking test plus 10 repeated runs of
  the full concurrent-test group, all clean.
- **The engine, session factory, and settings are all built at import
  time.** `hub/timekpr_hub/db/session.py:15-22` and
  `hub/timekpr_hub/settings.py:23` — this is why `pyproject.toml` has to
  force session-scoped event loops for pytest-asyncio
  (see the comment above `[tool.pytest.ini_options]`). There's no
  `lifespan` handler and no `engine.dispose()` on shutdown.
  `DATABASE_URL` bypasses pydantic-settings entirely
  (`os.environ.get` with a hardcoded default that embeds credentials),
  rather than being a field on `Settings`.
- ~~**N+1 query patterns.**~~ — **fixed, for the unbounded-fanout half**:
  `api/parent.py`'s `list_users` and `api/ui.py`'s `_user_summaries` (an
  almost-exact duplicate of the same logic, each running ~3-4 queries per
  user) are both now a single call into `services/summaries.py`'s new
  `compute_user_summaries`, which batches every user shown into a handful
  of queries total via new `*_batch` helpers in `services/aggregate.py`
  and `services/limits.py` (`global_spent_wallclock_batch`,
  `global_spent_parallel_batch`, `latest_activity_states_batch`,
  `grants_totals_batch`) — regardless of household size, and this is the
  path polled by every open hub UI tab every `default_next_poll_ms`.
  Cross-checked against the already-verified single-user functions for
  equivalence in `tests/integration/test_aggregate_postgres.py`.
  **Still open**: `/sync` (`hub/timekpr_hub/api/sync.py`) still runs
  roughly 8 queries per reported user, left alone deliberately -- its
  fan-out is bounded by how many local users one agent manages (normally
  1-2), not by household size, and half its per-user work is writes
  (upserts/interval inserts) that don't batch the same way reads do, so
  the ROI here is much lower than the summaries path was.
- ~~**No indexes for the hot `(user_id, day)` lookups**~~ — **fixed** for
  `activity_intervals` and `grants` (migration `8f3c2a1e9b04`); also added
  the same for `usage_counters`, which had the same gap. `Base` still has
  no `MetaData(naming_convention=...)` (`hub/timekpr_hub/db/models.py:35`),
  so unrelated constraints remain unnamed — untouched here.
- **Agent gaps:** ~~`post_events` (`hubclient.py`) posts to `/api/v1/events`,
  which the hub does not implement~~ — **fixed**: it was dead code (never
  called from `main.py`), removed rather than given a hub endpoint to call,
  since nothing in this codebase generates the events it would have sent.
  `enforcer.py` still reaches into `timekprAdminConnector`'s private
  `_timekprUserAdminDbusInterface` attribute (`connect()`) — no public
  alternative exists in timekpr-next's client library, so this is a
  standing risk rather than an oversight; it's exercised by
  `docs/agent-live-test-findings.md`-style live checks whenever timekpr is
  updated. ~~no `enroll` CLI subcommand~~ / ~~no `--ca-cert`/token-path
  flags~~ / ~~enroll printed the wrong flags and required flags manually~~
  — **fixed**: `main.py` now has `run`/`enroll`/`status` subcommands.
  `enroll` preflights the local timekpr install, prompts for users if
  omitted (validated against timekpr's own list), redeems the code,
  writes the token owned by the service's own user (`chown_to_service_user`
  — fixes a re-enroll lockout where a root-written token the service
  couldn't read would previously only work by accident, via
  `StateDirectory=`'s one-time re-own), writes `agent.env` itself instead
  of printing it for a parent to paste, and enables+starts the service.
  `status` reports one ✓/✗ line per link in the chain. The hub UI's
  enrollment snippet was also fixed (`ui.py`, previously printed the
  nonexistent `--hub` flag and omitted the required `--users`).
- ~~**`agent/packaging/PKGBUILD` issues**~~ — **fixed, then the whole venv
  approach was dropped**: the agent's only third-party dependency is
  `httpx` (it never imports `pydantic` itself), so the package now depends
  on `python-httpx`/`python-pydantic` directly and installs straight into
  system site-packages under `/usr/lib/timekpr-hub-agent/` (no
  `python3.*`-versioned path anywhere) instead of a
  `--system-site-packages` venv under `/opt` — an Arch Python minor
  upgrade no longer requires rebuilding this package at all. **Verified:
  `makepkg -f` produces a correctly-laid-out `.pkg.tar.zst`** (checked
  `usr/bin/timekpr-hub-agent`, the code layout, systemd unit,
  `sysusers.d`/`tmpfiles.d`/`agent.env`); an actual `pacman -U` +
  live-enrollment cycle on a real system was intentionally left for the
  user to run rather than done unattended (it creates a system user and a
  running service).
- ~~**`deploy/docker-compose.yml` issues.**~~ — **fixed**: Caddy no longer
  gets `env_file: .env` (which leaked `POSTGRES_PASSWORD` into a container
  that doesn't need it) -- it now gets only `HUB_DOMAIN` via `environment:`.
  `hub` and `caddy` both have healthchecks now, with `depends_on:
  condition: service_healthy` wired hub→postgres and caddy→hub. The nightly
  `backup` service dumps immediately then sleeps 24h, instead of the
  reverse (a fresh deployment used to have zero backups for a full day).
  **Reordering that surfaced three more, previously invisible, real bugs**
  in the same entrypoint (none of this had ever actually run within 24h of
  a fresh `deploy-up` before, per `README.md`'s own "not yet exercised"
  note) -- all fixed and verified against a live stack (see below):
  `pg_dump` had no `PGPASSWORD` (prompted interactively, dump silently
  never happened), no `PGUSER` (would have connected as the container's own
  OS user, `root`, once given a password), and no `PGDATABASE` (would then
  have looked for a database also named `root`); separately, the date
  suffix was `date +%%Y%%m%%d` -- correct for escaping `$$(...)` from
  compose's own interpolation, but `%` needs no such escaping, so `date`
  received a literal `%%` and emitted `%Y%m%d` as the filename instead of
  a real date, on every single dump. **Verified live**: brought up the full
  stack (`docker compose -f deploy/docker-compose.yml up -d`, rootful
  podman this time, LAN ports remapped to 3001/3002 so an unprivileged
  container can still bind them) -- all four containers reach `healthy`,
  `curl http://localhost:3001/healthz` round-trips through Caddy to the hub
  for `{"status":"ok"}` (the Caddy-fronted reverse-proxy path this repo's
  own notes had marked unverified, since rootless Podman can't bind 80/443
  directly), and the backup container produces a real, correctly-named,
  `pg_restore --list`-readable dump within seconds of starting.
- **Dependency hygiene (partially addressed by the uv migration):**
  `passlib` (unmaintained upstream) and `python-jose` (unmaintained, with
  known CVEs) were pinned in the old `requirements-dev.txt` but never
  imported anywhere — dropped in the uv workspace's dependency groups.
  When parent auth is built, prefer `pwdlib` or `argon2-cffi` directly
  over either.

## Low — open

- Parent-API endpoints (`create_grant`, `create_enrollment_code`,
  `list_devices`, `approve_device`, `revoke_device`, `delete_device` in
  `api/parent.py`) and `healthz` (`app.py:70`) return bare `dict`s with no
  `response_model`.
- Handlers use `session: AsyncSession = Depends(get_session)` rather than
  an `Annotated[AsyncSession, Depends(...)]` type alias; `settings` is
  imported as a module-level global rather than injected, so it can't be
  overridden in tests.
- `Grant.span`-adjacent TSTZRANGE column is typed `Mapped[str]`
  (`hub/timekpr_hub/db/models.py:218`) instead of a range type; the JSONB
  columns are bare `Mapped[list]`/`Mapped[dict]`. The ORM represents
  enum-like columns as `String` + `CHECK` rather than using the enums
  `core/timekpr_hub_core/models.py` already defines.
- ~~`AuditLog` ... table [is] defined but never written to; there is no
  `logging` configuration anywhere under `hub/`.~~ — **fixed, for
  `AuditLog`**: grant creation, policy edits, device revoke/delete, and
  parent login now write to it (`services/audit.py`, CHECKLIST.md Phase 2).
  `hub/timekpr_hub/logging_config.py`'s `configure_logging()` (called once
  from `app.py`) gives the `timekpr_hub` logger a real formatted handler
  and a level controlled by `TIMEKPR_HUB_LOG_LEVEL`. **`Alert` is still
  unwired** — nothing generates alerts yet (silent-device detection,
  drift, clock-skew are all still-open Phase 2 items each of which would
  feed it).
- `hub/timekpr_hub/web/templates/index.html` loads htmx from a CDN with no
  Subresource Integrity hash.
- The version string `0.1.0` is duplicated across every package's
  `pyproject.toml`, `app.py:47`, `agent/timekpr_hub_agent/main.py:52`, and
  the PKGBUILD — nothing keeps them in sync.
- ~~**No LICENSE file exists.**~~ — **fixed**: `LICENSE` (GPL-3.0) added at
  the repo root, and `license` set in every package's `pyproject.toml`.
