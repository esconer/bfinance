"""
Four silent-defect classes, each of which answers a caller with something they did not ask for.

1. ``download()`` accepted a misspelled/unknown keyword, logged a warning nobody reads, and
   returned a normal frame anyway. ``download(symbols, period="1mo", interval="1d")`` was a
   valid-looking call that fetched a different window than the one asked for -- the same shape of
   bug that hid ``raise_errors`` in this very package for years.
2. An unrecognised ``period`` became five years, which the archive clamp then pinned to
   2024-01-01. ``'BANANA'``, ``'2w'`` and ``'1M'`` all returned "the whole archive" with no raise
   and no warning; ``'1M'`` is the sharp one, because a human types it meaning one month, it
   lowercases to ``'1m'``, and the map key is ``'1mo'``.
3. The price-bearing ``profile`` cache entry was the only one of five that did not state its TTL,
   so it inherited 24h from the cache's construction default. Nobody chose 24h; a price is not
   "slightly old", it is a wrong number the moment the market moves.
4. The cache wrote JSON no strict parser can read (``NaN``/``Infinity``) and could not tell a
   stored ``null`` from a miss.

Every test here is offline: the ticker, the bhavcopy archive, the HTTP transport and the HTML
parser are stubbed or replaced. No test in this file may make a network request.

The house failure mode this file is written against is an assertion that cannot fail. Every
section ends with something that would go red if its fix were reverted; the two that genuinely
cannot fail (the characterising connection test, the source-level retry tripwire) say so in their
own docstrings rather than pretending to be proof.
"""

import ast
import asyncio
import gc
import importlib
import json
import logging
import sqlite3
from pathlib import Path

import httpx
import pandas as pd
import pytest

import bfinance.cache.sqlite_cache as cache_mod
import bfinance.market.ohlcv as ohlcv_mod
import bfinance.nse.bhavcopy as bc
from bfinance.cache.sqlite_cache import SQLiteCache
from bfinance.market.ohlcv import BHAVCOPY_FIRST_DATE, IST, OHLCVEngine
from bfinance.models.company import CompanyProfile
from bfinance.screener.client import ScreenerClient
from bfinance.utils.exceptions import UpstreamServiceError

# NB: `from bfinance import download` yields the *function*, not the module, because
# `bfinance/__init__.py` re-exports it. importlib is the reliable way to reach the module.
download_mod = importlib.import_module("bfinance.download")
download = download_mod.download


# ===================================================== 1. download() rejects unknown kwargs


class _StubTicker:
    """Stands in for Ticker so no network and no cache is involved. Records every construction."""

    calls: list = []

    def __init__(self, symbol, proxy=None):
        self.symbol = symbol
        type(self).calls.append(symbol)

    async def history_async(self, **kwargs):
        idx = pd.to_datetime(["2024-03-28", "2024-03-29"])
        return pd.DataFrame(
            {"Open": [1.0, 2.0], "High": [1.5, 2.5], "Low": [0.5, 1.5],
             "Close": [1.2, 2.2], "Volume": [10, 20]},
            index=idx,
        )


@pytest.fixture
def stub_ticker(monkeypatch):
    _StubTicker.calls = []
    monkeypatch.setattr(download_mod, "Ticker", _StubTicker)
    return _StubTicker


def test_download_rejects_an_unknown_kwarg(stub_ticker):
    """
    The defect, as an assertion: the call must not return a frame.

    Before the fix this returned a perfectly normal (2, 5) frame -- ``bogus_kwarg`` vanished and
    a WARNING was logged. ``TypeError`` is what Python raises for this, and what yfinance 1.7.0
    raises: its ``download()`` signature is closed (no ``**kwargs`` at all, checked with
    ``inspect.signature(yfinance.download)`` in this env), so rejecting *is* the yfinance match.
    """
    with pytest.raises(TypeError) as exc:
        download(["RELIANCE", "TCS"], bogus_kwarg=1)

    assert "bogus_kwarg" in str(exc.value), (
        f"the error must name the offending key; got: {exc.value}"
    )


def test_the_rejection_names_every_offending_key(stub_ticker):
    """
    One typo at a time is the easy case; the message has to survive a batch of them.

    A caller who misspells three arguments at once must not have to bisect the message to find
    all three.
    """
    with pytest.raises(TypeError) as exc:
        download(["RELIANCE"], raise_errorz=True, peroid="1mo", intervaly="1d")

    message = str(exc.value)
    for key in ("raise_errorz", "peroid", "intervaly"):
        assert key in message, f"{key} missing from: {message}"


def test_download_rejects_before_doing_any_work(stub_ticker):
    """
    The refusal has to come first, or a rejected call still costs a request per symbol.

    This is the assertion that says *when*, not just *that*: if the check is ever moved below the
    fetch, this goes red while the ``pytest.raises`` above stays green.
    """
    with pytest.raises(TypeError):
        download(["RELIANCE", "TCS"], bogus_kwarg=1)

    assert stub_ticker.calls == [], (
        f"an unsupported keyword must be refused before any ticker is constructed; got {stub_ticker.calls}"
    )


