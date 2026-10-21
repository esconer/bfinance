"""
The trendlyne fundamentals transport, offline.

The routes are real and were measured live on 2026-10-09 (recorded in
``tests/fixtures/trendlyne/META.json`` and in ``plans/trendlyne-fundamentals-yfinance.md``); this
file replays them against a fake transport so it runs without a socket. The point of the fakes is
not to make the tests pass -- it is to pin the three behaviours that are easy to get wrong and
expensive to notice:

* the **tab shell** answers 200 with zero ``<table>``, and that is the token source rather than a
  failure. A fetcher that treats an empty table count as "no data" never reaches the JSON route
  at all.
* the **token rotates**, and a rejection arrives as an HTTP 200 with ``head.status != "0"`` -- so
  the status code is not evidence of success, and exactly one re-scrape is worth spending.
* **pacing is 1000 ms** because the vendor's robots.txt says ``Crawl-delay: 1``. The floor is a
  compliance number, and a test that only checks "some delay happens" would not notice it being
  quietly tidied into a faster one.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path

import httpx
import pytest

from bfinance.statements import (
    Basis,
    Frequency,
    StatementKind,
    StatementRequest,
    resolve,
)
from bfinance.statements import fundamentals_fetch as ff
from bfinance.statements.fundamentals_fetch import (
    ALLOWED_ROUTES,
    FUNDAMENTALS_SITEMAP_PATH,
    MIN_REQUEST_INTERVAL_MS,
    FundamentalsFetchError,
    FundamentalsPage,
    FundamentalsTokenError,
    extract_json_token,
    fetch_fundamentals_json,
    fetch_fundamentals_json_sync,
    fundamentals_index,
    parse_fundamentals_sitemap,
)

# Verbatim shapes taken from the live captures, with the NTPC token replaced by a placeholder the
# fakes substitute. Every element here exists on the pages actually served.
SITEMAP = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
    "<url><loc>https://trendlyne.com/fundamentals/financials/8/20MICRONS/20-microns-ltd/</loc></url>"
    "<url><loc>https://trendlyne.com/fundamentals/financials/8/20MICRONS/20-microns-ltd/</loc></url>"
    "<url><loc>https://trendlyne.com/fundamentals/financials/959/NTPC/ntpc-ltd/</loc></url>"
    "<url><loc>https://trendlyne.com/fundamentals/financialals/878/MOTHERSON/x/</loc></url>"
    "<url><loc>https://trendlyne.com/fundamentals/financials/3622447/1216/bse-midsmall/</loc></url>"
    "</urlset>"
)

TAB_SHELL = (
    '<main id="fundamental_tables" data-tablesurl='
    '"https://trendlyne.com/fundamentals/get-fundamental_results-v2/959/TOKENPLACEHOLDER/"'
    ' class="x"></main>'
)

ENVELOPE_OK = '{"head": {"status": "0"}, "body": {"annualDataDump": {"consolidated": {"Mar 2026": {}}}}}'


class FakeResponse:
    """The attributes this module reads off a response."""

    def __init__(self, status_code: int, text: str = "", content: bytes = b"", headers=None):
        self.status_code = status_code
        self.text = text
        self.content = content or text.encode("utf-8")
        self.headers = httpx.Headers(headers or {})

    def json(self):
        import json

        return json.loads(self.text)


class FakeClient:
    """A transport that answers by URL prefix and records what was asked."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    async def get(self, url, **_kwargs):
        self.calls.append(url)
        for prefix, factory in self.routes:
            if url.startswith(prefix):
                return factory() if callable(factory) else factory
        return FakeResponse(404, "not found")


def _ntpc_client(token="TOKENPLACEHOLDER", shell=None, envelope=None, reject_first=False):
    """A client serving NTPC's three routes with a placeholder token."""
    state = {"token": token, "json_calls": 0}

    def shell_factory():
        return FakeResponse(200, shell if shell is not None else TAB_SHELL)

    def json_factory():
        state["json_calls"] += 1
        body = envelope if envelope is not None else ENVELOPE_OK
        if reject_first and state["json_calls"] == 1:
            body = '{"head": {"status": "2", "message": "Not Authorized !"}, "body": {}}'
        return FakeResponse(200, body)

    return FakeClient(
        [
            (ff.BASE_URL + FUNDAMENTALS_SITEMAP_PATH, FakeResponse(200, SITEMAP)),
            ("https://trendlyne.com/fundamentals/balance-sheet/", shell_factory),
            ("https://trendlyne.com/fundamentals/cash-flow/", shell_factory),
            ("https://trendlyne.com/fundamentals/quarterly-results/", shell_factory),
            ("https://trendlyne.com/fundamentals/annual-results/", shell_factory),
            ("https://trendlyne.com/fundamentals/financial-ratios/", shell_factory),
            ("https://trendlyne.com/fundamentals/get-fundamental_results-v2/", json_factory),
        ]
    ), state


