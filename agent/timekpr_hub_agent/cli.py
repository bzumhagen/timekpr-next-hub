"""The `timekpr-hub-agent` command line: `run`, `enroll`, `status`.

`agent/pyproject.toml`'s `[project.scripts]` and
`agent/packaging/timekpr-hub-agent`'s launcher both invoke
`timekpr_hub_agent.main:main`, which delegates to `main()` below.
"""

from __future__ import annotations

import argparse
import logging
import os
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from timekpr_hub_agent import config as config_mod
from timekpr_hub_agent import state as state_mod
from timekpr_hub_agent.enforcer import TimekprEnforcer
from timekpr_hub_agent.hubclient import (
    DEFAULT_TOKEN_PATH,
    DeviceRevokedError,
    EnrollError,
    HubClient,
    HubClientConfig,
    HubUnreachableError,
)
from timekpr_hub_agent.main import AGENT_VERSION
from timekpr_hub_agent.notify import sd_notify
from timekpr_hub_agent.tick import DEFAULT_POLL_MS, run_tick
from timekpr_hub_agent.timekpr_paths import TimekprNotFoundError

log = logging.getLogger("timekpr_hub_agent")

MACHINE_ID_PATHS = (Path("/etc/machine-id"), Path("/var/lib/dbus/machine-id"))
SERVICE_UNIT = "timekpr-hub-agent.service"


def _read_machine_id() -> str:
    for path in MACHINE_ID_PATHS:
        try:
            return path.read_text().strip()
        except OSError:
            continue
    raise RuntimeError(f"could not read a machine id from any of {[str(p) for p in MACHINE_ID_PATHS]}")


def _add_hub_connection_args(
    parser: argparse.ArgumentParser, env_values: dict[str, str], *, prompt_if_missing: bool = False
) -> None:
    parser.add_argument(
        "--hub-url",
        default=config_mod.env_default("TIMEKPR_HUB_URL", env_values),
        # `run` (prompt_if_missing=False) keeps today's behavior: argparse
        # itself rejects a missing value up front, since `run` is what
        # systemd launches non-interactively and a clear "the following
        # arguments are required" beats a confusing failure three calls
        # later. `enroll` (prompt_if_missing=True) never argparse-requires
        # it -- an admin running it bare gets prompted instead (see
        # _prompt_or_die in _cmd_enroll), and a non-interactive caller still
        # gets a clean, equivalent error from that same helper.
        required=(not prompt_if_missing) and config_mod.env_default("TIMEKPR_HUB_URL", env_values) is None,
        help="e.g. http://hub.local:8000 (http:// is assumed if you omit a scheme)"
        + ("; prompted if omitted" if prompt_if_missing else ""),
    )
    parser.add_argument(
        "--token-path", default=str(DEFAULT_TOKEN_PATH), help="where the device bearer token lives"
    )
    parser.add_argument(
        "--ca-cert",
        default=config_mod.env_default("TIMEKPR_HUB_CA_CERT", env_values),
        help="path to a CA bundle, for a hub with a self-signed cert",
    )


def _prompt_or_die(value: str | None, *, label: str, flag: str) -> str:
    """The interactive-input pattern shared by every prompted `enroll`
    argument (users, hub URL, code): fall through to a clean, actionable
    error rather than blocking forever when stdin isn't a terminal (a
    systemd unit, a script, CI) -- bare `input()` there would hang or raise
    EOFError instead of naming the flag to pass explicitly."""
    if value:
        return value
    if not sys.stdin.isatty():
        raise SystemExit(f"error: {flag} is required when not running interactively")
    entered = input(f"{label}: ").strip()
    if not entered:
        raise SystemExit(f"error: no {label.lower()} given")
    return entered


def _validate_users(enforcer: TimekprEnforcer, requested: list[str]) -> list[str]:
    """Reject usernames timekpr doesn't know about, instead of silently
    skipping them tick after tick. Best-effort: if the user list can't be
    read at all (e.g.
    timekprd not reachable right now), fall back to trusting the caller
    rather than blocking enrollment on a transient DBUS hiccup."""
    known = enforcer.get_user_list()
    if not known:
        return requested
    unknown = [u for u in requested if u not in known]
    if unknown:
        raise SystemExit(
            f"error: {', '.join(unknown)} not found in timekpr. "
            f"Users timekpr knows about: {', '.join(known) or '(none configured)'}"
        )
    return requested


