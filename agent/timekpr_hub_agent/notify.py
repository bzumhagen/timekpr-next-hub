"""Minimal sd_notify client -- no dependency on python-systemd.

PLAN "reboot survival": the systemd unit is `Type=notify` with a
`WatchdogSec=`, so a hung DBUS call or a wedged HTTP request gets the agent
killed and restarted (`Restart=always`) rather than sitting there forever
looking alive to systemd. Talking to $NOTIFY_SOCKET directly is ~15 lines
and avoids a dependency most distros ship as part of systemd's own Python
bindings anyway (inconsistently named across distros), which matters for
the "no bundled venv" packaging goal (CHECKLIST "Multi-distro support").
"""

from __future__ import annotations

import os
import socket


def sd_notify(message: str) -> None:
    """Send a message to systemd's notification socket, e.g. "READY=1" or
    "WATCHDOG=1". A no-op when not running under systemd (no
    $NOTIFY_SOCKET, e.g. `enroll` or a manual `run` in a terminal) or if
    the send fails for any reason -- notification is a nicety, never
    load-bearing for the agent's own logic."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):
        addr = "\0" + addr[1:]  # abstract namespace socket
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(addr)
            sock.sendall(message.encode("utf-8"))
    except OSError:
        pass