def _json_route_client(factory):
    """NTPC's sitemap and tab shell, with the JSON route replaced by ``factory``.

    The three routes are the ones ``test_a_5xx_is_retried`` builds inline; only the JSON route is
    substituted, so a retry test still exercises resolution and the token unchanged.
    """
    client, _ = _ntpc_client()
    client.routes = [
        (ff.BASE_URL + FUNDAMENTALS_SITEMAP_PATH, FakeResponse(200, SITEMAP)),
        ("https://trendlyne.com/fundamentals/balance-sheet/", FakeResponse(200, TAB_SHELL)),
        ("https://trendlyne.com/fundamentals/get-fundamental_results-v2/", factory),
    ]
    return client


def _sitemap_only_client(xml: str = SITEMAP):
    """A transport that serves only the sitemap -- the document a TTL cache must not re-request."""
    return FakeClient([(ff.BASE_URL + FUNDAMENTALS_SITEMAP_PATH, FakeResponse(200, xml))])


def _sitemap_requests(client):
    """Every sitemap URL the client was actually asked for."""
    return [url for url in client.calls if url.endswith(FUNDAMENTALS_SITEMAP_PATH)]


class FlakyTransport(FakeClient):
    """A transport that fails at the socket on one route, rather than answering.

    A timeout is not an HTTP status, so it cannot be faked with a response object -- the ``get``
    itself has to raise, which is the whole of the branch under test.
    """

    def __init__(self, routes, error, fail_times=1, on="/get-fundamental_results-v2/"):
        super().__init__(routes)
        self.error = error
        self.fail_times = fail_times
        self.on = on
        self.failures = 0
        self.attempts = []

    async def get(self, url, **kwargs):
        self.attempts.append(url)
        if self.on in url and self.attempts.count(url) <= self.fail_times:
            self.failures += 1
            raise self.error
        return await super().get(url, **kwargs)


def _record_sleeps(monkeypatch):
    """Record what the retry loop *would* have slept, without spending the time.

    ``asyncio.sleep`` is patched rather than the pacing helper, so nothing about pacing is stubbed
    out; the callers below pass ``min_request_interval_ms=0.0`` so the recorded values are the
    backoffs and only the backoffs.
    """
    import asyncio

    slept = []
    real_sleep = asyncio.sleep  # captured before the patch, or the lambda calls itself
    monkeypatch.setattr(ff.asyncio, "sleep", lambda s: slept.append(s) or real_sleep(0))
    return slept


@pytest.fixture(autouse=True)
def _reset_index():
    """The index is an in-memory TTL cache; clear it between tests so one test's sitemap does not
    answer another's."""
    ff._INDEX_CACHE["pages"] = {}
    ff._INDEX_CACHE["loaded_at"] = 0.0
    yield
    ff._INDEX_CACHE["pages"] = {}
    ff._INDEX_CACHE["loaded_at"] = 0.0


@pytest.fixture(autouse=True)
def _no_pacing():
    """Pacing is asserted by its own tests; everywhere else it would only add a second per call."""
    previous = ff._GLOBAL_LAST_REQUEST_TIME
    ff._GLOBAL_LAST_REQUEST_TIME = 0.0
    yield
    ff._GLOBAL_LAST_REQUEST_TIME = previous


# --------------------------------------------------------------------------- #
# resolution
# --------------------------------------------------------------------------- #


def test_the_page_builds_both_urls_exactly():
    """Both routes are three path segments in a fixed order after the route name, and the referer
    the JSON route requires is one of them verbatim. A dropped slash, a lower-cased symbol or a
    swapped id would answer 404 -- which here reads as "this company has no data"."""
    page = FundamentalsPage(symbol="NTPC", stock_id=959, slug="ntpc-ltd")
    assert page.tab_shell_path == "/fundamentals/balance-sheet/959/NTPC/ntpc-ltd/"
    assert page.tab_shell_url == (
        "https://trendlyne.com/fundamentals/balance-sheet/959/NTPC/ntpc-ltd/"
    )
    assert page.financials_path == "/fundamentals/financials/959/NTPC/ntpc-ltd/"
    assert page.financials_url == "https://trendlyne.com/fundamentals/financials/959/NTPC/ntpc-ltd/"


def test_a_page_parsed_out_of_the_sitemap_builds_the_same_urls():
    """The format is not only the literal NTPC example: a key carried through ``parse`` must build
    the URLs it was resolved from."""
    page = parse_fundamentals_sitemap(SITEMAP)["20MICRONS"]
    assert page.tab_shell_path == "/fundamentals/balance-sheet/8/20MICRONS/20-microns-ltd/"
    assert page.financials_path == "/fundamentals/financials/8/20MICRONS/20-microns-ltd/"


def test_the_sitemap_resolves_on_the_exact_symbol_segment():
    pages = parse_fundamentals_sitemap(SITEMAP)
    assert pages["NTPC"].stock_id == 959
    assert pages["NTPC"].slug == "ntpc-ltd"
    assert pages["20MICRONS"].stock_id == 8


def test_a_duplicate_loc_is_kept_once():
    """20MICRONS appears twice in the live document. First wins and the key resolves once."""
    pages = parse_fundamentals_sitemap(SITEMAP)
    assert set(pages) == {"20MICRONS", "NTPC", "1216"}
    assert pages["20MICRONS"].stock_id == 8
    assert pages["20MICRONS"].slug == "20-microns-ltd"