def test_the_lenient_default_does_not_buy_a_silent_keyword(stub_ticker):
    """
    ``raise_errors=False`` is the yfinance-compatible default, and it must not leak into this.

    ``raise_errors`` governs *upstream* failures: the default is to come back with an empty frame
    rather than raise. A wrong argument is not an upstream failure, so it raises either way --
    otherwise ``download(..., raise_errors=False, typo=1)`` would quietly be the lenient path and
    ``raise_errors=True`` the strict one, which is not a contract anyone can reason about.
    """
    with pytest.raises(TypeError):
        download(["RELIANCE"], period="5d", raise_errors=False, typo=1)


def test_every_named_parameter_is_still_accepted(stub_ticker):
    """
    The over-rejection control: nothing that used to work may stop working.

    Rejecting unknown keywords is only a fix if the *known* ones still pass. This calls
    ``download()`` with the entire yfinance-parity signature spelled out, because a check that
    rejects one legitimate argument is the same defect pointing the other way.
    """
    frame = download(
        "RELIANCE",
        start=None,
        end=None,
        actions=False,
        threads=False,
        ignore_tz=None,
        group_by="column",
        auto_adjust=False,
        back_adjust=False,
        repair=False,
        keepna=False,
        progress=False,
        period="5d",
        interval="1d",
        prepost=False,
        rounding=False,
        timeout=10,
        proxy=None,
        session=None,
        multi_level_index=False,
        raise_errors=False,
    )

    assert not frame.empty, (
        "every named parameter was passed and the call still produced no data -- the rejection "
        "has swallowed a legitimate argument"
    )


# ============================================== 2. an unrecognised period raises; ''/None means "default"


def _market_frame():
    """Whole-market bhavcopy-shaped frame, sliced to RELIANCE by the engine under test."""
    index = pd.MultiIndex.from_arrays(
        [pd.DatetimeIndex([pd.Timestamp("2024-06-03")]), pd.Index(["RELIANCE"], dtype=object)],
        names=["Date", "Symbol"],
    )
    return pd.DataFrame(
        [[100.0, 110.0, 99.0, 105.0, 1000]],
        columns=["Open", "High", "Low", "Close", "Volume"],
        index=index,
    )


@pytest.fixture
def stub_bhavcopy(monkeypatch):
    """Record the window the engine asks the archive for. A request is the only proof of intent."""
    calls: list = []

    def _range(start, end, **kwargs):
        calls.append((start, end))
        return _market_frame()

    monkeypatch.setattr(ohlcv_mod, "bhavcopy_range", _range)
    return calls


class _NoScreener:
    async def get_chart_timeseries(self, *args, **kwargs):
        raise AssertionError("the bhavcopy path must not consult the close-only chart API")

    async def get_company_profile(self, *args, **kwargs):
        raise AssertionError("the bhavcopy path must not consult the Screener profile")


class _NoActions:
    async def actions_async(self, symbol, start=None, end=None, index="equities"):
        from bfinance.nse import corporate_actions as ca

        return ca.actions_frame([], symbol=symbol, url="stub")


def _engine():
    return OHLCVEngine(_NoScreener(), corporate_actions_client=_NoActions())


def _clamped(days: int) -> str:
    """The window a ``days``-long request actually reaches, after the archive-floor clamp."""
    start = pd.Timestamp.now(tz=IST) - pd.Timedelta(days=days)
    floor = pd.Timestamp(BHAVCOPY_FIRST_DATE, tz=IST)
    return str(max(start, floor).date())


async def _repeat_asks_only_the_tail(stub_bhavcopy, period):
    """Run the same request again and pin the repeat half of the clamped-split contract.

    The first call is the cold prefix: one whole-window range fetch, the walk that was always
    spent. This helper re-runs it against the same ``$BFINANCE_HOME`` (the autouse fixture gives
    the test one store), so the prefix the first call stored is now a hit, and the archive may only
    be asked for the days after the immutable boundary. That is the half ``stub_bhavcopy[-1][0] ==
    BHAVCOPY_FIRST_DATE`` used to hide: on a warm store ``[-1]`` was the tail, and on a cold one
    the assertion said nothing about the repeat at all.
    """
    before = len(stub_bhavcopy)
    second = await _engine().fetch_history(
        symbol="RELIANCE", period=period, actions=False, auto_adjust=False
    )
    assert len(stub_bhavcopy) - before == 1, (
        "a repeat call must be answered from the stored prefix and ask for the tail alone; it "
        f"asked for {stub_bhavcopy[before:]}"
    )
    assert stub_bhavcopy[-1][0] > BHAVCOPY_FIRST_DATE, (
        "so the archive is not walked from the floor a second time"
    )
    assert stub_bhavcopy[-1][1] == stub_bhavcopy[0][1], "and the tail ends at the requested end"
    return second


@pytest.mark.parametrize(
    "period",
    ["BANANA", "2w", "1M", "1 month", "5years", "  ", "1d "],
    ids=["nonsense", "week-period", "one-month-typo", "spelled-out", "years-typo", "blank", "padded"],
)
async def test_an_unrecognised_period_raises(stub_bhavcopy, period):
    """
    Every one of these used to return the whole archive and look like a successful answer.

    Measured before the fix (today = 2026-09-28), the window requested was ``2026-08-29`` for
    ``'1mo'`` and ``2024-01-01`` -- the archive floor -- for ``'BANANA'``, ``'2w'``, ``'1M'`` and
    ``''``. One answer for four different questions, with no raise and no warning.
    """
    with pytest.raises(ValueError) as exc:
        await _engine().fetch_history(symbol="RELIANCE", period=period, actions=False)

    message = str(exc.value)
    assert repr(period) in message, f"the error must quote what it was given; got: {message}"
    assert "1mo" in message, (
        f"the error must list the valid vocabulary, or the caller cannot fix the call; got: {message}"
    )
    assert stub_bhavcopy == [], (
        f"an unrecognised period must cost no request; the archive was asked for {stub_bhavcopy}"
    )


