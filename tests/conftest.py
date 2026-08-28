"""
Pytest test fixtures and configuration for bfinance test suite.
"""

import pytest
from bfinance.cache.sqlite_cache import SQLiteCache
from bfinance.screener.client import ScreenerClient


@pytest.fixture
def temp_cache():
    """In-memory SQLite cache instance for tests."""
    cache = SQLiteCache(db_path=":memory:", default_ttl_hours=1.0)
    return cache


@pytest.fixture
def screener_client(temp_cache):
    """ScreenerClient initialized with test cache."""
    return ScreenerClient(timeout=15.0, cache=temp_cache)