def test_a_numeric_symbol_segment_is_tolerated():
    """BSE index families are keyed by a number, not a ticker. It resolves like anything else."""
    pages = parse_fundamentals_sitemap(SITEMAP)
    assert pages["1216"].stock_id == 3622447


def test_an_empty_sitemap_is_refused_rather_than_returned_as_no_symbols():
    with pytest.raises(FundamentalsFetchError, match="zero entries"):
        import asyncio

        asyncio.run(fundamentals_index(client=FakeClient([(ff.BASE_URL + FUNDAMENTALS_SITEMAP_PATH, FakeResponse(200, "<urlset></urlset>"))])))


def test_an_unknown_symbol_names_what_is_registered():
    import asyncio

    client, _ = _ntpc_client()
    with pytest.raises(FundamentalsFetchError, match="ZZZ"):
        asyncio.run(fetch_fundamentals_json("ZZZ", client=client, index=parse_fundamentals_sitemap(SITEMAP)))


# --------------------------------------------------------------------------- #
# the TTL cache
# --------------------------------------------------------------------------- #


def _frozen_clock(monkeypatch, start=10_000.0):
    """A ``time.monotonic`` the test advances by hand, so an expiry costs no wall-clock time."""
    clock = [start]
    monkeypatch.setattr(ff.time, "monotonic", lambda: clock[0])
    return clock


def test_a_second_lookup_inside_the_ttl_issues_no_second_sitemap_request(monkeypatch):
    """The autouse fixture empties ``_INDEX_CACHE`` before every test, so nothing else in this file
    can observe a cache hit at all. The document is 970,942 bytes for 6,201 entries: one request
    per lookup is the cost the cache exists to remove, so the request count is the assertion."""
    import asyncio

    clock = _frozen_clock(monkeypatch)
    client = _sitemap_only_client()

    asyncio.run(ff.fundamentals_index(client=client))
    assert len(_sitemap_requests(client)) == 1

    clock[0] += 60.0
    pages = asyncio.run(ff.fundamentals_index(client=client))

    assert len(_sitemap_requests(client)) == 1, "the sitemap was re-requested inside the TTL"
    assert pages["NTPC"].stock_id == 959


def test_an_entry_past_the_ttl_is_refetched(monkeypatch):
    """A cache that never expired would be a memory leak dressed as an optimisation: after 24 h the
    vendor's own symbol renames stop being visible to a caller."""
    import asyncio

    clock = _frozen_clock(monkeypatch)
    client = _sitemap_only_client()

    asyncio.run(ff.fundamentals_index(client=client))
    clock[0] += 24 * 3600.0 - 1.0
    asyncio.run(ff.fundamentals_index(client=client))
    assert len(_sitemap_requests(client)) == 1, "the entry expired a second early"

    clock[0] += 2.0
    asyncio.run(ff.fundamentals_index(client=client))
    assert len(_sitemap_requests(client)) == 2, "a stale entry was served past the 24 h TTL"


def test_force_refresh_goes_back_out_on_a_warm_entry(monkeypatch):
    """The escape hatch from a bad entry -- a vendor-side reindex, a symbol added mid-session --
    has to work without waiting out the TTL."""
    import asyncio

    clock = _frozen_clock(monkeypatch)
    client = _sitemap_only_client()

    asyncio.run(ff.fundamentals_index(client=client))
    clock[0] += 60.0
    asyncio.run(ff.fundamentals_index(client=client, force_refresh=True))

    assert len(_sitemap_requests(client)) == 2, "force_refresh=True was ignored on a fresh entry"


def test_ttl_hours_governs_the_boundary(monkeypatch):
    """Not 24 hours by accident. The same age is a miss under a ``ttl_hours`` that has passed and a
    hit under the default, which is the only way to show the argument is read rather than a
    constant used."""
    import asyncio

    clock = _frozen_clock(monkeypatch)
    client = _sitemap_only_client()

    asyncio.run(ff.fundamentals_index(client=client, ttl_hours=0.5))
    clock[0] += 30 * 60.0
    asyncio.run(ff.fundamentals_index(client=client, ttl_hours=0.5))
    assert len(_sitemap_requests(client)) == 2, "ttl_hours=0.5 served a half-hour-old entry"

    clock[0] += 30 * 60.0
    asyncio.run(ff.fundamentals_index(client=client))
    assert len(_sitemap_requests(client)) == 2, "the 24 h default expired a one-hour-old entry"

    asyncio.run(ff.fundamentals_index(client=client, ttl_hours=0.5))
    assert len(_sitemap_requests(client)) == 3, "ttl_hours=0.5 was not re-read on the same entry"


