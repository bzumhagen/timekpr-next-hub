"""Shared test-session setup.

Sets `DATABASE_URL` to the test database *before* anything in the test
session imports `timekpr_hub` (and therefore `hub/timekpr_hub/db/session.py`,
which builds its engine off that env var at import time -- see the module's
own docstring). Most DB-backed tests never touch that module-level engine
directly (they override FastAPI's `get_session` dependency instead, per
`tests/integration/test_hub_api.py`'s top-of-file comment), so this didn't
matter before. `tests/e2e` runs the real app under a real uvicorn server with
no such override -- there, whichever env var happened to be set (or not) when
`timekpr_hub.app` was first imported would silently decide which database a
live e2e run actually hits, and pytest's collection order decides that
import's timing, not anything explicit. Setting this here, in the one file
pytest always loads before collecting any test module, removes that ordering
accident entirely.
"""

from __future__ import annotations

import os

from tests.dbutil import TEST_DATABASE_URL

os.environ.setdefault("DATABASE_URL", TEST_DATABASE_URL)
