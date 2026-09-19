#!/usr/bin/env python3
"""Set the project version in all six places at once.

The counterpart to scripts/check_versions.py -- see that file for why the
version lives in six places rather than one. Rewrites them, then re-runs
the check so a botched substitution fails here rather than in CI.

Deliberately does not touch git: it leaves a dirty tree for you to review,
commit, and tag yourself (see docs/releasing.md).

Usage:
    scripts/bump_version.py 0.2.0
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from check_versions import AGENT_MAIN, PKGBUILD, PYPROJECTS, ROOT, VERSION_RE

# `version = "..."` in the [project] table, which in all four files is the
# first such line -- count=1 keeps a dependency pin like
# `version = "..."` elsewhere in the file out of scope.
PROJECT_VERSION_RE = re.compile(r'^version = "[^"]+"$', re.MULTILINE)


def _sub_once(path: Path, pattern: re.Pattern[str], replacement: str) -> None:
    text = path.read_text()
    new_text, count = pattern.subn(replacement, text, count=1)
    if count != 1:
        raise SystemExit(f"{path}: no version line matched -- has its format changed?")
    path.write_text(new_text)


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2

    version = sys.argv[1].removeprefix("v")
    if not VERSION_RE.match(version):
        print(
            f"{version!r} is not a MAJOR.MINOR.PATCH[a|b|rc]N version "
            "(makepkg forbids a hyphen in pkgver, so use 1.0.0rc1, not 1.0.0-rc1)",
            file=sys.stderr,
        )
        return 1

    for rel in PYPROJECTS:
        _sub_once(ROOT / rel, PROJECT_VERSION_RE, f'version = "{version}"')

    _sub_once(
        ROOT / AGENT_MAIN, re.compile(r'^AGENT_VERSION = "[^"]+"$', re.M), f'AGENT_VERSION = "{version}"'
    )

    # `pkgrel` counts packaging-only rebuilds of the *same* upstream
    # version, so a new pkgver always resets it to 1.
    pkgbuild = ROOT / PKGBUILD
    _sub_once(pkgbuild, re.compile(r"^pkgver=.+$", re.M), f"pkgver={version}")
    _sub_once(pkgbuild, re.compile(r"^pkgrel=.+$", re.M), "pkgrel=1")

    print(f"bumped to {version}:\n", flush=True)
    return subprocess.call([sys.executable, str(ROOT / "scripts" / "check_versions.py")])


if __name__ == "__main__":
    raise SystemExit(main())