def _prompt_for_users(enforcer: TimekprEnforcer) -> str:
    known = enforcer.get_user_list()
    if not known:
        raise SystemExit(
            "error: --users not given and could not list timekpr's users -- pass --users explicitly"
        )
    print(f"Users timekpr knows about: {', '.join(known)}")
    chosen = input("Which should this hub manage? (comma-separated): ").strip()
    if not chosen:
        raise SystemExit("error: no users selected")
    return chosen


def _cmd_enroll(args: argparse.Namespace) -> None:
    try:
        enforcer = TimekprEnforcer()
        timekprd_ok = enforcer.connect()
    except TimekprNotFoundError as exc:
        raise SystemExit(f"error: {exc}") from None
    print(
        "✓ timekpr-next found"
        + (" and timekprd reachable" if timekprd_ok else " (timekprd not reachable yet -- continuing anyway)")
    )

    users_arg = args.users
    if not users_arg:
        if not sys.stdin.isatty():
            raise SystemExit("error: --users is required when not running interactively")
        users_arg = _prompt_for_users(enforcer)
    local_users = [u.strip() for u in users_arg.split(",") if u.strip()]
    if timekprd_ok:
        local_users = _validate_users(enforcer, local_users)

    local_policies = {}
    for username in local_users:
        snapshot = enforcer.get_user_policy_snapshot(username) if timekprd_ok else None
        if snapshot:
            local_policies[username] = snapshot

    hub_url_input = _prompt_or_die(
        args.hub_url, label="Hub URL (e.g. http://hub.local:8000)", flag="--hub-url"
    )
    try:
        hub_url = config_mod.normalize_hub_url(hub_url_input)
    except config_mod.InvalidHubUrlError as exc:
        raise SystemExit(f"error: {exc}") from None
    code = _prompt_or_die(args.code, label="Enrollment code", flag="--code")

    hub = HubClient(HubClientConfig(base_url=hub_url, token_path=Path(args.token_path), ca_cert=args.ca_cert))
    hostname = args.hostname or socket.gethostname()
    machine_id = args.machine_id or _read_machine_id()

    try:
        data = hub.enroll(
            enrollment_code=code,
            hostname=hostname,
            machine_id=machine_id,
            agent_version=AGENT_VERSION,
            local_users=local_users,
            local_policies=local_policies,
        )
    except EnrollError as exc:
        raise SystemExit(f"error: {exc}") from None

    config_mod.chown_to_service_user(Path(args.token_path))
    if data.get("rebound"):
        # Same machine_id as an existing, non-revoked device -- the hub
        # rotated that device's token and reused its row instead of forking
        # a second history for the same machine (e.g. after `pacman -R` +
        # `pacman -U` and a re-enroll). Say so explicitly: silently doing
        # this without telling the admin looks identical to a fresh
        # enrollment, and they may reasonably expect a new device to appear.
        since = data.get("previously_enrolled_at", "")
        print(
            f"↻ re-bound to existing device {data['device_id']} "
            f"(first enrolled {since or 'previously'}; token rotated, history preserved)"
        )
    else:
        print(f"✓ enrolled as device {data['device_id']} (token written to {args.token_path}, mode 0600)")

    hub_tz = data.get("hub_tz") or args.tz
    for username in local_users:
        policy = data.get("policies", {}).get(username)
        if username in data.get("new_users", []):
            note = (
                "new hub user, policy seeded from this device"
                if local_policies.get(username)
                else "new hub user, hub default policy (1h/day) applied"
            )
        else:
            note = "joined an existing hub user -- pooling with its other device(s)"
        if policy:
            hours = policy["daily_limits_s"][0] / 3600
            print(f"  {username}: hub daily limit {hours:g}h ({note})")
        else:
            print(f"  {username}: {note}")

    config_mod.write_env_file(
        hub_url=hub_url, managed_users=",".join(local_users), tz=hub_tz, ca_cert=args.ca_cert
    )
    print(f"✓ config written to {config_mod.DEFAULT_ENV_PATH}")

    if not args.no_start:
        try:
            # `enable --now` is a no-op on an *already-running* unit -- it
            # only ensures the unit is enabled and started, neither of
            # which changes for a unit that's already both. That silently
            # orphaned the token this enroll just wrote: a re-enroll while
            # the service was already active (e.g. a rebind after a
            # reinstall) left the running process holding the OLD token in
            # memory (HubClient loads it once, at __init__) while the DB
            # now expects the new one, and every subsequent /sync 401'd --
            # which the agent treats as DeviceRevokedError and enters
            # `closed` enforcement immediately, i.e. the user looks locked
            # out for no reason even though the hub thinks everything is
            # fine. `enable` (idempotent, no restart) followed by an
            # unconditional `restart` (starts a stopped unit, restarts a
            # running one) covers both the first-ever enroll and every
            # re-enroll case with the same two commands.
            subprocess.run(["systemctl", "enable", SERVICE_UNIT], check=True)
            subprocess.run(["systemctl", "restart", SERVICE_UNIT], check=True)
            print(f"✓ {SERVICE_UNIT} enabled and (re)started")
        except (subprocess.CalledProcessError, OSError) as exc:
            print(f"! could not enable/restart {SERVICE_UNIT}: {exc}")
            print(f"  Run manually: sudo systemctl enable --now {SERVICE_UNIT}")