async def test_period_1M_never_silently_becomes_the_archive_floor(stub_bhavcopy):
    """
    The plausible typo, pinned as its own test because it is the one a real user hits.

    ``'1M'`` reads as "one month" to anyone typing it. It lowercases to ``'1m'``, which is not a
    key in :data:`PERIOD_DAYS_MAP` -- the key is ``'1mo'`` -- so it took the 1825-day default and
    was clamped to 2024-01-01: a caller asking for a month got the whole archive.

    Either outcome is acceptable and the test accepts only the two honest ones: raise with a
    message that says how to spell it, or serve the month. What it rules out is the quiet floor.
    """
    try:
        await _engine().fetch_history(symbol="RELIANCE", period="1M", actions=False)
    except ValueError as exc:
        assert "1mo" in str(exc), f"a refusal must name the spelling that works; got: {exc}"
        assert stub_bhavcopy == [], "a refusal must cost no request"
        return

    assert stub_bhavcopy[-1][0] != BHAVCOPY_FIRST_DATE, (
        f"period='1M' resolved to the archive floor {stub_bhavcopy[-1][0]}, i.e. it was silently "
        f"treated as 'everything' rather than as one month"
    )


@pytest.mark.parametrize(
    ("period", "expected_days"),
    [("1d", 2), ("5d", 7), ("1mo", 30), ("1MO", 30), ("3mo", 90), ("1y", 365), ("max", 10000)],
)
async def test_a_recognised_period_reaches_the_window_it_names(stub_bhavcopy, period, expected_days):
    """
    The positive control, and the reason the raise is safe: a valid period still reaches the fetch.

    Asserted against the window actually requested, because that is the observable the defect was
    about. ``'1MO'`` is here to prove the match really is case-insensitive -- which is what makes
    ``'1M'`` a *typo* rather than an unsupported feature. ``'max'`` is here to prove the archive
    clamp is applied identically on the way in and in this expectation, so the assertion above
    cannot be satisfied by simply refusing long windows.

    The first range is asserted, and the call count with it, because a clamped window is split now:
    the cold prefix is ONE whole-window range fetch (the walk that was always spent), so "the first
    call spans the resolved window" is the contract. ``stub_bhavcopy[-1]`` was the last call, which
    on a warm store is the tail and on a cold one merely happened to be the only call.
    """
    # auto_adjust=False is explicit and is the pre-2026-09-30 default: these tests are about the
    # WINDOW the archive was asked for, and on the new default auto_adjust=True raises out of a
    # call whose corporate-actions feed is not stubbed here, before the window can be inspected.
    await _engine().fetch_history(
        symbol="RELIANCE", period=period, actions=False, auto_adjust=False
    )

    requested_start = stub_bhavcopy[0][0]
    assert requested_start == _clamped(expected_days), (
        f"period={period!r} should have requested {_clamped(expected_days)} "
        f"({expected_days} days back, clamped to the archive floor), "
        f"but the archive was asked for {requested_start}"
    )
    assert len(stub_bhavcopy) == 1, (
        "and the first call is one whole-window range fetch, not one fetch per half: "
        f"{stub_bhavcopy}"
    )


async def test_a_long_but_recognised_period_still_clamps_with_a_warning(stub_bhavcopy, caplog):
    """
    The clamp is not the bug; the raise must not become one.

    ``'5y'`` legitimately wants more than the archive holds, so it legitimately resolves to
    2024-01-01 -- and that has to stay a *logged* clamp rather than a refusal, or the fix has
    traded a silent wrong answer for a loud wrong refusal. This is the assertion that separates
    "period we do not know" (raise) from "period the archive cannot fill" (clamp + warn).

    The clamp assertion is on the FIRST call and on the repeat: the first is the cold prefix, one
    whole-window range fetch from the floor, and the second is answered from that stored prefix and
    asks only for the tail. ``stub_bhavcopy[-1][0]`` used to stand for both halves and no longer
    can -- on a warm store the last call is the tail.
    """
    with caplog.at_level(logging.WARNING, logger="bfinance"):
        first = await _engine().fetch_history(
            symbol="RELIANCE", period="5y", actions=False, auto_adjust=False
        )

    assert stub_bhavcopy[0][0] == BHAVCOPY_FIRST_DATE, (
        f"a 5y request should clamp to the archive floor, not ask for {stub_bhavcopy[0][0]}"
    )
    assert len(stub_bhavcopy) == 1, (
        "and the cold prefix is one whole-window range fetch, the walk that was always spent"
    )
    assert BHAVCOPY_FIRST_DATE in caplog.text, (
        "the clamp must be logged, exactly as the floor case was; got: " + caplog.text
    )

    second = await _repeat_asks_only_the_tail(stub_bhavcopy, "5y")
    assert second.attrs["bfinance_window_truncated"] is True, (
        "the repeat is still a clamped request, however it was served"
    )
    assert first["Close"].tolist() == second["Close"].tolist(), (
        "and the repeat frame is the same series, prefix and tail together"
    )


