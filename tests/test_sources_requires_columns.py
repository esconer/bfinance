"""
``provides`` and ``requires``: the cascade's second eligibility rule, and the narrow request.

Why this file exists
--------------------
A basis is not the only way a price source can fail to answer. ``marketlens-close`` is on the
**as_traded** basis -- the right basis, the one this cascade is asked for every day -- and it
publishes ``Close`` and ``Volume`` and **no Open/High/Low**
(``bfinance.nse.marketlens`` builds ``{'Close': ..., 'Volume': ...}`` and says so twice in its own
docstring). So it was *eligible* for an OHLC request, and
:func:`bfinance.sources.cascade.is_empty_payload` calls its close-only frame a perfectly good
answer, which means an OHLC request could be satisfied by a frame that has none of the bars it
asked for. Measured cost of the alternative: the as_traded ``nse-bhavcopy`` row downloads one
archive file per trading day, so a 1-year window is 60,736 rows over 18.3s and a 2-year window is
1,211,192 rows over 530s (2026-10-13, TCS), against ``marketlens-close``'s 2.5s over 1,237 rows.
``Ticker.info`` paid 184s for a frame it read two columns out of five.

What is under test
------------------
1. :attr:`bfinance.sources.registry.SourceSpec.provides` -- the declared column set, verified
   against the readers that build the frames rather than against the note that describes them.
2. The **skip**: a source that cannot publish every requested column is never called, and the skip
   is recorded with :data:`~bfinance.sources.model.SKIPPED_COLUMNS` and a reason naming the
   columns it lacks. Same invariant as a basis mismatch: the reason is recorded and it costs
   nothing.
3. ``requires=None`` -- the same winner, the same bars and the same attrs, whether the keyword is
   passed or omitted. Not "still works": the attempt ledger, the call list and the refusal text are
   compared against the shipped table, so a change to the default path is visible here rather than
   noticed by a caller. A missing *requirement* is not the same thing as an ordinary engine call,
   though, and the snapshot says so: a history frame is an OHLCV frame, so a default ``history()``
   is planned with the four price columns and ``marketlens-close`` -- which publishes Close and
   Volume and no Open/High/Low -- is passed over without a request.
4. :meth:`Ticker.info` -- the narrow request it now makes, and the fact that it still degrades
   through ``_degrade`` when the narrow fetch fails.

Everything here is offline. Sources are reached only through an injected ``fetch`` or a stubbed
Ticker; no test in this file may make a network request. The one full-OHLCV engine call in the file
also injects a recorded corporate-actions client, because the default ``auto_adjust=True``
(2026-09-30) resolves that feed whenever either flag asks for it (``actions or auto_adjust`` in
``fetch_history``), and an uninjected engine builds a REAL client for it.
"""

import json
from pathlib import Path

import pandas as pd
import pytest

import bfinance.sources as sources
from bfinance.market.ohlcv import (
    PRICE_SOURCE_ATTEMPTS_ATTR,
    PRICE_SOURCE_ATTR,
    OHLCVEngine,
    PriceSourceRouter,
    RealOHLCUnavailableError,
)
from bfinance.nse import corporate_actions as ca
from bfinance.sources.cascade import plan, resolve
from bfinance.sources.errors import SourceBasisError
from bfinance.sources.health import HealthLedger
from bfinance.sources.model import (
    ATTEMPT_OUTCOMES,
    CALLED_OUTCOMES,
    SERVED,
    SKIPPED_BASIS,
    SKIPPED_COLUMNS,
)
from bfinance.sources.registry import DEFAULT, Registry, SourceSpec
from bfinance.utils.exceptions import BFinanceError

BHAVCOPY = "nse-bhavcopy"
MARKETLENS = "marketlens-close"
TRENDLYNE = "trendlyne-ohlc"

#: Four sessions, so the window arithmetic has more than one bar and the trailing averages in
#: ``build_info_dict`` have something to average.
_DATES = ["2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24"]


# ============================================================ stubs


