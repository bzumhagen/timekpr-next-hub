# Best-practices review

A review of `timekpr-next-hub` against general Python/FastAPI/SQLAlchemy
practices, done at the point where `CHECKLIST.md` marks Phase 1 complete.
Findings are ranked by severity; each names a file and, where useful, a
line. Items under **Fixed in this change** were addressed alongside the
Makefile/README/uv-workspace work (see the top-level `git log` for the
commits); everything else is reported only — deliberately, so this review
doesn't quietly expand into an app-logic rewrite. Re-run `make check`
after picking any of these up, and update this file's status when you do.

---

## High — open

- **No parent authentication, and the Caddyfile is set up for a public TLS
  domain.** Every route in `hub/timekpr_hub/api/parent.py` and
  `api/ui.py` has no auth dependency — anyone who can reach the hub can
  grant screen time, approve devices, and mint enrollment codes. The
  `Parent`/`ParentSession` tables (`hub/timekpr_hub/db/models.py:48-67`)
  exist but nothing uses them. `CHECKLIST.md` already tracks this as
  deferred to Phase 2 — flagging here so it isn't missed before any
  deployment beyond a trusted LAN. **Do not point `deploy/Caddyfile`'s
  `HUB_DOMAIN` at a real public domain until this lands.**
- **A `pending` (unapproved) device can sync.** `hub/timekpr_hub/api/auth.py:35`
  only rejects `status == "revoked"`; nothing checks for `pending`. That
  contradicts the enrollment flow's own intent (`api/enroll.py:64` comment:
  "a parent still has to approve the device before it's usable") — in
  practice an unapproved device can immediately report usage for any user
  it auto-aliased. No test exercises this path.
- **The agent's offline grace timer uses `time.monotonic()` persisted
  across process restarts.** `agent/timekpr_hub_agent/state.py:31`
  (`last_hub_contact_monotonic: float`) and its use in
  `agent/timekpr_hub_agent/main.py:205-206`
  (`time.monotonic() - user_state.last_hub_contact_monotonic`) assume the
  clock's origin never changes. `time.monotonic()`'s epoch is arbitrary and
  resets on reboot, so after a restart this subtraction can be deeply
  negative — `seconds_since_contact` comes out negative, `in_grace` is
  `True` forever, and the agent silently stays in the "open" (unenforced)
  branch of `_apply_offline_policy` indefinitely while genuinely offline.
- **The agent computes its canonical day/rollover in UTC, not
  `HUB_TZ`.** `agent/timekpr_hub_agent/main.py:57` uses
  `datetime.now(timezone.utc)`, and `_canonical_day_str` (line 42) takes
  `.date()` of that. The hub's own day boundary is timezone-aware
  (`core/timekpr_hub_core/calendar.py`, `HUB_TZ`), but the agent's forced
  `'='` write on day rollover (line 71-78) fires at UTC midnight regardless
  of the household's configured timezone. `--tz` is accepted
  (`main.py:233`) but only forwarded to the hub, never used locally for
  this calculation.

## Medium — open

- **Unvalidated input reaches the database and can 500.**
  `EnrollRequest` (`core/timekpr_hub_core/models.py:45-52`) has no
  `max_length` on any field, while the backing columns are bounded
  (`name`/`hostname` `String(128)`, `machine_id` `String(64)`,
  `agent_version` `String(32)`, `tz` `String(64)` — see
  `hub/timekpr_hub/db/models.py`); an oversized value raises an unhandled
  asyncpg `StringDataRightTruncationError`. `GrantCreate.seconds`
  (`models.py:191`) has no bounds either, and `hub/timekpr_hub/api/ui.py`'s
  form field is a bare `int`. `hub/timekpr_hub/api/sync.py:112` calls
  `datetime.fromisoformat()` on raw strings with no `start < end` check.
- **Enrollment-code and first-policy races.** `api/enroll.py:35-45` reads
  the code row with a plain `SELECT`, not `SELECT ... FOR UPDATE` or a
  conditional `UPDATE ... WHERE used_at IS NULL`; two concurrent enrolls
  with the same code can both pass the check. Similarly,
  `hub/timekpr_hub/services/policy.py:48-61`'s `create_initial_policy` can
  be raced by two devices syncing a brand-new user for the first time,
  raising on `uq_policies_user_version`. Neither path has rate limiting.
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
- **No indexes for the hot `(user_id, day)` lookups** on
  `activity_intervals` or `grants` (see
  `hub/migrations/versions/fafdbdc4fd31_initial_schema.py`). `Base` has no
  `MetaData(naming_convention=...)`
  (`hub/timekpr_hub/db/models.py:35`), so constraints in that migration
  are unnamed, which makes future Alembic autogenerate diffs noisier.
- **Agent gaps:** no `enroll` CLI subcommand (the PKGBUILD's
  `agent.env` template implies one exists), no `--ca-cert`/token-path
  flags on `main.py`'s `argparse` parser even though
  `agent/timekpr_hub_agent/hubclient.py`'s `HubClientConfig` supports both,
  `post_events` (`hubclient.py:99`) posts to `/api/v1/events`, which the
  hub does not implement, and `enforcer.py:57` reaches into
  `timekprAdminConnector`'s private `_timekprUserAdminDbusInterface`
  attribute.