def test_the_cached_index_cannot_be_written_to_by_a_caller(monkeypatch):
    """The index is process-wide and :func:`fundamentals_index` is typed ``Mapping``.

    It handed out the ``dict`` behind that annotation, so one caller's ``pages["X"] = ...`` was a
    write to the cache every other caller in the process resolves against -- and the failure it
    causes is a symbol that silently stops resolving, not an exception. Both paths are covered
    here: the value returned by the fetch that fills the cache, and the value the TTL later hands
    back.
    """
    import asyncio

    clock = _frozen_clock(monkeypatch)
    client = _sitemap_only_client()

    filled = asyncio.run(ff.fundamentals_index(client=client))
    with pytest.raises(TypeError):
        filled["ZZZ"] = FundamentalsPage(symbol="ZZZ", stock_id=1, slug="z")

    clock[0] += 60.0
    served = asyncio.run(ff.fundamentals_index(client=client))
    assert len(_sitemap_requests(client)) == 1, "the sitemap was re-requested inside the TTL"
    with pytest.raises(TypeError):
        served["ZZZ"] = FundamentalsPage(symbol="ZZZ", stock_id=1, slug="z")

    # The reads every caller actually performs are untouched, and the cache is intact.
    assert served["NTPC"].stock_id == 959
    assert served.get("ZZZ") is None
    assert "NTPC" in served and len(served) == 3
    assert sorted(served) == ["1216", "20MICRONS", "NTPC"]


def test_parse_fundamentals_sitemap_still_hands_back_a_mutable_dict():
    """The public parse function is not the cache. A caller who parsed their own document may
    reasonably want to edit what they parsed, and freezing that too would be a behaviour change
    this fix does not make."""
    pages = parse_fundamentals_sitemap(SITEMAP)
    pages["ZZZ"] = FundamentalsPage(symbol="ZZZ", stock_id=1, slug="z")
    assert pages["ZZZ"].stock_id == 1


# --------------------------------------------------------------------------- #
# the token, and the shell that carries it
# --------------------------------------------------------------------------- #


def test_the_token_comes_from_data_tablesurl():
    assert extract_json_token(TAB_SHELL, symbol="NTPC") == "TOKENPLACEHOLDER"


def test_a_shell_without_data_tablesurl_is_a_token_error_not_a_retry():
    """The route worked and the markup changed. That is a signal to re-measure, so it is typed
    separately from a transport failure and never retried."""
    with pytest.raises(FundamentalsTokenError, match="markup change"):
        extract_json_token("<html><body>no attribute here</body></html>", symbol="NTPC")


def test_a_shell_with_zero_tables_is_the_token_source_not_a_failure():
    """The five tab shells answer 200 with zero ``<table>``. A fetcher that reads an empty table
    count as "the vendor has no data" never reaches the JSON route."""
    import asyncio

    client, _state = _ntpc_client()
    payload = asyncio.run(fetch_fundamentals_json("NTPC", client=client))
    assert payload["head"]["status"] == "0"
    assert any("/fundamentals/balance-sheet/" in url for url in client.calls)
    assert any("/get-fundamental_results-v2/" in url for url in client.calls)


def test_the_json_route_carries_a_referer():
    """**Measured live 2026-10-09, and the reason this module works at all.**

    On the *same* valid URL the vendor answers **HTTP 444 with a zero-length body** when no
    ``Referer`` is sent, and 200 with 841,448 bytes of JSON when the symbol's own financials page
    is the referer. The plain HTML routes answer 200 either way. So the referer is set exactly
    where the vendor requires it -- the JSON route -- and is not sprayed onto every request, which
    would look like a spoof rather than a browser.
    """
    import asyncio

    seen = {}

    class RecordingClient(FakeClient):
        async def get(self, url, **kwargs):
            seen[url] = kwargs
            return await super().get(url, **kwargs)

    client, _ = _ntpc_client()
    recording = RecordingClient(client.routes)
    asyncio.run(fetch_fundamentals_json("NTPC", client=recording))

    json_calls = [u for u in seen if "/get-fundamental_results-v2/" in u]
    assert json_calls, "the JSON route was not called"
    referer = seen[json_calls[0]].get("headers", {}).get("Referer")
    assert referer == "https://trendlyne.com/fundamentals/financials/959/NTPC/ntpc-ltd/"
    # And the HTML routes are NOT sent one.
    sitemap_calls = [u for u in seen if u.endswith(FUNDAMENTALS_SITEMAP_PATH)]
    assert sitemap_calls
    assert "Referer" not in seen[sitemap_calls[0]].get("headers", {})


def test_the_payload_parses_through_the_shipped_parser():
    """The point of the whole module: the bytes it returns are the bytes the statement layer
    reads. Driven with the recorded fixture, because a synthetic envelope carries no rows."""
    import asyncio

    fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "trendlyne" / "fundamental_results_ntpc.json")
        .read_text(encoding="utf-8")
    )
    client, _ = _ntpc_client(envelope=json.dumps(fixture, separators=(",", ":")))
    payload = asyncio.run(fetch_fundamentals_json("NTPC", client=client))
    request = StatementRequest(
        symbol="NTPC", kind=StatementKind.INCOME,
        frequency=Frequency.ANNUAL, basis=Basis.CONSOLIDATED,
    )
    result = resolve(request, fetch=lambda spec: payload)
    assert result.provider == "trendlyne-json"
    assert result.periods
    assert result.row_labels["SR_A"] == "Total Rev. Ann."


