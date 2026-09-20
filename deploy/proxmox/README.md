# Running the hub on Proxmox (native, no Docker)

The smallest-footprint way to run the hub: one unprivileged Debian LXC,
Postgres from apt, the hub as a systemd unit against a `uv`-managed venv.
No container runtime, no VM overhead -- LXC shares the host kernel, so the
actual cost is just what Postgres and uvicorn touch (roughly 300MB steady
state; see [Sizing](#sizing) below for the ceiling you should set).

If you'd rather use `docker compose`, see [`../docker-compose.yml`](../docker-compose.yml)
and the main [README.md](../../README.md) instead -- both paths run the same
code and are kept in sync.

## Bring-up

On the Proxmox host, from a checkout of this repo:

```sh
deploy/proxmox/create-lxc.sh
```

Or standalone, without a checkout (community-scripts style):

```sh
curl -fsSL https://raw.githubusercontent.com/bzumhagen/timekpr-next-hub/main/deploy/proxmox/create-lxc.sh | bash
```

Run from a terminal, it prompts for `CTID` (defaults to the next free ID,
via `pvesh get /cluster/nextid`), `CT_HOSTNAME`, `TEMPLATE_STORAGE`, `STORAGE`,
`BRIDGE`, `CORES`, `MEMORY_MB`, `SWAP_MB`, `DISK_GB`, and the hub's timezone
(`HUB_TZ`, defaulting to the Proxmox host's own timezone) -- press Enter on
any prompt to accept the default. Set any of those as an environment variable
beforehand to skip its prompt (e.g. `CORES=4 deploy/proxmox/create-lxc.sh`),
or set `NONINTERACTIVE=1` to skip all prompts and take every default,
useful for unattended/scripted runs. `TEMPLATE` and `NET_CONF` are
advanced-only overrides (not prompted for). This creates the container,
starts it, gets the source in (from the local checkout if the script is run
from one, otherwise fetched straight from GitHub -- override with `REPO_URL`/
`REF` for a fork or branch), and runs `install.sh` inside it. Any extra
arguments are passed straight through to `install.sh` (e.g.
`deploy/proxmox/create-lxc.sh --bind 127.0.0.1`, or
`curl -fsSL .../create-lxc.sh | bash -s -- --bind 127.0.0.1`).

Or do it by hand: create an unprivileged Debian 13 LXC yourself (see
[Sizing](#sizing) for the geometry), copy this repo into it, and run:

```sh
bash deploy/proxmox/install.sh --tz America/Denver
```

### Claim it immediately

Once `install.sh` finishes, open `http://<the LXC's IP>:8000/setup` in a
browser **right away** and create the first admin account. Until you do,
anyone who can reach the hub can create that account themselves. Keep the
hub on your LAN or behind a Tailscale/WireGuard tunnel -- it serves plain
HTTP, no TLS.

### `install.sh` flags

```
--tz TZ             HUB_TZ to configure (default: UTC)
--bind ADDR         address uvicorn binds (default: 0.0.0.0)
--port PORT         port uvicorn binds (default: 8000)
--skip-postgres     don't touch Postgres (assume it's already tuned/running)
--no-start          install everything but don't enable/start the units
```

## Upgrading

From a newer checkout (or after `git pull`), just re-run `create-lxc.sh`
against the same `CTID` -- it reuses the existing container instead of
trying to recreate it -- or copy the updated repo in and re-run
`install.sh` inside the container directly. `install.sh` is what's
idempotent:

- The database role, database, and password are all reused -- never
  recreated or regenerated.
- A one-off safety dump is taken (into `/var/backups/timekpr-hub`) before
  the code is touched.
- The hub is stopped, the venv is rebuilt from the new `uv.lock`, migrations
  are re-run, and the hub is started again.
- The admin account and every enrolled device survive untouched.

## Backups

There's no backup timer installed by `install.sh` -- back up how you
already back up everything else on this host. A logical dump is the
simplest option and works from inside the container:

```sh
su -s /bin/sh postgres -c "pg_dump -Fc -d timekpr_hub -f /var/backups/timekpr-hub/manual_$(date +%Y%m%d).dump"
```

On Proxmox, `vzdump` / Proxmox Backup Server already snapshots the whole
container nightly. A logical `pg_dump` on top of that is still worth having
-- it's what lets you restore onto a different Postgres major version later
(a raw container snapshot only restores onto the same one).

### Restoring a dump

```sh
su -s /bin/sh postgres -c "pg_restore -d timekpr_hub --clean --if-exists /path/to/the.dump"
```

This also works with a `-Fc` dump produced by the old `docker compose`
stack's backup job -- Postgres 17 (what Debian trixie ships) reads the
Postgres 16 dumps that stack produced.

## Sizing

```sh
pct create 110 local:vztmpl/debian-13-standard_13.x_amd64.tar.zst \
  --hostname timekpr-hub --unprivileged 1 --features '' \
  --cores 2 --memory 1024 --swap 512 \
  --rootfs local-lvm:8 --onboot 1 \
  --net0 name=eth0,bridge=vmbr0,ip=dhcp
```

- **No `nesting=1`, no `keyctl=1`.** Those exist for running Docker inside
  the LXC, which this deployment deliberately doesn't do. Plain
  unprivileged is correct and more secure.
- **`--memory 1024`** is a ceiling, not a reservation -- steady state sits
  near 300MB and the rest stays available to the Proxmox host for other
  guests. 512MB does work with the tuning in
  [`postgresql/10-timekpr-hub.conf`](postgresql/10-timekpr-hub.conf), but
  leaves no headroom for an `apt`/`uv sync` spike during install or upgrade.
- **`--cores 2`** -- 1 is plenty once the hub is running (a handful of
  devices polling every 20s), but 2 keeps `uv sync` and the Postgres apt
  install from taking minutes.
- **`--rootfs 8`** -- Debian base (~1.2GB) + Postgres (~250MB) + the venv
  (~200MB) + `uv`'s package cache (~150MB, prunable) + WAL (up to 512MB per
  the tuning conf) + whatever dumps you keep.

## Known risks / things to watch

- **Python version.** The LXC's system Python (3.13 on Debian trixie) is
  newer than this repo's `.python-version` (3.12) -- `install.sh` excludes
  that file from what it copies in and passes `--python /usr/bin/python3`
  explicitly, and every dependency in `uv.lock` ships a wheel for both, so
  no compiler is needed. If a future `uv lock` update drops a prebuilt
  wheel for 3.13, `UV_PYTHON_DOWNLOADS=never` makes that fail loudly at
  install time instead of silently downloading an interpreter.
- **Cluster encoding.** `install.sh` refuses to touch a non-UTF8 cluster
  that already has real databases in it -- see the error message it prints
  for the manual dump/recreate/restore steps.
- **`/dev/shm` in LXC** is small by default; the tuning conf disables
  parallel query (`max_parallel_workers_per_gather = 0`) so Postgres never
  needs it. Don't set `kernel.shmmax` from inside the container -- modern
  Postgres uses `mmap`, not System V shared memory.
- **A future `apt dist-upgrade` that bumps the Postgres major version**
  needs `pg_upgradecluster` and the `conf.d` drop-in copied to the new
  cluster by hand. `install.sh` never attempts a major-version upgrade on
  its own.
- **Egress is required** for `uv sync` (against PyPI) and for the pinned
  `uv` binary's initial download. There's no offline/air-gapped mode.
- **Never add `--workers` to the uvicorn command.** A single worker against
  a single Postgres pool (`pool_size=5, max_overflow=5` in
  `hub/timekpr_hub/db/session.py`) is the deployment this hub is built for;
  two workers would mean two separate connection pools racing each other.
