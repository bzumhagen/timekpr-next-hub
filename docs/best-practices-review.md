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

## High — open

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
- **Enrollment-code and first-policy races.** `api/enroll.py:39-46` reads
  the code row with a plain `SELECT`, not `SELECT ... FOR UPDATE` or a
  conditional `UPDATE ... WHERE used_at IS NULL`; two concurrent enrolls
  with the same code can both pass the check. Similarly,
  `hub/timekpr_hub/services/policy.py`'s `create_initial_policy` can
  be raced by two devices syncing a brand-new user for the first time,
  raising on `uq_policies_user_version`. Neither path has rate limiting.
  ~~`update_policy`'s own version of this race~~ — **fixed**: it now takes
  `SELECT ... FOR UPDATE` on the user row for the duration
  (`services/policy.py`'s `update_policy`, doc'd there) — only
  `create_initial_policy` (the enroll-time / first-sync path) is still open.
- **The engine, session factory, and settings are all built at import
  time.** `hub/timekpr_hub/db/session.py:15-22` and
  `hub/timekpr_hub/settings.py:23` — this is why `pyproject.toml` has to
  force session-scoped event loops for pytest-asyncio
  (see the comment above `[tool.pytest.ini_options]`). There's no
  `lifespan` handler and no `engine.dispose()` on shutdown.
  `DATABASE_URL` bypasses pydantic-settings entirely
  (`os.environ.get` with a hardcoded default that embeds credentials),
  rather than being a field on `Settings`.
- **N+1 query patterns.** `api/parent.py`'s `list_users` and
  `api/ui.py`'s `_user_summaries` (an almost-exact duplicate of the same
  logic) each run ~3 queries per user; `/sync`
  (`hub/timekpr_hub/api/sync.py`) runs roughly 8 queries per reported
  user. A join or `IN`-batch would collapse most of these.
- ~~**No indexes for the hot `(user_id, day)` lookups**~~ — **fixed** for
  `activity_intervals` and `grants` (migration `8f3c2a1e9b04`); also added
  the same for `usage_counters`, which had the same gap. `Base` still has
  no `MetaData(naming_convention=...)` (`hub/timekpr_hub/db/models.py:35`),
  so unrelated constraints remain unnamed — untouched here.
- **Agent gaps:** `post_events` (`hubclient.py:99`) posts to
  `/api/v1/events`, which the hub does not implement, and `enforcer.py`
  still reaches into `timekprAdminConnector`'s private
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
- **`deploy/docker-compose.yml` issues:** Caddy gets
  `env_file: .env`, which leaks `POSTGRES_PASSWORD` into a container that
  doesn't need it; the nightly `backup` service sleeps 24h before its
  *first* dump, so a fresh deployment has zero backups for a full day;
  neither `hub` nor `caddy` has a healthcheck or a `depends_on: condition:`
  on the hub.
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
- `AuditLog` and `Alert` tables are defined but never written to; there is
  no `logging` configuration anywhere under `hub/`.
- `hub/timekpr_hub/web/templates/index.html` loads htmx from a CDN with no
  Subresource Integrity hash.
- The version string `0.1.0` is duplicated across every package's
  `pyproject.toml`, `app.py:47`, `agent/timekpr_hub_agent/main.py:52`, and
  the PKGBUILD — nothing keeps them in sync.
- **No LICENSE file exists.** `agent/packaging/PKGBUILD` declares
  `license=('GPL3')`, and the agent imports timekpr-next's own (GPL-3)
  Python package at runtime — pick and add an actual `LICENSE` before
  distributing this anywhere.
- `scratchpad/phase0_spike.py` is untracked (no entry in `git ls-files`)
  despite `docs/phase0-findings.md` and `CHECKLIST.md` both referencing it
  as the artifact that produced their findings.
