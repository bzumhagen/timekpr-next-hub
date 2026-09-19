# Changelog

Notable changes per release. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning is
[semantic](https://semver.org/), with the hub, the agent and the shared
core package released together under one version (see
`docs/releasing.md`).

Each released version's section here becomes that GitHub Release's notes,
so write it for someone deciding whether to upgrade — the commit list is
appended automatically underneath.

## [Unreleased]

## [0.1.0] - 2026-09-19

First tagged release. Pools a kid's daily screen-time budget across every
machine they use, instead of `timekpr-next` tracking each one separately.

### Added

- **Hub** (`timekpr-hub`): FastAPI service over Postgres holding the
  household's users, devices, policies and usage. Web UI for the day's
  balance per kid, per-device activity, enrollment codes, and an audit
  log of every parent action.
- **Agent** (`timekpr-hub-agent`): systemd sidecar for each kid's machine.
  Reports usage to the hub and applies the balance the hub hands back via
  `timekprd`'s DBUS interface. Stdlib-only (no third-party runtime
  dependency at all), survives reboots, and keeps enforcing on a
  wall-clock offline grace budget when the hub is unreachable.
- **Shared core** (`timekpr-hub-core`): the convergence, calendar and
  interval-union math, written once and property-tested, so the hub and
  the agent cannot drift apart on the safety-critical arithmetic.
- **Policies**: pooled daily limits, allowed-hours windows, per-weekday
  schedules, one-off per-date overrides, and a temporary override of the
  hours restriction — all append-only and versioned, with the agent
  gated on the policy version it has applied.
- **Enrollment**: one-time codes from the hub UI; `timekpr-hub-agent
  enroll` preflights the local timekpr install, redeems the code, writes
  its device token `0600`, and enables the service. Re-enrolling the same
  machine rebinds to the existing device row rather than duplicating it.
- **Deployment**: `deploy/docker-compose.yml` (hub + Postgres), and a
  native Proxmox LXC install (`deploy/proxmox/`) for the smallest
  footprint — Postgres from apt, the hub as a systemd unit, no Docker.
- **Packaging**: Arch/CachyOS package for the agent, installing into a
  Python-version-independent `/usr/lib/timekpr-hub-agent/` so a system
  Python upgrade needs no rebuild.

### Known limits

- Pooled **daily** budget only; week/month pooling and drift detection
  aren't built yet — see [`CHECKLIST.md`](CHECKLIST.md).
- The agent ships for Arch/CachyOS only. Other systemd distros work via
  the manual install path, but have no package yet.
- The hub serves plain HTTP: run it on your LAN or behind a
  Tailscale/WireGuard tunnel, and put your own reverse proxy in front of
  it if you want TLS.
- Anyone with `sudo` on a kid's machine can stop the agent. The hub UI
  flags a device that stops checking in; it can't prevent it.

[Unreleased]: https://github.com/bzumhagen/timekpr-next-hub/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/bzumhagen/timekpr-next-hub/releases/tag/v0.1.0
