"""
Pytest test fixtures and configuration for bfinance test suite.
"""

import sqlite3

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
