"""App-level logging setup.

Without this, nothing configures the "timekpr_hub" logger at all: every
`log.debug`/`log.info`/`log.warning` call in the hub (app.py's
unclaimed-hub warning among them) would fall back to Python's defaults --
level WARNING, reaching stderr only via the standard library's unformatted
"last resort" handler (no timestamp, no logger name). Called once, at
import time, by app.py.
"""

from __future__ import annotations

import logging
import os


def configure_logging() -> None:
    level_name = os.environ.get("TIMEKPR_HUB_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    logger = logging.getLogger("timekpr_hub")
    if logger.handlers:
        return  # already configured -- e.g. re-imported under a test harness
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False  # don't also hand these to the root/uvicorn config