def _cmd_run(args: argparse.Namespace) -> None:
    hub = HubClient(
        HubClientConfig(base_url=args.hub_url, token_path=Path(args.token_path), ca_cert=args.ca_cert)
    )
    state_path = Path(args.state_path)
    state = state_mod.load(state_path)
    managed_users = [u.strip() for u in args.users.split(",") if u.strip()]

    # Retry (never exit) on a transient condition: timekprd not up yet, or
    # the hub unreachable once the tick loop starts below. Restart=always
    # would bring the process back anyway, but that's a 10s outage window
    # on every blip for no reason. Missing --hub-url/TIMEKPR_HUB_URL is
    # *not* transient -- argparse already rejected it before this function
    # ran (_add_hub_connection_args, prompt_if_missing=False).
    enforcer: TimekprEnforcer | None = None
    ready_sent = False
    while enforcer is None:
        try:
            enforcer = TimekprEnforcer()
        except TimekprNotFoundError as exc:
            log.error("%s -- retrying in 30s", exc)
            time.sleep(30)

    next_poll_ms = DEFAULT_POLL_MS
    while True:
        next_poll_ms = run_tick(
            enforcer=enforcer,
            hub=hub,
            state=state,
            managed_users=managed_users,
            agent_version=AGENT_VERSION,
            tz_name=args.tz,
            previous_poll_ms=next_poll_ms,
        )
        state_mod.save(state, state_path)
        if not ready_sent:
            # First tick has completed (whether or not it reached the hub --
            # that's exactly what the watchdog/offline handling is for), so
            # systemd can stop waiting and consider the unit started.
            sd_notify("READY=1")
            ready_sent = True
        sd_notify("WATCHDOG=1")
        if args.once:
            break
        time.sleep(next_poll_ms / 1000)


def _check(label: str, ok: bool, detail: str = "") -> bool:
    mark = "✓" if ok else "✗"
    print(f"{mark} {label}" + (f" -- {detail}" if detail and not ok else ""))
    return ok


