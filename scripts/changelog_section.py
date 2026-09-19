#!/usr/bin/env python3
"""Print one version's section of CHANGELOG.md.

The release workflow feeds this to `gh release create --notes-file`, so
the hand-written "what changed and why it matters" text is what a reader
sees first, with GitHub's auto-generated commit list appended under it.

Exits non-zero if the version has no section, which is deliberate: the
release workflow runs this as an early gate, so a tag with no changelog
entry fails before anything is published rather than after.

Usage:
    scripts/changelog_section.py 0.2.0
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

CHANGELOG = Path(__file__).resolve().parent.parent / "CHANGELOG.md"

# `## [0.2.0] - 2026-09-19`, the Keep a Changelog heading form.
HEADING_RE = re.compile(r"^## \[(?P<version>[^\]]+)\]")


def section(version: str) -> str | None:
    lines = CHANGELOG.read_text().splitlines()
    start = None
    for i, line in enumerate(lines):
        match = HEADING_RE.match(line)
        if match is None:
            continue
        if start is not None:
            return "\n".join(lines[start:i]).strip()
        if match.group("version") == version:
            start = i + 1
    if start is None:
        return None
    # Last section in the file: everything to the end, minus any trailing
    # link-reference block (`[0.2.0]: https://...`).
    rest = [ln for ln in lines[start:] if not re.match(r"^\[[^\]]+\]: ", ln)]
    return "\n".join(rest).strip()


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2

    version = sys.argv[1].removeprefix("v")
    body = section(version)
    if not body:
        print(
            f"CHANGELOG.md has no (non-empty) `## [{version}]` section.\n"
            "Add one before tagging -- it becomes the release notes.",
            file=sys.stderr,
        )
        return 1

    print(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
