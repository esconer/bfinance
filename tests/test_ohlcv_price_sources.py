"""
``source=`` / ``sources=`` on the price path: the injection seam, the failsafe, and the four
attrs keys.

Why this file exists
--------------------
:mod:`bfinance.sources.cascade` is a complete, tested, multi-source failover engine -- try-order,
health ledger, price-basis enforcement, attempt ledger. Before this change nothing in ``src/``
reached it: ``Ticker.history()`` hardcoded ``ohlc="bhavcopy"`` and went straight to
:meth:`OHLCVEngine._fetch_bhavcopy_bars`, so ``marketlens-close`` -- the one genuine second
as-traded source in the whole package -- was never consulted by anything. The machinery existed
and was unplugged.

What is under test
------------------
1. **The seam.** An optional :class:`PriceSourceRouter` on :class:`OHLCVEngine`, mirroring
   ``cascade.resolve(..., fetch=...)``. With nothing injected and no source named, the direct
   bhavcopy path runs byte-for-byte as before and the registry is never even imported. That is
   the property that keeps the ~130 existing ``OHLCVEngine(screener, corporate_actions_client=...)``
   call sites across ten files meaningful; the control that proves it is in this file too.
2. **The failsafe, against the REAL registry.** Every pre-existing cross-capability failover test
   in this package runs against a *synthetic* registry built inside the test. These do not: they
   use :data:`bfinance.sources.registry.DEFAULT`, so the try-order, the declared bases and the
   per-row measured notes are the shipped ones.
3. **The basis rule, which is stronger than the plan claimed.** An as-traded request skips
   ``trendlyne-ohlc`` on basis and spends **zero** requests on it, so the call list is
   ``["nse-bhavcopy", "marketlens-close"]`` and never contains it. HDFCBANK's close on that
   vendor's feed is exactly 0.5x the bhavcopy's on all 62 sessions of 2024-12-16..2025-03-13,
   so an as-traded request served from it would carry a move that never happened.
4. **The four attrs keys**, on the populated frame and the empty one.

Everything here is offline. The price sources are reached only through an injected ``fetch``; the
corporate-actions feed and the Screener client are stubbed; the direct path's archive is stubbed
wherever it is exercised. No test in this file may make a network request.
"""

import importlib

import pandas as pd
import pytest

import bfinance.market.ohlcv as ohlcv_mod
from bfinance.market.ohlcv import (
    BHAVCOPY_SOURCE,
    PRICE_BASIS_AS_TRADED,
    PRICE_BASIS_SHARE_COUNT_ADJUSTED,
    PRICE_BASIS_VENDOR_SPLIT_ADJUSTED,
    PRICE_SOURCE_ATTR,
    PRICE_SOURCE_ATTEMPTS_ATTR,
    PRICE_SOURCE_DEGRADED_ATTR,
    PRICE_SOURCE_DETAIL_ATTR,
    OHLCVEngine,
    PriceSourceRouter,
    resolve_price_source_names,
)
from bfinance.sources.errors import SourceUnavailableError
from bfinance.sources.health import HealthLedger
from bfinance.sources.model import SKIPPED_BASIS
from bfinance.sources.registry import DEFAULT

# `from bfinance import download` yields the FUNCTION, not the module, because __init__ re-exports
# it. importlib is the reliable way to reach the module (the same note as test_silent_fallbacks).
download_mod = importlib.import_module("bfinance.download")

#: The vendor whose adjusted series the as-traded rule must never reach. Named once so the two
#: tests that make opposite claims about it cannot disagree about which row they mean.
TRENDLYNE = "trendlyne-ohlc"

#: Four consecutive sessions, so ``_infer_bar_interval`` can read a spacing and the window
#: arithmetic has more than one bar to work with.
_DATES = ["2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24"]


# ============================================================ stubs


def _bars(close=100.0, volume=1000, index=None):
    """A per-symbol OHLCV frame shaped like every price source's: naive ascending Date index."""
    index = pd.DatetimeIndex(index or _DATES, name="Date")
    return pd.DataFrame(
        {
            "Open": [close] * len(index),
            "High": [close * 1.01] * len(index),
            "Low": [close * 0.99] * len(index),
            "Close": [close] * len(index),
            "Volume": [volume] * len(index),
        },
        index=index,
    )


def _market_frame(rows):
    """Whole-market bhavcopy-shaped frame, exactly as ``nse.bhavcopy`` returns it.

    Used only to drive the DIRECT path, because that is the only path that goes to the archive.
    A row per (date, symbol) so "this symbol is absent" is expressible.
    """
    dates, symbols, values = [], [], []
    for symbol, bars in rows.items():
        for date, o, h, low, close, volume in bars:
            dates.append(date)
            symbols.append(symbol)
            values.append((o, h, low, close, volume))
    index = pd.MultiIndex.from_arrays(
        [pd.DatetimeIndex(dates), pd.Index(symbols, dtype=object, name="Symbol")],
        names=["Date", "Symbol"],
    )
    frame = pd.DataFrame(
        values, columns=["Open", "High", "Low", "Close", "Volume"], index=index
    )
    frame["Volume"] = frame["Volume"].astype("int64")
    return frame.sort_index()