- **`agent/packaging/PKGBUILD` issues:** the quoted glob
  `"$venv_dir/lib/python3*/site-packages"` (line 27) is never expanded by
  the shell and creates a literal directory named `python3*`; `pip
  install` inside `package()` hits the network unpinned; `source=()` is
  empty; `python-virtualenv` is an unused makedepend. It has never
  actually been run through `makepkg` (`CHECKLIST.md` says as much).
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

- Four parent-API endpoints (`create_grant`, `create_enrollment_code`,
  `list_devices`, `approve_device` in `api/parent.py`) and `healthz`
  (`app.py:23`) return bare `dict`s with no `response_model`.
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
  `pyproject.toml`, `app.py:14`, `agent/timekpr_hub_agent/main.py:252`, and
  the PKGBUILD — nothing keeps them in sync.
- **No LICENSE file exists.** `agent/packaging/PKGBUILD` declares
  `license=('GPL3')`, and the agent imports timekpr-next's own (GPL-3)
  Python package at runtime — pick and add an actual `LICENSE` before
  distributing this anywhere.
- `scratchpad/phase0_spike.py` is untracked (no entry in `git ls-files`)
  despite `docs/phase0-findings.md` and `CHECKLIST.md` both referencing it
  as the artifact that produced their findings.

## Fixed in this change

- **PYTHONPATH hacks replaced with a real uv workspace.** `core/`, `hub/`,
  and `agent/` are now installable packages (each with its own
  `pyproject.toml`), wired together via `[tool.uv.workspace]` at the repo
  root. `pytest`'s `pythonpath` setting and `hub/migrations/env.py`'s
  `sys.path.insert` hack are both gone — see `README.md` and the
  `Makefile` for how to invoke things instead.
- **`requirements-dev.txt` / `hub/requirements.txt` (raw `pip freeze`
  output) replaced** by per-package `dependencies` plus a root
  `[dependency-groups] dev` in `pyproject.toml`, resolved into a committed
  `uv.lock`.
- **Lint/type tooling actually wired up**: `ruff` (format + a real lint
  rule set: `E`, `F`, `I`, `UP`, `B`) and `mypy` are dev dependencies and
  run via `make lint`/`make fmt`/`make typecheck`; both are clean as of
  this change (`git grep` the `per-file-ignores` in `pyproject.toml` for
  the handful of deliberate, commented exceptions — FastAPI's
  `Depends(...)` default-argument idiom, and two `str`+`Enum` classes left
  alone because their wire-format behavior differs from `enum.StrEnum`
  across Python versions).
- **Test-database safety.** The integration tests used to `TRUNCATE`
  whatever `$DATABASE_URL` pointed at — which defaulted to the same
  database `make dev` uses. `tests/dbutil.py`'s `require_db()` now refuses
  to run any `db`-marked test unless the target database name ends in
  `_test`, and DB-backed tests are marked `pytest.mark.db` so a plain
  `pytest`/`make test` run skips them instead of silently touching
  whatever `DATABASE_URL` happens to be set to.
- **A real dev/test Postgres** (`deploy/compose.dev.yml`,
  `deploy/dev-initdb.sql`) gives `make dev` and `make test-db`/`test-all`
  separate databases (`timekpr_hub_dev` / `timekpr_hub_test`) on a
  loopback-only port, distinct from the production
  `deploy/docker-compose.yml` stack.
- **`hub/Dockerfile` rewritten as a multi-stage uv build**: only the
  `timekpr-hub` package and its own dependencies are installed (not the
  dev group, not the agent's httpx/dbus footprint); the final stage runs
  as a non-root `hub` user; the `CMD` execs `uvicorn` after migrations so
  it receives `SIGTERM` directly instead of leaving `sh` as an
  unreachable PID 1. A `.dockerignore` was added alongside it.
- **Hygiene**: `.gitignore` now covers `.ruff_cache/`, `.mypy_cache/`,
  `dist/`, `build/`, coverage output; the stray empty
  `hub/timekpr_hub/migrations/` directory (a duplicate of
  `hub/migrations/`, never used) was removed.

---

## Verified live during this review

- `make check` (ruff + mypy + non-DB tests): clean.
- `make test-all` against a real Postgres: **47 passed, 0 skipped**.
- `tests/dbutil.py`'s guard: confirmed it hard-fails (not skips) when
  pointed at `timekpr_hub_dev` instead of a `_test` database.
- `make build`: three wheels built; unzipped `timekpr_hub-*.whl` and
  confirmed `web/templates/*.html` ship inside it.
- The hub image: built, run against the dev Postgres, confirmed it runs
  as the non-root `hub` user, applies migrations, and serves `/healthz`
  and `/`.
- The full enroll → approve → sync → grant flow, live against `make dev`:
  a `+30 min` grant moved a seeded user's effective daily limit from 3600s
  to 5400s, and the change was visible both via the parent API and the
  actual `/ui/users-fragment` HTML the browser renders.
- `deploy/docker-compose.yml` (the production stack) was **not**
  re-verified here — rootless podman on this machine can't bind ports
  80/443, the same limitation `CHECKLIST.md` already notes.
