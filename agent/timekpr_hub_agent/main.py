"""Console-script entry point for `timekpr-hub-agent`.

`agent/pyproject.toml`'s `[project.scripts]` and
`agent/packaging/timekpr-hub-agent`'s launcher both hardcode
`timekpr_hub_agent.main:main` -- the actual `run`/`enroll`/`status`
implementation lives in `cli.py` (the tick loop itself in `tick.py`,
pushing a policy to timekpr in `policy_push.py`).
"""

from __future__ import annotations

# Not read via importlib.metadata: the PKGBUILD copies raw .py files into
# system site-packages with no dist-info (see agent/packaging/PKGBUILD's
# header comment), so that lookup would raise PackageNotFoundError on every
# real packaged install. Kept a literal, pinned to agent/pyproject.toml's
# `version` by tests/unit/test_agent_config.py, and regexed directly by
# scripts/check_versions.py/bump_version.py -- keep this a single,
# unconditional assignment.
AGENT_VERSION = "0.1.0"


def main() -> None:
    # Deferred: cli.py imports AGENT_VERSION back from this module, so a
    # module-level import here would make the two modules import each
    # other. Importing lazily, only once main() is actually called, means
    # this module always finishes defining AGENT_VERSION before cli.py is
    # ever loaded, regardless of which of the two a caller imports first.
    from timekpr_hub_agent.cli import main as _cli_main

    _cli_main()


if __name__ == "__main__":
    main()
