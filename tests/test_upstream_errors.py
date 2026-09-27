"""
Upstream failures must be typed and attributable, not silently degraded.

Theme
-----
Screener.in IP-blocks datacenter clients with HTTP 403. Before these tests, that
block was reported to the caller three different ways, none of them correct:

* ``ScreenerClient.search()``            -> returned ``[]``   (indistinguishable from "no match")
* ``ScreenerClient.resolve_company_id()``-> returned ``None`` (indistinguishable from "unknown symbol")
* ``ScreenerClient.get_chart_timeseries()`` -> raised ``TickerNotFoundError``
  i.e. "RELIANCE does not exist" for a company that very much does.

On top of that, ``Ticker._ensure_profile`` *cached* the empty fallback profile, so
one transient 403 permanently poisoned the Ticker instance.

The reference implementation for "classify, don't guess" is
``bfinance.nse.marketlens.MarketLensClient._get_json``, which tracks ``last_error``
and maps any non-200 non-404 to ``UpstreamServiceError``.

Leniency is preserved: ``Ticker(raise_errors=False)`` (the default) still degrades
to an empty profile instead of crashing. What changes is *which* error the strict
path reports, and that a transient failure is not memoised.
"""

import asyncio
from contextlib import contextmanager

import httpx
import pytest

import bfinance as bf
from bfinance.cache.sqlite_cache import SQLiteCache
from bfinance.screener.client import ScreenerClient
from bfinance.utils.exceptions import (
    BFinanceError,
    TickerNotFoundError,
    UpstreamServiceError,
)


# --------------------------------------------------------------------------- helpers


@contextmanager
def fake_httpx(responder):
    """Replace ``httpx.AsyncClient.get`` with ``responder(url) -> httpx.Response``.

    Yields a list that records every requested URL, so a test can assert on
    whether a call was *retried* (the point of defect 2) and not only on the
    final return value.
    """
    calls = []

    async def _get(self, url, *args, **kwargs):
        calls.append(str(url))
        return responder(str(url))

    original = httpx.AsyncClient.get
    httpx.AsyncClient.get = _get
    try:
        yield calls
    finally:
        httpx.AsyncClient.get = original


def _response(status, url, json=None):
    return httpx.Response(status, json=json, request=httpx.Request("GET", url))


def blocked(status=403):
    return lambda url: _response(status, url)


def empty_ok(url):
    """A genuine upstream miss: HTTP 200 with an empty JSON list."""
    return _response(200, url, json=[])


def _client():
    """Uncached, no-retry, unpaced client so nothing touches the real cache or network."""
    return ScreenerClient(timeout=5, cache_ttl_hours=0, max_retries=1, min_request_interval_ms=0)


# ------------------------------------------------- defect 1: a block is not "no result"


def test_search_raises_upstream_service_error_on_403():
    """The search step must not turn a blocked request into an empty result set."""
    with fake_httpx(blocked(403)):
        with pytest.raises(UpstreamServiceError) as excinfo:
            asyncio.run(_client().search("RELIANCE"))
    assert "403" in str(excinfo.value)


def test_search_error_message_does_not_claim_the_ticker_is_missing():
    """A block must not be reported in the vocabulary of a delisted ticker."""
    with fake_httpx(blocked(403)):
        with pytest.raises(UpstreamServiceError) as excinfo:
            asyncio.run(_client().search("RELIANCE"))
    assert "not found" not in str(excinfo.value).lower()


def test_search_error_is_a_bfinance_error_so_lenient_callers_catch_it():
    with fake_httpx(blocked(403)):
        with pytest.raises(BFinanceError):
            asyncio.run(_client().search("RELIANCE"))


def test_resolve_company_id_propagates_the_block_instead_of_returning_none():
    with fake_httpx(blocked(403)):
        with pytest.raises(UpstreamServiceError):
            asyncio.run(_client().resolve_company_id("RELIANCE"))


