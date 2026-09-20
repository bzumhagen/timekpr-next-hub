#!/usr/bin/env python3
"""Set the project version in all six places at once.

The counterpart to scripts/check_versions.py -- see that file for why the
version lives in six places rather than one. Rewrites them, re-locks (so
uv.lock's own version fields don't drift), then re-runs the check so a
botched substitution fails here rather than in CI.

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


def _substituted(path: Path, pattern: re.Pattern[str], replacement: str) -> str:
    text = path.read_text()
    new_text, count = pattern.subn(replacement, text, count=1)
    if count != 1:
        raise SystemExit(f"{path}: no version line matched -- has its format changed?")
    return new_text


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

    # Compute every replacement before writing any of them, so a pattern
    # that fails to match (e.g. a file whose format changed) leaves the
    # tree untouched instead of half-bumped.
    pkgbuild = ROOT / PKGBUILD
    writes = {
        ROOT / rel: _substituted(ROOT / rel, PROJECT_VERSION_RE, f'version = "{version}"')
        for rel in PYPROJECTS
    }
    writes[ROOT / AGENT_MAIN] = _substituted(
        ROOT / AGENT_MAIN, re.compile(r'^AGENT_VERSION = "[^"]+"$', re.M), f'AGENT_VERSION = "{version}"'
    )
    # `pkgrel` counts packaging-only rebuilds of the *same* upstream
    # version, so a new pkgver always resets it to 1.
    pkgbuild_text = _substituted(pkgbuild, re.compile(r"^pkgver=.+$", re.M), f"pkgver={version}")
    pkgbuild_text, count = re.compile(r"^pkgrel=.+$", re.M).subn("pkgrel=1", pkgbuild_text, count=1)
    if count != 1:
        raise SystemExit(f"{pkgbuild}: no pkgrel line matched -- has its format changed?")
    writes[pkgbuild] = pkgbuild_text

    for path, new_text in writes.items():
        path.write_text(new_text)

    print(f"bumped to {version}, re-locking:\n", flush=True)
    lock = subprocess.call(["uv", "lock"], cwd=ROOT)
    if lock != 0:
        return lock

    return subprocess.call([sys.executable, str(ROOT / "scripts" / "check_versions.py")])


if __name__ == "__main__":
    raise SystemExit(main())
