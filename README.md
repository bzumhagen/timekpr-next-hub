# timekpr-next-hub

A self-hosted hub that pools screen-time budgets for
[timekpr-next](https://github.com/mjasnik/timekpr-next) across multiple
machines in a household. A small agent runs alongside `timekprd` on each
machine, reporting usage to the hub and applying whatever balance the hub
says is left — so a kid's daily limit is shared across their desktop and
laptop instead of being tracked separately on each.

**Status: Phase 1 (MVP), pooled daily budget only.** Week/month pooling,
drift detection, and a few other planned features aren't built yet — see
[`CHECKLIST.md`](CHECKLIST.md) for what's built vs. planned, and
[`docs/best-practices-review.md`](docs/best-practices-review.md) for known
gaps.

This README has two parts: [setting up your hub](#setting-up-your-hub) if
you just want to run it, and [developing on the hub](#developing-on-the-hub)
if you're contributing code.

---

# Setting up your hub

## What you need

- A machine that stays on (a home server, a mini PC, a Raspberry Pi) to run
  the hub itself.
- Docker or Podman with `compose` support, on that machine.
- On each of your kid's machines: `timekpr-next` already installed and
  running, and a systemd-based Linux distro (Arch/CachyOS today; see
  [multi-distro support](#multi-distro-support-not-started) for others).

## 1. Start the hub

On the machine that will run the hub:

```sh
cp deploy/.env.example deploy/.env   # fill in a real POSTGRES_PASSWORD, HUB_TZ, HUB_DOMAIN
make deploy-up
```

`HUB_DOMAIN`: a real subdomain you control gets automatic HTTPS via Caddy +
Let's Encrypt. Leave it as `localhost`, or use the commented `:80` block in
`deploy/Caddyfile`, for a LAN-only deployment with no public DNS and no TLS.
A LAN-only or VPN/Tailscale-gated deployment is recommended — see step 2.

`make deploy-logs` tails everything if you want to watch it come up.

## 2. Claim the hub — do this immediately

Open the hub in a browser. The **first thing** it will ask you to do is
create a parent account at `/setup`. Do this right away: until an account
exists, anyone who can reach the hub can create the first (and only)
account themselves. Don't expose the hub to anything beyond your LAN/VPN
until you've claimed it.

## 3. Add a kid's machine

Install the agent package on the kid's machine (see
[Installing the agent](#installing-the-agent) below for other install
options):

```sh
cd agent/packaging && makepkg -f
sudo pacman -U timekpr-hub-agent-*.pkg.tar.zst
```

In the hub's web UI, open **Devices** and click **Generate enrollment
code** — it shows you the exact command to run, with the hub's URL and the
code already filled in:

```sh
sudo timekpr-hub-agent enroll --hub-url http://<hub>:8000 --code K7F29Q
```

Or just run `sudo timekpr-hub-agent enroll` with no arguments and it will
prompt you for the hub URL, the code, and which local user accounts to
manage. A parent-minted code is itself the approval — there's no separate
"approve this device" step. This also starts the background service, so it
survives reboots on its own.

Confirm it worked:

```sh
sudo timekpr-hub-agent status
```

This prints one ✓/✗ line per step (timekpr installed, connected to
timekpr, config present, service running, hub reachable), plus each managed
user's last sync and current balance.

Repeat for every machine you want pooled into the same limit — enrolling a
second machine under the same local username automatically pools it into
the same hub user, no extra step needed.

## 4. Daily use

The hub's home page shows, per kid: today's usage bar and an activity badge
(draining / idle / offline). Device sync status and enrollment live on their
own **Devices** page (linked from the top of the dashboard), out of the way
until you actually need them.

- **Give extra time today**: use the **+30 min** / **-30 min** buttons on
  the dashboard.
- **Manage the full policy**: click **Edit policy** on a kid's card for the
  basic editor -- per-day limits and an hours grid for time-of-day windows.
  Everything else timekpr can do (weekly/monthly caps, which days login is
  allowed at all, lockout behavior, PlayTime) is one click away behind
  **Advanced policy settings** on the same page.
- **See where the time went**: click **Usage stats** for a day-by-day
  spend-vs-limit history and a per-device breakdown.
- **"No time until approval is given"**: on the kid's **Settings** page, check
  which days are approval-gated (e.g. weekends). On a gated day the dashboard
  shows a **⏸ Time now released** badge; click **Release today** once they're
  done and the day's normal limit applies immediately — no policy edit, no
  device restart. The gate re-arms on its own the next time that weekday
  comes around.
- **"You lose your time tomorrow"** (or any other date): use **Adjust a
  day** on the dashboard — pick a date and either "no time at all" or a
  specific limit. This replaces that day's limit outright rather than
  editing the standing policy, so it can't be undone by accident when you
  later change the regular schedule.
- **Manage a device**: on the **Devices** page, revoke it (stops hub
  management, reversible) or delete it (erases its history). Revoking does
  **not** lock the child out — it hands the machine back to local
  self-management at whatever limit it already had, exactly as if it had
  never been enrolled. If you actually want the machine locked, set its
  policy to 0 minutes/day (or grant negative time) before revoking, or
  just don't re-enroll it once you're done.

## 5. Troubleshooting

- **A device shows no recent activity / usage looks stuck.** Check
  `sudo timekpr-hub-agent status` on that machine — it will show whether
  the hub is reachable and when it last synced.
- **Time isn't converging the way you expect.** Confirm the child's account
  is actually enrolled (`status` lists managed users) and that the device
  hasn't been revoked in the hub UI.
- **Reinstalling the agent, or a fresh OS install on the same machine.**
  Just re-run `enroll` — it detects the existing device by machine ID and
  rebinds to it (prints `↻ re-bound to existing device …`), keeping its
  history. If you want a clean slate instead, revoke or delete the old
  device in the hub UI first.
- **Backups.** Nightly `pg_dump` dumps land in `deploy/backups/`, kept 14
  days.

---

# Developing on the hub

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
on `core`.

## Prerequisites

- [`uv`](https://docs.astral.sh/uv/) (`sudo pacman -S uv` on Arch/CachyOS;
  see uv's docs for other platforms)
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

Then open <http://127.0.0.1:8000>.

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

There is no `make run-agent`: the agent needs the system's `dbus`/`PyGObject`
bindings, which a plain uv venv doesn't have. Its logic is exercised in
`make test` via `FakeTimekprDaemon`, a model of timekpr's own accounting
semantics (`agent/timekpr_hub_agent/fake_timekpr.py`).

## Testing

Tests are layered:

1. **Unit / property tests** (`tests/unit/`) — Hypothesis property tests on
   the pure convergence controller, calendar boundaries, and wall-clock
   interval union. No services needed.
2. **Fake-daemon integration** (`tests/integration/test_fake_timekpr_parity.py`,
   `test_multi_device_simulation.py`) — `FakeTimekprDaemon`, plus a
   multi-device/multi-day simulation. No services needed.
3. **Postgres-backed tests** (`test_aggregate_postgres.py`,
   `test_hub_api.py`) — marked `@pytest.mark.db`, need a real database.
4. **End-to-end** (`tests/e2e/`) — the real agent tick loop + real
   convergence math + a real hub + real Postgres, all in one process.

`make test` runs 1+2 (`pytest -m "not db"`). `make test-db` runs only the
DB-backed tests; `make test-all` runs everything. Both bring up the dev
Postgres first, apply migrations to the **separate** `timekpr_hub_test`
database (these tests `TRUNCATE` every table they touch between tests), and
run with `TIMEKPR_HUB_REQUIRE_DB=1` so a missing database is a hard failure
rather than a silently-skipped test. `tests/dbutil.py`'s `require_db()`
refuses to run against any `$TEST_DATABASE_URL` whose database name doesn't
end in `_test`.

## Migrations

Alembic lives under `hub/migrations/`, config at `hub/alembic.ini`. Always
go through the Makefile (`make migrate`, `make migrate-test`,
`make revision MSG="..."`) — they set `DATABASE_URL` to the right one of
the two dev-compose databases and run inside the uv-managed venv.

## Deploying the hub

The production stack (`deploy/docker-compose.yml`) is Postgres (internal
only) + the hub + Caddy (the only container exposed to the network) + a
nightly `pg_dump` backup job.

```sh
cp deploy/.env.example deploy/.env   # fill in a real POSTGRES_PASSWORD, HUB_TZ, HUB_DOMAIN
make deploy-up
make deploy-logs                     # tail everything
```

`HUB_DOMAIN`: a real subdomain you control gets automatic HTTPS via Caddy +
Let's Encrypt DNS-01 (see `deploy/Caddyfile` for the DNS provider plugin
note — Caddy's default build only does HTTP-01/TLS-ALPN, so DNS-01 needs an
`xcaddy` build or a provider-specific image). Leave it as `localhost`, or
use the commented `:80` block in the Caddyfile, for a LAN-only deployment
with no public DNS and no TLS.

Backups land in `deploy/backups/` as nightly `pg_dump -Fc` dumps, kept 14
days.

## Installing the agent

The agent's own code is stdlib-only beyond `pydantic` (it uses
`urllib.request`, not a third-party HTTP client), plus the distro's system
`dbus`/`PyGObject` packages to talk to `timekprd`. No venv: the Arch package
installs straight into system site-packages under a Python-version-
independent path (`/usr/lib/timekpr-hub-agent/`).

**Via the PKGBUILD** (`agent/packaging/PKGBUILD`, Arch/CachyOS). Builds
directly from this checkout (via `$startdir/../..`) rather than a
`source=()` archive — run it from `agent/packaging/`:

```sh
cd agent/packaging && makepkg -f      # builds timekpr-hub-agent-*.pkg.tar.zst
sudo pacman -U timekpr-hub-agent-*.pkg.tar.zst
```

pacman's own systemd hooks handle `sysusers`/`tmpfiles`/`daemon-reload`
automatically — nothing else to run by hand.

**Manual install** (any systemd distro with `python-dbus`/`python-gobject`
available):

```sh
sudo mkdir -p /usr/lib/timekpr-hub-agent
sudo cp -r agent/timekpr_hub_agent core/timekpr_hub_core /usr/lib/timekpr-hub-agent/
sudo install -Dm755 agent/packaging/timekpr-hub-agent /usr/bin/timekpr-hub-agent
# then install the systemd unit + sysusers/tmpfiles config from
# agent/packaging/ by hand, and run systemd-sysusers/systemd-tmpfiles/daemon-reload
```

### Enrolling a device

```sh
sudo timekpr-hub-agent enroll --hub-url http://<hub>:8000 --code K7F29Q
```

Or run it bare (`sudo timekpr-hub-agent enroll`) and it prompts for
anything you didn't pass: the hub URL (a scheme-less value like
`hub.local:8000` is fine — `http://` is assumed), the enrollment code, and
which local users to manage (from timekpr's own user list). Flags always
take precedence, so scripted/unattended enrollment is unaffected.

This preflights the local timekpr install, redeems the code, writes the
device token (`0600`, owned by the service's own user), writes
`/etc/timekpr-hub-agent/agent.env`, and enables + starts the service. Pass
`--no-start` to skip the last step, or `--ca-cert` for a hub with a
self-signed certificate.

Re-enrolling from the same machine is detected by `machine_id` and
**rebinds** to the existing device row instead of creating a duplicate —
that device's history (past usage, activity) is preserved. Revoke or delete
the old device in the hub UI first for a clean slate instead.

`sudo timekpr-hub-agent status` reports one ✓/✗ line per link in the chain
(timekpr installed, DBUS reachable, config present, token readable, service
enabled/active, hub reachable), plus each managed user's last sync and
current balance.

## Surviving reboots & upgrades

- The service is `Restart=always` with no backoff limit, `Type=notify` +
  a watchdog, and has no `After=`/`Requires=` ordering against `timekprd`
  or `dbus` — it retries its own DBUS connection every tick. `enroll` runs
  `systemctl enable --now`, so a fresh enrollment survives a reboot without
  a separate step, and a re-enroll always picks up its new token
  immediately.
- Its offline-grace clock is wall-clock (`last_hub_contact_utc`), not
  `time.monotonic()` — a reboot while the hub is unreachable can't reset
  that clock and leave the agent silently unenforced.
- `state.json` is written atomically (temp file + fsync + rename + fsync
  the directory) and tolerates unknown/missing fields.
- The one thing this can't guarantee: anyone with `sudo` on the kid's
  machine can stop the service or uninstall the package. The hub UI flags a
  device that hasn't checked in recently.

## Multi-distro support (not started)

timekpr-next itself supports Ubuntu & derivatives (PPA), Debian (native),
Arch/Manjaro (AUR), Fedora (COPR), and openSUSE (native). Only the Arch
PKGBUILD exists today; see `CHECKLIST.md`'s "Multi-distro support" section
for the plan.

## Configuration reference

**Hub** (environment variables):

| Variable | Default | Notes |
|---|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://timekpr_hub:timekpr_hub@localhost:5432/timekpr_hub` | Set explicitly in any real deployment |
| `HUB_TZ` | `UTC` | The one household timezone every day/week/month boundary is computed in |

**Agent** (`timekpr-hub-agent` CLI flags / `agent.env`): three subcommands,
`enroll`, `run`, and `status`.

| Flag | env var (via `agent.env`) | Notes |
|---|---|---|
| `run --hub-url` (required) / `enroll --hub-url` | `TIMEKPR_HUB_URL` | `run` requires it up front (systemd launches it non-interactively); `enroll` prompts if omitted, and normalizes a scheme-less value to `http://` |
| `run --users` (required) | `TIMEKPR_HUB_MANAGED_USERS` | Comma-separated local usernames |
| `run --tz` / `enroll --tz` | `TIMEKPR_HUB_TZ` | Default `UTC` |
| `run --state-path` | | Default `/var/lib/timekpr-hub-agent/state.json` |
| `run --once` | | Run a single tick and exit, for testing |
| `run --token-path` / `enroll --token-path` | | Default `/var/lib/timekpr-hub-agent/device_token` |
| `run --ca-cert` / `enroll --ca-cert` | | For a hub with a self-signed certificate |
| `enroll --code` | | One-time enrollment code from the hub; prompted if omitted |
| `enroll --users` | | Comma-separated; prompted interactively (from timekpr's own user list) if omitted |
| `enroll --hostname` / `--machine-id` / `--os` | | Default to the local machine's own values |
| `enroll --no-start` | | Don't enable/restart the service after enrolling |
| `status` | | No flags; reports the full enrollment/connectivity chain |

## Further reading

- [`docs/best-practices-review.md`](docs/best-practices-review.md) — known
  gaps and findings against general Python/FastAPI/SQLAlchemy practices.
- [`docs/phase0-findings.md`](docs/phase0-findings.md) — empirical
  findings about the real `timekprd`'s DBUS behavior.
- [`docs/agent-live-test-findings.md`](docs/agent-live-test-findings.md) —
  bugs found by running the agent against a live daemon.
- [`CHECKLIST.md`](CHECKLIST.md) — execution status against the original
  design plan, phase by phase.