def _cmd_status(args: argparse.Namespace) -> None:
    """`timekpr-hub-agent status`: one line per check, so an admin (or this
    agent's own `run` at startup) can see exactly which link in the chain
    is broken instead of a bare "it's not working"."""
    all_ok = True

    try:
        enforcer = TimekprEnforcer()
        _check("timekpr-next installed", True)
    except TimekprNotFoundError as exc:
        _check("timekpr-next installed", False, str(exc))
        enforcer = None
        all_ok = False

    if enforcer is not None:
        connected = enforcer.connect()
        all_ok &= _check(
            "timekprd reachable over DBUS", connected, "check group membership / timekprd status"
        )

    env_values = config_mod.read_env_file()
    hub_url = args.hub_url or config_mod.env_default("TIMEKPR_HUB_URL", env_values)
    all_ok &= _check(
        "config present", bool(hub_url), f"run `timekpr-hub-agent enroll` ({config_mod.DEFAULT_ENV_PATH})"
    )

    token_path = Path(args.token_path)
    token_ok = token_path.exists() and os.access(token_path, os.R_OK)
    all_ok &= _check("device token present and readable", token_ok, str(token_path))

    try:
        enabled = subprocess.run(
            ["systemctl", "is-enabled", SERVICE_UNIT], capture_output=True, text=True, check=False
        ).stdout.strip()
        active = subprocess.run(
            ["systemctl", "is-active", SERVICE_UNIT], capture_output=True, text=True, check=False
        ).stdout.strip()
        all_ok &= _check(f"service enabled ({enabled or 'unknown'})", enabled == "enabled")
        all_ok &= _check(f"service active ({active or 'unknown'})", active == "active")
    except OSError:
        all_ok &= _check("service enabled/active", False, "systemctl not available")

    if hub_url:
        try:
            hub = HubClient(
                HubClientConfig(
                    base_url=hub_url,
                    token_path=token_path,
                    ca_cert=config_mod.env_default("TIMEKPR_HUB_CA_CERT", env_values),
                )
            )
            probe_sent_at = datetime.now(UTC)
            response = hub.sync(
                {
                    "agent_time": probe_sent_at.isoformat(),
                    "agent_version": AGENT_VERSION,
                    "users": [],
                }
            )
            all_ok &= _check("hub reachable", True)
            hub_time = response.get("hub_time")
            if hub_time:
                # Comparing against `probe_sent_at` (before the round trip)
                # rather than `datetime.now(UTC)` again keeps network
                # latency out of the estimate -- what's being checked is
                # this machine's own clock, not how long the request took.
                skew_ms = round((datetime.fromisoformat(hub_time) - probe_sent_at).total_seconds() * 1000)
                all_ok &= _check(f"clock within 30s of the hub ({skew_ms:+d}ms)", abs(skew_ms) < 30_000)
        except (HubUnreachableError, DeviceRevokedError) as exc:
            all_ok &= _check("hub reachable", False, str(exc))
    else:
        all_ok &= _check("hub reachable", False, "no --hub-url and no TIMEKPR_HUB_URL in agent.env")

    state = state_mod.load(Path(args.state_path))
    managed_users = [
        u.strip()
        for u in (config_mod.env_default("TIMEKPR_HUB_MANAGED_USERS", env_values) or "").split(",")
        if u.strip()
    ]
    for username in managed_users:
        user_state = state.users.get(username)
        if user_state is None:
            print(f"  {username}: no sync recorded yet")
            continue
        last_sync = (
            datetime.fromtimestamp(user_state.last_hub_contact_utc, UTC).isoformat()
            if user_state.last_hub_contact_utc
            else "never"
        )
        print(
            f"  {username}: last sync {last_sync}, global spent {user_state.last_global_spent_s}s / "
            f"limit {user_state.last_effective_limit_today_s}s, policy v{user_state.policy_version_applied}, "
            f"enforcement={user_state.last_enforcement or 'unknown'}"
        )

    sys.exit(0 if all_ok else 1)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    env_values = config_mod.read_env_file()

    parser = argparse.ArgumentParser(prog="timekpr-hub-agent")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="run the tick loop against an enrolled hub")
    _add_hub_connection_args(run_parser, env_values)
    run_parser.add_argument(
        "--users",
        default=config_mod.env_default("TIMEKPR_HUB_MANAGED_USERS", env_values),
        required=config_mod.env_default("TIMEKPR_HUB_MANAGED_USERS", env_values) is None,
        help="comma-separated list of local usernames to manage",
    )
    run_parser.add_argument("--tz", default=config_mod.env_default("TIMEKPR_HUB_TZ", env_values) or "UTC")
    run_parser.add_argument("--state-path", default=str(state_mod.DEFAULT_STATE_PATH))
    run_parser.add_argument("--once", action="store_true", help="run a single tick and exit (for testing)")
    run_parser.set_defaults(func=_cmd_run)

    enroll_parser = subparsers.add_parser(
        "enroll", help="redeem an enrollment code, store the token, write config, and start the service"
    )
    _add_hub_connection_args(enroll_parser, env_values, prompt_if_missing=True)
    enroll_parser.add_argument(
        "--code", default=None, help="one-time enrollment code from the hub (prompted if omitted)"
    )
    enroll_parser.add_argument(
        "--users",
        default=None,
        help="comma-separated local usernames this device reports (prompted interactively if omitted)",
    )
    enroll_parser.add_argument("--tz", default="UTC", help="overridden by the hub's HUB_TZ once enrolled")
    enroll_parser.add_argument("--hostname", default=None, help="default: this machine's hostname")
    enroll_parser.add_argument("--machine-id", default=None, help="default: /etc/machine-id")
    enroll_parser.add_argument(
        "--no-start", action="store_true", help="don't run `systemctl enable --now` after enrolling"
    )
    enroll_parser.set_defaults(func=_cmd_enroll)

    status_parser = subparsers.add_parser("status", help="check every link in the chain, one line per check")
    status_parser.add_argument("--token-path", default=str(DEFAULT_TOKEN_PATH))
    status_parser.add_argument("--state-path", default=str(state_mod.DEFAULT_STATE_PATH))
    status_parser.add_argument(
        "--hub-url", default=None, help="override TIMEKPR_HUB_URL from agent.env for this check"
    )
    status_parser.set_defaults(func=_cmd_status)

    args = parser.parse_args()
    args.func(args)