# --------------------------------------------------------------------------- #
# a rejected token, and the one retry that is worth spending
# --------------------------------------------------------------------------- #


def test_a_rejected_token_rescrapes_the_shell_exactly_once():
    """The token rotates, and a rejection arrives as an HTTP 200 with ``head.status != "0"``. One
    re-scrape is measured to help; a second only spends the vendor's patience."""
    import asyncio

    client, _state = _ntpc_client(reject_first=True)
    payload = asyncio.run(fetch_fundamentals_json("NTPC", client=client))
    assert payload["head"]["status"] == "0"
    shells = [u for u in client.calls if "/fundamentals/balance-sheet/" in u]
    jsons = [u for u in client.calls if "/get-fundamental_results-v2/" in u]
    assert len(jsons) == 2, "the JSON route was not asked twice"
    assert len(shells) == 2, "the shell was not re-scraped once"


def test_a_token_still_rejected_after_one_rescrape_raises_a_token_error():
    import asyncio

    client, _ = _ntpc_client(envelope='{"head": {"status": "2"}, "body": {}}')
    with pytest.raises(FundamentalsTokenError, match="re-scrape"):
        asyncio.run(fetch_fundamentals_json("NTPC", client=client))


# --------------------------------------------------------------------------- #
# transport behaviour
# --------------------------------------------------------------------------- #


def test_a_zero_length_body_is_terminal_and_is_never_decoded():
    """A 444, or a zero-length 200, is a bot gate. Retrying an unchanged request only spends the
    vendor's patience, and handing the body to a decoder is the most confusing possible symptom."""
    import asyncio

    client = FakeClient([("https://trendlyne.com/fundamentals/", FakeResponse(444, ""))])
    with pytest.raises(FundamentalsFetchError, match="zero-length"):
        asyncio.run(fetch_fundamentals_json("NTPC", client=client, index=parse_fundamentals_sitemap(SITEMAP)))


def test_a_4xx_is_terminal():
    import asyncio

    client = FakeClient([("https://trendlyne.com/fundamentals/", FakeResponse(403, "denied"))])
    with pytest.raises(FundamentalsFetchError, match="terminal"):
        asyncio.run(fetch_fundamentals_json("NTPC", client=client, index=parse_fundamentals_sitemap(SITEMAP)))


def test_a_5xx_is_retried():
    import asyncio

    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            return FakeResponse(503, "unavailable")
        return FakeResponse(200, ENVELOPE_OK)

    client, _ = _ntpc_client(shell=TAB_SHELL)
    # Substitute a flaky route for the JSON one only, keeping the real shell and sitemap.
    client.routes = [
        (ff.BASE_URL + FUNDAMENTALS_SITEMAP_PATH, FakeResponse(200, SITEMAP)),
        ("https://trendlyne.com/fundamentals/balance-sheet/", FakeResponse(200, TAB_SHELL)),
        ("https://trendlyne.com/fundamentals/get-fundamental_results-v2/", flaky),
    ]
    payload = asyncio.run(fetch_fundamentals_json("NTPC", client=client))
    assert payload["head"]["status"] == "0"
    assert calls["n"] >= 2


def test_exhausting_the_retries_raises_rather_than_returning_a_failed_response(monkeypatch):
    """``test_a_5xx_is_retried`` only proves a retry can succeed. This is the other end of the
    budget: it is a bound, and running out of it raises rather than handing the caller a response
    that is not the payload.
    """
    import asyncio

    slept = _record_sleeps(monkeypatch)
    calls = {"n": 0}

    def always_unavailable():
        calls["n"] += 1
        return FakeResponse(503, "unavailable")

    client = _json_route_client(always_unavailable)
    with pytest.raises(FundamentalsFetchError, match=r"after 3 attempt\(s\)"):
        asyncio.run(fetch_fundamentals_json("NTPC", client=client, min_request_interval_ms=0.0))

    assert calls["n"] == 3, "the retry budget was not the bound"
    # One backoff *between* attempts, so three attempts cost two backoffs. The transport branch
    # guards its final attempt and never sleeps into a raise; the 429 and 5xx branches used to
    # sleep unconditionally, which added a third, pointless wait (up to the 30 s Retry-After cap)
    # to a call that was about to fail anyway. They now guard the same way.
    assert len(slept) == calls["n"] - 1, (
        f"a sleep after the final attempt is back: {len(slept)} backoffs for {calls['n']} attempts"
    )


def test_max_retries_of_one_costs_a_single_attempt(monkeypatch):
    """The knob is honoured downward as well as upward: a caller on a one-second budget must not
    pay a backoff at all for it -- there is no attempt after the first to wait for."""
    import asyncio

    slept = _record_sleeps(monkeypatch)
    calls = {"n": 0}

    def always_unavailable():
        calls["n"] += 1
        return FakeResponse(503, "unavailable")

    client = _json_route_client(always_unavailable)
    with pytest.raises(FundamentalsFetchError, match=r"after 1 attempt\(s\)"):
        asyncio.run(
            fetch_fundamentals_json(
                "NTPC", client=client, max_retries=1, min_request_interval_ms=0.0
            )
        )
    assert calls["n"] == 1
    # Zero, not one: the single attempt is the last one, and the 5xx branch no longer sleeps into
    # the terminal message. This assertion used to pin the wasted sleep.
    assert not slept, f"a one-attempt caller was made to wait {len(slept)} time(s)"


