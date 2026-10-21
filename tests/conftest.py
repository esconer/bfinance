"""
Pytest test fixtures and configuration for bfinance test suite.
"""

import asyncio
import os
import sqlite3

import pandas as pd
import pytest
from bfinance.cache.sqlite_cache import SQLiteCache
from bfinance.screener.client import ScreenerClient


#: ``$BFINANCE_HOME`` as the process had it before any fixture could change it. It is what lets the
#: autouse fixture below tell "the runner pointed the home somewhere for the whole session" apart
#: from "a test pointed it somewhere for itself": only the second is a choice to leave alone.
_SHELL_HOME = os.environ.get("BFINANCE_HOME")


@pytest.fixture(autouse=True)
def bfinance_home(tmp_path, monkeypatch):
    """One ``$BFINANCE_HOME`` per test, so nothing one test caches can be served to the next one.

    **The defect this exists for.** ``OHLCVEngine`` reads through a ``PriceCache`` by default
    (``market/ohlcv.py:869``), whose store is ``$BFINANCE_HOME/frames.db`` -- a *shared* file that
    building an engine does not reset. So a frame one test caches is handed to the next test that
    asks the same question, and that test is then measuring the earlier test's fixture instead of its
    own stub. Measured while this was being written: a row for ``RELIANCE|NSE|ALL|2024-08-16|2024-08-21``
    written during one test answered a later test whose own archive stub returned nothing at all, so
    "an empty frame still raises" came back as "a hit from somebody else's data".

    Three artefacts resolve through this variable and are isolated by this fixture: the frame store
    (``cache/frame_store.py:141-158``), the bhavcopy mirror (``mirror.py:102-105``) and the universe
    archive (``universe.py:762``). It does **not** isolate ``SQLiteCache``'s default file:
    ``sqlite_cache.py:109-119`` places that at ``~/.bfinance/cache.db`` and does not consult
    ``$BFINANCE_HOME``, so a test that builds a ``ScreenerClient`` with no cache of its own still
    writes to the user's real home. That gap is pre-existing and not this fixture's to close.

    **A test's own choice wins, whichever order the two fixtures run in.** The suite already has
    modules and tests that point the home at a directory of their own -- ``test_mirror_integration.py``'s
    autouse fixture, ``test_mirror.py``, ``test_frame_store.py``, ``test_market_price_cache.py``,
    ``test_ohlcv_cached_window.py`` -- and the stand-down below is what leaves them in charge of what
    they measure: it declines to touch the variable once it is no longer the shell's value, i.e. once
    another fixture has pointed it somewhere; and when this fixture runs first, that other fixture's
    ``monkeypatch.setenv`` overwrites this one anyway.

    The directory is *named* and never created, so "nothing was written" stays assertable: the path
    exists only if the code under test makes it.

    Tests: ``tests/test_bfinance_home_isolation.py``.
    """
    if os.environ.get("BFINANCE_HOME") != _SHELL_HOME:
        return
    monkeypatch.setenv("BFINANCE_HOME", str(tmp_path / "bfinance_home"))


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
