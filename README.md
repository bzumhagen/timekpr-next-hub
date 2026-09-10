# timekpr-next-hub

A self-hosted hub that pools screen-time budgets for
[timekpr-next](https://github.com/mjasnik/timekpr-next) across multiple
machines in a household. A small agent runs alongside `timekprd` on each
machine, reporting usage to the hub and applying whatever balance the hub
says is left — so a kid's daily limit is shared across their desktop and
laptop instead of being tracked separately on each.

> **⚠ Status: Phase 1 (MVP), pooled daily budget only.** There is **no
> parent authentication yet** — every parent-facing endpoint and the web UI
> are open to anyone who can reach the hub. Keep this on a trusted LAN (or
> behind your own VPN/Tailscale) until that lands; do not point
> `deploy/Caddyfile` at a public domain in the meantime. See
> [`docs/best-practices-review.md`](docs/best-practices-review.md) for
> this and every other known gap, and [`CHECKLIST.md`](CHECKLIST.md) for
> what's built vs. planned.

## Repo layout

```
core/    timekpr_hub_core  -- pure, IO-free logic shared by hub + agent
                              (convergence math, calendar rules, wire types)
hub/     timekpr_hub        -- FastAPI app: Postgres-backed API + a small
                              server-rendered (Jinja2 + htmx) parent UI
agent/   timekpr_hub_agent  -- systemd sidecar: talks to the local timekprd
                              over DBUS, syncs with the hub over HTTP
deploy/                     -- docker-compose (Postgres + hub + Caddy +
                              nightly backups) for a real deployment, plus
                              a throwaway dev/test Postgres compose file
tests/                      -- unit, integration (some DB-backed), e2e
docs/                       -- phase 0/agent findings, this review
```

`core` has no dependency on `hub` or `agent`; `hub` and `agent` both depend
on `core`, so the convergence math and wire contract are written once.

## Prerequisites

- [`uv`](https://docs.astral.sh/uv/) (`sudo pacman -S uv` on Arch/CachyOS;
  see uv's docs for other platforms) — manages the Python toolchain and
  dependencies, no system Python version required (uv provisions its own).
- `make`
- Docker or Podman with `compose` support (used both for the dev database
  and for the production stack)
- To actually run the agent against a real daemon: `timekpr-next` itself,
  installed and running (`timekprd`), and membership in the `timekpr`
  group

## Quickstart

```sh
make install       # uv sync's the workspace (core + hub + agent + dev deps)
make test          # unit/property/simulation tests -- no services needed
make db-up         # start a throwaway dev/test Postgres on 127.0.0.1:55432
make dev           # apply migrations, run the hub with --reload on :8000
```

Then open <http://127.0.0.1:8000>. The UI lets you generate an enrollment
code and approve devices, but there's no way to create a *user* from the
UI yet (Phase 1 gap — see below) — insert one directly to try the full
flow:

```sh
make db-shell
# inside psql:
INSERT INTO users (id, canonical_username, display_name)
VALUES (gen_random_uuid(), 'alice', 'Alice');
```

## Make targets

Run `make help` for the authoritative, current list. Grouped summary:

| | |
|---|---|
| `install` | Sync the uv workspace venv |
| `lock` / `upgrade` | Re-lock dependencies (respecting pins / upgrading everything) |
| `fmt` / `lint` | Auto-fix / check formatting and lint rules (ruff) |
| `typecheck` | mypy across `core`, `hub`, `agent` |
| `test` | Tests needing no services (unit, property, `FakeTimekprDaemon` simulation) |
| `test-db` | Only the Postgres-backed tests (starts + migrates the dev DB) |
| `test-all` | The full suite in one run, DB required — this is the CI gate |
| `check` | `lint` + `typecheck` + `test` |
| `db-up` / `db-down` / `db-shell` | Manage the local dev/test Postgres |
| `migrate` / `migrate-test` | Apply Alembic migrations to the dev / test database |
| `revision MSG="..."` | Autogenerate a new migration |
| `dev` | Run the hub locally with auto-reload against the dev database |
| `build` | Build wheels for all three packages + the hub's container image |
| `deploy-up` / `deploy-down` / `deploy-logs` | Manage the production stack |
| `clean` / `distclean` | Remove caches / caches + the venv |

There is deliberately **no `make run-agent`**: the agent needs the
system's `dbus`/`PyGObject` bindings (see "Installing the agent" below),
which a plain uv venv doesn't have. The agent's logic is exercised in
`make test` via `FakeTimekprDaemon`, a model of timekpr's own accounting
semantics validated against the real daemon
(`docs/phase0-findings.md`, `tests/integration/test_fake_timekpr_parity.py`).

## Testing

Tests are layered (see `CHECKLIST.md` "Verification" for the full
rationale):

1. **Unit / property tests** (`tests/unit/`) — Hypothesis property tests on
   the pure convergence controller, calendar boundaries, and wall-clock
   interval union. No services needed.
2. **Fake-daemon integration** (`tests/integration/test_fake_timekpr_parity.py`,
   `test_multi_device_simulation.py`) — `FakeTimekprDaemon` pinned to
   real-daemon behavior observed in `docs/phase0-findings.md`, plus a
   multi-device/multi-day simulation. No services needed.
3. **Postgres-backed tests** (`test_aggregate_postgres.py`,
   `test_hub_api.py`) — marked `@pytest.mark.db`, need a real database.

`make test` runs only 1+2 (`pytest -m "not db"`). `make test-db` or
`make test-all` bring up the dev Postgres, apply migrations to the
**separate** `timekpr_hub_test` database, and run the DB-backed tests
against it with `TIMEKPR_HUB_REQUIRE_DB=1` — so a missing database is a
hard failure, not a silently-skipped test.

**Why a separate test database at all:** these tests `TRUNCATE` every
table they touch between tests. `tests/dbutil.py`'s `require_db()` refuses
to run against any `$TEST_DATABASE_URL` whose database name doesn't end in
`_test`, specifically so a `make test-db` run can never be pointed at
`make dev`'s data (or, worse, a production database) by an environment
variable left over from another terminal.

## Migrations

Alembic lives under `hub/migrations/`, config at `hub/alembic.ini`. Always
go through the Makefile (`make migrate`, `make migrate-test`,
`make revision MSG="..."`) rather than calling `alembic` directly — they
set `DATABASE_URL` to the right one of the two dev-compose databases and
run inside the uv-managed venv where `timekpr_hub` is actually importable.

## Deploying the hub

The production stack (`deploy/docker-compose.yml`) is Postgres (internal
only) + the hub + Caddy (the only container exposed to the network) + a
nightly `pg_dump` backup job.

```sh
cp deploy/.env.example deploy/.env   # fill in a real POSTGRES_PASSWORD, HUB_TZ, HUB_DOMAIN
make deploy-up
make deploy-logs                     # tail everything
```

- **`HUB_DOMAIN`**: a real subdomain you control gets automatic HTTPS via
  Caddy + Let's Encrypt DNS-01 (see `deploy/Caddyfile` for the DNS
  provider plugin note — Caddy's default build only does HTTP-01/TLS-ALPN,
  so DNS-01 needs an `xcaddy` build or a provider-specific image). Leave it
  as `localhost`, or use the commented `:80` block in the Caddyfile, for a
  LAN-only deployment with no public DNS and no TLS.
- **Given the "no parent auth" status above**: a LAN-only or
  VPN/Tailscale-gated deployment is the only currently-safe option.
- Backups land in `deploy/backups/` as nightly `pg_dump -Fc` dumps, kept
  14 days. This has not been exercised end-to-end in this environment —
  rootless Podman can't bind privileged ports 80/443, so the
  Caddy-fronted path is unverified here (`CHECKLIST.md` notes the same);
  the Dockerfile build and hub+Postgres path *have* been verified (see
  `docs/best-practices-review.md`, "Verified live during this review").

## Installing the agent

The agent needs the distro's system `dbus`/`PyGObject` packages (to talk
to `timekprd`) *and* `httpx`/`pydantic` (pip) in the same interpreter —
hence a `--system-site-packages` venv rather than a plain one.