# --------------------------------------------------------------------------- #
# rate limiting
# --------------------------------------------------------------------------- #


def test_a_429_is_retried_and_then_succeeds(monkeypatch):
    """Rate limiting is retryable, unlike every other 4xx: the vendor is asking to be slowed down,
    not refusing the route."""
    import asyncio

    _record_sleeps(monkeypatch)
    calls = {"n": 0}

    def rate_limited_once():
        calls["n"] += 1
        if calls["n"] == 1:
            return FakeResponse(429, "slow down", headers={"Retry-After": "1"})
        return FakeResponse(200, ENVELOPE_OK)

    client = _json_route_client(rate_limited_once)
    payload = asyncio.run(fetch_fundamentals_json("NTPC", client=client, min_request_interval_ms=0.0))

    assert payload["head"]["status"] == "0"
    assert calls["n"] == 2


def test_a_429_backs_off_for_the_capped_retry_after(monkeypatch):
    """``Retry-After`` is the vendor naming its own cool-down, and it wins over the exponential
    schedule. 45 s in the header is clamped to the 30 s cap: a header must not be able to stall a
    caller indefinitely."""
    import asyncio

    slept = _record_sleeps(monkeypatch)
    calls = {"n": 0}

    def rate_limited_once():
        calls["n"] += 1
        if calls["n"] == 1:
            return FakeResponse(429, "slow down", headers={"Retry-After": "45"})
        return FakeResponse(200, ENVELOPE_OK)

    client = _json_route_client(rate_limited_once)
    asyncio.run(fetch_fundamentals_json("NTPC", client=client, min_request_interval_ms=0.0))

    assert len(slept) == 1, "the 429 did not back off once"
    assert slept[0] == pytest.approx(ff._MAX_RETRY_AFTER_SEC), (
        f"a backoff of {slept[0]} ignored the capped Retry-After"
    )


def test_a_run_of_429s_raises_the_rate_limit_message(monkeypatch):
    """``saw_429`` is what separates "you are being throttled" from "the service is down". A caller
    backing off needs the first; a caller alerting needs the second, so they must not read alike.

    Also pins the 429 backoff count, and this is where the guard matters most: ``Retry-After`` is
    capped at 30 s, so the sleep a persistent 429 used to add *after* the final attempt was up to
    half a minute of dead time per call. Three attempts, two backoffs.
    """
    import asyncio

    slept = _record_sleeps(monkeypatch)
    calls = {"n": 0}

    def always_rate_limited():
        calls["n"] += 1
        return FakeResponse(429, "slow down")

    client = _json_route_client(always_rate_limited)
    with pytest.raises(FundamentalsFetchError, match=r"rate-limited .* after 3 attempt\(s\)"):
        asyncio.run(fetch_fundamentals_json("NTPC", client=client, min_request_interval_ms=0.0))
    assert calls["n"] == 3
    assert len(slept) == calls["n"] - 1, (
        f"a sleep after the final 429 is back: {len(slept)} backoffs for {calls['n']} attempts"
    )


def test_a_run_of_5xxs_does_not_report_a_rate_limit(monkeypatch):
    """The other side of the same branch: a vendor that is down must not be reported as throttling,
    or a caller backs off a failure it should retry later under a different policy."""
    import asyncio

    _record_sleeps(monkeypatch)
    client = _json_route_client(lambda: FakeResponse(503, "unavailable"))

    with pytest.raises(FundamentalsFetchError) as excinfo:
        asyncio.run(fetch_fundamentals_json("NTPC", client=client, min_request_interval_ms=0.0))

    assert "did not answer" in str(excinfo.value)
    assert "rate-limited" not in str(excinfo.value)


# --------------------------------------------------------------------------- #
# a transport failure, which is not a status code
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "error",
    [
        httpx.TimeoutException("timed out"),
        httpx.NetworkError("connection reset"),
        httpx.RemoteProtocolError("server disconnected"),
    ],
    ids=["timeout", "network-error", "remote-protocol-error"],
)
def test_a_transport_failure_is_retried_and_then_succeeds(monkeypatch, error):
    """These three are the ways httpx says "the request never got an answer", and none of them can
    be faked with a response object -- the transport has to raise, which is the branch itself."""
    import asyncio

    _record_sleeps(monkeypatch)
    base = _json_route_client(lambda: FakeResponse(200, ENVELOPE_OK))
    client = FlakyTransport(base.routes, error, fail_times=1)

    payload = asyncio.run(fetch_fundamentals_json("NTPC", client=client, min_request_interval_ms=0.0))

    assert payload["head"]["status"] == "0"
    assert client.failures == 1, "the transport failure was not retried"
    json_attempts = [u for u in client.attempts if "/get-fundamental_results-v2/" in u]
    assert len(json_attempts) == 2