def test_chart_timeseries_reports_a_block_as_upstream_error_not_a_missing_ticker():
    """The regression that motivated the whole change: 403 used to raise
    ``TickerNotFoundError("Cannot resolve company ID for 'RELIANCE'")``."""
    with fake_httpx(blocked(403)):
        with pytest.raises(UpstreamServiceError) as excinfo:
            asyncio.run(_client().get_chart_timeseries("RELIANCE"))
    assert not isinstance(excinfo.value, TickerNotFoundError)
    assert "RELIANCE" in str(excinfo.value)


def test_get_company_profile_still_raises_upstream_service_error_on_403():
    """Already correct before the fix; pinned so the search change cannot regress it."""
    with fake_httpx(blocked(403)):
        with pytest.raises(UpstreamServiceError):
            asyncio.run(_client().get_company_profile("RELIANCE"))


def test_search_5xx_is_an_upstream_error_not_an_empty_result():
    """_send_with_retry burns its retries on 5xx, then raises UpstreamServiceError."""
    with fake_httpx(blocked(503)):
        with pytest.raises(UpstreamServiceError):
            asyncio.run(_client().search("RELIANCE"))


def test_rate_limit_keeps_its_own_error_type():
    with fake_httpx(blocked(429)):
        with pytest.raises(UpstreamServiceError) as excinfo:
            asyncio.run(_client().search("RELIANCE"))
    assert "rate limit" in str(excinfo.value).lower()


# ------------------------------------------- the distinction that must survive the fix


def test_genuine_search_miss_is_still_an_empty_list():
    """HTTP 200 + [] is a real 'nothing matched' and must stay a quiet empty list."""
    with fake_httpx(empty_ok):
        assert asyncio.run(_client().search("NOSUCHTICKERXYZ")) == []


def test_genuine_miss_still_produces_ticker_not_found_for_the_chart_step():
    with fake_httpx(empty_ok):
        with pytest.raises(TickerNotFoundError):
            asyncio.run(_client().get_chart_timeseries("NOSUCHTICKERXYZ"))


def test_block_and_miss_are_distinguishable():
    """The whole point: one is an outage, the other is a bad symbol."""
    with fake_httpx(blocked(403)):
        with pytest.raises(UpstreamServiceError) as blocked_exc:
            asyncio.run(_client().search("RELIANCE"))
    with fake_httpx(empty_ok):
        assert asyncio.run(_client().search("RELIANCE")) == []
    assert type(blocked_exc.value) is not TickerNotFoundError


# -------------------------------------- defect 2: a transient failure must not be cached


def test_blocked_first_access_does_not_poison_the_ticker_profile():
    with fake_httpx(blocked(403)):
        ticker = bf.Ticker("RELIANCE.NS", cache_ttl_hours=0, raise_errors=False)
        profile = ticker._ensure_profile()

    # Lenient default: degrades, does not crash.
    assert profile is not None
    assert profile.company_id == 0
    # The defect: the empty fallback was assigned to self._profile, which is exactly
    # what suppresses the refetch on the next access.
    assert ticker._profile is None, "empty fallback profile must not be memoised"


def test_second_access_after_a_transient_failure_actually_retries():
    calls = []

    def fail_first(url):
        calls.append(url)
        return _response(403, url)

    with fake_httpx(fail_first):
        ticker = bf.Ticker("RELIANCE.NS", cache_ttl_hours=0, raise_errors=False)
        ticker._ensure_profile()
        first_round = len(calls)
        ticker._ensure_profile()
        second_round = len(calls)

    assert first_round > 0
    assert second_round > first_round, "a cached failure was replayed instead of refetching"