@pytest.mark.parametrize("period", ["", None], ids=["empty-string", "none"])
async def test_no_period_given_means_the_documented_default(stub_bhavcopy, period):
    """
    The deliberate second decision: ``''``/``None`` are legitimate, an unrecognised string is not.

    "No period given, use the default lookback" is a real instruction a caller can mean, so it
    keeps working, and it means what :data:`PERIOD_DAYS_MAP` documents: 1825 days, the same window
    as ``'5y'``. The distinction being pinned is *refused* vs *accepted*, so this goes red if the
    validation is ever widened to reject the empty case.

    Both spellings resolve to the same clamped window, so the second half of the split contract
    belongs here too: the first call asks for the whole window in one range fetch, and a repeat call
    reads the prefix and asks only for the tail. ``stub_bhavcopy[-1][0]`` was the last request, which
    is the tail once the store is warm.
    """
    first = await _engine().fetch_history(
        symbol="RELIANCE", period=period, actions=False, auto_adjust=False
    )

    assert OHLCVEngine.PERIOD_DAYS_MAP["5y"] == 1825, (
        "the documented no-period default is 1825 days; if that changes this test's expectation "
        "has to change with it"
    )
    assert stub_bhavcopy[0][0] == _clamped(1825), (
        f"the no-period default is 1825 days -> {_clamped(1825)}; "
        f"the archive was asked for {stub_bhavcopy[0][0]}"
    )
    assert len(stub_bhavcopy) == 1, (
        "and the first call is one whole-window range fetch: " f"{stub_bhavcopy}"
    )

    second = await _repeat_asks_only_the_tail(stub_bhavcopy, period)
    assert first["Close"].tolist() == second["Close"].tolist(), (
        "the repeat frame is the same series, prefix and tail together"
    )


def test_the_period_vocabulary_is_the_documented_one():
    """
    Pin the map itself, because its contents are the contract the error message quotes.

    Spelled out rather than compared against itself, so that a deliberate change to the vocabulary
    is a visible edit here instead of a silent divergence between the map, the error text and the
    docs.
    """
    assert OHLCVEngine.PERIOD_DAYS_MAP == {
        "1d": 2, "5d": 7, "1mo": 30, "3mo": 90, "6mo": 180, "1y": 365, "2y": 730,
        "3y": 1095, "5y": 1825, "10y": 3652, "ytd": 365, "max": 10000,
    }
    assert "1m" not in OHLCVEngine.PERIOD_DAYS_MAP, (
        "'1m' means one minute to yfinance and one month to a human; accepting it silently is "
        "exactly the ambiguity this vocabulary refuses"
    )


# ================================================= 3. every cache.set states its TTL; the profile's is 1h


def _cache_set_call_nodes() -> list:
    """Every ``self.cache.set(...)`` call in the screener client, as AST nodes."""
    source = importlib.import_module("bfinance.screener.client").__file__
    with open(source, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    found = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "set"
            and isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr == "cache"
        ):
            found.append(node)
    return found


def test_every_cache_set_call_site_passes_an_explicit_ttl():
    """
    A TTL that exists only at the call sites that remembered it is not a policy.

    Before the fix, four of the five ``self.cache.set`` calls then in :class:`ScreenerClient` passed
    ``ttl_hours`` and the price-bearing ``profile`` entry did not, so it inherited
    ``SQLiteCache(default_ttl_hours=24.0)``. This scans the source, so an *eighth* call site added
    tomorrow without a TTL is caught too -- a behavioural test could only ever cover the seven paths
    it already knows about.
    """
    nodes = _cache_set_call_nodes()
    assert len(nodes) == 7, (
        f"expected the seven known cache.set call sites, found {len(nodes)}; a new one needs a TTL "
        f"declared here and in CATEGORY_TTL_HOURS"
    )

    for node in nodes:
        keys = {keyword.arg for keyword in node.keywords}
        assert "ttl_hours" in keys, (
            f"a cache.set at line {node.lineno} passes no ttl_hours, so its age would come from "
            f"the cache's construction default"
        )

    # The seven sites again, by the category each stores under: ``search``; ``company_id`` twice
    # (``resolve_company_id`` and the identifier row ``get_company_profile`` writes from the page it
    # already fetched); ``profile``; ``chart``; and the two ``/market/`` pages, which store
    # ``category="market"`` and select their TTL under ``market_overview``/``market_leaf``. Pinned
    # beside the count so the number above is auditable rather than merely moved: an eighth site has
    # to add its category here -- and therefore its TTL to ``CATEGORY_TTL_HOURS`` -- or this fails.
    categories = []
    for node in nodes:
        named = [keyword for keyword in node.keywords if keyword.arg == "category"]
        assert len(named) == 1 and isinstance(named[0].value, ast.Constant), (
            f"the cache.set at line {node.lineno} must name its category as a literal, so the TTL "
            f"it selects (CATEGORY_TTL_HOURS) is auditable from here"
        )
        categories.append(named[0].value.value)

    assert sorted(categories) == [
        "chart", "company_id", "company_id", "market", "market", "profile", "search",
    ], (
        f"the known cache.set sites are search, resolve_company_id, get_company_profile x2, "
        f"get_chart_timeseries, market overview and market leaf; got {sorted(categories)}"
    )


