"""
E1 — the symbol -> company-id mapping is cached, so a chart-cache **HIT costs zero requests**.

**Why this file exists.** ``get_chart_timeseries`` builds its cache key out of the numeric company
id (``screener_chart_{id}_{metric}_{days}``), so the id has to exist *before* the chart row can be
looked up — and ``resolve_company_id`` used to be an unconditional HTTP request. A warm chart row
therefore still cost a live request on every call, and the cost was invisible: the frame came back
saying ``'hit'`` while the network was being asked about the symbol. Measured here, on the recorded
ITC fixtures, before the fix:

    chart call 1: 2 transport calls, frame status 'miss'   (company page + chart API)
    chart call 2: 1 transport call,  frame status 'hit'    <-- the defect
    chart call 3: 1 transport call,  frame status 'hit'

A company id is *identity*, not a price: it does not change for the life of a listing, so re-reading
the page can only discover whether the page is reachable today. The mapping is therefore cached
under this client's existing TTL vocabulary — a real ``CATEGORY_TTL_HOURS["company_id"]`` row at
168h, the same life ``search`` gives the same fact — and after the fix chart calls 2 and 3 spend
**0** transport calls.

**The direction of the write is half the fix.** ``get_company_profile`` already fetches the page the
id rides on, so it writes the row for free. Routing the profile *through* ``resolve_company_id``
would add a request to the profile path — a latency regression dressed as a refactor — so the
profile path is pinned to the exact two requests it made before.

**``search()`` stays off this path entirely.** It is robots-disallowed (``Disallow: /*?q=``) and was
the source of a documented silent-empty-profile incident. It appears below only as the route a
mutation would have to reintroduce to turn a test red (``TestNoSearchFallback``).

Nothing here touches the network and nothing reads the user's ``~/.bfinance`` cache: every client is
built with the ``temp_cache`` fixture (a real on-disk :class:`SQLiteCache` under ``tmp_path``) or
with ``cache=None``. Every test names, in its docstring, the mutation it dies on; the five
load-bearing ones are also demonstrated by actually applying that mutation to ``screener/client.py``
(see the task report), because "it passed" is not evidence that a test would fail.
"""

from __future__ import annotations

import inspect
import json
import sqlite3
import time

import pandas as pd
import pytest

from bfinance.screener.charts import ScreenerChartEngine
from bfinance.screener.client import ScreenerClient, extract_company_id

# The transport stub and the recorded fixtures are shared with the other screener test modules
# rather than copied: `_send_with_retry` is the single seam every request goes through, so two
# copies could drift into testing different things. `tests/` is not a package and pytest prepends
# its own directory to `sys.path`, so the module is imported by bare name, exactly as pytest
# imports it (the same convention `tests/test_cache_status_provenance.py` uses).
from test_screener_offline import (
    Recordings,
    chart_route,
    company_route,
    consolidated_route,
    peers_route,
    routes,
    text,
)

#: Read off the recordings rather than typed in, so a re-recorded fixture with a different id makes
#: this file fail loudly instead of silently addressing a route the stub does not serve.
ITC_ID = extract_company_id(text("company_itc.html"))
TCS_ID = extract_company_id(text("company_tcs.html"))
ITC_CLOSE = 269.0  # #top-ratios' 'Current Price' on the ITC recording, and the chart's last close


def id_key(symbol: str) -> str:
    """The key the client is expected to cache a symbol's id under, written out longhand.

    Written out here rather than imported from the client so that a rename turns this into a red
    test naming the side that moved, instead of a test that quietly addresses a key nobody writes.
    """
    return f"screener_company_id_{symbol}"


def cached_client(recorder: Recordings, cache) -> ScreenerClient:
    """A :class:`ScreenerClient` on an explicit cache, with the transport replaced."""
    client = ScreenerClient(cache=cache, min_request_interval_ms=0.0, max_retries=1)
    client._send_with_retry = recorder
    return client


def id_rows(cache_path: str) -> list[str]:
    """Every ``company_id`` row in the cache file, read straight out of the table.

    The point of the key move is *one row per company*, and one row is a claim about the table —
    so it is read from the table rather than inferred from a request count.
    """
    with sqlite3.connect(cache_path, timeout=5.0) as conn:
        return [
            row[0]
            for row in conn.execute(
                "SELECT cache_key FROM bfinance_cache WHERE category = 'company_id' "
                "ORDER BY cache_key"
            )
        ]


