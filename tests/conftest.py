"""
Pytest test fixtures and configuration for bfinance test suite.
"""

import asyncio
import sqlite3

import pandas as pd
import pytest
from bfinance.cache.sqlite_cache import SQLiteCache
from bfinance.screener.client import ScreenerClient


# The previous fixture used ``db_path=":memory:"``. That was a no-op: ``_connect()`` opens a NEW
# connection on every call, and every ``:memory:`` connection is a separate throwaway database.
# ``_init_db`` therefore created the table in a database that was immediately discarded, and every
# later ``get``/``set`` hit a schema-less database — every "cache" test was silently exercising the
# UNCACHED path, while emitting "no such table: bfinance_cache" per call.
#
# A real file is used instead. It is slower than an in-memory database but it exercises the actual
# code path, including the WAL and busy_timeout pragmas that are no-ops on ``:memory:``.
#
# ``:memory:`` can be made to work with ``file::memory:?cache=shared`` plus a held-open keeper
# connection, but that still skips the file-backed pragmas, so it would still not test what ships.


@pytest.fixture
def cache_path(tmp_path):
    """Real on-disk SQLite path, unique per test."""
    return str(tmp_path / "bfinance_test_cache.db")


@pytest.fixture
def temp_cache(cache_path):
    """A working SQLiteCache. ``set`` then ``get`` must return the stored value."""
    return SQLiteCache(db_path=cache_path, default_ttl_hours=1.0)


@pytest.fixture
def screener_client(temp_cache):
    """ScreenerClient initialized with a working test cache."""
    return ScreenerClient(timeout=15.0, cache=temp_cache)


@pytest.fixture
def shared_memory_conn(cache_path):
    """A raw connection to the same file, for asserting on cache internals."""
    conn = sqlite3.connect(cache_path, timeout=5.0)
    try:
        yield conn
    finally:
        conn.close()


class StubCorporateActionsClient:
    """Offline stand-in for :class:`bfinance.nse.corporate_actions.CorporateActionsClient`.

    ``OHLCVEngine(screener_client, corporate_actions_client=None)`` documents its second argument
    as "injected by tests", and
    :func:`bfinance.market.corporate.CorporateActionsEngine.resolve_actions_async` calls exactly
    one method on it — ``actions_async(symbol, start=, end=)`` — returning a frame with
    :data:`ACTION_COLUMNS`. When that argument is ``None`` a *real* client is constructed, which is
    how four tests inside the ``-m "not live"`` selection were making live DNS calls to
    ``www.nseindia.com``: a cold ``HOME`` has no ``~/.bfinance`` cache to satisfy them, so the
    request went out. The calls only appear on a fresh machine — which is exactly where CI runs,
    and exactly why a warm local cache hides them.

    Every call is recorded in :attr:`calls` so a test can assert the request was made in-process
    rather than over the network.
    """

    def __init__(self, rows=None):
        self._rows = list(rows or [])
        self.calls = []

    async def actions_async(self, symbol, start=None, end=None, **kwargs):
        from bfinance.nse.corporate_actions import ACTION_COLUMNS

        self.calls.append((symbol, start, end))
        index = pd.DatetimeIndex([], tz="Asia/Kolkata", name="Date")
        return pd.DataFrame(
            {column: pd.Series(dtype="float64") for column in ACTION_COLUMNS}, index=index
        )

    def actions(self, symbol, start=None, end=None, **kwargs):
        return asyncio.run(self.actions_async(symbol, start=start, end=end))


@pytest.fixture
def stub_actions_client():
    """A :class:`StubCorporateActionsClient` that reports no actions. Records every call."""
    return StubCorporateActionsClient()
