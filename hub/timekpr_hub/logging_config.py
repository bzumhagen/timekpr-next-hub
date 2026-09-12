"""App-level logging setup.

Nothing configured the "timekpr_hub" logger at all until now
(docs/best-practices-review.md): `logging.getLogger("timekpr_hub")` calls
throughout the codebase (app.py's unclaimed-hub warning, main.py's tick
logging, etc.) relied on Python's defaults -- level WARNING, and only
reaching stderr at all via the standard library's unformatted "last resort"
handler (no timestamp, no logger name), with every `log.debug`/`log.info`
call silently dropped. Called once, at import time, by app.py.
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
