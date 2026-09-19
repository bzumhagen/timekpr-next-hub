#!/usr/bin/env python3
"""Assert that every place this project writes its version agrees.

There are six of them, and they cannot be collapsed into one:

  - the four `pyproject.toml` files (root + core/hub/agent) -- uv wants a
    literal `version` in each workspace member, and the root's is what a
    plain `pip install .` of the repo would report;
  - `AGENT_VERSION` in `agent/timekpr_hub_agent/main.py` -- a literal
    rather than `importlib.metadata.version()` because the Arch package
    copies raw `.py` files into `/usr/lib/timekpr-hub-agent/` with no
    dist-info, so the metadata lookup would raise on every real install
    (see the comment above the constant);
  - `pkgver` in `agent/packaging/PKGBUILD` -- makepkg needs a literal, and
    it is also what names the release tarball the PKGBUILD downloads.

`tests/unit/test_agent_config.py` already pins AGENT_VERSION to
agent/pyproject.toml; this covers the other four sites, and with
`--expect` it is also the release workflow's gate that the git tag
matches what the tree actually declares.

Usage:
    scripts/check_versions.py              # all six agree with each other
    scripts/check_versions.py --expect 1.2.3   # ...and equal 1.2.3
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PYPROJECTS = ("pyproject.toml", "core/pyproject.toml", "hub/pyproject.toml", "agent/pyproject.toml")
AGENT_MAIN = "agent/timekpr_hub_agent/main.py"
PKGBUILD = "agent/packaging/PKGBUILD"

# Kept in sync with scripts/bump_version.py, which rewrites these two.
AGENT_VERSION_RE = re.compile(r'^AGENT_VERSION = "(?P<version>[^"]+)"$', re.MULTILINE)
PKGVER_RE = re.compile(r"^pkgver=(?P<version>.+)$", re.MULTILINE)

# PEP 440 release segment only. Deliberately narrower than PEP 440 at
# large: makepkg forbids a hyphen in `pkgver`, so a Python-style
# pre-release (1.0.0rc1) is fine but 1.0.0-rc1 is not, and this project
# has no use for epochs or local versions.
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+([ab]|rc)?\d*$")


def _read_versions() -> dict[str, str]:
    found: dict[str, str] = {}

    for rel in PYPROJECTS:
        data = tomllib.loads((ROOT / rel).read_text())
        found[rel] = data["project"]["version"]

    for rel, pattern, label in (
        (AGENT_MAIN, AGENT_VERSION_RE, "AGENT_VERSION"),
        (PKGBUILD, PKGVER_RE, "pkgver"),
    ):
        match = pattern.search((ROOT / rel).read_text())
        if match is None:
            raise SystemExit(f"{rel}: no {label} line found -- has its format changed?")
        found[rel] = match.group("version")

    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--expect",
        metavar="VERSION",
        help="also require every site to equal this (the release tag, minus its leading 'v')",
    )
    args = parser.parse_args()

    found = _read_versions()
    expected = args.expect or next(iter(found.values()))

    mismatched = {rel: v for rel, v in found.items() if v != expected}
    width = max(len(rel) for rel in found)
    for rel, value in found.items():
        marker = "✗" if rel in mismatched else "✓"
        print(f"  {marker} {rel:<{width}}  {value}")

    if mismatched:
        source = f"the tag ({expected})" if args.expect else f"{next(iter(found))} ({expected})"
        print(f"\nversion mismatch: {len(mismatched)} site(s) disagree with {source}", file=sys.stderr)
        print("run `make bump VERSION=<x.y.z>` to set all of them at once", file=sys.stderr)
        return 1

    if not VERSION_RE.match(expected):
        print(
            f"\n{expected!r} is not a MAJOR.MINOR.PATCH[a|b|rc]N version "
            "(makepkg forbids a hyphen in pkgver, so use 1.0.0rc1, not 1.0.0-rc1)",
            file=sys.stderr,
        )
        return 1

    print(f"\nall {len(found)} version sites agree: {expected}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