**Via the PKGBUILD** (`agent/packaging/PKGBUILD`, Arch/CachyOS): this is a
solid draft — it builds the `--system-site-packages` venv, installs the
systemd unit, `sysusers.d`/`tmpfiles.d` config, and the `agent.env`
template — but **it has not actually been run through `makepkg`** yet.
Treat it as a starting point, not a tested package.

**Manual install**, the path that has been validated end-to-end
(`docs/agent-live-test-findings.md`):

```sh
sudo python3 -m venv --system-site-packages /opt/timekpr-hub-agent/venv
sudo /opt/timekpr-hub-agent/venv/bin/pip install httpx pydantic pydantic-core
# then copy agent/timekpr_hub_agent/ and core/timekpr_hub_core/ into that
# venv's site-packages, and install the systemd unit + sysusers/tmpfiles
# config from agent/packaging/ by hand (see PKGBUILD's package() for the
# exact paths)
```

**Enrolling a device**: there is no `enroll` CLI subcommand yet (a real
gap — see the review). Enroll by hand against the hub's HTTP API, then
drop the returned token where the agent expects it:

```sh
CODE=$(curl -s -X POST http://<hub>/api/v1/enrollment-codes | jq -r .code)
curl -s -X POST http://<hub>/api/v1/enroll -H 'Content-Type: application/json' -d '{
  "enrollment_code": "'"$CODE"'", "hostname": "'"$(hostname)"'",
  "machine_id": "'"$(cat /etc/machine-id)"'", "os": "linux", "tz": "America/Denver",
  "agent_version": "0.1.0", "local_users": ["alice"]
}' | tee /tmp/enroll.json
sudo mkdir -p /var/lib/timekpr-hub-agent
sudo sh -c 'jq -r .device_token /tmp/enroll.json > /var/lib/timekpr-hub-agent/device_token'
sudo chmod 600 /var/lib/timekpr-hub-agent/device_token
# then have a parent approve it: POST /api/v1/devices/{device_id}/approve
```