class _Fetch:
    """A stand-in for every price source, with the cascade's own ``fetch`` contract.

    ``script`` maps a registry row name to either a frame (it answers), an exception (it raises,
    which is the cascade's fall-through case), or a callable taking ``(spec, request)``. A name
    with no entry answers with a fresh default frame, because a test that only cares about the
    ORDER should not have to script the winner as well.

    ``calls`` is the whole point: it is the only evidence that a request was or was not spent, and
    a test asserting that a source was "skipped on basis" is only non-vacuous if the absence is
    observable. The cascade already distinguishes "called and failed" from "never asked"
    (``Attempt.called``); this records which of the two actually happened.
    """

    def __init__(self, script=None, close=100.0):
        self.script = dict(script or {})
        self.calls = []
        self.requests = []
        self.close = close

    def __call__(self, spec, request):
        self.calls.append(spec.name)
        self.requests.append(dict(request))
        if spec.name in self.script:
            outcome = self.script[spec.name]
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome(spec, request) if callable(outcome) else outcome
        return _bars(close=self.close)


class _NoScreener:
    """Tripwire screener. The bhavcopy path must never consult it, and says so by raising."""

    async def get_chart_timeseries(self, *args, **kwargs):
        raise AssertionError("the bhavcopy path must not consult the close-only chart API")

    async def get_company_profile(self, *args, **kwargs):
        raise AssertionError(
            "the NSE corporate-actions stub answered, so the Screener profile fallback must not "
            "be reached"
        )


class _SplitStub:
    """Corporate-actions stub declaring a 1:1 bonus with an ex-date AFTER every bar.

    The direction matters and it is the real one: a share-count event restates the bars BEFORE its
    ex-date, which is why HDFCBANK's 2025-08-26 bonus halves all 53 bars of a 2024-09..2024-11
    request. Ratio 2.0 therefore makes ``_split_adjustment_factor`` return exactly 0.5 for every
    bar, so the two branches of the vendor-adjusted test are separable by a factor of two and not
    by a rounding difference: an as-traded winner's closes come back halved, a split-adjusted
    winner's do not move at all.
    """

    #: One day past the last stub bar, so every bar is strictly before the ex-date.
    EX_DATE = "2026-09-25"

    def __init__(self, ratio=2.0, ex_date=None):
        self.ratio = ratio
        self.ex_date = ex_date or self.EX_DATE
        self.calls = []

    async def actions_async(self, symbol, start=None, end=None, index="equities"):
        from bfinance.nse.corporate_actions import ACTION_COLUMNS

        self.calls.append((symbol, start, end))
        stamps = pd.DatetimeIndex([self.ex_date], name="Date", tz="Asia/Kolkata")
        data = {column: pd.Series(0.0, index=stamps, dtype="float64") for column in ACTION_COLUMNS}
        data["Stock Splits"] = pd.Series([self.ratio], index=stamps, dtype="float64")
        return pd.DataFrame(data, index=stamps)


def _engine(fetch=None, actions=None, screener=None):
    """An engine with the seam wired exactly the way a caller would wire it."""
    return OHLCVEngine(
        screener or _NoScreener(),
        corporate_actions_client=actions,
        price_sources=PriceSourceRouter(
            fetch=fetch,
            # A fresh ledger, not the process-wide one. The process-wide ledger *learns*, so a
            # recorded failure in one test would reorder the try-order in the next one -- which is
            # the right behaviour in production and a source of order-dependent tests offline.
            health=HealthLedger(),
        ),
    )


def _outcomes(attrs):
    return {a["source"]: a["outcome"] for a in attrs[PRICE_SOURCE_ATTEMPTS_ATTR]}


# ============================================ 1. the seam, and the default it must not change