def itc_chart_recorder() -> Recordings:
    return routes(company_route("itc"), chart_route("itc"))


# ================================================== 1. the assertion that matters most


class TestAChartCacheHitCostsNothing:
    """A warm chart row must cost **zero** transport calls, not "one fewer"."""

    async def test_second_chart_call_spends_no_transport_call_at_all(self, temp_cache):
        """Mutation: make ``resolve_company_id`` fetch unconditionally again — the pre-E1 ordering,
        where the resolver's request sits in front of the chart read. The second call then spends
        one request (the company page) and this fails on ``calls[2:] == []``.
        """
        recorder = itc_chart_recorder()
        client = cached_client(recorder, temp_cache)

        live = await client.get_chart_timeseries("ITC", days=365)
        assert live.attrs["bfinance_cache_status"] == "miss", "the first call is the fetch"
        assert len(recorder.calls) == 2, (
            f"the cold call is the company page plus the chart API: {recorder.calls}"
        )

        served = await client.get_chart_timeseries("ITC", days=365)
        assert served.attrs["bfinance_cache_status"] == "hit", "the second call must read the cache"
        assert recorder.calls[2:] == [], (
            f"a chart cache HIT spent {len(recorder.calls) - 2} request(s): {recorder.calls[2:]}. "
            f"The chart cache key contains the company id, so an uncached resolve puts a live "
            f"request in front of every chart read."
        )
        # Identical numbers, distinguishable provenance: "zero requests" is only the headline; a
        # frame that cost nothing to serve must still be the frame the recording describes.
        pd.testing.assert_frame_equal(live, served)
        assert served["Price"].iloc[-1] == pytest.approx(ITC_CLOSE)

    async def test_the_hit_is_free_because_the_id_row_is_there(self, temp_cache):
        """...and it is the *cache* that makes it free, not a hidden memo.

        Mutation: write the id anywhere other than under ``category="company_id"`` (for instance
        keyed by the un-normalised symbol) — this test fails, because the row it names is how the
        next caller gets the id without a request.
        """
        client = cached_client(itc_chart_recorder(), temp_cache)
        await client.get_chart_timeseries("ITC", days=365)

        row = temp_cache.get_result(id_key("ITC"))
        assert row.status == "hit" and row.value == ITC_ID, (
            "the chart path must leave the symbol -> id row behind, with the id off the page"
        )
        assert temp_cache.get_result(id_key("ITC")).value == extract_company_id(
            text("company_itc.html")
        )


# ================================================== 2. one row per company, not per spelling


class TestOneRowPerCompany:
    """``TCS``, ``tcs`` and ``TCS.NS`` are three spellings of one company, and must be one row."""

    async def test_three_spellings_resolve_from_one_row_and_one_request(self, temp_cache, cache_path):
        """Mutation: build the cache key from the raw ``symbol`` instead of ``normalize_symbol``.
        The same three calls then write three rows and spend three requests.
        """
        recorder = routes(company_route("tcs"))
        client = cached_client(recorder, temp_cache)

        assert await client.resolve_company_id("TCS") == TCS_ID
        assert await client.resolve_company_id("tcs") == TCS_ID
        assert await client.resolve_company_id("TCS.NS") == TCS_ID

        assert len(recorder.calls) == 1, (
            f"three spellings of one company spent {len(recorder.calls)} requests: {recorder.calls}"
        )
        assert id_rows(cache_path) == [id_key("TCS")], (
            "the three spellings must share one row, named after the normalised symbol"
        )
        assert temp_cache.get_result(id_key("TCS.NS")).status == "miss", (
            "a suffix is not part of the identity; no row may be written under the raw spelling"
        )

    async def test_the_chart_path_inherits_it_and_costs_two_requests_for_three_spellings(
        self, temp_cache
    ):
        """The same fact seen from the chart path: one company page and one chart, for three
        spellings. Mutation: the same missing ``normalize_symbol`` — then the page is fetched three
        times while the chart (keyed by the id, which is the same for all three) is still fetched
        once, so the count is 4 rather than 2.
        """
        recorder = itc_chart_recorder()
        client = cached_client(recorder, temp_cache)

        for spelling in ("ITC", "itc", "ITC.NS"):
            frame = await client.get_chart_timeseries(spelling, days=365)
            assert frame["Price"].iloc[-1] == pytest.approx(ITC_CLOSE)

        assert len(recorder.calls) == 2, (
            f"three spellings of one series must cost one page and one chart: {recorder.calls}"
        )