def test_a_transport_failure_that_never_clears_raises_and_keeps_the_cause(monkeypatch):
    """The socket died and stayed dead. The vendor's status line is absent here, so the message has
    to carry the failure itself -- and ``raise ... from`` has to keep the original exception."""
    import asyncio

    _record_sleeps(monkeypatch)
    base = _json_route_client(lambda: FakeResponse(200, ENVELOPE_OK))
    error = httpx.TimeoutException("timed out")
    client = FlakyTransport(base.routes, error, fail_times=99)

    with pytest.raises(FundamentalsFetchError, match="transport failure") as excinfo:
        asyncio.run(fetch_fundamentals_json("NTPC", client=client, min_request_interval_ms=0.0))

    assert "after 3 attempt(s)" in str(excinfo.value)
    assert "timed out" in str(excinfo.value)
    assert excinfo.value.__cause__ is error, "the httpx error must survive as the cause"
    assert client.failures == 3


# --------------------------------------------------------------------------- #
# Retry-After, in every form a server is entitled to send
# --------------------------------------------------------------------------- #


def _retry_after(value):
    """``_retry_after_seconds`` for a response carrying one ``Retry-After`` header."""
    return ff._retry_after_seconds(FakeResponse(429, "", headers={"Retry-After": value}))


def test_retry_after_is_read_in_the_digit_form():
    """The form the vendor actually sends: a count of seconds."""
    assert _retry_after("5") == 5.0
    assert _retry_after(" 12 ") == 12.0, "the header is stripped before it is parsed"


def test_retry_after_is_read_in_the_http_date_form(monkeypatch):
    """The other form RFC 9110 allows. This is the branch that reads ``datetime_now_utc``, so the
    clock is patched: behind the real one, a defect in it is invisible. 20 s is under the cap, so
    what arrives here is the arithmetic and not the clamp above it."""
    now = datetime(2026, 10, 21, 7, 25, 0, tzinfo=timezone.utc)
    in_twenty_seconds = format_datetime(now.replace(second=20), usegmt=True)

    monkeypatch.setattr(ff, "datetime_now_utc", lambda: now)
    assert _retry_after(in_twenty_seconds) == pytest.approx(20.0)

    # A date already past is no cool-down at all, never a negative sleep.
    monkeypatch.setattr(ff, "datetime_now_utc", lambda: now.replace(second=40))
    assert _retry_after(in_twenty_seconds) == 0.0


