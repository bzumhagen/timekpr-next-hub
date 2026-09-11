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
code and approve devices — there's no separate "create a user" step:
enrolling a device provisions a hub `User` row for each of its
`local_users` that the hub doesn't already know about, and aliases into
the existing one for any that it does (so the same account on a second
machine pools into the same user rather than getting a duplicate). A
parent still has to approve the device afterward before it can sync.

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

The agent's own code needs only `httpx` beyond the standard library — it
never imports `pydantic` itself — plus the distro's system `dbus`/`PyGObject`
packages to talk to `timekprd`. No venv: the Arch package installs straight
into system site-packages under a Python-version-independent path
(`/usr/lib/timekpr-hub-agent/`), depending on `python-httpx`/`python-pydantic`
directly, so an Arch Python minor upgrade needs no rebuild.

**Via the PKGBUILD** (`agent/packaging/PKGBUILD`, Arch/CachyOS). Builds
directly from this checkout (via `$startdir/../..`, since there's no
release tarball yet) rather than a `source=()` archive — run it from
`agent/packaging/`:

```sh
cd agent/packaging && makepkg -f      # builds timekpr-hub-agent-*.pkg.tar.zst
sudo pacman -U timekpr-hub-agent-*.pkg.tar.zst
```

pacman's own systemd hooks handle `sysusers`/`tmpfiles`/`daemon-reload`
automatically — nothing else to run by hand. Verified in this repo:
`makepkg -f` produces a package with `usr/bin/timekpr-hub-agent`, the code
under `usr/lib/timekpr-hub-agent/` (no `python3.*` path anywhere in it —
confirmed with `tar -tf *.pkg.tar.zst | grep python3\\.`), the systemd
unit, and `sysusers.d`/`tmpfiles.d`/`agent.env`. **Not yet verified**: an
actual `pacman -U` + real enrollment on this machine (would create a
system user and a running service — left for you to do, not run
unattended during this change).

**Manual install** (any systemd distro with `python-dbus`/`python-gobject`
and `httpx` available):

```sh
sudo mkdir -p /usr/lib/timekpr-hub-agent
sudo cp -r agent/timekpr_hub_agent core/timekpr_hub_core /usr/lib/timekpr-hub-agent/
sudo install -Dm755 agent/packaging/timekpr-hub-agent /usr/bin/timekpr-hub-agent
# then install the systemd unit + sysusers/tmpfiles config from
# agent/packaging/ by hand, and run systemd-sysusers/systemd-tmpfiles/daemon-reload
```

### Enrolling a device

Click **Generate enrollment code** in the hub UI — it prints the exact
command to run, with the hub's own URL and the code already filled in:

```sh
sudo timekpr-hub-agent enroll --hub-url http://<hub>:8000 --code K7F29Q
```

This preflights the local timekpr install, prompts for which local users
to manage if `--users` isn't given (validating each against timekpr's own
user list), redeems the code, writes the device token (`0600`, owned by
the service's own user — safe to re-run against a device that's already
been running for a while), writes `/etc/timekpr-hub-agent/agent.env`, and
enables + starts the service. A parent-minted code is itself the
approval — there's no separate "approve this device" step to do
afterward. Pass `--no-start` to skip the last step, or `--ca-cert` for a
hub with a self-signed certificate.

Check `sudo timekpr-hub-agent status` afterward — one ✓/✗ line per link in
the chain (timekpr installed, DBUS reachable, config present, token
readable, service enabled/active, hub reachable), plus each managed
user's last sync and current balance.

## Surviving reboots & upgrades

- The service is `Restart=always` with no backoff limit, `Type=notify` +
  a watchdog (a hung DBUS/HTTP call gets killed and restarted rather than
  sitting there looking alive), and deliberately has no `After=`/`Requires=`
  ordering against `timekprd` or `dbus` — it retries its own DBUS
  connection every tick, so starting in any order is fine. `enroll` runs
  `systemctl enable --now`, so a fresh enrollment survives a reboot without
  a separate step.
- Its offline-grace clock is wall-clock, not `time.monotonic()` (whose
  epoch resets on reboot) — so a reboot while the hub is unreachable can't
  make the agent think it's still "recently in contact" and stay
  unenforced.
- `state.json` is written atomically (temp file + fsync + rename + fsync
  the directory) and tolerates unknown/missing fields, so an interrupted
  write or a downgrade/upgrade across a state-schema change can't
  crash-loop the service.
- The one thing this can't guarantee: anyone with `sudo` can stop the
  service or uninstall the package. Check the hub UI's device list (it
  flags a device that hasn't checked in recently) if you suspect that's
  happened.

## Configuration reference

**Hub** (environment variables):

| Variable | Default | Notes |
|---|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://timekpr_hub:timekpr_hub@localhost:5432/timekpr_hub` | Set explicitly in any real deployment |
| `HUB_TZ` | `UTC` | The one household timezone every day/week/month boundary is computed in |

**Agent** (`timekpr-hub-agent` CLI flags / `agent.env`, written by `enroll` —
see "Enrolling a device" above): three subcommands, `enroll`, `run`, and
`status`. `enroll` and `run` both take `--hub-url`, `--token-path` (default
`/var/lib/timekpr-hub-agent/device_token`) and `--ca-cert`; a flag always
overrides the matching env var, which overrides `agent.env`.

| Flag | env var (via `agent.env`) | Notes |
|---|---|---|
| `run`/`enroll --hub-url` | `TIMEKPR_HUB_URL` | Required unless already set via env/`agent.env` |
| `run --users` | `TIMEKPR_HUB_MANAGED_USERS` | Comma-separated local usernames |
| `run`/`enroll --ca-cert` | `TIMEKPR_HUB_CA_CERT` | For a hub with a self-signed certificate |
| `run --tz` | `TIMEKPR_HUB_TZ` | Overwritten by the hub's `HUB_TZ` on the next successful sync |
| `run --state-path` / `status --state-path` | | Default `/var/lib/timekpr-hub-agent/state.json` |
| `run --once` | | Run a single tick and exit, for testing |
| `enroll --code` (required) | | One-time enrollment code from the hub |
| `enroll --users` | | Comma-separated; prompted interactively (from timekpr's own user list) if omitted |
| `enroll --no-start` | | Don't `systemctl enable --now` after enrolling |
| `enroll --hostname` / `--machine-id` / `--os` | | Default to the local machine's own values |

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