# ================================================== 3. the profile path gained nothing


class TestTheProfilePathDidNotGainARequest:
    """The id is written *by* the profile fetch, never *for* it."""

    @pytest.mark.parametrize("ticker", ["ITC", "itc", "ITC.NS"])
    async def test_a_cached_profile_fetch_still_makes_exactly_its_two_requests(
        self, temp_cache, cache_path, ticker
    ):
        """The pre-E1 request sequence, unchanged, with a cache in hand — for all three spellings.

        Mutation 1: route the profile through ``resolve_company_id`` (the tempting DRY — one
        resolver for the id). The sequence then grows a third request, ``/company/ITC/``, and this
        fails: the profile would fetch the same page twice to learn one id.
        Mutation 2: build the id key from the raw ``ticker`` rather than the normalised symbol. The
        row is then named ``screener_company_id_itc`` (or ``..._ITC.NS`` for two of the three
        cases), which the row assertion catches.
        """
        recorder = routes(consolidated_route("itc"), peers_route("itc"))
        client = cached_client(recorder, temp_cache)

        profile = await client.get_company_profile(ticker)

        assert recorder.calls == [
            "https://www.screener.in/company/ITC/consolidated/",
            "https://www.screener.in/api/company/1552/peers/",
        ], f"the profile path's request sequence changed for {ticker!r}: {recorder.calls}"
        assert profile.company_id == ITC_ID
        # ...and the id row came free, off the page that sequence already fetched, under one name.
        assert id_rows(cache_path) == [id_key("ITC")]

    def test_the_profile_method_never_calls_the_resolver(self):
        """The other half of the same claim, and the one a request count cannot see: the profile
        path must not *reach* for the resolver at all, because that is what would add the request
        the test above pins. Checked in source, like the robots-disallowed ``search`` call, because
        a call graph is not otherwise visible from one invocation.
        """
        source = inspect.getsource(ScreenerClient.get_company_profile)
        assert "await self.resolve_company_id" not in source, (
            "get_company_profile reads the id off the page it already fetched; calling the "
            "resolver here would spend a request to learn what it already knows"
        )
        assert "await self.search(" not in source

    async def test_a_profile_first_makes_the_chart_path_free_of_the_page(self, temp_cache):
        """Written in the direction the brief asks for: profile first, then a cold chart row.

        The chart call still costs its own chart request — it has no chart row yet — but it must
        not cost a company-page request, because the profile already wrote the id.
        Mutation: skip the write in ``get_company_profile`` (the id is fetched but not remembered);
        this call then spends 2 requests instead of 1.
        """
        recorder = routes(consolidated_route("itc"), peers_route("itc"), chart_route("itc"))
        client = cached_client(recorder, temp_cache)
        await client.get_company_profile("ITC")
        after_profile = len(recorder.calls)

        await client.get_chart_timeseries("ITC", days=365)

        spent = recorder.calls[after_profile:]
        metric = ScreenerChartEngine.get_query_param("price")
        assert spent == [
            f"https://www.screener.in/api/company/{ITC_ID}/chart/"
            f"?companyId={ITC_ID}&q={metric}&days=365"
        ], f"the chart call paid for something besides its own chart: {spent}"


# ================================================== 4. a miss still resolves, and refetches


