"""Read/write /etc/timekpr-hub-agent/agent.env.

PLAN "single-command enrollment": `enroll` writes this file itself instead
of printing it for a parent to paste by hand, and `run`'s argparse defaults
come from it (falling back further to the environment, e.g. when
`EnvironmentFile=` has already loaded it into the process for the systemd
unit) -- so the unit's ExecStart never has to change to match what a
particular device enrolled with. CLI flags still override everything, for
`--once` testing and one-off manual runs.
"""

from __future__ import annotations

import grp
import os
import pwd
from pathlib import Path

DEFAULT_ENV_PATH = Path("/etc/timekpr-hub-agent/agent.env")

# Keys written/read in agent.env, and the env var each maps to.
_KEYS = (
    "TIMEKPR_HUB_URL",
    "TIMEKPR_HUB_MANAGED_USERS",
    "TIMEKPR_HUB_TZ",
    "TIMEKPR_HUB_CA_CERT",
)


def read_env_file(path: Path = DEFAULT_ENV_PATH) -> dict[str, str]:
    """Parse a simple `KEY=value` file, one per line, `#` comments allowed.
    Missing file or unreadable file -> empty dict, never an exception --
    this is a fallback source of defaults, not a required config."""
    values: dict[str, str] = {}
    try:
        text = path.read_text()
    except OSError:
        return values
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key in _KEYS:
            values[key] = value.strip()
    return values


def env_default(key: str, env_values: dict[str, str]) -> str | None:
    """Precedence: real process environment (e.g. under systemd's
    EnvironmentFile=, or a parent's own `export`), then the config file
    directly (so `sudo timekpr-hub-agent status` works even when invoked
    outside systemd), then None (argparse's own default/required kicks in)."""
    return os.environ.get(key) or env_values.get(key) or None


def write_env_file(
    *,
    hub_url: str,
    managed_users: str,
    tz: str,
    ca_cert: str | None,
    owner_group: str = "timekpr-hub",
    path: Path = DEFAULT_ENV_PATH,
) -> None:
    """Write agent.env, group-readable by the service's own group so `run`
    (running as that user) can read it directly too, not just via
    EnvironmentFile=. 0640, owned by root:owner_group -- matches
    `agent/packaging/PKGBUILD`'s `install -Dm640` for this file."""
    lines = [
        "# Written by `timekpr-hub-agent enroll` -- re-run enroll (with a new",
        "# code) rather than hand-editing this to move a device to a",
        "# different hub or re-manage a different set of users.",
        f"TIMEKPR_HUB_URL={hub_url}",
        f"TIMEKPR_HUB_MANAGED_USERS={managed_users}",
        f"TIMEKPR_HUB_TZ={tz}",
        f"TIMEKPR_HUB_CA_CERT={ca_cert or ''}",
        "",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines))
    path.chmod(0o640)
    try:
        gid = grp.getgrnam(owner_group).gr_gid
        os.chown(path, 0, gid)
    except (KeyError, PermissionError, OSError):
        # Group doesn't exist yet (not installed via the package), or we're
        # not root (a manual/dev run) -- the file is still written and
        # usable, just not group-locked down. Not fatal.
        pass


class InvalidHubUrlError(ValueError):
    """Raised by `normalize_hub_url` for input that can't plausibly be a
    hub URL at all (empty, or a scheme other than http/https) -- distinct
    from ValueError so callers can catch it specifically without also
    swallowing an unrelated bug."""


def normalize_hub_url(raw: str) -> str:
    """Make `--hub-url`/the interactive prompt forgiving of the two most
    common ways to mistype it: no scheme at all (`192.168.1.5:8000`,
    `myhub:8000` -- a bare `urllib.request.Request` raises an opaque
    `ValueError: unknown url type` for these, which used to escape as a
    traceback rather than hubclient.EnrollError, see hubclient._post), and a
    trailing slash (harmless today only because `_post` separately
    `rstrip("/")`s it, but `write_env_file` persists whatever is passed here
    verbatim -- including the hub UI's own enrollment snippet, which builds
    the URL from `request.base_url` and that always carries one)."""
    value = raw.strip()
    if not value:
        raise InvalidHubUrlError("hub URL cannot be empty")
    if "://" not in value:
        value = f"http://{value}"
    scheme = value.split("://", 1)[0].lower()
    if scheme not in ("http", "https"):
        raise InvalidHubUrlError(f"unsupported URL scheme {scheme!r} -- use http:// or https://")
    return value.rstrip("/")


def chown_to_service_user(path: Path, username: str = "timekpr-hub") -> None:
    """Used after `enroll` writes the device token: it's created by
    whoever ran `enroll` (typically root via sudo), but the service reads
    it running as `username`. Without this, the *first* enrollment only
    works because systemd's StateDirectory= recursively re-owns the
    directory on first start; a re-enroll after the service has already
    run once would otherwise leave a root-owned token the service can't
    read (see docs/best-practices-review.md)."""
    try:
        pw = pwd.getpwnam(username)
    except KeyError:
        return  # not installed via the package (e.g. a dev/test run) -- leave as-is
    try:
        os.chown(path, pw.pw_uid, pw.pw_gid)
    except (PermissionError, OSError):
        pass  # not root -- caller should already have surfaced this some other way