async def test_the_two_argument_construction_still_means_the_direct_bhavcopy_path(monkeypatch):
    """
    The control that makes "no regression" checkable, and the reason the seam exists at all.

    Roughly 130 offline call sites across ten test files construct
    ``OHLCVEngine(screener_client, corporate_actions_client=...)`` and stub those two objects.
    :func:`bfinance.sources.cascade.load_provider` imports each row's module at CALL time, so the
    moment the cascade is on the default path every one of those stubs is bypassed and the suite
    starts reaching the real NSE. Asserted here on the *behaviour*, not the signature: with two
    arguments and no source named, the archive stub is what answers.
    """
    asked = []

    def _range(start, end, **kwargs):
        asked.append((start, end))
        return _market_frame(
            {"RELIANCE": [(date, 100.0, 101.0, 99.0, 100.5, 1000) for date in _DATES]}
        )

    monkeypatch.setattr(ohlcv_mod, "bhavcopy_range", _range)

    frame = await OHLCVEngine(_NoScreener()).fetch_history(
        symbol="RELIANCE", period="5d", actions=False
    )

    assert asked, "the direct archive reader was bypassed, so something else answered"
    assert not frame.empty
    assert frame.attrs[PRICE_SOURCE_ATTR] is None, (
        "no registry row was consulted on the default path, so naming a vendor here would be a "
        "claim about a routing decision that was never made"
    )
    assert frame.attrs[PRICE_SOURCE_ATTEMPTS_ATTR] == []
    assert frame.attrs[PRICE_SOURCE_DEGRADED_ATTR] is False
    assert "no price-source cascade ran" in frame.attrs[PRICE_SOURCE_DETAIL_ATTR]


def test_the_constructor_signature_still_takes_the_same_two_positional_arguments():
    """
    Pinned on the signature because the behaviour test above cannot see a *reorder*.

    ``screener_client`` and ``corporate_actions_client`` must remain the first two parameters with
    the same defaults; the router is third and optional, so every existing call site -- positional
    or keyword -- resolves to exactly what it did before.
    """
    import inspect

    params = list(inspect.signature(OHLCVEngine.__init__).parameters)
    assert params[:3] == ["self", "screener_client", "corporate_actions_client"], (
        f"the two-argument construction other tests depend on moved: {params}"
    )
    defaults = inspect.signature(OHLCVEngine.__init__).parameters
    assert defaults["corporate_actions_client"].default is None
    assert defaults["price_sources"].default is None


def test_no_source_named_does_not_import_the_registry():
    """
    The isolation half of "opt-in", asserted at the point where it would be lost.

    ``tests/test_public_surface.py`` requires that ``import bfinance`` does not pull
    :mod:`bfinance.sources` in, and ``tests/test_sources_cascade.py::TestSubprocessProbes``
    re-checks it in a fresh interpreter. Those probe the *import*; this probes the *call path*,
    which is where a well-meaning "always resolve through the cascade" would reintroduce the
    dependency. ``resolve_price_source_names()`` is the guard, so it is asserted directly.
    """
    assert resolve_price_source_names() == ()
    assert resolve_price_source_names(sources=[]) == ()


# ============================== 2. failover against the REAL registry (not a synthetic one)


async def test_a_real_registry_as_traded_fails_over_and_skips_the_adjusted_source():
    """
    The headline test, and the one the plan got wrong in its assertion.

    The plan asked for ``fetch.calls == ["trendlyne-ohlc", "nse-bhavcopy"]``. That cannot happen:
    ``trendlyne-ohlc`` publishes ``split_adjusted``, so an as-traded request SKIPS it on basis and
    spends zero requests on it. The real claim is stronger and is the one asserted here -- on
    :data:`bfinance.sources.registry.DEFAULT`, with the first as-traded source failing, the second
    answers, and the adjusted source is recorded as a skip and is **absent from the call list**.

    The registry is the shipped one. A synthetic registry would make this test pass while the
    shipped try-order could be anything, which is exactly the gap this file exists to close: every
    pre-existing cross-capability failover test builds its own registry.
    """
    fetch = _Fetch(
        {
            BHAVCOPY_SOURCE: ConnectionError("www.nseindia.com did not respond after 30s"),
            "marketlens-close": _bars(close=269.0),
        }
    )

    frame = await _engine(fetch).fetch_history(symbol="ITC", period="5d", actions=False)

    # (1) The real registry, and the real try-order within it.
    assert DEFAULT.names_for("price_history")[:2] == (BHAVCOPY_SOURCE, "marketlens-close"), (
        "this test is a claim about the SHIPPED try-order; if the registry changed, the claim "
        "changed and this test must be rewritten, not adjusted"
    )

    # (2) The exact call list. Not "contains", not "at least" -- the order is the failover.
    assert fetch.calls == [BHAVCOPY_SOURCE, "marketlens-close"], (
        f"the cascade must try the registry order and only then fall through; got {fetch.calls}"
    )

    # (3) The basis rule, on the same frame: the adjusted source was considered, recorded, and
    # never asked. Its absence from `calls` and its presence in `attempts` are different facts and
    # both are asserted, because "never called" is the stronger claim and only it means a
    # rate-limited vendor did not pay for a request it could not have honoured.
    assert TRENDLYNE not in fetch.calls, (
        f"an as_traded request must not spend a request on a split_adjusted source; got {fetch.calls}"
    )
    outcomes = _outcomes(frame.attrs)
    assert outcomes[TRENDLYNE] == SKIPPED_BASIS
    assert outcomes[BHAVCOPY_SOURCE] == "failed"
    assert outcomes["marketlens-close"] == "served"

    # (4) Degraded, and the winner's bars, whole.
    assert frame.attrs[PRICE_SOURCE_ATTR] == "marketlens-close"
    assert frame.attrs[PRICE_SOURCE_DEGRADED_ATTR] is True
    assert list(frame["Close"]) == [269.0] * len(_DATES)


