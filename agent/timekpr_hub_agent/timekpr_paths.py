"""Locate the installed timekpr Python package on disk.

PLAN reference: "Agent — Python 3.11+" and phase0-findings.md §4.

`timekprd` itself invokes `/usr/bin/python3 /usr/lib/python3/dist-packages/
timekpr/server/timekprd.py` directly by hardcoded path (confirmed by reading
its installed shebang wrapper on this machine, an Arch/CachyOS box) -- it
does NOT rely on `sys.path`, and that directory is not on a typical distro's
default `sys.path` either (Arch's system Python looks under
`/usr/lib/python3.NN/site-packages`, not `dist-packages`). So the agent must
explicitly locate and prepend this directory before importing
`timekpr.client.interface.dbus.administration`.
"""

from __future__ import annotations

import glob
import sys
from pathlib import Path

# Debian/Ubuntu package the whole tree under this fixed path regardless of
# the Python version in use.
_DIST_PACKAGES_CANDIDATES = ["/usr/lib/python3/dist-packages"]

# Arch/CachyOS (and this timekpr-next build, confirmed empirically) also uses
# dist-packages -- but cover the site-packages layout too in case a future
# packaging changes that, or another distro places it there.
_SITE_PACKAGES_GLOB = "/usr/lib/python3.*/site-packages"


class TimekprNotFoundError(RuntimeError):
    pass


def find_timekpr_package_dir() -> Path:
    """Return the directory that contains the `timekpr` package (i.e. the
    directory to add to `sys.path`, not the package directory itself).

    Raises `TimekprNotFoundError` with an actionable message if not found --
    per PLAN: "fails with a clear message" rather than a bare ImportError.
    """
    candidates = list(_DIST_PACKAGES_CANDIDATES) + sorted(glob.glob(_SITE_PACKAGES_GLOB))

    checked = []
    for candidate in candidates:
        pkg_dir = Path(candidate) / "timekpr"
        checked.append(str(pkg_dir))
        if (pkg_dir / "client" / "interface" / "dbus" / "administration.py").is_file():
            return Path(candidate)

    raise TimekprNotFoundError(
        "Could not locate the installed timekpr-next Python package "
        "(looked for timekpr/client/interface/dbus/administration.py under: "
        + ", ".join(checked)
        + "). Is timekpr-next installed on this machine? "
        "See https://github.com/mjasnik/timekpr-next for installation instructions."
    )


def ensure_timekpr_importable() -> None:
    """Idempotently add the timekpr package directory to sys.path.

    Call this before `from timekpr.client.interface.dbus.administration
    import timekprAdminConnector` anywhere in the agent.
    """
    pkg_parent = str(find_timekpr_package_dir())
    if pkg_parent not in sys.path:
        sys.path.insert(0, pkg_parent)
