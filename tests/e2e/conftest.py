"""The `live_hub` fixture, as a conftest.py fixture rather than a name
importable from tests/e2e/harness.py.

pytest injects a conftest.py fixture into any test by parameter name alone,
with no import needed -- which matters here specifically: a test function
parameter named `live_hub` needs `live_hub` in scope for pytest to *find*
the fixture, but importing the fixture function into that same module (so
it's in scope) makes ruff correctly flag the parameter as redefining that
import (F811). Defining it here instead of in harness.py sidesteps the
conflict entirely rather than silencing it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest_asyncio

from tests.e2e.harness import start_live_hub


@pytest_asyncio.fixture
async def live_hub() -> AsyncIterator[str]:
    async for base_url in start_live_hub():
        yield base_url