async def test_a_healthy_first_choice_is_not_degraded_and_asks_nobody_else():
    """
    The mirror of the test above, and the reason the degraded flag is worth reading.

    Without it, ``degraded`` could be a constant ``True`` and the failover test above would still
    be green. Template and rationale taken from
    ``tests/test_sources_cascade.py::TestResolution::test_a_healthy_first_choice_is_not_degraded``.
    """
    fetch = _Fetch({})

    frame = await _engine(fetch).fetch_history(symbol="ITC", period="5d", actions=False)

    assert fetch.calls == [BHAVCOPY_SOURCE], (
        f"one answer ends the cascade; the rest must not be asked. got {fetch.calls}"
    )
    assert frame.attrs[PRICE_SOURCE_ATTR] == BHAVCOPY_SOURCE
    assert frame.attrs[PRICE_SOURCE_DEGRADED_ATTR] is False, (
        "a source that answered first was not degraded from anything"
    )
    # The basis skips are still recorded, and still are not degradation. This is the distinction
    # the whole flag rests on: "passed over because it cannot serve" is the system working.
    outcomes = _outcomes(frame.attrs)
    assert outcomes[TRENDLYNE] == SKIPPED_BASIS
    assert frame.attrs[PRICE_SOURCE_DEGRADED_ATTR] is False


async def test_a_split_adjusted_request_asks_exactly_the_adjusted_source():
    """
    The other half of the basis rule, and the only way to reach the split-adjusted row at all.

    ``sources=["trendlyne-ohlc"]`` names one source, so the requested basis is that source's own
    declared basis -- the engine asks for what the caller named rather than defaulting to
    as-traded and then refusing. One call, no skip, and the frame is published on the vendor's own
    basis because bfinance applied no further share-count factor (see the test below).
    """
    fetch = _Fetch({TRENDLYNE: _bars(close=932.0)})

    frame = await _engine(fetch).fetch_history(
        symbol="HDFCBANK", period="5d", actions=False, sources=[TRENDLYNE]
    )

    assert fetch.calls == [TRENDLYNE]
    assert frame.attrs[PRICE_SOURCE_ATTR] == TRENDLYNE
    assert frame.attrs[PRICE_SOURCE_DEGRADED_ATTR] is False
    assert frame.attrs["price_basis"] == PRICE_BASIS_VENDOR_SPLIT_ADJUSTED
    assert list(frame["Close"]) == [932.0] * len(_DATES)


# ============================== 3. the basis rule, and what it forbids


async def test_an_as_traded_request_never_calls_the_split_adjusted_source():
    """
    The rule as its own test, with the negative control built in.

    The failover test above also skips the adjusted source, but it is busy asserting the failover.
    This one exists so the claim is pinned on its own: an as-traded request spends **zero**
    requests on a source that publishes a different basis, and the skip is recorded with a reason
    naming the basis rather than silently omitted.

    The control is the last two assertions: the same stub, asked for that source explicitly, IS
    called. Without it, "trendlyne-ohlc was never called" would also be satisfied by a broken
    routing table that could not reach it at all.
    """
    fetch = _Fetch({})

    frame = await _engine(fetch).fetch_history(symbol="ITC", period="5d", actions=False)

    assert TRENDLYNE not in fetch.calls
    skip = next(
        a for a in frame.attrs[PRICE_SOURCE_ATTEMPTS_ATTR] if a["source"] == TRENDLYNE
    )
    assert skip["outcome"] == SKIPPED_BASIS
    assert skip["called"] is False, (
        "'never called' and 'called and rejected' are different diagnoses, and only the first one "
        "means the vendor did not pay for a request"
    )
    assert "split_adjusted" in skip["reason"] and "as_traded" in skip["reason"], (
        f"the skip must name both bases so 'why is my data not from that vendor' is answerable "
        f"without reading the registry; got: {skip['reason']!r}"
    )

    # -- control: the same source, asked for by name, IS reached.
    asked = _Fetch({})
    await _engine(asked).fetch_history(
        symbol="ITC", period="5d", actions=False, sources=[TRENDLYNE]
    )
    assert asked.calls == [TRENDLYNE], (
        "if this is empty the test above proves nothing but a routing table that cannot reach the "
        "row at all"
    )


