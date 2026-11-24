"""Test fixtures.

pytest-asyncio (mode=auto) runs each async test on its OWN event loop, but pythia.db keeps a
module-global asyncpg pool. A pool created in one test's loop is invalid in the next ("got
Future attached to a different loop"). Close it after every test so each gets a fresh pool.
"""

from __future__ import annotations

import pytest

from pythia.db import close_pool


@pytest.fixture(autouse=True)
async def _fresh_db_pool():
    yield
    await close_pool()