def test_recovery_after_a_transient_failure_is_observed():
    """Block once, then let the source through: the Ticker must pick up real fundamentals."""
    real = bf.CompanyProfile(symbol="RELIANCE", company_id=42, name="Reliance Industries")
    attempts = {"n": 0}

    async def flaky_profile(ticker, force_refresh=False):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise UpstreamServiceError("Screener.in returned status code 403")
        return real

    ticker = bf.Ticker("RELIANCE.NS", cache_ttl_hours=0, raise_errors=False)
    ticker.screener_client.get_company_profile = flaky_profile

    assert ticker._ensure_profile().company_id == 0, "first access degrades"
    assert ticker._ensure_profile().company_id == 42, "second access must see the recovery"
    assert ticker._ensure_profile() is real, "a real profile is cached normally"
    assert attempts["n"] == 2


def test_lenient_default_still_degrades_instead_of_raising():
    with fake_httpx(blocked(403)):
        ticker = bf.Ticker("RELIANCE.NS", cache_ttl_hours=0, raise_errors=False)
        assert ticker.sector is None
        assert ticker.industry is None


def test_raise_errors_true_still_propagates():
    with fake_httpx(blocked(403)):
        ticker = bf.Ticker("RELIANCE.NS", cache_ttl_hours=0, raise_errors=True)
        with pytest.raises(UpstreamServiceError):
            ticker._ensure_profile()


def test_async_profile_variant_does_not_cache_the_fallback_either():
    with fake_httpx(blocked(403)):
        ticker = bf.Ticker("RELIANCE.NS", cache_ttl_hours=0, raise_errors=False)
        asyncio.run(ticker.get_profile_async())
        assert ticker._profile is None


# ------------------------------------- defect 3: cache=None must mean "no cache", literally


def test_cache_none_disables_the_cache(monkeypatch, tmp_path):
    """``cache=None`` used to be indistinguishable from 'argument omitted'."""

    class _Recorder(SQLiteCache):
        def __init__(self, default_ttl_hours=24.0, db_path=None):
            self.db_path = "<home-dir cache>"
            self.default_ttl_hours = default_ttl_hours

    monkeypatch.setattr("bfinance.screener.client.SQLiteCache", _Recorder)
    client = ScreenerClient(timeout=1, cache=None)
    assert client.cache is None


def test_omitted_cache_still_builds_the_default_cache(monkeypatch):
    """Existing callers that never pass ``cache`` must keep their home-dir cache."""

    built = []

    class _Recorder:
        def __init__(self, default_ttl_hours=24.0, db_path=None):
            built.append(default_ttl_hours)

    monkeypatch.setattr("bfinance.screener.client.SQLiteCache", _Recorder)
    client = ScreenerClient(timeout=1)
    assert isinstance(client.cache, _Recorder)
    assert built == [24.0]


def test_explicit_cache_object_is_used_as_given(temp_cache):
    client = ScreenerClient(timeout=1, cache=temp_cache)
    assert client.cache is temp_cache


def test_cache_ttl_hours_zero_still_means_no_cache(monkeypatch):
    monkeypatch.setattr(
        "bfinance.screener.client.SQLiteCache",
        lambda *a, **k: pytest.fail("SQLiteCache built despite cache_ttl_hours=0"),
    )
    assert ScreenerClient(timeout=1, cache_ttl_hours=0).cache is None


def test_ticker_with_no_cache_passes_no_cache_through(monkeypatch):
    """Ticker(cache_ttl_hours=0) passes cache=None down; that must stay 'disabled'."""
    monkeypatch.setattr(
        "bfinance.screener.client.SQLiteCache",
        lambda *a, **k: pytest.fail("ScreenerClient rebuilt a cache Ticker had disabled"),
    )
    assert bf.Ticker("RELIANCE.NS", cache_ttl_hours=0).screener_client.cache is None


def test_cache_arg_and_ttl_are_documented_as_distinct_intents():
    """Regression guard: the two spellings must stay distinguishable in the source."""
    import inspect

    signature = inspect.signature(ScreenerClient.__init__)
    assert signature.parameters["cache"].default is not None, (
        "the `cache` default must be a sentinel, not None, or 'omitted' and "
        "'explicitly disabled' collapse into the same value again"
    )