async def test_naming_two_sources_on_different_bases_is_refused_not_silently_ordered():
    """
    A failsafe list has to be on ONE basis to be a failsafe, and the refusal says why.

    The tempting behaviour is to try both anyway and hand back whichever answers, which looks like
    resilience and is actually the defect this package is organised against: bfinance would then
    be asking two vendors a question they answer differently. Measured size of that difference --
    HDFCBANK, 62 consecutive sessions, the split-adjusted close is exactly 0.5x the as-traded one.
    """
    fetch = _Fetch({})

    with pytest.raises(ValueError) as exc:
        await _engine(fetch).fetch_history(
            symbol="HDFCBANK", period="5d", actions=False,
            sources=[BHAVCOPY_SOURCE, TRENDLYNE],
        )

    message = str(exc.value)
    assert "do not overlap" in message
    assert "as_traded" in message and "split_adjusted" in message
    assert fetch.calls == [], "a refused request must cost no requests"


# ============================================ 4. the negative controls


async def test_every_as_traded_source_failing_raises_and_names_every_one_of_them():
    """
    The negative control for the failover test, without which the failover could be an artefact of
    the stub.

    Both as-traded sources are asked, both fail, and the call raises -- a caller must never get a
    frame that looks like data and is nothing. The refusal has to carry the whole attempt ledger,
    because "which vendor did I already try" is the only thing that makes a retry decision
    possible, and it is the one piece of context an exception message would otherwise lose.

    The basis-skipped sources are named too, and separately: they were NOT asked, and a message
    that listed them beside the failures would tell a caller to retry a source that was never
    touched.
    """
    fetch = _Fetch(
        {
            BHAVCOPY_SOURCE: ConnectionError("nse down"),
            "marketlens-close": TimeoutError("marketlens timed out"),
        }
    )

    with pytest.raises(SourceUnavailableError) as exc:
        await _engine(fetch).fetch_history(symbol="ITC", period="5d", actions=False)

    assert fetch.calls == [BHAVCOPY_SOURCE, "marketlens-close"], (
        "both eligible as-traded sources must be tried before the call gives up"
    )
    message = str(exc.value)
    assert BHAVCOPY_SOURCE in message and "marketlens-close" in message
    assert "nse down" in message and "marketlens timed out" in message
    assert "were not asked at all" in message, (
        "the sources that were skipped on basis are a different diagnosis from the ones that "
        "failed, and the message has to keep them apart"
    )
    assert TRENDLYNE in message


async def test_a_source_answering_with_nothing_is_a_failure_not_an_empty_history():
    """
    The one payload shape that is dangerous rather than merely disappointing: a vendor serving an
    HTML shell or an empty table at HTTP 200.

    A zero-row frame returned as data reads as "this company has no price history", which is a
    claim about the market that the source never made. So an empty answer is a fall-through, the
    next source is asked, and if nothing else answers the call raises.
    """
    fetch = _Fetch(
        {
            BHAVCOPY_SOURCE: pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"]),
            "marketlens-close": _bars(close=269.0),
        }
    )

    frame = await _engine(fetch).fetch_history(symbol="ITC", period="5d", actions=False)

    assert fetch.calls == [BHAVCOPY_SOURCE, "marketlens-close"]
    assert frame.attrs[PRICE_SOURCE_ATTR] == "marketlens-close"
    assert frame.attrs[PRICE_SOURCE_DEGRADED_ATTR] is True
    assert not frame.empty


# ============================================ 5. the refusal happens before any request


async def test_an_unknown_source_name_raises_before_a_single_call_is_spent():
    """
    The timing, not just the refusal -- the assertion that says *when*.

    ``tests/test_silent_fallbacks.py::test_download_rejects_before_doing_any_work`` pins the same
    property for an unknown ``download()`` keyword, and the reasoning is identical: a source name
    is a local vocabulary, so discovering it is invalid by asking a vendor. Both tripwires are
    present -- the injected ``fetch`` and the module-level archive -- because a validation that ran
    after the first fetch would still produce a raise.
    """
    fetch = _Fetch({})
    asked = []
    original = ohlcv_mod.bhavcopy_range
    ohlcv_mod.bhavcopy_range = lambda *a, **k: asked.append(a) or pd.DataFrame()
    try:
        with pytest.raises(ValueError) as exc:
            await _engine(fetch).fetch_history(
                symbol="ITC", period="5d", actions=False, sources=["nse-bhavcopy", "not-a-source"]
            )
    finally:
        ohlcv_mod.bhavcopy_range = original

    message = str(exc.value)
    assert "not-a-source" in message, f"the refusal must quote what it was given; got: {message}"
    assert "nse-bhavcopy" in message, (
        f"the refusal must list the vocabulary so the caller can fix the call; got: {message}"
    )
    assert fetch.calls == [], f"a refused source name must cost no request; got {fetch.calls}"
    assert asked == [], "and it must not have fallen through to the direct archive reader either"