class TestAMissStillResolvesCorrectly:
    """Eviction and ``force_refresh`` must never produce a blank or a stale id."""

    async def test_an_expired_row_is_refetched_rather_than_served(self, temp_cache, cache_path):
        """A *wrong* id, sealed with a negative TTL, so a row that is ignored and a row that is
        served are different outcomes.

        The premise is read straight out of the table: ``get_result`` deletes an expired row as a
        side effect of the read, and a mutation that ignores expiry would then find nothing there
        and look harmless.

        Mutation: hand-roll a second read path that ignores expiry — a raw ``SELECT payload``
        straight out of the table, which is exactly the "second read path" ``_cache_read`` exists
        to prevent. 999 is then served, the chart URL is built for another company, and both the
        URL assertion and the price assertion fail.
        """
        temp_cache.set(id_key("ITC"), 999, category="company_id", ttl_hours=-1.0)
        with sqlite3.connect(cache_path, timeout=5.0) as conn:
            row = conn.execute(
                "SELECT payload, expires_at FROM bfinance_cache WHERE cache_key = ?",
                (id_key("ITC"),),
            ).fetchone()
        assert row is not None, "the seeded row is gone; nothing below would mean anything"
        assert json.loads(row[0]) == 999, "the seeded id is not the wrong one this test is about"
        assert row[1] < time.time(), "the seeded row must be expired"

        recorder = itc_chart_recorder()
        frame = await cached_client(recorder, temp_cache).get_chart_timeseries("ITC", days=365)

        assert recorder.calls[0] == "https://www.screener.in/company/ITC/", (
            f"an expired row must send us back to the page: {recorder.calls}"
        )
        assert recorder.calls[1].startswith(f"https://www.screener.in/api/company/{ITC_ID}/chart/")
        assert "999" not in recorder.calls[1], "the expired id was served"
        assert frame["Price"].iloc[-1] == pytest.approx(ITC_CLOSE)

    async def test_a_stored_null_row_is_refetched_and_never_served_as_a_blank_id(self, temp_cache):
        """The row ``SQLiteCache.get`` cannot tell from an absent key: it exists, is unexpired, and
        holds nothing. Serving it would make the id ``None`` and raise ``TickerNotFoundError`` —
        a claim about the symbol, produced by a cache row.

        Mutation: trust any unexpired row instead of a row whose value is an ``int`` (read the
        ``CacheLookup.status`` and take ``hit``/``null`` as the answer). The stored null then
        becomes the answer and this fails with ``TickerNotFoundError``.
        """
        temp_cache.set(id_key("ITC"), None, category="company_id", ttl_hours=1.0)
        assert temp_cache.get_result(id_key("ITC")).status == "null"

        recorder = itc_chart_recorder()
        frame = await cached_client(recorder, temp_cache).get_chart_timeseries("ITC", days=365)

        assert len(recorder.calls) == 2, f"the blank row must be refetched: {recorder.calls}"
        assert frame["Price"].iloc[-1] == pytest.approx(ITC_CLOSE)

    async def test_force_refresh_refetches_the_chart_and_not_the_identity(self, temp_cache):
        """``force_refresh`` is about the *series*, not about identity: an id does not change for
        the life of a listing, so re-reading the page to re-learn it spends a request to learn
        nothing.

        Mutation: make ``force_refresh`` bypass the identity row too. This call then spends 2
        requests (page + chart) instead of 1, and the company page is re-read for an id that was
        already known.
        """
        recorder = itc_chart_recorder()
        client = cached_client(recorder, temp_cache)
        await client.get_chart_timeseries("ITC", days=365)

        forced = await client.get_chart_timeseries("ITC", days=365, force_refresh=True)

        assert forced.attrs["bfinance_cache_status"] == "miss", "the series is refetched"
        assert len(recorder.calls) == 3, (
            f"force_refresh must spend one request — the chart — not two: {recorder.calls}"
        )
        assert recorder.calls[2].startswith(
            f"https://www.screener.in/api/company/{ITC_ID}/chart/"
        )

    async def test_force_refresh_with_an_expired_identity_refetches_both(self, temp_cache):
        """The combination: a forced series *and* an evicted identity. No stale id is served and
        the frame is still right. Mutation: as above, the id is ignored — the failing assertion
        is the page request that must come first.
        """
        recorder = itc_chart_recorder()
        client = cached_client(recorder, temp_cache)
        await client.get_chart_timeseries("ITC", days=365)
        temp_cache.set(id_key("ITC"), 999, category="company_id", ttl_hours=-1.0)

        frame = await client.get_chart_timeseries("ITC", days=365, force_refresh=True)

        assert recorder.calls[2] == "https://www.screener.in/company/ITC/", (
            f"an evicted identity must be re-read even when the series is forced: {recorder.calls}"
        )
        assert recorder.calls[3].startswith(f"https://www.screener.in/api/company/{ITC_ID}/chart/")
        assert frame["Price"].iloc[-1] == pytest.approx(ITC_CLOSE)