def _bars(close=100.0, volume=1000, columns=None):
    """A per-symbol price frame, ``columns`` defaulting to the full OHLCV five."""
    index = pd.DatetimeIndex(_DATES, name="Date")
    index = index.tz_localize("Asia/Kolkata")
    values = {
        "Open": [close] * len(index),
        "High": [close * 1.01] * len(index),
        "Low": [close * 0.99] * len(index),
        "Close": [close] * len(index),
        "Volume": [volume] * len(index),
    }
    return pd.DataFrame(
        {c: values[c] for c in (columns or list(values))}, index=index
    )


def _market_frame(rows):
    """Whole-market bhavcopy-shaped frame, exactly as ``nse.bhavcopy`` returns it.

    Used only to drive the DIRECT path, because that is the only path that reads the archive.
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
    """A stand-in for every price source, recording which ones were actually ASKED.

    ``calls`` is the whole point. "Skipped" and "not called" are different claims, and only the
    second one means a source with an expensive request never spent it -- which for the bhavcopy is
    the difference between 2.5s and 530s.
    """

    def __init__(self, script=None):
        self.script = dict(script or {})
        self.calls = []

    def __call__(self, spec, request):
        self.calls.append(spec.name)
        if spec.name in self.script:
            outcome = self.script[spec.name]
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome(spec, request) if callable(outcome) else outcome
        return _bars()


#: ITC's genuine per-symbol NSE feed, recorded from the corporate-actions client. Its last recorded
#: ex-date is 2026-05-27 and its share-count events are 2010 and 2016, so the dividend AND
#: split factors across the 2026-09-21..24 bars below are exactly 1.0: the frame stays as-traded and
#: no price under test moves because of this stub.
_ACTIONS_FIXTURE = (
    Path(__file__).parent / "fixtures" / "corporate_actions" / "nse_itc_2025_demerger.json"
)


class _RecordedActionsClient:
    """The recorded NSE feed through the real parser. No network.

    The shared :class:`tests.conftest.StubCorporateActionsClient` is not usable here, and this is
    the one place in this file where an actions client is consulted at all. It answers with an
    EMPTY frame -- the honest shape of "this feed has nothing" -- and an empty answer means "the
    feed does not index this instrument", which makes the engine resolve the Screener profile
    fallback. The engine is built over ``object()`` here, there is no profile to fall back to, and
    so the resolution ends in ``ACTIONS_SOURCE_NONE`` -- which the default ``auto_adjust=True``
    (2026-09-30) refuses with :class:`AdjustmentNotAvailableError`. Pinning ``auto_adjust=False``
    instead would put "Adj Close" back into the column list that
    :class:`TestRequiresNoneChangesNothing` asserts exactly, so the only faithful substitute for the
    live feed is a REAL recorded one.
    """

    def __init__(self, payload=None):
        self.payload = _load_actions() if payload is None else payload
        self.calls = []

    async def actions_async(self, symbol, start=None, end=None, index="equities"):
        self.calls.append(symbol)
        events = ca.parse_corporate_actions(self.payload, symbol=symbol, url="fixture")
        return ca.actions_frame(events, symbol=symbol, url="fixture")


def _load_actions():
    with open(_ACTIONS_FIXTURE, encoding="utf-8") as handle:
        return json.load(handle)


def _engine(fetch, registry=None):
    return OHLCVEngine(
        object(),
        corporate_actions_client=_RecordedActionsClient(),
        price_sources=PriceSourceRouter(
            registry=registry, fetch=fetch, health=HealthLedger()
        ),
    )


def _outcomes(attrs):
    return {a["source"]: a["outcome"] for a in attrs[PRICE_SOURCE_ATTEMPTS_ATTR]}


# ================================================= 1. the declared column sets


class TestProvides:
    def test_every_price_row_declares_what_its_reader_actually_builds(self):
        """Each value read off the reader, not off the note that describes the reader.

        Three of the five are checked against the module that builds the frame, and the assertion is
        that they are the SAME TUPLE rather than merely equal in content: a row that quotes a
        column set the reader stopped producing is exactly the claim this table exists to prevent.
        ``screener-chart`` is the interesting one -- its columns are ``datasets[].metric``, so a
        price chart's column is called ``price`` and there is no OHLCV name to declare at all.
        """
        from bfinance.nse.bhavcopy import OHLCV_COLUMNS
        from bfinance.nse.marketlens import price_history as marketlens_entry  # noqa: F401
        from bfinance.sources import price_provider

        assert DEFAULT.source(TRENDLYNE).provides == price_provider.COLUMNS
        assert DEFAULT.source(BHAVCOPY).provides == OHLCV_COLUMNS
        assert DEFAULT.source(MARKETLENS).provides == ("Close", "Volume")
        # The second vendor's leaf reader, read not imported (the isolation rules forbid the
        # import; reading the source file is how the note's claim gets checked).
        assert DEFAULT.source("vendor-b-adjustments").provides == (
            "Open", "High", "Low", "Close", "Volume",
        )
        # And the one row that publishes no nameable price column, which is why an empty
        # `provides` must mean "not filtered" rather than "filtered out of everything".
        assert DEFAULT.source("screener-chart").provides == ()

    def test_the_close_only_row_is_the_one_that_is_short(self):
        """The distinction the whole mechanism rests on, stated on its own.

        If every row published the OHLCV five, ``provides`` would be a constant and a column-aware
        cascade would be a no-op wearing a keyword.
        """
        published = {s.name: s.provides for s in DEFAULT.specs_for("price_history")}
        assert published[MARKETLENS] == ("Close", "Volume")
        assert published[BHAVCOPY] == ("Open", "High", "Low", "Close", "Volume")
        assert DEFAULT.source(MARKETLENS).missing_columns(("Close",)) == ()
        assert DEFAULT.source(MARKETLENS).missing_columns(("Open", "Close")) == ("Open",)
        assert DEFAULT.source(BHAVCOPY).missing_columns(("Open", "High", "Low", "Close")) == ()

    def test_an_empty_provides_means_unfiltered_rather_than_unusable(self):
        """The conservative reading, pinned on a row.

        A source that publishes no column this layer can name is not skipped for every column: that
        would turn a working failover into a refusal on the strength of a name this layer invented.
        """
        unnamed = SourceSpec(
            name="unnamed", capabilities=("price_history",), bases=(sources.Basis.AS_TRADED,),
            note="Publishes no nameable price column. Not filtered on columns.",
        )
        assert unnamed.missing_columns(("Open", "High", "Low", "Close")) == ()
        assert unnamed.missing_columns(("Close",)) == ()

    def test_the_new_outcome_is_a_non_call_in_the_closed_vocabulary(self):
        """``Attempt.called`` is derived from the tables, so a new outcome must join them.

        Asserted here as well as in ``tests/test_sources_cascade.py`` because the two places must
        not disagree: if this ever passed while ``ATTEMPT_OUTCOMES`` had grown, ``called`` would be
        reporting "this source was asked" for a source that was skipped.
        """
        assert SKIPPED_COLUMNS in ATTEMPT_OUTCOMES
        assert SKIPPED_COLUMNS not in CALLED_OUTCOMES
        assert sources.SKIPPED_COLUMNS == SKIPPED_COLUMNS


# ============================================ 2. the skip, and what it costs


class TestColumnSkip:
    def test_an_ohlc_request_skips_the_close_only_source_without_calling_it(self):
        """(a) An OHLC request still reaches a real OHLC source and gets real OHLC.

        And the close-only row is passed over: this is the case that was broken, because a
        close-only frame is a non-empty answer and would have been accepted.
        """
        fetch = _Fetch()

        result = resolve(
            "price_history",
            request={"symbol": "TCS"},
            basis=sources.Basis.AS_TRADED,
            registry=DEFAULT,
            health=HealthLedger(),
            fetch=fetch,
            requires=("Open", "High", "Low", "Close"),
        )

        assert result.source == BHAVCOPY, (
            "the bhavcopy is the row that publishes bars, so an OHLC request is routed to it; a "
            "winner of marketlens-close here would mean the column filter did nothing"
        )
        assert fetch.calls == [BHAVCOPY]
        assert list(result.value.columns) == ["Open", "High", "Low", "Close", "Volume"]

        # (c) The skip is recorded, with its own outcome and a reason naming what is missing.
        #
        # Read off ``plan()``, not off ``resolved.attempts``: resolve() stops recording the moment a
        # provider answers, which is right for "what happened" and wrong for "what was passed over"
        # -- and marketlens is ordered AFTER the bhavcopy here, so it is passed over behind the
        # winner. That is exactly what plan() exists to answer, without calling anything.
        planned = plan(
            "price_history", basis=sources.Basis.AS_TRADED, registry=DEFAULT,
            health=HealthLedger(), requires=("Open", "High", "Low", "Close"),
        )
        skipped = {a.provider: a for a in planned.attempts}
        assert skipped[MARKETLENS].outcome == SKIPPED_COLUMNS
        assert skipped[MARKETLENS].called is False
        assert "Open" in skipped[MARKETLENS].reason
        assert "Volume" in skipped[MARKETLENS].reason
        # The basis skips are still distinguished from the column skip: two different reasons, and
        # "why is my data from that vendor" has to be answerable for both.
        assert skipped[TRENDLYNE].outcome == SKIPPED_BASIS
        assert skipped["screener-chart"].outcome == SKIPPED_BASIS
        assert result.degraded is False, (
            "a source passed over because it cannot serve the request is the system working, not a "
            "fallback the caller was denied"
        )

    def test_a_close_only_request_reaches_marketlens_and_never_asks_the_bhavcopy(self):
        """(b) The narrow request. One call, the cheap one, and the expensive one never made.

        ``sources=`` is the caller's own try-order, which is the documented way to choose a vendor
        for one call without mutating the registry. Without it the shipped order would route a
        Close-only request to the bhavcopy -- which can serve it, correctly, at 70x the price --
        because column eligibility is a filter, not a ranking.
        """
        fetch = _Fetch()

        result = resolve(
            "price_history",
            request={"symbol": "TCS"},
            basis=sources.Basis.AS_TRADED,
            registry=DEFAULT,
            health=HealthLedger(),
            fetch=fetch,
            only=(MARKETLENS, BHAVCOPY),
            requires=("Close", "Volume"),
        )

        assert result.source == MARKETLENS
        assert len(fetch.calls) == 1, (
            f"exactly one request, and it must be the cheap one; got {fetch.calls}"
        )
        assert BHAVCOPY not in fetch.calls, (
            "the bhavcopy downloads one archive file per trading day; a narrow request must not "
            "reach it when a per-symbol source can answer"
        )

    def test_the_bhavcopy_is_still_reached_when_the_close_only_source_fails(self):
        """The failsafe is the reason to name two sources, so it is asserted rather than assumed.

        Both are as_traded, so the fallback cannot splice two bases -- the property that makes
        ``sources=[a, b]`` a genuine failsafe rather than a coin flip.
        """
        fetch = _Fetch({MARKETLENS: ConnectionError("marketlens timed out")})

        result = resolve(
            "price_history",
            request={"symbol": "TCS"},
            basis=sources.Basis.AS_TRADED,
            registry=DEFAULT,
            health=HealthLedger(),
            fetch=fetch,
            only=(MARKETLENS, BHAVCOPY),
            requires=("Close", "Volume"),
        )

        assert fetch.calls == [MARKETLENS, BHAVCOPY]
        assert result.source == BHAVCOPY
        assert result.degraded is True

    def test_a_source_short_of_one_column_is_skipped_and_the_reason_names_it(self):
        """The mechanism on a stubbed row, so the claim is not only about the shipped table.

        Also pins that the check is per COLUMN rather than per shape: a source with four of the five
        columns is still skipped, because a frame with four of the five is still a frame missing
        one the caller asked for.
        """
        almost = SourceSpec(
            name="almost", capabilities=("price_history",),
            bases=(sources.Basis.AS_TRADED,),
            provides=("Open", "High", "Low", "Close"),
            note="Publishes every price column but no volume.",
        )
        full = SourceSpec(
            name="full", capabilities=("price_history",), bases=(sources.Basis.AS_TRADED,),
            provides=("Open", "High", "Low", "Close", "Volume"),
            note="Publishes the full OHLCV five.",
        )
        registry = Registry()
        for spec in (almost, full):
            registry.register(spec)
        fetch = _Fetch({})

        result = resolve(
            "price_history", basis=sources.Basis.AS_TRADED, registry=registry,
            health=HealthLedger(), fetch=fetch, requires=("Close", "Volume"),
        )

        assert fetch.calls == ["full"]
        skipped = {a.provider: a for a in result.attempts if a.outcome == SKIPPED_COLUMNS}
        assert list(skipped) == ["almost"]
        assert skipped["almost"].called is False
        assert "Volume" in skipped["almost"].reason
        assert "Open, High, Low, Close" in skipped["almost"].reason, (
            "the reason has to say what the source DOES publish as well as what it lacks, or a "
            "reader cannot tell a column gap from a shape change"
        )

    def test_a_basis_mismatch_is_still_reported_as_a_basis_mismatch(self):
        """The two rules are ordered, and the order is observable.

        ``trendlyne-ohlc`` publishes the full OHLCV five, so on columns it is eligible -- and it is
        still never called for an as_traded request. A column filter that could override the basis
        rule would make this row reachable, and the measured cost of that is a series carrying a
        corporate-action-sized move that never happened.
        """
        fetch = _Fetch()

        result = resolve(
            "price_history", basis=sources.Basis.AS_TRADED, registry=DEFAULT,
            health=HealthLedger(), fetch=fetch, requires=("Open", "High", "Low", "Close"),
        )

        assert TRENDLYNE not in fetch.calls
        attempt = next(a for a in result.attempts if a.provider == TRENDLYNE)
        assert attempt.outcome == SKIPPED_BASIS, (
            "the basis rule is checked first, so the reason given is the one that actually applies"
        )

    def test_every_eligible_source_short_of_a_column_is_a_refusal_not_an_unavailability(self):
        """Nothing went wrong and retrying cannot help, so it must not read as an outage.

        The existing basis refusal is the model, and it is deliberately a ``ValueError``: a caller
        told to retry a request that can never succeed is a caller who will retry.
        """
        registry = Registry()
        registry.register(
            SourceSpec(
                name="close-only", capabilities=("price_history",),
                bases=(sources.Basis.AS_TRADED,), provides=("Close",),
                note="Publishes Close alone.",
            )
        )
        fetch = _Fetch({})

        with pytest.raises(SourceBasisError) as exc:
            resolve(
                "price_history", basis=sources.Basis.AS_TRADED, registry=registry,
                health=HealthLedger(), fetch=fetch, requires=("Open", "High"),
            )

        message = str(exc.value)
        assert fetch.calls == [], "a refused request must cost no requests"
        assert "Open" in message and "High" in message, (
            "the refusal has to name the columns, or it is the basis sentence with a new hat on"
        )
        assert "close-only" in message


# ================================== 3. requires=None is byte-identical to before


class TestRequiresNoneChangesNothing:
    def test_the_plan_is_identical_with_and_without_the_keyword(self):
        """(d) Not "still routes correctly" -- the SAME attempt ledger, source for source.

        Compared field by field against ``plan()`` called with no ``requires`` at all, so a column
        filter that leaked into the default path could not hide behind a passing outcome assertion.
        """
        before = plan("price_history", basis=sources.Basis.AS_TRADED, registry=DEFAULT,
                      health=HealthLedger())
        after = plan("price_history", basis=sources.Basis.AS_TRADED, registry=DEFAULT,
                     health=HealthLedger(), requires=None)

        assert [s.name for s in after.ordered] == [s.name for s in before.ordered]
        assert [(a.provider, a.outcome, a.reason, a.basis) for a in after.attempts] == [
            (a.provider, a.outcome, a.reason, a.basis) for a in before.attempts
        ]
        assert dict(after.excluded) == dict(before.excluded)
        assert after.basis is before.basis
        assert after.basis_explicit == before.basis_explicit

    def test_an_empty_requires_is_no_requirement(self):
        """``()`` is what a caller who computed the column list and got nothing passes.

        Treating it as a requirement would skip every price row for an empty list of columns, which
        is a refusal nobody asked for. The winner is therefore whichever row the registry puts
        first among the as-traded rows -- ``marketlens-close`` since the 2026-10-13 reorder --
        which is what makes the second assertion the load-bearing one: an empty tuple filters
        nothing and this row is reached on its own merits rather than being the row that happened
        to be left.
        """
        fetch = _Fetch()
        result = resolve(
            "price_history", basis=sources.Basis.AS_TRADED, registry=DEFAULT,
            health=HealthLedger(), fetch=fetch, requires=(),
        )
        assert result.source == MARKETLENS
        assert not any(a.outcome == SKIPPED_COLUMNS for a in result.attempts)

    def test_a_default_history_call_is_untouched_by_the_keyword(self):
        """The engine's own control, at the layer where the keyword lives.

        ``requires=None`` must take the identical path: same winner, same bars, same attrs, whether
        the keyword is passed or omitted. The cascade's own skips are unchanged by it either -- what
        is in the snapshot below is the ENGINE's requirement, which is not the keyword's: an
        ordinary history is an OHLCV history, so the four price columns are what the cascade is
        planned with, and ``marketlens-close`` is passed over for publishing no Open/High/Low while
        the other three are passed over for being on a different basis.
        """
        with_none = awaitable(_engine(_Fetch({})), requires=None)
        without = awaitable(_engine(_Fetch({})))

        # The injected client replayed ITC's recorded NSE feed, which is what the default
        # auto_adjust=True (2026-09-30) needs a real series for: with the shared empty stub the
        # resolution would end in 'none' and this call would be REFUSED rather than answered, and
        # with no client injected at all the engine built a real one and reached www.nseindia.com.
        for frame in (with_none, without):
            assert frame.attrs["bfinance_actions_source"] == "nse_corporate_actions", (
                "the recorded feed was not what answered, so this call went somewhere else"
            )

        assert list(with_none.columns) == list(without.columns) == [
            "Open", "High", "Low", "Close", "Volume",
        ]
        assert list(with_none["Close"]) == list(without["Close"])
        assert _outcomes(with_none.attrs) == _outcomes(without.attrs)
        # The whole ledger, merges and all: the bhavcopy served, marketlens-close was passed over
        # for publishing no Open/High/Low, and the three rows that publish a different basis were
        # passed over for that.
        assert _outcomes(with_none.attrs) == {
            TRENDLYNE: SKIPPED_BASIS,
            MARKETLENS: SKIPPED_COLUMNS,
            BHAVCOPY: SERVED,
            "screener-chart": SKIPPED_BASIS,
            "vendor-b-adjustments": SKIPPED_BASIS,
        }
        assert with_none.attrs[PRICE_SOURCE_ATTR] == without.attrs[PRICE_SOURCE_ATTR] == BHAVCOPY
        assert with_none.attrs.get("bfinance_price_source_columns") is None, (
            "a full OHLCV frame is not a column-narrow one, so it must not carry the narrow key and "
            "let a consumer read it as one"
        )


def awaitable(engine, requires=None, sources=None):
    """Drive ``fetch_history`` synchronously so the test needs no async plumbing.

    ``requires`` and ``sources`` are omitted rather than passed as ``None`` when unset, so the
    "identical to calling it without the keyword" control is literally a call without it.
    """
    import asyncio

    kwargs = {"symbol": "ITC", "period": "5d", "actions": False}
    if requires is not None:
        kwargs["requires"] = requires
    if sources is not None:
        kwargs["sources"] = sources
    return asyncio.run(engine.fetch_history(**kwargs))


def awaitable_on(engine, registry, requires, sources):
    """The same call against a registry the test built itself."""
    import asyncio

    return asyncio.run(
        engine.fetch_history(
            symbol="ITC", period="5d", actions=False,
            requires=requires, sources=sources,
        )
    )


# ============================ 4. the engine, the narrow frame, and Ticker.info


class TestNarrowEngineCall:
    def test_a_narrow_call_does_not_engage_the_cascade_on_its_own(self):
        """``requires`` says which columns, not which vendor -- and the two are orthogonal.

        Asserted on the *architecture*, not just on the output: the direct archive reader still
        answers a narrow call, which is what keeps the ~130 offline call sites that stub
        ``ohlcv_mod.bhavcopy_range`` stubbing the thing that answers. Routing a narrow request
        through the cascade would import each row's module at call time and turn those stubs into
        live NSE calls -- the exact failure :class:`PriceSourceRouter` exists to prevent.
        """
        asked = []

        def _range(start, end, **kwargs):
            asked.append((start, end))
            return _market_frame(
                {"ITC": [(date, 100.0, 101.0, 99.0, 100.5, 1000) for date in _DATES]}
            )

        import bfinance.market.ohlcv as ohlcv_mod

        original = ohlcv_mod.bhavcopy_range
        ohlcv_mod.bhavcopy_range = _range
        try:
            frame = awaitable(OHLCVEngine(object()), requires=("Close", "Volume"))
        finally:
            ohlcv_mod.bhavcopy_range = original

        assert asked, "the direct archive reader was bypassed, so something else answered"
        assert list(frame.columns) == ["Close", "Volume"]
        assert frame.attrs[PRICE_SOURCE_ATTR] is None, (
            "no registry row was consulted, so naming a vendor here would be a claim about a "
            "routing decision that was never made"
        )
        assert frame.attrs["bfinance_price_source_columns"] == ("Close", "Volume")
        # And nothing claims an adjustment that did not happen: a narrow frame carries no Adj Close
        # and no adjustment flag, because a key reading False on a frame whose prices were never
        # restated is indistinguishable from one that was restated and failed part-way.
        assert "Adj Close" not in frame.attrs
        assert frame.attrs["price_basis"] == "as_traded"

    def test_a_narrow_call_reaches_the_close_only_vendor_when_the_caller_names_it(self):
        """The cheap route, and it takes both halves: ``sources=`` for the vendor and ``requires``
        for the columns.

        The stub answers marketlens; if the engine reached the bhavcopy first the recorded call list
        would say so, and the assertion is on that list rather than on a timing nobody can assert in
        a unit test.
        """
        fetch = _Fetch({})
        frame = awaitable(
            _engine(fetch),
            requires=("Close", "Volume"),
            sources=(MARKETLENS, BHAVCOPY),
        )

        assert frame.attrs[PRICE_SOURCE_ATTR] == MARKETLENS
        assert len(fetch.calls) == 1, (
            f"exactly one request, and it must be the cheap one; got {fetch.calls}"
        )
        assert BHAVCOPY not in fetch.calls
        assert list(frame.columns) == ["Close", "Volume"]

    def test_a_narrow_call_never_invents_a_column(self):
        """Defence in depth, against a row that over-declares rather than under-declares.

        ``provides`` is the routing table, so the cascade trusts it and never asks a source it
        believes cannot serve the request. That trust has a second wall behind it: a row whose
        declared columns and whose actual frame disagree -- a vendor schema change, a stale row --
        must be refused by the engine rather than handed on as if it had the columns. A narrow
        request is where that wall matters most, because the whole point of it is to skip columns.
        """
        lying = SourceSpec(
            name="liar", capabilities=("price_history",), bases=(sources.Basis.AS_TRADED,),
            provides=("Open", "High", "Low", "Close", "Volume"),
            note="Declares the full OHLCV five and returns Close and Volume. It happens.",
        )
        registry = Registry()
        registry.register(lying)
        fetch = _Fetch({"liar": _bars(close=269.0, columns=("Close", "Volume"))})

        with pytest.raises(RealOHLCUnavailableError) as exc:
            awaitable_on(
                _engine(fetch, registry=registry),
                registry,
                requires=("Open", "High", "Low", "Close"),
                sources=("liar",),
            )

        assert fetch.calls == ["liar"], "the cascade trusted the row and asked it; the engine refused"
        assert "Open" in str(exc.value), (
            "the refusal has to name the column it will not fabricate"
        )


class TestInfoMakesANarrowRequest:
    def test_info_asks_for_two_columns_and_names_its_try_order(self):
        """What ``info`` now requests, asserted on the constants rather than on a timer.

        And the registry's own order is untouched: ``info`` picks no vendor, so the operator's
        try-order still decides, and nothing here rewrites the table every other caller reads.
        """
        import bfinance.ticker as ticker_mod

        assert ticker_mod.INFO_HISTORY_COLUMNS == ("Close", "Volume")
        assert ticker_mod.INFO_PRICE_SOURCES == (MARKETLENS, BHAVCOPY)
        assert [s.name for s in DEFAULT.specs_for("price_history")] == [
            TRENDLYNE, MARKETLENS, BHAVCOPY, "screener-chart", "vendor-b-adjustments",
        ]
        # Both rows in the documented cheap order really can serve the narrow request, which is
        # what makes the pair a failsafe rather than a guess.
        for name in ticker_mod.INFO_PRICE_SOURCES:
            assert DEFAULT.source(name).missing_columns(ticker_mod.INFO_HISTORY_COLUMNS) == ()

    def test_info_builds_its_close_inputs_from_the_narrow_frame(self):
        """End to end on the builder, with ``build_info_dict``'s own readers doing the reading.

        The frame handed in is close-only -- no Open/High/Low at all -- which is the shape this
        change made possible and the one the old full-OHLC request could never produce.
        """
        from bfinance.market.quotes import QuoteEngine, moving_average, previous_close_from_history
        from bfinance.models.company import CompanyProfile

        profile = CompanyProfile(symbol="TCS", company_id=1, name="Tata Consultancy Services")
        index = pd.bdate_range("2025-01-01", periods=250)
        close_only = pd.DataFrame({"Close": range(100, 350), "Volume": [10] * 250}, index=index)

        info = QuoteEngine.build_info_dict(profile, latest_price=349.0, history=close_only)

        assert info["previousClose"] == 348.0
        assert info["fiftyDayAverage"] == 324.5, "the 50-day mean of the closes handed in"
        assert info["twoHundredDayAverage"] == 249.5, "the 200-day mean of the closes handed in"
        assert info["volume"] == 10
        # The builder reads Close and Volume and nothing else, which is the claim that makes a
        # two-column request sufficient. Proved by a frame that has nothing else at all.
        assert moving_average(close_only, 50) == 324.5
        assert previous_close_from_history(close_only) == 348.0

    def test_info_still_degrades_when_the_narrow_fetch_fails(self):
        """The failure path is unchanged: ``_degrade`` records it, ``raise_errors`` still wins.

        A narrow request that failed and a full one that failed are the same event, and the caller
        must not be able to tell which attempt was made by whether they were told.
        """
        import bfinance.ticker as ticker_mod

        degraded = []

        class _Ticker(ticker_mod.Ticker):
            def __init__(self):
                self._info = None
                self._profile_degraded = False
                self._profile = None
                self._history_for_metadata = None
                self.raise_errors = False

            def _ensure_profile(self):
                from bfinance.models.company import CompanyProfile

                return CompanyProfile(symbol="TCS", company_id=1, name="TCS")

            def _degrade(self, what, cause):
                degraded.append((what, cause))

            def history(self, *args, **kwargs):
                raise RealOHLCUnavailableError("every source declined")

        info = _Ticker().info

        assert degraded, "a failed history fetch must be recorded through _degrade"
        assert "previous close" in degraded[0][0]
        assert info["previousClose"] is None, (
            "an honest None, never a zero: a fabricated previous close is a number somebody would "
            "quote"
        )
        assert info["fiftyDayAverage"] is None
        assert info["currentPrice"] == 0.0 or info["currentPrice"] is not None

    def test_info_propagates_the_failure_when_raise_errors_is_set(self):
        """``raise_errors=True`` must still raise, narrow request or not."""
        import bfinance.ticker as ticker_mod

        class _Ticker(ticker_mod.Ticker):
            def __init__(self):
                self._info = None
                self._profile_degraded = False
                self.raise_errors = True

            def _ensure_profile(self):
                from bfinance.models.company import CompanyProfile

                return CompanyProfile(symbol="TCS", company_id=1, name="TCS")

            def history(self, *args, **kwargs):
                raise RealOHLCUnavailableError("every source declined")

        with pytest.raises(BFinanceError):
            _Ticker().info


# ================================================ the recorded measurements


def test_the_registry_notes_the_cost_the_column_set_exists_to_avoid():
    """The reason for the mechanism, in the table, where a reader of a report will find it.

    A skip reason is only trustworthy if the thing it avoids is real, and the two rows involved
    disagree about cost by two orders of magnitude.
    """
    assert "no Open/High/Low" in DEFAULT.source(MARKETLENS).note
    assert "530s" in DEFAULT.source(MARKETLENS).note
    assert "PER TRADING DAY" in DEFAULT.source(BHAVCOPY).note
    assert "184s" in DEFAULT.source(BHAVCOPY).note
    assert "5 columns" not in DEFAULT.source(MARKETLENS).describe()
    assert "columns: Close, Volume" in DEFAULT.source(MARKETLENS).describe()