async def test_download_refuses_an_unknown_source_before_constructing_a_ticker(monkeypatch):
    """
    The batch-level half. One typo must not cost one request per symbol in a 5,000-symbol run.

    ``download`` is where that cost is real, so the validation happens in ``download()`` and not
    only inside each symbol's ``history()`` call. The stub ticker records its own constructions,
    which is the only way to see the fan-out never started.
    """

    class _StubTicker:
        constructed = []

        def __init__(self, symbol, proxy=None):
            type(self).constructed.append(symbol)

        async def history_async(self, **kwargs):
            raise AssertionError("an unknown source must be refused before any history call")

    _StubTicker.constructed = []
    monkeypatch.setattr(download_mod, "Ticker", _StubTicker)

    with pytest.raises(ValueError) as exc:
        download_mod.download(["RELIANCE", "TCS"], source="not-a-source")

    assert "not-a-source" in str(exc.value)
    assert _StubTicker.constructed == [], (
        f"a refused source name must be refused before a single Ticker is built; got "
        f"{_StubTicker.constructed}"
    )


async def test_download_forwards_the_failsafe_list_to_every_symbol(monkeypatch):
    """
    The positive control for the test above: the arguments still reach the per-symbol call.

    Rejecting an unknown name is only a fix if the known ones still work, and a check that
    swallowed a legitimate argument is the same defect pointing the other way. Asserted on what
    each symbol's ``history_async`` actually received.
    """
    seen = []

    class _StubTicker:
        def __init__(self, symbol, proxy=None):
            self.symbol = symbol

        async def history_async(self, **kwargs):
            seen.append((self.symbol, kwargs.get("source"), tuple(kwargs.get("sources") or ())))
            return _bars()

    monkeypatch.setattr(download_mod, "Ticker", _StubTicker)

    download_mod.download(
        "RELIANCE TCS", sources=[BHAVCOPY_SOURCE, "marketlens-close"]
    )

    assert sorted(seen) == [
        ("RELIANCE", None, (BHAVCOPY_SOURCE, "marketlens-close")),
        ("TCS", None, (BHAVCOPY_SOURCE, "marketlens-close")),
    ], f"the failsafe list did not reach every symbol intact: {seen}"


# ============================================ 6. the four attrs keys


def test_the_four_keys_are_the_documented_names():
    """
    Spelled out rather than compared against themselves, so a rename is a visible edit here
    instead of a silent divergence between the module and its consumers.

    ``bfinance_source`` is deliberately NOT in this set: it means the TRANSPORT
    (``network``/``mirror``/``mirror+network``) and its vocabulary is a published contract that
    this change does not touch. A frame can be real bhavcopy read off disk a week ago, so "which
    vendor printed this" and "how stale can it be" are different questions with different keys.
    """
    assert (PRICE_SOURCE_ATTR, PRICE_SOURCE_ATTEMPTS_ATTR) == (
        "bfinance_price_source",
        "bfinance_price_source_attempts",
    )
    assert (PRICE_SOURCE_DEGRADED_ATTR, PRICE_SOURCE_DETAIL_ATTR) == (
        "bfinance_price_source_degraded",
        "bfinance_price_source_detail",
    )
    assert "bfinance_price_source" not in ("bfinance_source", "bfinance_ohlc_source")


async def test_the_four_keys_are_present_on_the_populated_frame(monkeypatch):
    """
    Asserted on a real frame rather than on the record builder, because the record is private and
    a frame is what a caller has.
    """
    monkeypatch.setattr(
        ohlcv_mod, "bhavcopy_range",
        lambda start, end, **k: _market_frame(
            {"RELIANCE": [(date, 100.0, 101.0, 99.0, 100.5, 1000) for date in _DATES]}
        ),
    )
    frame = await OHLCVEngine(_NoScreener()).fetch_history(
        symbol="RELIANCE", period="5d", actions=False
    )
    for key in (
        PRICE_SOURCE_ATTR, PRICE_SOURCE_ATTEMPTS_ATTR,
        PRICE_SOURCE_DEGRADED_ATTR, PRICE_SOURCE_DETAIL_ATTR,
    ):
        assert key in frame.attrs, f"{key} is missing from a populated frame"
    assert frame.attrs[PRICE_SOURCE_DEGRADED_ATTR] is False