# ================================================== 5. negative control: no cache at all


class TestWithTheCacheDisabled:
    """``cache=None`` is the explicit "cache nothing" argument; every claim above is qualified by
    the cache being there, and this is the control that shows it."""

    async def test_every_call_pays_for_both_requests_and_says_miss(self):
        """The same scenario as section 1, with nothing to read.

        Mutation: memoise the id on the instance (``self._company_id``) — a second vocabulary for a
        fact the cache already owns, which would also serve a stale identity past any TTL. The
        second call then spends 1 request instead of 2 and this fails. The test below demonstrates
        that mutation without needing it in the source.
        """
        recorder = itc_chart_recorder()
        client = cached_client(recorder, None)
        assert client.cache is None

        first = await client.get_chart_timeseries("ITC", days=365)
        second = await client.get_chart_timeseries("ITC", days=365)

        assert first.attrs["bfinance_cache_status"] == "miss"
        assert second.attrs["bfinance_cache_status"] == "miss"
        assert len(recorder.calls) == 4, (
            f"with no cache each call pays for the page and the chart: {recorder.calls}"
        )
        assert recorder.calls[2] == "https://www.screener.in/company/ITC/", (
            "the second call re-reads the page here — that is what having no cache means"
        )

    async def test_an_instance_memo_would_turn_that_assertion_red(self):
        """The mutation, applied to the client in memory and shown to move the counter.

        This is the shape a "cheap" version of this optimisation takes: skip the cache vocabulary
        and keep the id on the object. It would satisfy the headline (a chart hit costs no
        request), and it is refused: the value would outlive any TTL, be invisible to the cache
        table, and be unfixable by clearing a category.
        """
        recorder = itc_chart_recorder()
        client = cached_client(recorder, None)
        real = client.resolve_company_id
        memo: list = []

        async def memoised(symbol):
            if not memo:
                memo.append(await real(symbol))
            return memo[0]

        client.resolve_company_id = memoised  # type: ignore[method-assign]
        await client.get_chart_timeseries("ITC", days=365)
        await client.get_chart_timeseries("ITC", days=365)

        assert len(recorder.calls) == 3, (
            "the memo saved the second page request, so the counter no longer agrees with the "
            "cache-disabled contract asserted in the test above"
        )


# ================================================== the route that must not come back


class TestNoSearchFallback:
    """``/api/company/search/?q=`` is robots-disallowed and produced a silent-empty profile once."""

    async def test_a_page_without_the_id_returns_none_instead_of_searching(self, temp_cache):
        """Nothing is cached for a page that had no id, and no fallback route is called.

        Mutation: fall back to ``search()`` when the page has no ``data-company-id`` ("warm the
        cache with the name search"). The search route is recorded below precisely so that a
        mutation shows up as a requested URL rather than as an unrouted-call crash — and both the
        ``is None`` and the "no search URL" assertions fail.
        """
        stripped = text("company_itc.html").replace(
            'data-company-id="1552"', 'data-warehouse-id="1552"'
        )
        assert extract_company_id(stripped) is None, "premise: the id attribute is gone"
        recorder = routes(
            ("/company/ITC/", 200, stripped),
            ("/api/company/search/", 200, json.dumps([{"id": 999, "name": "ITC", "url": "/company/ITC/"}]))
        )
        client = cached_client(recorder, temp_cache)

        assert await client.resolve_company_id("ITC") is None
        assert recorder.calls == ["https://www.screener.in/company/ITC/"], (
            f"the id path spent more than the one page request: {recorder.calls}"
        )
        assert not any("search" in call for call in recorder.calls)
        assert temp_cache.get_result(id_key("ITC")).status == "miss", (
            "'no id on this page' must not be cached as an answer, let alone as 0"
        )