Then start the systemd unit (`agent/packaging/timekpr-hub-agent.service`),
configured via `/etc/timekpr-hub-agent/agent.env`
(`TIMEKPR_HUB_URL`, `TIMEKPR_HUB_MANAGED_USERS`, `TIMEKPR_HUB_TZ`).

## Configuration reference

**Hub** (environment variables):

| Variable | Default | Notes |
|---|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://timekpr_hub:timekpr_hub@localhost:5432/timekpr_hub` | Set explicitly in any real deployment |
| `HUB_TZ` | `UTC` | The one household timezone every day/week/month boundary is computed in |

**Agent** (`timekpr-hub-agent` CLI flags / `agent.env`):

| Flag | env var (via `agent.env`) | Notes |
|---|---|---|
| `--hub-url` (required) | `TIMEKPR_HUB_URL` | |
| `--users` (required) | `TIMEKPR_HUB_MANAGED_USERS` | Comma-separated local usernames |
| `--tz` | `TIMEKPR_HUB_TZ` | Default `UTC` — see the review's note that this isn't yet used for the agent's own day-rollover math |
| `--state-path` | | Default `/var/lib/timekpr-hub-agent/state.json` |
| `--once` | | Run a single tick and exit, for testing |

## Further reading

- [`docs/best-practices-review.md`](docs/best-practices-review.md) — the
  full findings list this README's status warnings are drawn from.
- [`docs/phase0-findings.md`](docs/phase0-findings.md) — empirical
  findings about the real `timekprd`'s DBUS behavior, from before any code
  was written against it.
- [`docs/agent-live-test-findings.md`](docs/agent-live-test-findings.md) —
  two real bugs found only by running the agent against a live daemon,
  after all synthetic tests were already green.
- [`CHECKLIST.md`](CHECKLIST.md) — execution status against the original
  design plan, phase by phase.