async def test_the_four_keys_are_present_on_the_empty_frame_too(monkeypatch):
    """
    The half most easily forgotten, and the one with the most bite.

    An empty frame is precisely the frame a consumer is most likely to treat as "nothing went
    wrong", so it is the worst place to leave "which vendor, and did anything fail on the way"
    unanswered. Asserted on a consumer reading one key per frame rather than probing for absence.
    """
    # A whole-market archive that simply does not carry this symbol: non-empty, so the reader
    # does not raise, but the per-symbol slice is empty. That is the real shape of "no data".
    monkeypatch.setattr(
        ohlcv_mod, "bhavcopy_range",
        lambda start, end, **k: _market_frame(
            {"SOMETHINGELSE": [(date, 1.0, 1.0, 1.0, 1.0, 1) for date in _DATES]}
        ),
    )
    # auto_adjust=False restores the pre-2026-09-30 default. It has to be explicit here: with
    # auto_adjust now True by default, an EMPTY frame raises AdjustmentNotAvailableError (there is
    # no Adj Close to scale by), which would turn a test about attrs keys on an empty frame into a
    # test about that refusal.
    frame = await OHLCVEngine(_NoScreener()).fetch_history(
        symbol="RELIANCE", period="5d", actions=False, auto_adjust=False
    )

    assert frame.empty, "this test is about the EMPTY frame; the archive stub answered"
    for key in (
        PRICE_SOURCE_ATTR, PRICE_SOURCE_ATTEMPTS_ATTR,
        PRICE_SOURCE_DEGRADED_ATTR, PRICE_SOURCE_DETAIL_ATTR,
    ):
        assert key in frame.attrs, f"{key} is missing from the EMPTY frame"
    assert frame.attrs[PRICE_SOURCE_ATTR] is None
    assert frame.attrs[PRICE_SOURCE_DEGRADED_ATTR] is False


async def test_the_four_keys_are_present_on_an_empty_frame_from_the_cascade_too():
    """
    The same guarantee on the cascade path, where the early return is reached for a different
    reason and could easily have been the branch that lost them.

    A vendor that answers with rows whose closes are all unparseable is a real failure mode, not a
    contrived one; the cascade is satisfied (the payload is not empty) and this engine then drops
    every row as unpriced, which is the empty-frame branch.
    """
    unusable = _bars(close=100.0)
    unusable["Close"] = "n/a"
    fetch = _Fetch({BHAVCOPY_SOURCE: unusable})

    frame = await _engine(fetch).fetch_history(
        symbol="ITC", period="5d", actions=False, auto_adjust=False
    )

    assert frame.empty
    assert frame.attrs[PRICE_SOURCE_ATTR] == BHAVCOPY_SOURCE, (
        "the resolution happened and its winner is known even though no bar survived tidying"
    )
    assert frame.attrs[PRICE_SOURCE_DEGRADED_ATTR] is False
    assert frame.attrs[PRICE_SOURCE_ATTEMPTS_ATTR], "the attempt ledger must survive too"


async def test_the_detail_sentence_names_what_failed_and_why():
    """
    The sentence exists for the reader who will not parse ``attempts``, so it has to carry the
    failure and the winner, not just say something happened.

    And it has to stay a SENTENCE. A basis skip's reason ends with that registry row's whole note
    -- measured evidence, 900 characters of arithmetic about HDFCBANK on the split-adjusted row --
    and pasting it in verbatim produced a 4 KB "sentence" on every as-traded call. The evidence
    stays in ``attempts``; the sentence is the summary, and the two assertions below are what keep
    them from being the same string twice.
    """
    fetch = _Fetch({BHAVCOPY_SOURCE: ConnectionError("www.nseindia.com refused the connection")})

    frame = await _engine(fetch).fetch_history(symbol="ITC", period="5d", actions=False)

    detail = frame.attrs[PRICE_SOURCE_DETAIL_ATTR]
    assert "marketlens-close" in detail
    assert "nse-bhavcopy" in detail
    assert "nseindia.com refused the connection" in detail
    assert "DEGRADED" in detail
    assert TRENDLYNE in detail, "and a source that was passed over must be named as passed over"
    assert "publishes only the split_adjusted basis" in detail
    assert "MEASURED BEFORE RANKED" not in detail, (
        "the registry note must stay in bfinance_price_source_attempts and not be pasted into a "
        "key documented as a sentence"
    )
    # The measurement it dropped is still on the frame, which is the point of dropping it here.
    assert any(
        "MEASURED BEFORE RANKED" in a["reason"]
        for a in frame.attrs[PRICE_SOURCE_ATTEMPTS_ATTR]
    ), "the registry's measured note must still travel with the frame, in the attempt ledger"