def test_the_price_bearing_profile_ttl_is_sixty_minutes(temp_cache):
    """
    Asserted in **minutes**, because that is the unit a stale price is judged in.

    24 hours was never chosen: it was ``SQLiteCache``'s construction default reached by accident
    through the one call site of five that passed no ``ttl_hours``. The replacement sits BELOW the
    6-hour chart series, because a profile is read as a quote far more often than a 5-year daily
    series is re-requested, and a price an hour old is a smaller lie than a price a day old.
    """
    client = ScreenerClient(cache=temp_cache)

    assert client.ttl_hours("profile") * 60 == 60, (
        f"the profile TTL must be 60 minutes; got {client.ttl_hours('profile') * 60} minutes"
    )
    assert client.ttl_hours("profile") < client.ttl_hours("chart"), (
        "a live price must not outlive a price time series"
    )
    assert client.ttl_hours("profile") < 24.0, (
        "the 24h the profile inherited was a fallback, not a decision, and must not come back"
    )


def test_the_stored_profile_entry_really_expires_in_an_hour(cache_path):
    """
    End-to-end: the number that matters is the one on disk, not the constant in the table.

    Drives :meth:`ScreenerClient.get_company_profile` with the HTTP layer and the HTML parser
    replaced, then reads ``expires_at - created_at`` out of the cache file. Reading the stored
    value means this fails if the TTL is lost anywhere between the table and the row -- which is
    precisely how the 24h default got in. The cache is deliberately built with a 999h default so
    that the only thing that can produce a 60-minute row is the call site.
    """
    client = ScreenerClient(cache=SQLiteCache(db_path=cache_path, default_ttl_hours=999.0))

    async def _fake_send(url, params=None, headers=None, follow_redirects=True):
        # A response needs its request attached, or `resp.url` (the provenance the profile
        # records) raises inside httpx.
        return httpx.Response(200, text="<html>stub</html>", request=httpx.Request("GET", url))

    ratios = CompanyProfile.model_fields["ratios"].annotation
    profile = CompanyProfile(
        symbol="RELIANCE",
        company_id=1,
        name="Reliance Industries",
        ratios=ratios(current_price=1400.0, market_cap=1_000_000.0),
    )
    client._send_with_retry = _fake_send
    client.parser.parse_full_profile = lambda **kwargs: profile

    asyncio.run(client.get_company_profile("RELIANCE"))

    conn = sqlite3.connect(cache_path)
    try:
        row = conn.execute(
            "SELECT created_at, expires_at FROM bfinance_cache WHERE cache_key = ?",
            ("screener_profile_RELIANCE",),
        ).fetchone()
    finally:
        conn.close()

    assert row is not None, "the profile was not cached at all, so there is no TTL to measure"
    created, expires = row
    assert (expires - created) / 60.0 == pytest.approx(60.0, abs=0.5), (
        f"the stored profile row lives for {(expires - created) / 60.0:.1f} minutes, not 60"
    )


def test_cache_ttl_hours_is_a_default_and_says_which_categories_override_it(monkeypatch):
    """
    The second half of defect 3: a parameter whose meaning is a fallback is not a parameter.

    ``cache_ttl_hours`` used to be the only lever on the profile TTL, which is exactly why the
    profile could inherit a number without a decision. It is now *a default for categories that
    do not state one*, and every category this client caches states one -- so the argument no
    longer reaches any of them, and the docstring says so and lists them. Both halves are
    asserted: the behaviour (a 999h constructor argument does not lengthen the profile TTL) and
    the promise (every category in the table is named in the docstring, so the two cannot drift).
    """
    built: list = []

    class _Recorder:
        """Stands in for SQLiteCache so the default-cache branch is observable and writes no file."""

        def __init__(self, default_ttl_hours=24.0, db_path=None):
            built.append(default_ttl_hours)
            self.default_ttl_hours = default_ttl_hours
            self.db_path = db_path

    monkeypatch.setattr("bfinance.screener.client.SQLiteCache", _Recorder)
    client = ScreenerClient(cache_ttl_hours=999.0)

    assert built == [999.0], (
        "cache_ttl_hours must still be what the cache is constructed with -- it is also the "
        "0-means-off switch, and a caller may rely on it for that"
    )
    assert client.ttl_hours("profile") == 1.0, (
        "a category that states its own TTL must not be overridden by the constructor default"
    )

    doc = ScreenerClient.__init__.__doc__ or ""
    for category in ScreenerClient.CATEGORY_TTL_HOURS:
        assert category in doc, (
            f"{category!r} overrides cache_ttl_hours but is not named in the docstring that "
            f"defines it"
        )
    assert "default, not a ceiling" in doc, (
        "the docstring must state which kind of knob cache_ttl_hours is, or a reader has to guess"
    )