def test_retry_after_is_capped_so_a_header_cannot_stall_a_caller(monkeypatch):
    """The cap applies to both forms, and a value sitting exactly on it is left alone."""
    assert _retry_after("600") == ff._MAX_RETRY_AFTER_SEC, "a hostile header was honoured in full"
    assert _retry_after("30") == 30.0, "a value exactly on the cap was altered"

    now = datetime(2026, 10, 21, 7, 25, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(ff, "datetime_now_utc", lambda: now)
    a_decade_out = format_datetime(now.replace(year=2036), usegmt=True)
    assert _retry_after(a_decade_out) == ff._MAX_RETRY_AFTER_SEC


def test_retry_after_is_none_rather_than_zero_when_there_is_nothing_to_wait_for():
    """``None`` and ``0.0`` are different answers here. ``None`` leaves the exponential backoff on
    its own schedule; ``0.0`` would tell it to stop backing off altogether."""
    assert ff._retry_after_seconds(None) is None, "no response at all"
    assert ff._retry_after_seconds(FakeResponse(429, "")) is None, "header absent"
    assert _retry_after("soon") is None, "neither a count nor a date"
    assert _retry_after("") is None, "present but empty"
    # An HTTP-date carrying no zone is naive and cannot be subtracted from an aware clock. The real
    # ``datetime_now_utc`` is used deliberately here -- that asymmetry is the case under test.
    assert _retry_after("Wed, 21 Oct 2026 07:28:00") is None


def test_undecodable_json_is_a_typed_error_not_a_json_decode_error():
    import asyncio

    client, _ = _ntpc_client(envelope="<html>nope</html>")
    with pytest.raises(FundamentalsFetchError, match="decodable JSON"):
        asyncio.run(fetch_fundamentals_json("NTPC", client=client))


# --------------------------------------------------------------------------- #
# the sync wrapper -- the entry point ticker.py actually calls
# --------------------------------------------------------------------------- #


def test_the_sync_wrapper_answers_on_its_own():
    """Every other test in this file wraps the async entry point in its own ``asyncio.run``, so the
    one function ``ticker.py`` calls was never executed. This is that call, end to end: sitemap,
    tab shell, token, JSON, through the blocking wrapper.
    """
    client, _ = _ntpc_client()
    payload = fetch_fundamentals_json_sync("NTPC", client=client)

    assert payload["head"]["status"] == "0"
    assert any("/fundamentals/balance-sheet/" in url for url in client.calls)
    assert any("/get-fundamental_results-v2/" in url for url in client.calls)


def test_the_sync_wrapper_forwards_its_keywords(monkeypatch):
    """A wrapper, not a second implementation: the caller's ``index``, ``max_retries`` and pacing
    must arrive at the fetcher rather than being dropped on the way through.
    """
    _record_sleeps(monkeypatch)
    calls = {"n": 0}

    def always_unavailable():
        calls["n"] += 1
        return FakeResponse(503, "unavailable")

    client = _json_route_client(always_unavailable)
    with pytest.raises(FundamentalsFetchError, match=r"after 1 attempt\(s\)"):
        fetch_fundamentals_json_sync(
            "NTPC",
            client=client,
            index=parse_fundamentals_sitemap(SITEMAP),
            max_retries=1,
            min_request_interval_ms=0.0,
        )

    assert calls["n"] == 1, "max_retries did not reach the retry loop"
    assert not _sitemap_requests(client), "an index was passed and the sitemap was fetched anyway"


def test_the_sync_wrapper_raises_the_typed_error_rather_than_swallowing_it():
    """A caller that catches ``FundamentalsFetchError`` must not be handed an ``asyncio`` error out
    of the wrapper instead."""
    client, _ = _ntpc_client()
    with pytest.raises(FundamentalsFetchError, match="ZZZ"):
        fetch_fundamentals_json_sync(
            "ZZZ", client=client, index=parse_fundamentals_sitemap(SITEMAP)
        )


# --------------------------------------------------------------------------- #
# pacing, the robots compliance floor
# --------------------------------------------------------------------------- #


def test_the_pacing_floor_is_one_second():
    """``Crawl-delay: 1`` in the vendor's ``User-agent: *`` group. This number was 750 ms once, which
    was 1.33 requests per second against a published floor of 1."""
    assert MIN_REQUEST_INTERVAL_MS == 1000.0


def test_a_second_immediate_request_is_delayed(monkeypatch):
    """Measured on a fake clock rather than by sleeping, so the test stays fast and asserts the
    magnitude rather than the existence of a delay."""
    import asyncio

    slept = []
    real_sleep = asyncio.sleep  # captured before the patch, or the lambda calls itself
    monkeypatch.setattr(ff.time, "monotonic", lambda: 1000.0)
    monkeypatch.setattr(ff.asyncio, "sleep", lambda s: slept.append(s) or real_sleep(0))
    ff._GLOBAL_LAST_REQUEST_TIME = 1000.0  # a request just fired

    asyncio.run(ff._throttle_pacing(MIN_REQUEST_INTERVAL_MS))
    assert slept, "the second request was not delayed"
    assert slept[0] >= (MIN_REQUEST_INTERVAL_MS / 1000.0) - 0.001


def test_zero_disables_pacing(monkeypatch):
    import asyncio

    slept = []
    monkeypatch.setattr(ff.asyncio, "sleep", lambda s: slept.append(s) or asyncio.sleep(0))
    ff._GLOBAL_LAST_REQUEST_TIME = ff.time.monotonic()
    asyncio.run(ff._throttle_pacing(0.0))
    assert slept == []


def test_the_pacing_state_is_global_not_per_call():
    """Two callers must observe one floor. A per-call slot would let two concurrent fetchers run
    at 2x the published rate between them."""
    assert isinstance(ff._GLOBAL_PACING_LOCK, type(__import__("threading").Lock()))


# --------------------------------------------------------------------------- #
# robots posture
# --------------------------------------------------------------------------- #


def test_every_path_this_module_can_build_is_allowed():
    """A route cannot be added without the evidence behind it."""
    page = FundamentalsPage(symbol="NTPC", stock_id=959, slug="ntpc-ltd")
    for path in (
        FUNDAMENTALS_SITEMAP_PATH,
        page.tab_shell_path,
        page.financials_path,
        ff.FUNDAMENTALS_JSON_PATH.format(stock_id=959, token="T"),
    ):
        assert any(path.startswith(prefix) for prefix in ALLOWED_ROUTES), path


def test_the_disallowed_fundamentals_routes_are_not_buildable():
    """``*/fundamentals/chart/v2/`` and ``*/fundamentals/back-test/runhistory/`` are Disallow-ed by
    the vendor's robots.txt. Nothing here addresses them, and this is the assertion that keeps it
    that way."""
    for forbidden in ("/fundamentals/chart/v2/", "/fundamentals/back-test/runhistory/"):
        assert not any(forbidden.startswith(prefix) for prefix in ALLOWED_ROUTES), forbidden
    source = (ff.__file__ or "")
    import pathlib

    text = pathlib.Path(source).read_text(encoding="utf-8")
    assert "/fundamentals/chart/v2/" not in text.replace(
        "*/fundamentals/chart/v2/", ""
    ), "the chart/v2 route appears as a buildable path"
    assert "get-fundamental_results-v2" in text  # the one route that is used


def test_the_module_does_not_import_the_leaf_package():
    """``bfinance.trendlyne`` is a leaf; no other module may name it."""
    import ast
    import pathlib

    tree = ast.parse(pathlib.Path(ff.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        names = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        assert not any("trendlyne" in n for n in names), f"line {node.lineno}"