async def test_an_enabled_mirror_is_not_silently_dropped_on_the_cascade_path(caplog):
    """
    The one capability difference between the two paths, stated rather than discovered.

    The mirror is a transport for exactly one vendor, and the cascade's whole job is choosing the
    vendor, so the mirror-aware reader is not in the cascade's path. That is a real loss for a
    caller who has been running a local tape, so it is both written into
    ``attrs["bfinance_mirror"]["reason"]`` and logged -- a silently-ignored cache has the same
    defect shape as a silently-ignored cache hit, and a caller whose frames suddenly get staler
    needs to be able to find out why from the frame alone.

    The control is the second call: with no mirror configured there is no warning, so the first
    assertion is about the cascade path rather than about this logger.
    """
    fetch = _Fetch({})

    with caplog.at_level("WARNING", logger="bfinance"):
        frame = await _engine(fetch).fetch_history(
            symbol="ITC", period="5d", actions=False, mirror=True
        )

    assert "not consulted" in frame.attrs["bfinance_mirror"]["reason"]
    assert frame.attrs["bfinance_mirror"]["enabled"] is False
    assert "local mirror is configured" in caplog.text

    caplog.clear()
    with caplog.at_level("WARNING", logger="bfinance"):
        await _engine(_Fetch({})).fetch_history(symbol="ITC", period="5d", actions=False)
    assert "local mirror is configured" not in caplog.text


# ============================== 7. no blending, and the winner's real basis is published

async def test_a_vendor_adjusted_winner_is_not_re_adjusted_by_this_engine():
    """
    The seam between "which vendor answered" and "what this frame's prices are on", and the reason
    a split-adjusted winner is not a free substitute for an as-traded one.

    Both calls run against the same stub bars (close 932.0) and the same corporate-actions stub,
    which declares a 1:1 bonus with an ex-date one day past the window -- so the share-count factor
    is exactly 0.5 on every bar, which is the real HDFCBANK shape (its 2025-08-26 bonus halves all
    53 bars of a 2024-09..2024-11 request). The as-traded winner's closes come back at 466.0,
    because bfinance applied the factor. The split-adjusted winner's come back at 932.0
    **untouched**, because the vendor had already applied one and applying a second would halve
    every bar twice.

    These two must not agree, and they must not be reconciled either: ``price_basis`` publishes
    which is which, because a consumer who cannot tell them apart is holding a series that changes
    meaning at a corporate-action date with nothing on the frame to say so.
    """
    as_traded = await _engine(_Fetch(close=932.0), actions=_SplitStub()).fetch_history(
        symbol="HDFCBANK", period="5d", actions=True, sources=[BHAVCOPY_SOURCE]
    )
    adjusted = await _engine(_Fetch(close=932.0), actions=_SplitStub()).fetch_history(
        symbol="HDFCBANK", period="5d", actions=True, sources=[TRENDLYNE]
    )

    assert as_traded.attrs["price_basis"] == PRICE_BASIS_SHARE_COUNT_ADJUSTED
    assert adjusted.attrs["price_basis"] == PRICE_BASIS_VENDOR_SPLIT_ADJUSTED
    assert list(as_traded["Close"]) == [466.0] * len(_DATES), (
        "the as-traded path must still apply the share-count factor -- this test is about the "
        "OTHER path not applying it a second time, and it is vacuous if this half is wrong"
    )
    assert list(adjusted["Close"]) == [932.0] * len(_DATES), (
        "a vendor that already published split/bonus restated bars must be used whole; halving "
        "them here would apply the 1:1 bonus twice"
    )
    assert "no further share-count factor" in adjusted.attrs[
        "bfinance_corporate_actions_applied_detail"
    ]


def test_the_published_vendor_basis_is_the_vendors_own_string():
    """
    The two vocabularies must not drift.

    :data:`PRICE_BASIS_VENDOR_SPLIT_ADJUSTED` is copied from the reader that publishes it rather
    than re-invented, so a frame bfinance built and a frame the vendor built carry the same label.
    A paraphrase would be a second vocabulary for one fact, which is how "split adjusted" and
    "split-adjusted" end up comparing unequal to each other and a basis check silently switching
    itself off.
    """
    from bfinance.sources.price_provider import PRICE_BASIS

    assert PRICE_BASIS_VENDOR_SPLIT_ADJUSTED == PRICE_BASIS
    assert PRICE_BASIS_VENDOR_SPLIT_ADJUSTED != PRICE_BASIS_AS_TRADED


async def test_synthetic_and_a_named_source_cannot_be_combined():
    """
    The two arguments are orthogonal by design, and this is the one cell where they collide.

    ``ohlc="synthetic"`` is the legacy close-only path with Open/High/Low DERIVED from Close, and
    no row in the price registry does that. Refusing is the only honest answer: inventing a row
    that does not exist would be a fiction, and routing it to a real source would silently swap a
    derived frame for a real one -- which is exactly what
    ``attrs["bfinance_synthetic_ohlc"]`` exists to prevent.
    """
    fetch = _Fetch({})

    with pytest.raises(ValueError) as exc:
        await _engine(fetch).fetch_history(
            symbol="ITC", period="5d", ohlc="synthetic", sources=[BHAVCOPY_SOURCE]
        )

    assert "synthetic" in str(exc.value)
    assert fetch.calls == []