def test_a_ttl_can_be_retuned_per_client_without_leaking_to_other_clients(temp_cache):
    """
    The documented escape hatch, which is what makes "default, not ceiling" a real answer.

    Without it, removing ``cache_ttl_hours``' grip on the profile would have silently taken away
    the only control a caller had. The per-instance copy is the part worth asserting: a retune on
    one client must not retune the process.
    """
    loud = ScreenerClient(cache=temp_cache)
    loud.CATEGORY_TTL_HOURS["profile"] = 168.0

    assert loud.ttl_hours("profile") == 168.0
    assert ScreenerClient(cache=temp_cache).ttl_hours("profile") == 1.0, (
        "the TTL table is shared class state; a per-client retune must not reach other clients"
    )
    assert ScreenerClient.CATEGORY_TTL_HOURS["profile"] == 1.0, "the class table must be untouched"


def test_an_unknown_ttl_category_raises_rather_than_defaulting():
    """A typo in a category name must not quietly become another category's TTL."""
    assert ScreenerClient.CATEGORY_TTL_HOURS, "the vocabulary must not be empty"
    with pytest.raises(KeyError):
        ScreenerClient(cache=None).ttl_hours("proflie")


# ======================================== 4a. the cache only writes JSON a strict parser can read


def _strict_constant(name):
    raise AssertionError(f"the cache wrote {name!r}, which is not JSON")


