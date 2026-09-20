# timekpr-next-hub

A self-hosted hub that pools screen-time budgets for
[timekpr-next](https://github.com/mjasnik/timekpr-next) across multiple
machines in a household. A small agent runs alongside `timekprd` on each
machine, reporting usage to the hub and applying whatever balance the hub
says is left — so a user's daily limit is shared across their desktop and
laptop instead of being tracked separately on each.

---

# Setting up your hub

## What you need

- A machine that stays on to run the hub itself — a home server, a mini
  PC, or a [Proxmox LXC](#proxmox-smallest-footprint) for the smallest
  footprint.
- Docker or Podman with `compose` support, or a Proxmox server.
- On each machine you want managed: `timekpr-next` already installed and
  running, and a systemd-based Linux distro (see
  [multi-distro support](#multi-distro-support)).

The hub serves plain HTTP and isn't meant for the open internet — put it
on your LAN or behind a Tailscale/WireGuard tunnel, or add your own
reverse proxy for TLS.

## 1. Start the hub

On the machine that will run the hub:

```sh
cp deploy/.env.example deploy/.env   # fill in a real POSTGRES_PASSWORD, HUB_TZ
make deploy-up
```

This pulls the published image (`ghcr.io/bzumhagen/timekpr-hub`, amd64 and
arm64). Set `HUB_VERSION=0.1.0` in `deploy/.env` to pin an exact
[release](https://github.com/bzumhagen/timekpr-next-hub/releases) instead
of tracking `latest`. `make deploy-logs` tails everything as it comes up.

To **upgrade** later: bump `HUB_VERSION` (or leave it on `latest`), then
`docker compose -f deploy/docker-compose.yml --env-file deploy/.env pull &&
make deploy-up`. Migrations run themselves on start. Take a `pg_dump`
first — see [Backups](#backups).

### Proxmox (smallest footprint)

Run the hub natively inside an unprivileged Debian LXC instead of the
compose stack — see [`deploy/proxmox/README.md`](deploy/proxmox/README.md)
for the install script and sizing guidance.

### Backups

There's no bundled backup job:

```sh
docker compose -f deploy/docker-compose.yml exec postgres \
  pg_dump -U timekpr_hub -Fc timekpr_hub > timekpr_hub_$(date +%Y%m%d).dump
```

## 2. Claim the hub — do this immediately

Open the hub in a browser. It asks you to create an admin account before
anything else; until one exists, anyone who can reach the hub can create
it themselves. Add a second admin later from the **Admins** page.

## 3. Add a machine

Install the agent on the machine you want managed — see
[Installing the agent](#installing-the-agent) for every install path
(AUR, a release package, or a manual install on non-Arch distros).

In the hub's web UI, open **Devices** and click **Generate enrollment
code** — it shows the exact command to run, with the hub's URL and code
already filled in. See [Enrolling a device](#enrolling-a-device) for what
that command does and how re-enrolling a machine behaves.

Repeat for every machine you want pooled into the same limit — enrolling a
second machine under the same local username automatically pools it into
the same hub user, no extra step needed.

## 4. Daily use

The hub's home page shows, per user: today's usage bar and an activity
badge (draining / idle / offline). Device sync status and enrollment live
on their own **Devices** page, out of the way until you need them.

- **Give extra time today**: the **+30 min** / **-30 min** buttons on the
  dashboard.
- **Manage the full policy**: **Edit policy** on a user's card — per-day
  limits and an hours grid for time-of-day windows. Weekly/monthly caps,
  which days login is allowed at all, lockout behavior, and PlayTime are
  behind **Advanced policy settings** on the same page.
- **See where the time went**: **Usage stats** for a day-by-day
  spend-vs-limit history and a per-device breakdown.
- **Approval-gated days** ("no time until a chore is done"): set which
  days are gated on the user's **Settings** page. A gated day shows a
  **⏸ Chores not done** badge on the dashboard; **Release today** applies
  the day's normal limit immediately, no policy edit or device restart.
  The gate re-arms on its own the next time that weekday comes around.
- **A specific date's limit** ("no time tomorrow", or any other date):
  **Adjust a day** on the dashboard — pick a date and either "no time at
  all" or a specific limit. Replaces that day's limit outright rather
  than editing the standing policy, so it isn't undone by a later
  schedule change.
- **Manage a device**: on the **Devices** page, revoke it (stops hub
  management, reversible) or delete it (erases its history). Revoking
  does **not** lock the user out — it hands the machine back to local
  self-management at whatever limit it already had. To actually lock it,
  set its policy to 0 minutes/day (or grant negative time) before
  revoking, or just don't re-enroll it.
- **While the hub is unreachable**: on the user's **Settings** page,
  choose whether that device keeps enforcing its last-known limit, allows
  some extra time before capping, or locks out immediately, and how long
  the grace period is.
- **Audit log**: every change any admin has made (grants, policy edits,
  device revokes, ...), with a before/after diff.
- **Add another admin**: the **Admins** page generates a one-time invite
  link; they set their own password when they open it.

## 5. Troubleshooting

- **A device shows no recent activity.** `sudo timekpr-hub-agent status`
  on that machine shows whether the hub is reachable and when it last
  synced.
- **Reinstalling the agent, or a fresh OS install on the same machine.**
  Just re-run `enroll` — it rebinds to the existing device by machine ID,
  keeping its history. Revoke or delete the old device in the hub UI
  first for a clean slate instead.

---

# Developing on the hub

## Repo layout

```
core/    timekpr_hub_core  -- pure, IO-free logic shared by hub + agent
                              (convergence math, calendar rules, wire types)
hub/     timekpr_hub        -- FastAPI app: Postgres-backed API + a small
                              server-rendered (Jinja2 + vanilla JS) admin UI
agent/   timekpr_hub_agent  -- systemd sidecar: talks to the local timekprd
                              over DBUS, syncs with the hub over HTTP
deploy/                     -- docker-compose (Postgres + hub), a native
                              Proxmox LXC install, a source-build overlay,
                              and a throwaway dev/test Postgres compose file
tests/                      -- unit, integration (some DB-backed), e2e
docs/                       -- release process
scripts/                    -- version/changelog/packaging helpers used by
                              both the Makefile and CI
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

Then open <http://127.0.0.1:8000>. Run `make help` for the full list of
targets; there is no `make run-agent` — the agent needs the system's
`dbus`/`PyGObject` bindings, which a plain uv venv doesn't have, so its
logic is exercised in `make test` via `FakeTimekprDaemon`
(`tests/fakes/fake_timekpr.py`), a model of timekpr's own accounting
semantics.

## CI and releases

[`.github/workflows/ci.yml`](.github/workflows/ci.yml) runs on every push
to `main` and every PR: lint + typecheck + shellcheck + the
version-consistency check, the full test suite against a real Postgres on
Python 3.12 **and** 3.13 (the LXC install runs trixie's 3.13), and a build
of the agent's Arch package in an `archlinux` container.

Releases are cut by pushing a `vX.Y.Z` tag, which runs that same workflow
and then publishes the source tarball, wheels, the Arch package, a
multi-arch container image, and the AUR update. The whole procedure — plus
why the version lives in six files and how the PKGBUILD's checksum works —
is in [`docs/releasing.md`](docs/releasing.md).

## Testing

Tests are layered:

1. **Unit / property tests** (`tests/unit/`) — Hypothesis property tests
   on the pure convergence controller, calendar boundaries, wall-clock
   interval union, and a `FakeTimekprDaemon`-driven multi-device
   simulation. No services needed.
2. **Postgres-backed tests** (`tests/integration/`) — marked
   `@pytest.mark.db`, need a real database.
3. **End-to-end** (`tests/e2e/`) — the real agent tick loop + real
   convergence math + a real hub + real Postgres, all in one process.

`make test` runs layer 1 (`pytest -m "not db"`). `make test-db` runs only
the DB-backed tests; `make test-e2e` only the end-to-end ones;
`make test-all` runs everything — this is what CI runs. All three bring up
the dev Postgres first and apply migrations to the **separate**
`timekpr_hub_test` database (these tests `TRUNCATE` every table they touch
between tests), with `TIMEKPR_HUB_REQUIRE_DB=1` so a missing database is a
hard failure rather than a silently-skipped test.

## Migrations

Alembic lives under `hub/migrations/`, config at `hub/alembic.ini`. Always
go through the Makefile (`make migrate`, `make migrate-test`,
`make revision MSG="..."`) — they set `DATABASE_URL` to the right one of
the two dev-compose databases and run inside the uv-managed venv.

## Deploying the hub

The production stack (`deploy/docker-compose.yml`) is Postgres (internal
only) + the hub, which publishes `:8000` directly — see
[Start the hub](#1-start-the-hub) above for the commands.

To run the hub from this checkout instead of the published image — an
unreleased fix, or a local change under the real production stack — use
`make deploy-up-source`, which layers
[`deploy/compose.source.yml`](deploy/compose.source.yml) on top and builds
`hub/Dockerfile` locally.

## Installing the agent

The agent's own code is stdlib-only (it uses `urllib.request`, not a
third-party HTTP client), plus the distro's system `dbus`/`PyGObject`
packages to talk to `timekprd`. No venv: the Arch package installs
straight into system site-packages under a Python-version-independent
path (`/usr/lib/timekpr-hub-agent/`).

**From the AUR** (Arch/CachyOS — the easiest path, and how you get
upgrades):

```sh
paru -S timekpr-hub-agent      # or yay, or any other AUR helper
```

**From a release** (Arch/CachyOS, no AUR helper). Every
[release](https://github.com/bzumhagen/timekpr-next-hub/releases) attaches
a built package — substitute the current version for `0.1.0`:

```sh
sudo pacman -U ./timekpr-hub-agent-0.1.0-1-any.pkg.tar.zst
```

**From the PKGBUILD**, if you'd rather build it yourself — downloads and
builds the same release tarball, so this needs the release to already
exist (it does not build local changes; see the Prerequisites section
above for that):

```sh
cd agent/packaging && makepkg -si
```

pacman's own systemd hooks handle `sysusers`/`tmpfiles`/`daemon-reload`
automatically in every case — nothing else to run by hand.

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
take precedence over prompts.

This preflights the local timekpr install, redeems the code, writes the
device token (`0600`, owned by the service's own user), writes
`/etc/timekpr-hub-agent/agent.env`, and enables + starts the service. Pass
`--no-start` to skip the last step, or `--ca-cert` for a hub with a
self-signed certificate.

Re-enrolling from the same machine is detected by `machine_id` and
**rebinds** to the existing device row instead of creating a duplicate —
that device's history is preserved.

`sudo timekpr-hub-agent status` reports one ✓/✗ line per link in the
chain — timekpr-next installed, timekprd reachable over DBUS, config
present, device token readable, service enabled, service active, hub
reachable, and the device's clock skew against the hub — plus each
managed user's last sync and current balance.

## Surviving reboots & upgrades

`enroll` runs `systemctl enable --now`, so a fresh enrollment survives a
reboot without a separate step. The one thing this can't guarantee: anyone
with `sudo` on a managed machine can stop the service or uninstall the
package — the hub UI flags a device that hasn't checked in recently.

## Multi-distro support

timekpr-next itself supports Ubuntu & derivatives (PPA), Debian (native),
Arch/Manjaro (AUR), Fedora (COPR), and openSUSE (native). Only the Arch
package exists here; the agent is stdlib-only and systemd-based, so the
[manual install](#installing-the-agent) works on any of them — there's
just no `.deb`/`.rpm` built for it yet.

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
| `enroll --hostname` / `--machine-id` | | Default to the local machine's own values |
| `enroll --no-start` | | Don't enable/restart the service after enrolling |
| `status` | | `--hub-url` overrides `TIMEKPR_HUB_URL` for this one check; otherwise reports the full enrollment/connectivity chain |

## JSON API

Everything the hub UI does, it does through `/api/v1/*` — there's no
separate "internal" API. Two authentication schemes, matching who's
calling:

- **`/api/v1/enroll` and `/api/v1/sync`** are device-authenticated:
  `enroll` takes a one-time code, `sync` takes the resulting device
  bearer token (`Authorization: Bearer tkh_...`). These are what
  `timekpr-hub-agent` itself calls.
- **Everything else** (`/api/v1/users/*`, `/api/v1/devices/*`,
  `/api/v1/admins/*`, `/api/v1/audit`, ...) needs a logged-in admin
  session — the same `tkh_session` cookie `/login` sets for the browser.
  There's no separate API token; script against it with a cookie jar the
  way you'd script against any session-authenticated site.

The route modules under [`hub/timekpr_hub/api/`](hub/timekpr_hub/api/) are
the source of truth for the full endpoint list — `admin/` for the JSON
API, `enroll.py`/`sync.py` for the agent-facing pair, `ui/` for the HTML
routes the JSON API mirrors.

## Further reading

- [`docs/releasing.md`](docs/releasing.md) — how a release is cut, what a
  tag produces, and the one-time AUR/GHCR setup.
- [`CHANGELOG.md`](CHANGELOG.md) — what changed in each release.