def _all_scalars(value):
    if isinstance(value, dict):
        for item in value.values():
            yield from _all_scalars(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _all_scalars(item)
    else:
        yield value


def _holds_non_finite(value) -> bool:
    return any(
        isinstance(item, float) and (item != item or item in (float("inf"), float("-inf")))
        for item in _all_scalars(value)
    )


def test_a_non_finite_payload_is_refused_and_named(temp_cache, caplog):
    """
    ``json.dumps`` accepts NaN and Infinity and writes them as bare tokens, with no warning here.

    The row that produced could not be read by any strict parser -- another language, ``jq``, a
    columnar loader -- and round-tripped only because Python's own ``json.loads`` happens to accept
    the same two tokens. Refusing is the honest outcome: the payload has no JSON form, so it is not
    cached, the key is named, and the next call re-fetches.
    """
    with caplog.at_level(logging.ERROR, logger="bfinance"):
        temp_cache.set("screener_profile_NAN", {"close": float("nan"), "x": float("inf")})

    assert temp_cache.get_result("screener_profile_NAN").status == "miss", (
        "a payload that cannot be represented must not be stored"
    )
    assert "screener_profile_NAN" in caplog.text, (
        f"the refusal must name the key it refused; got: {caplog.text!r}"
    )


def test_every_row_the_cache_writes_is_readable_by_a_strict_json_parser(temp_cache, cache_path):
    """
    The property, over a payload battery rather than one value.

    The point is not that NaN is rejected (that is the test above) but that *no row this cache
    writes* is unreadable to a conforming parser -- including a non-finite float nested deep
    inside a dict, which is what a real ``profile.model_dump()`` looks like.
    """
    payloads = {
        "plain": {"a": 1, "b": [1, 2, 3], "c": {"d": "e"}},
        "null_value": None,
        "empty_dict": {},
        "float": 3.14,
        "unicode": {"name": " Reliance \u20b9rs\u00e9 "},
        "nested_nan": {"ratios": {"current_price": float("nan"), "pe": 20.5}},
        "nested_inf": [1.0, float("inf"), -1.0],
        "bools_and_nulls": {"a": True, "b": None, "c": False},
        "deep": {"l1": {"l2": {"l3": [1, {"l4": None}]}}},
    }
    for key, payload in payloads.items():
        temp_cache.set(key, payload, category="profile", ttl_hours=1.0)

    conn = sqlite3.connect(cache_path)
    try:
        rows = dict(conn.execute("SELECT cache_key, payload FROM bfinance_cache").fetchall())
    finally:
        conn.close()

    # (1) Every row that exists must satisfy a conforming parser. parse_constant fires on the bare
    #     NaN/Infinity tokens and fails the test, which is the whole assertion.
    for key, raw in rows.items():
        json.loads(raw, parse_constant=_strict_constant)
        assert isinstance(raw, str)

    # (2) The finite payloads must have round-tripped exactly -- refusing is not allowed to become
    #     "silently store something else".
    for key, payload in payloads.items():
        if _holds_non_finite(payload):
            assert key not in rows, (
                f"{key} carries a non-finite float and must have been refused, but a row was written"
            )
        else:
            assert key in rows, f"{key} is representable and must have been stored"
            assert json.loads(rows[key]) == payload


def test_a_circular_payload_is_refused_not_raised(temp_cache):
    """
    ``ValueError`` is also what a circular reference raises, and it must be as contained as NaN.

    A ``set`` that raised would turn a metrics problem into a total outage, which is the reason
    the original swallow-and-log was there in the first place.
    """
    cycle: dict = {}
    cycle["self"] = cycle

    temp_cache.set("cyclic", cycle)

    assert temp_cache.get_result("cyclic").status == "miss"
    temp_cache.set("still_usable", {"ok": True})
    assert temp_cache.get("still_usable") == {"ok": True}


# ================================================ 4b. hit / null / miss / expired / corrupt are distinct


def test_a_stored_null_is_not_a_miss(temp_cache):
    """
    The hole: a stored JSON ``null`` came back as ``None``, which is also what a missing key
    returns, and also what an expired key returns. So "is this thing ever cached?" was
    unanswerable -- a cache holding only nulls looked exactly like a cache holding nothing.

    Impact, stated honestly: every caller in this package reads ``None`` as "go fetch it", so the
    cost was a refetch, not wrong data. It is exposed rather than silently repaired because the
    next caller needs the distinction and ``get()`` cannot express it in its return type.
    """
    temp_cache.set("screener_profile_NULL", None, category="profile")

    stored_null = temp_cache.get_result("screener_profile_NULL")
    absent = temp_cache.get_result("never_written")

    assert stored_null.status == "null", f"got {stored_null.status!r}"
    assert stored_null.found is True
    assert absent.status == "miss"
    assert absent.found is False
    assert stored_null.status != absent.status, "a stored null and a miss must be distinguishable"
    # The row really is on disk -- this is not "nothing was written" wearing a new name.
    assert temp_cache.get("screener_profile_NULL") is None
    assert temp_cache.get("never_written") is None


def test_the_lookup_statuses_cover_the_documented_cases(temp_cache):
    """
    All four non-error statuses, because "distinguish hit / miss / corrupt" is only true if each
    is separately reachable. ``expired`` needs no clock: a negative TTL is already expired when
    written. ``corrupt`` has its own test below, which needs a hand-damaged row.
    """
    temp_cache.set("hit", {"a": 1}, ttl_hours=1)
    temp_cache.set("null", None, ttl_hours=1)
    temp_cache.set("expired", {"a": 1}, ttl_hours=-1)

    assert temp_cache.get_result("hit").status == "hit"
    assert temp_cache.get_result("hit").value == {"a": 1}
    assert temp_cache.get_result("hit").found is True
    assert temp_cache.get_result("null").status == "null"
    assert temp_cache.get_result("null").found is True
    assert temp_cache.get_result("expired").status == "expired"
    assert temp_cache.get_result("expired").found is False
    assert temp_cache.get_result("absent").status == "miss"
    assert temp_cache.get_result("absent").found is False


def test_a_corrupt_row_is_reported_and_not_returned(temp_cache, cache_path, caplog):
    """
    The behaviour that was already right, pinned so it stays right: log at ERROR, answer a miss.

    A caller must not be handed an unparseable row, and the traceback is the only evidence a
    corrupt cache leaves behind, so it is kept rather than swallowed.
    """
    temp_cache.set("corrupt_me", {"ok": 1})
    conn = sqlite3.connect(cache_path)
    try:
        conn.execute(
            "UPDATE bfinance_cache SET payload = ? WHERE cache_key = ?", ("{not json", "corrupt_me")
        )
        conn.commit()
    finally:
        conn.close()

    with caplog.at_level(logging.ERROR, logger="bfinance"):
        result = temp_cache.get_result("corrupt_me")

    assert result.status == "corrupt", f"got {result.status!r}"
    assert result.value is None
    assert result.found is False
    assert "corrupt_me" in caplog.text


# =============================== 4c. the connection-lifetime claims, measured rather than asserted


def _open_connections() -> int:
    """Live ``sqlite3.Connection`` objects that are still *usable* (i.e. not closed).

    Counting objects is not enough and would have made the test below meaningless: a closed
    connection stays alive as cyclic garbage until the collector runs, so an object count cannot
    tell "closed" from "still holding the database file open". ``SELECT 1`` can.
    """
    open_now = 0
    for obj in gc.get_objects():
        if not isinstance(obj, sqlite3.Connection):
            continue
        try:
            obj.execute("SELECT 1")
        except sqlite3.ProgrammingError:
            continue  # closed: the file handle is gone whatever the object graph says
        except Exception:
            pass
        open_now += 1
    return open_now


def test_a_failed_read_does_not_pin_a_connection(cache_path):
    """
    Measured claim (b), pinned.

    ``logger.exception`` stores a traceback on the record, and the traceback holds the frame that
    holds the connection. Any handler that retains records -- a list handler, pytest's ``caplog``
    -- therefore pinned one open SQLite file handle per corrupt read: 50 corrupt reads left 51
    open connections, released only when the records were dropped. The ERROR log and its
    traceback are kept; the connection no longer rides along on them.
    """
    cache = SQLiteCache(db_path=cache_path, default_ttl_hours=1.0)
    cache.set("corrupt", {"ok": 1})
    conn = sqlite3.connect(cache_path)
    try:
        conn.execute("UPDATE bfinance_cache SET payload = '{bad' WHERE cache_key = 'corrupt'")
        conn.commit()
    finally:
        conn.close()

    retained: list = []

    class _Retain(logging.Handler):
        def emit(self, record):
            retained.append(record)

    handler = _Retain()
    log = logging.getLogger("bfinance")
    log.addHandler(handler)
    try:
        gc.collect()
        baseline = _open_connections()
        for _ in range(50):
            assert cache.get("corrupt") is None
        held = _open_connections()
    finally:
        log.removeHandler(handler)

    assert len(retained) == 50, "the 50 ERROR records must still be logged -- that is not what changed"
    assert held <= baseline, (
        f"50 failed reads took the open-connection count from {baseline} to {held}; each one is "
        f"being held open by the retained traceback"
    )


def test_repeated_operations_do_not_accumulate_connections(temp_cache):
    """
    Measured claim (a): there is no per-call leak, and this keeps it that way.

    8,500 operations leave 0 permanently-open connections: what is left *between* collections is
    cyclic garbage the generational collector reclaims (45 objects mid-run, 1 after a collection),
    bounded by its thresholds rather than growing with call count. That is also why the note in
    ``tests/test_sqlite_cache.py`` saying "``get``/``set``/``clear`` leak one per call" is wrong,
    and why no leak was "fixed" here.

    Honest limitation: this assertion cannot tell a missing explicit close from a present one --
    both are collected, and the object count is the same. It is a guard against a *real*
    regression (a connection cached on the instance and never closed survives the collection), not
    a pin on the fix. The assertion that pins the fix is the failed-read test above.
    """
    gc.collect()
    baseline = _open_connections()
    for i in range(60):
        temp_cache.set(f"k{i}", {"i": i}, ttl_hours=1)
        assert temp_cache.get(f"k{i}") == {"i": i}
    gc.collect()

    assert _open_connections() <= baseline, (
        f"60 set+get pairs left {_open_connections() - baseline} open connections after a "
        f"collection; a per-call connection that is never closed would show up here"
    )


def test_the_cache_works_when_a_pragma_cannot_be_set(cache_path, monkeypatch, caplog):
    """
    The bare ``except Exception`` around the two PRAGMAs is deliberate, and this says so.

    WAL needs a shared-memory-mappable file (network shares, some FUSE mounts). A filesystem that
    refuses it is still a fine place to keep a cache, so the pragma failure is logged and the
    connection is used as-is. If this test ever fails because the PRAGMA failure became fatal, the
    cache would be refusing to work over a lock-coarsening detail.
    """
    real_connect = cache_mod.sqlite3.connect

    class _NoPragmaConnection:
        """A real connection whose PRAGMA statements raise, standing in for the hostile FS."""

        def __init__(self, path):
            self._inner = real_connect(path, timeout=5.0)

        def execute(self, sql, *args):
            if sql.strip().upper().startswith("PRAGMA"):
                raise sqlite3.OperationalError(f"cannot set {sql} on this filesystem")
            return self._inner.execute(sql, *args)

        def cursor(self):
            return self._inner.cursor()

        def commit(self):
            return self._inner.commit()

        def close(self):
            return self._inner.close()

    monkeypatch.setattr(cache_mod.sqlite3, "connect", lambda path, timeout=5.0: _NoPragmaConnection(path))
    with caplog.at_level(logging.WARNING, logger="bfinance"):
        cache = cache_mod.SQLiteCache(db_path=cache_path, default_ttl_hours=1.0)
        cache.set("k", {"a": 1}, ttl_hours=1.0)
    assert cache.get("k") == {"a": 1}, "a refused WAL/busy_timeout must not stop the cache"
    assert "Failed to enable WAL mode" in caplog.text
    assert "Failed to set busy_timeout" in caplog.text


# ============================================== the pre-existing retry discipline must stay green


def test_the_retry_discipline_still_names_the_attempt_count(monkeypatch):
    """
    An independent guard on the retry contract, so weakening it has to break two files.

    ``tests/test_nse_bhavcopy.py`` asserts ``"failed after 2 attempts"`` with ``max_retries=2``;
    ``tests/test_nse_corporate_actions.py`` bounds the attempt count (``<= 3`` with
    ``max_retries=3``) and requires a 429 to name itself. None of the four defects fixed here
    touch a retry loop, and this asserts that behaviourally rather than trusting it.
    """
    attempts = {"n": 0}

    def _always_503(request):
        attempts["n"] += 1
        return httpx.Response(503, text="busy")

    class _FakeHttpx:
        Response = httpx.Response
        TimeoutException = httpx.TimeoutException
        TransportError = httpx.TransportError

        @staticmethod
        def AsyncClient(**kwargs):
            kwargs.pop("transport", None)
            kwargs.pop("proxy", None)
            return httpx.AsyncClient(transport=httpx.MockTransport(_always_503))

    monkeypatch.setattr(bc, "httpx", _FakeHttpx)
    client = bc.BhavcopyClient(min_request_interval_ms=0.0, max_retries=2)

    with pytest.raises(UpstreamServiceError, match="failed after 2 attempts"):
        asyncio.run(client._get_archive("https://example.invalid/x.zip"))

    assert attempts["n"] == 2, "a 5xx must be retried, and the count must be the configured one"


def test_the_retry_assertions_in_the_existing_suites_are_still_there():
    """
    A tripwire against weakening, in the only place a weakening could hide.

    Both suites' assertions are load-bearing and none is affected by these four fixes. If someone
    later relaxes ``match="failed after 2 attempts"`` to ``match="failed"``, or drops the bound on
    the attempt count, this fails. A check whose subject is another test file cannot be anything
    but a tripwire, and it says so rather than pretending to be a behavioural test.
    """
    tests = Path(__file__).parent

    bhavcopy_src = (tests / "test_nse_bhavcopy.py").read_text(encoding="utf-8")
    assert 'match="failed after 2 attempts"' in bhavcopy_src, (
        "test_nse_bhavcopy.py no longer asserts the attempt count in the retry error"
    )
    assert "max_retries=2" in bhavcopy_src
    assert "max_retries=3" in bhavcopy_src

    actions_src = (tests / "test_nse_corporate_actions.py").read_text(encoding="utf-8")
    assert 'assert calls["n"] <= 3' in actions_src, (
        "test_nse_corporate_actions.py no longer bounds the attempt count"
    )
    assert "max_retries=3" in actions_src
    assert "max_retries=2" in actions_src
