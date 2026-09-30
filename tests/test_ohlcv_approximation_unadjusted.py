"""
``auto_adjust=True`` on the Screener.in fiscal-year-end approximation: ANSWER, do not refuse.

The regression
--------------
NSE's corporate-actions feed has no index for ETFs. ``resolve_actions_async`` answers an empty
list for a symbol it does not cover, which is a real answer only about the WINDOW, so the
legacy approximation is consulted instead -- and the approximation is dividends dated at
**fiscal year end** with **no share-count data at all**
(:mod:`bfinance.market.corporate`).

Then commit e6b4913 made ``auto_adjust=True`` the default and gated it on the PROVENANCE of
the actions, with the approximation as one of the four refusing cases. Measured on the same
default call that returns 21 real bars for each of NTPC, RELIANCE, WIPRO, CIPLA and INDGN:

    MAFANG       -> AdjustmentNotAvailableError
    HNGSNGBEES   -> AdjustmentNotAvailableError
    NIFTYBEES    -> AdjustmentNotAvailableError

An operator holding a mixed equity + ETF portfolio lost every ETF. Refusing is the wrong
answer twice over: it returns NOTHING where the caller had real prices 21 bars deep, and
yfinance returns a frame for the same three symbols, so it is also a parity divergence on the
one axis this package exists to match. What is missing on that path is the ADJUSTMENT, not
the price data.

The decision, and the hazard it has to survive
----------------------------------------------
Return the bars as traded, and say so loudly. ``O/H/L/C`` are the archive's prints byte for
byte, ``Adj Close`` is still dropped so the column set is yfinance's either way, and the frame
carries ``attrs["bfinance_adjustment_applied"] is False`` plus a reason naming the
approximation, the absence of share-count data and the ``auto_adjust=False`` escape.

The hazard is that the price columns are scaled BEFORE that decision can be taken, somewhere
two lines away, by a ratio this very feed implies. So the fixture here is built to make the
implied ratio **not 1.0**: the profile's fiscal year end (2024-09-30) is a bar inside the
window, so the approximation's dividend really does merge onto a bar and really does imply
``1 - 5 / 251`` on the bar before it. A fixture whose ratio was 1.0 would pass a scaled
implementation, which is why ``test_the_implied_ratio_is_not_one_and_is_still_not_applied``
asserts the ratio is 0.98 before the main test asserts the prices did not move by it.

What is KEPT refusing, each with its own test below: ``ohlc="synthetic"`` (derived O/H/L, so
scaling would invent the rest of the bar), an empty frame, and no ex-dated feed at all. And
what is kept WORKING: a real ex-dated feed still applies its factor on the default path, and
``auto_adjust=False`` still returns ``Adj Close`` for these same symbols.

Offline by construction. The bhavcopy reader is stubbed, and the actions stub either replays a
**recorded** NSE payload through the real parser or returns no rows at all -- which is exactly
what NSE does for an ETF.
"""

import asyncio
import json
from pathlib import Path

import httpx
import pandas as pd
import pytest

import bfinance.market.ohlcv as ohlcv_mod
from bfinance.market.corporate import ACTIONS_SOURCE_NSE, ACTIONS_SOURCE_SCREENER
from bfinance.market.ohlcv import (
    PRICE_BASIS_AS_TRADED,
    AdjustmentNotAvailableError,
    OHLCVEngine,
)
from bfinance.models.company import CompanyProfile
from bfinance.models.statements import FinancialStatement
from bfinance.nse import bhavcopy as bc
from bfinance.nse import corporate_actions as ca

FIXTURES = Path(__file__).parent / "fixtures" / "corporate_actions"
BHAVCOPY_FIXTURES = Path(__file__).parent / "fixtures" / "bhavcopy"

#: Whole NSE CM archives, recorded, replayed below through the REAL reader and the REAL parser.
#: Provenance in ``tests/fixtures/bhavcopy/META.json``. These are the frames the as-traded claim
#: is measured against, rather than literals written by this file.
ARCHIVES = {
    "20260924": (BHAVCOPY_FIXTURES / "udiff_cm_20260924_full.csv.zip").read_bytes(),
    "20260925": (BHAVCOPY_FIXTURES / "udiff_cm_20260925_full.csv.zip").read_bytes(),
}

#: RELIANCE's real per-symbol NSE feed, recorded: the 2024-08-19 Rs 10 dividend and the
#: 2024-10-28 1:1 bonus whose ratio is 2.0. The negative control for "a real feed still scales".
RELIANCE_PAYLOAD = json.loads(
    (FIXTURES / "nse_reliance_2020_2026.json").read_text(encoding="utf-8")
)

#: A screener profile whose ONLY signal is a dividend the approximation can date: 50% of
#: Rs 10 EPS for the period ending Sep 2024 -> Rs 5.00 dated at 2024-09-30. A profile with no
#: "EPS in Rs" row would give the approximation nothing to work with, and then the implied ratio
#: would be exactly 1.0 and the whole test would be satisfied by a scaled implementation.
FY_END = "2024-09-30"
APPROXIMATED_DIVIDEND = 5.00

#: The ETF's real-shaped bars across the approximated ex-date. 2024-09-30 is a Monday, so the
#: merge actually lands on a bar and the implied factor is real. These are the numbers the
#: returned frame must reproduce exactly.
ETF_BARS = {
    "MAFANG": [
        ("2024-09-27", 250.00, 252.50, 249.00, 251.00, 1_000_000),
        ("2024-09-30", 251.00, 253.00, 250.00, 252.50, 1_100_000),
        ("2024-10-01", 252.50, 255.00, 252.00, 254.00, 1_200_000),
    ]
}
ETF_CLOSES = [251.00, 252.50, 254.00]
ETF_VOLUMES = [1_000_000, 1_100_000, 1_200_000]
ETF_WINDOW = {"start": "2024-09-27", "end": "2024-10-02"}

#: 1 - 5.00 / 251.00, measured off the approximation's own rupee amount and the bar before it.
#: This is the number a scaled implementation would multiply into the first bar, and the whole
#: point of the test is that it does not.
IMPLIED_FACTOR = 1.0 - APPROXIMATED_DIVIDEND / ETF_CLOSES[0]

#: RELIANCE's real prints either side of the 2024-10-28 1:1 bonus, verbatim from the archive.
BONUS_BARS = {
    "RELIANCE": [
        ("2024-10-24", 2620.00, 2660.00, 2610.00, 2640.90, 8_132_084),
        ("2024-10-25", 2640.90, 2668.00, 2622.00, 2655.70, 9_298_748),
        ("2024-10-28", 1337.00, 1353.00, 1322.10, 1334.35, 10_824_350),
        ("2024-10-29", 1328.10, 1343.20, 1320.30, 1340.00, 12_008_361),
    ]
}
BONUS_WINDOW = {"start": "2024-10-24", "end": "2024-10-30"}


# ------------------------------------------------------------------------- stubs (no network)

class _NoRowsActions:
    """NSE answering what it answers for an ETF: no rows at all, so the fallback is reached."""

    def __init__(self, error=None):
        self.error = error

    async def actions_async(self, symbol, start=None, end=None, index="equities"):
        if self.error is not None:
            raise self.error
        return pd.DataFrame(columns=["Dividends", "Stock Splits", "Action"])


class _RecordedActions:
    """Replays the recorded NSE payload through the real parser. No network, no clock."""

    def __init__(self, payload):
        self.payload = payload

    async def actions_async(self, symbol, start=None, end=None, index="equities"):
        events = ca.parse_corporate_actions(self.payload, symbol=symbol, url="fixture")
        return ca.actions_frame(events, symbol=symbol, url="fixture")


class _Screener:
    """Close-only screener stub. ``profile`` is what the approximation fallback is fed."""

    def __init__(self, chart_df=None, profile=None):
        self.chart_df = chart_df if chart_df is not None else pd.DataFrame()
        self.profile = profile
        self.chart_calls = []

    async def get_chart_timeseries(self, ticker, metric="price", days=1825):
        self.chart_calls.append(ticker)
        return self.chart_df

    async def get_company_profile(self, ticker, force_refresh=False):
        if self.profile is None:
            raise ValueError("no profile")
        return self.profile


def _approximation_profile(symbol="MAFANG"):
    """A Screener.in P&L the approximation can date, and nothing else it could invent from."""
    return CompanyProfile(
        symbol=symbol,
        company_id=1,
        name="Motilal Oswal Financial Services",
        profit_loss=FinancialStatement(
            headers=["Sep 2024"],
            rows={"Dividend Payout %": [50.0], "EPS in Rs": [10.0]},
        ),
    )


def _market_frame(rows):
    """Whole-market bhavcopy-shaped frame: (Date, Symbol) MultiIndex, 5 OHLCV columns."""
    dates, symbols, values = [], [], []
    for symbol, bars in rows.items():
        for day, o, h, low, close, volume in bars:
            dates.append(day)
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


@pytest.fixture
def bars(monkeypatch):
    """Install a bhavcopy stub returning a whole-market frame of the given bars."""

    def _factory(rows):
        monkeypatch.setattr(
            ohlcv_mod, "bhavcopy_range", lambda start, end, **kw: _market_frame(rows)
        )
        return rows

    return _factory


def _etf_engine(actions=None, profile=None):
    return OHLCVEngine(
        _Screener(profile=profile if profile is not None else _approximation_profile()),
        corporate_actions_client=actions if actions is not None else _NoRowsActions(),
    )


# ============================================== the answer: the bars come back, unadjusted

async def test_auto_adjust_true_returns_the_unadjusted_frame_instead_of_raising(bars):
    """
    The regression, as an assertion: this call used to raise, and the frame it must return is
    the exchange's own print.

    Byte-identical, not approximately equal: the fix is "do not scale these columns", and a
    relative tolerance would let a factor of 0.9999 through while still claiming nothing was
    applied. The values are compared against the literals the stub returns AND against the
    unadjusted frame the same engine builds one keyword away, so the claim is anchored twice --
    to the numbers under test and to the answer bfinance itself would give unasked.
    """
    bars(ETF_BARS)
    engine = _etf_engine()

    plain = await engine.fetch_history(
        symbol="MAFANG", actions=True, auto_adjust=False, **ETF_WINDOW
    )
    auto = await engine.fetch_history(
        symbol="MAFANG", actions=True, auto_adjust=True, **ETF_WINDOW
    )

    # It returns. That is the whole regression, and it is asserted first so a later failure
    # cannot be mistaken for a raise that was merely moved.
    assert not auto.empty, "the ETF call must return bars, not an exception and not an empty frame"
    assert len(auto) == len(ETF_CLOSES) == 3

    # yfinance's column set, unchanged by the fact that no adjustment was applied: dropping
    # "Adj Close" is what the caller asked for, and it is the only reason the unadjusted answer
    # does not read as a different contract.
    assert "Adj Close" not in auto.columns
    assert list(auto.columns) == [
        "Open", "High", "Low", "Close", "Volume", "Dividends", "Stock Splits"
    ]

    # THE LOAD-BEARING ASSERTION. The as-traded prints, exactly.
    assert auto["Close"].tolist() == ETF_CLOSES, (
        "auto_adjust=True on the approximation must return the archive's own Close, not the "
        f"Close multiplied by the implied ratio {IMPLIED_FACTOR!r}"
    )
    assert auto["Open"].tolist() == [250.00, 251.00, 252.50]
    assert auto["High"].tolist() == [252.50, 253.00, 255.00]
    assert auto["Low"].tolist() == [249.00, 250.00, 252.00]
    # Volume is a share count and is not restated on this path either: the caller asked what
    # traded, and the int64 the archive returned is the int64 they get.
    assert auto["Volume"].tolist() == ETF_VOLUMES
    assert auto["Volume"].dtype.kind == "i"

    # And the same comparison against bfinance's own unadjusted answer, so the claim is not only
    # "matches the fixture" but "matches what this package calls unadjusted".
    for column in ("Open", "High", "Low", "Close"):
        pd.testing.assert_series_equal(auto[column], plain[column], check_names=False)
    pd.testing.assert_series_equal(auto["Volume"], plain["Volume"], check_names=False)


async def test_the_implied_ratio_is_not_one_and_is_still_not_applied(bars):
    """
    The non-vacuity control, and it has to come first in the reader's mind.

    Without this, "the prices are unadjusted" is satisfied by a fixture whose implied factor
    happens to be 1.0 -- which is exactly what a profile with no EPS row produces, and exactly
    what a scaled implementation would then sail through. So the ratio is measured here, off the
    ``auto_adjust=False`` frame where the approximation is still allowed to publish its
    dividend series, and asserted to be 0.98 on the bar before the approximated ex-date. The
    main test then proves that number was not applied to anything.
    """
    bars(ETF_BARS)
    engine = _etf_engine()

    plain = await engine.fetch_history(
        symbol="MAFANG", actions=True, auto_adjust=False, **ETF_WINDOW
    )
    assert plain.attrs["bfinance_actions_source"] == ACTIONS_SOURCE_SCREENER
    assert "Adj Close" in plain.columns

    ratio = (plain["Adj Close"] / plain["Close"]).tolist()
    assert ratio == pytest.approx([IMPLIED_FACTOR, 1.0, 1.0], rel=1e-12), (
        f"the approximation stopped implying a factor, so this file no longer tests "
        f"anything: {ratio}"
    )
    assert IMPLIED_FACTOR == pytest.approx(0.980079681, rel=1e-9), "measured, not assumed"
    assert plain["Dividends"].sum() == pytest.approx(APPROXIMATED_DIVIDEND)
    # The date it landed on is the fiscal year end, not an ex-date: that is the defect the whole
    # case is about, and it is worth pinning while the frame is in the unlabelled state.
    assert str(plain["Dividends"].idxmax().date()) == FY_END

    # ... and the same ratio, on the auto_adjust=True call, is nowhere near the prices.
    auto = await engine.fetch_history(
        symbol="MAFANG", actions=True, auto_adjust=True, **ETF_WINDOW
    )
    scaled = (ETF_CLOSES[0] * IMPLIED_FACTOR, *ETF_CLOSES[1:])
    assert auto["Close"].tolist() != pytest.approx(scaled, rel=1e-9), (
        "the implied ratio reached the price column; the frame claims not to have applied it"
    )
    assert auto["Close"].iloc[0] == pytest.approx(ETF_CLOSES[0])


# ======================================================== the disclosure, on the frame itself

async def test_the_frame_says_it_is_unadjusted_and_names_the_approximation(bars):
    """
    The disclosure is the deliverable, so it is asserted by what a consumer has to read.

    ``bfinance_adjustment_applied`` is the boolean a caller branches on before computing a
    return off the frame; the detail is the sentence that says why, and it has to name the
    approximation (otherwise "not adjusted" is unexplained), say that it carries no share-count
    data (otherwise the caller may assume an adjustment of 1.0 was measured rather than
    declined), and name the escape (otherwise the caller is told about a problem and given no
    keyword that changes it).
    """
    bars(ETF_BARS)
    frame = await _etf_engine().fetch_history(
        symbol="MAFANG", actions=True, auto_adjust=True, **ETF_WINDOW
    )

    assert frame.attrs["bfinance_adjustment_applied"] is False
    detail = frame.attrs["bfinance_adjustment_applied_detail"]
    assert "fiscal-year-end approximation" in detail, detail
    assert "NO share-count data at all" in detail, detail
    assert "auto_adjust=False" in detail, detail
    # And the feed's own provenance is still on the frame, unchanged by the new key.
    assert frame.attrs["bfinance_actions_source"] == ACTIONS_SOURCE_SCREENER
    assert "does not index this instrument" in frame.attrs["bfinance_actions_fallback_reason"]
    assert frame.attrs["bfinance_actions_share_count_observable"] is False
    # No adjustment was applied, so nothing is INCOMPLETE about one: this is a declared absence,
    # not a half-finished calculation, and reporting it as the latter would be a second false
    # flag on the same frame.
    assert frame.attrs["bfinance_adjustment_incomplete"] is None
    assert frame.attrs["bfinance_corporate_actions_applied"] is False
    assert "fiscal-year-end approximation" in frame.attrs[
        "bfinance_corporate_actions_applied_detail"
    ]
    # The basis key agrees with the data: these are as-traded prices, and it is the one place
    # that says so in a machine-readable form.
    assert frame.attrs["price_basis"] == PRICE_BASIS_AS_TRADED
    # A key whose spelling only exists in this file's docstrings is a key nothing can rely on.
    assert ohlcv_mod.ADJUSTMENT_APPLIED_ATTR == "bfinance_adjustment_applied"
    assert ohlcv_mod.ADJUSTMENT_APPLIED_DETAIL_ATTR == "bfinance_adjustment_applied_detail"


async def test_the_key_is_present_on_every_frame_so_the_check_never_probes(bars):
    """
    ``is True`` / ``is False`` is only a reliable test if the key is always there.

    The rule every other disclosure key in this module follows: present on the populated frame
    and the EMPTY one, a bool rather than a missing value. A key present only on the frame where
    something went wrong forces a consumer to probe for its absence, which is the pattern that
    let the original false-success flag through in the first place.
    """
    bars(ETF_BARS)
    engine = _etf_engine()

    unadjusted = await engine.fetch_history(
        symbol="MAFANG", actions=True, auto_adjust=True, **ETF_WINDOW
    )
    assert unadjusted.attrs[ohlcv_mod.ADJUSTMENT_APPLIED_ATTR] is False
    assert isinstance(unadjusted.attrs[ohlcv_mod.ADJUSTMENT_APPLIED_DETAIL_ATTR], str)

    # auto_adjust=False on the same symbol still scales by the labelled approximation -- that is
    # unchanged behaviour, and the key has to report it rather than report False forever.
    plain = await engine.fetch_history(
        symbol="MAFANG", actions=True, auto_adjust=False, **ETF_WINDOW
    )
    assert plain.attrs[ohlcv_mod.ADJUSTMENT_APPLIED_ATTR] is True
    assert plain.attrs[ohlcv_mod.ADJUSTMENT_APPLIED_DETAIL_ATTR] == ""

    # The empty frame, which is the one a consumer is most likely to treat as harmless. An empty
    # frame on auto_adjust=True raises above, so this is reached with auto_adjust=False.
    empty_engine = OHLCVEngine(_Screener(), corporate_actions_client=_NoRowsActions())
    empty = await empty_engine.fetch_history(
        symbol="NOSUCHSCRIP", actions=False, auto_adjust=False, **ETF_WINDOW
    )
    assert empty.empty
    assert empty.attrs[ohlcv_mod.ADJUSTMENT_APPLIED_ATTR] is False
    assert "no bars in the requested window" in empty.attrs[
        ohlcv_mod.ADJUSTMENT_APPLIED_DETAIL_ATTR
    ]


async def test_auto_adjust_false_still_returns_adj_close_for_the_same_symbol(bars):
    """
    The escape has to work, or the disclosure is a dead end.

    ``auto_adjust=False`` is the documented way back to ``Adj Close`` for exactly these symbols,
    and it keeps the labelled approximation in that column rather than dropping it. Nothing about
    this call changed; the test exists so a later edit to the guard cannot quietly make the
    unadjusted answer the ONLY answer.
    """
    bars(ETF_BARS)
    frame = await _etf_engine().fetch_history(
        symbol="MAFANG", actions=True, auto_adjust=False, **ETF_WINDOW
    )

    assert list(frame.columns) == [
        "Open", "High", "Low", "Close", "Adj Close", "Volume", "Dividends", "Stock Splits"
    ]
    assert frame.attrs["bfinance_actions_source"] == ACTIONS_SOURCE_SCREENER
    assert frame.attrs["bfinance_adjustment_applied"] is True
    # The approximation still reaches Adj Close here, which is what the caller asking for the
    # dividend series is getting, and the price columns are untouched by it on this path too.
    assert frame["Adj Close"].iloc[0] == pytest.approx(ETF_CLOSES[0] * IMPLIED_FACTOR)
    assert frame["Close"].tolist() == ETF_CLOSES


# ================================================= the refusals that are KEPT, as controls

async def test_the_synthetic_path_still_raises_on_the_same_engine_and_profile(bars):
    """
    The negative control that pairs with the change above, in one test on purpose.

    A single "it no longer raises" assertion would also be satisfied by deleting the gate. This
    runs the approximation and the synthetic path through the SAME engine, with the SAME screener
    profile loaded, and requires opposite answers: the approximation is answered (the price feed
    is real, only the adjustment is missing) and the synthetic path still raises (its Open/High/
    Low are DERIVED from Close, so scaling them by anything invents the rest of the bar).
    """
    bars(ETF_BARS)
    engine = _etf_engine()

    answered = await engine.fetch_history(
        symbol="MAFANG", actions=True, auto_adjust=True, **ETF_WINDOW
    )
    assert not answered.empty

    chart = pd.DataFrame(
        {"Price": [251.00, 252.50, 254.00], "Volume": ETF_VOLUMES},
        index=pd.DatetimeIndex(
            pd.to_datetime(["2024-09-27", "2024-09-30", "2024-10-01"]), name="Date"
        ),
    )
    synthetic_engine = OHLCVEngine(
        _Screener(chart_df=chart, profile=_approximation_profile()),
        corporate_actions_client=_NoRowsActions(),
    )
    with pytest.raises(AdjustmentNotAvailableError) as exc:
        await synthetic_engine.fetch_history(
            symbol="MAFANG", ohlc="synthetic", actions=True, auto_adjust=True, **ETF_WINDOW
        )
    message = str(exc.value)
    assert "ohlc='synthetic'" in message, message
    assert "DERIVES Open/High/Low from Close" in message, message
    # Still decided locally, before a request is spent -- download() runs this per symbol.
    assert synthetic_engine.screener.chart_calls == []


async def test_no_ex_dated_feed_at_all_still_raises(bars):
    """
    The second kept refusal, and the one whose message a caller acts on.

    With no corporate-actions source and no profile there is nothing to scale by and no labelled
    approximation either, so the frame that would come back is indistinguishable from an
    unadjusted one with nothing on it saying so. That is the case a raise is for, and it is
    deliberately NOT the case the ETFs are in: there the source tag exists and is published.
    """
    bars(ETF_BARS)
    engine = OHLCVEngine(_Screener(), corporate_actions_client=_NoRowsActions())

    with pytest.raises(AdjustmentNotAvailableError) as exc:
        await engine.fetch_history(symbol="MAFANG", actions=True, auto_adjust=True, **ETF_WINDOW)
    message = str(exc.value)
    assert "no corporate-actions feed with real" in message, message
    assert "would fabricate history" in message, message

    # The feed being DOWN rather than empty lands in the same place, and says so in the detail.
    down = OHLCVEngine(_Screener(), corporate_actions_client=_NoRowsActions(error=RuntimeError()))
    with pytest.raises(AdjustmentNotAvailableError):
        await down.fetch_history(symbol="MAFANG", actions=True, auto_adjust=True, **ETF_WINDOW)


async def test_an_empty_frame_still_raises_on_auto_adjust_true(bars):
    """
    The third kept refusal: no bars at all.

    The archive holds a real bar for a DIFFERENT symbol, so the whole-market read succeeds and
    the slice to MAFANG is what comes back empty. Returning that empty frame with yfinance's
    adjusted column list would be the silent shape this package does not produce, so it refuses.
    """
    bars({"TCS": [("2024-09-30", 400.0, 402.0, 390.0, 391.0, 500)]})
    with pytest.raises(AdjustmentNotAvailableError) as exc:
        await _etf_engine().fetch_history(
            symbol="MAFANG", actions=True, auto_adjust=True, **ETF_WINDOW
        )
    assert "no corporate-actions feed with real" in str(exc.value)


# ================== the same claim, end to end, through the real bhavcopy reader and parser

class _ArchiveTransport(httpx.AsyncBaseTransport):
    """Serves the recorded NSE archives by URL stamp; 404 for anything else. No network.

    The pattern is the one ``tests/test_bhavcopy_keepalive.py`` established: ``bhavcopy.py``
    reaches ``httpx.AsyncClient`` at call time, so patching the attribute on the module is
    enough and the library keeps shipping exactly as it does.
    """

    def __init__(self):
        self.urls = []

    async def handle_async_request(self, request):
        self.urls.append(str(request.url))
        await asyncio.sleep(0)
        for stamp, payload in ARCHIVES.items():
            if stamp in str(request.url):
                return httpx.Response(200, content=payload, request=request)
        return httpx.Response(404, request=request)

    async def aclose(self):
        return None


@pytest.fixture
def real_bhavcopy(monkeypatch):
    """Patch ``httpx.AsyncClient`` so the real reader serves the recorded archives."""
    real_client = httpx.AsyncClient

    def _factory(**kwargs):
        kwargs["transport"] = _ArchiveTransport()
        kwargs.setdefault("timeout", 5.0)
        return real_client(**kwargs)

    monkeypatch.setattr(bc.httpx, "AsyncClient", _factory)
    return bc


async def test_the_close_is_nses_own_print_read_through_the_real_bhavcopy_parser(
    real_bhavcopy, monkeypatch
):
    """
    The proof the whole decision rests on, executed: the returned Close IS the archive's.

    Every other test here feeds the engine a stubbed whole-market frame, so the numbers under
    assertion are ones this file wrote. This one does not. It serves two recorded NSE CM
    archives through the REAL :mod:`bfinance.nse.bhavcopy` reader and its real parser, reads the
    ETF's published O/H/L/C/V off the returned frame, hands that same frame to the engine, and
    requires the engine's ``auto_adjust=True`` answer to carry those numbers through untouched.
    The ETFs are in the recording -- MAFANG, HNGSNGBEES and NIFTYBEES all print on 2026-09-25 --
    which is what makes the claim about ETFs specifically rather than about a symbol I chose.

    What this test does NOT prove is the guard, and the docstring says so rather than letting
    the reader assume: the approximation's dividend is dated at fiscal year end and neither
    archived day is a month end, so the implied ratio here is exactly 1.0. The guard is proved by
    ``test_the_implied_ratio_is_not_one_and_is_still_not_applied`` above, which is built around a
    window that does contain one. This test proves the VALUES, with NSE's own prints.
    """
    market = await asyncio.to_thread(real_bhavcopy.bhavcopy_range, "2026-09-24", "2026-09-26")
    monkeypatch.setattr(ohlcv_mod, "bhavcopy_range", lambda start, end, **kw: market)

    window = {"start": "2026-09-24", "end": "2026-09-26"}
    for symbol in ("MAFANG", "HNGSNGBEES", "NIFTYBEES"):
        published = ohlcv_mod._select_symbol_rows(market, symbol)
        assert not published.empty, f"{symbol} is not in the recorded archive"

        frame = await _etf_engine(profile=_approximation_profile(symbol)).fetch_history(
            symbol=symbol, actions=True, auto_adjust=True, **window
        )

        assert not frame.empty, symbol
        assert frame.attrs["bfinance_actions_source"] == ACTIONS_SOURCE_SCREENER
        assert frame.attrs["bfinance_adjustment_applied"] is False
        assert list(frame.columns) == [
            "Open", "High", "Low", "Close", "Volume", "Dividends", "Stock Splits"
        ]
        # The published prints, compared to the row the real reader produced for this symbol.
        published = published.sort_index()
        assert frame["Close"].tolist() == published["Close"].astype(float).tolist()
        for column in ("Open", "High", "Low"):
            assert frame[column].tolist() == published[column].astype(float).tolist()
        assert frame["Volume"].tolist() == published["Volume"].astype("int64").tolist()
        assert frame.attrs["price_basis"] == PRICE_BASIS_AS_TRADED


# ============================== the negative control on the change itself: a REAL feed still scales

async def test_a_real_ex_dated_feed_still_applies_its_factor_on_the_default_path(bars):
    """
    The guard is keyed on the SOURCE, so the control has to be the other source, not the other
    symbol. If it were keyed on the symbol, or on "did the ratio come out near 1", this test
    would go red.

    Same code path, same default ``auto_adjust=True``, same stub machinery -- only the actions
    client differs. RELIANCE's recorded feed carries the real 2024-10-28 1:1 bonus, so the two
    bars before that ex-date come back halved, ``bfinance_adjustment_applied`` reads True, and
    the detail is empty because there is nothing to disclose. A guard that suppressed every
    adjustment would satisfy the ETF tests above and fail here.
    """
    bars(BONUS_BARS)
    engine = OHLCVEngine(_Screener(), corporate_actions_client=_RecordedActions(RELIANCE_PAYLOAD))

    frame = await engine.fetch_history(
        symbol="RELIANCE", actions=True, auto_adjust=True, **BONUS_WINDOW
    )

    assert frame.attrs["bfinance_actions_source"] == ACTIONS_SOURCE_NSE
    assert frame.attrs["bfinance_adjustment_applied"] is True
    assert frame.attrs["bfinance_adjustment_applied_detail"] == ""
    # The 1:1 bonus, applied. If this ever reads as the raw print, the guard has grown a condition
    # it should not have and the ETF tests above would still be green.
    assert frame["Close"].tolist() == pytest.approx(
        [2640.90 * 0.5, 2655.70 * 0.5, 1334.35, 1340.00]
    )
    assert frame["Volume"].tolist() == [8_132_084 * 2, 9_298_748 * 2, 10_824_350, 12_008_361]
    assert "Adj Close" not in frame.columns
    # And the same bars under the approximation are NOT halved, which is the contrast this whole
    # file exists to pin.
    bars(BONUS_BARS)
    approx = await OHLCVEngine(
        _Screener(profile=_approximation_profile("RELIANCE")),
        corporate_actions_client=_NoRowsActions(),
    ).fetch_history(
        symbol="RELIANCE", start="2024-10-24", end="2024-10-30", actions=True, auto_adjust=True
    )
    assert approx.attrs["bfinance_actions_source"] == ACTIONS_SOURCE_SCREENER
    assert approx.attrs["bfinance_adjustment_applied"] is False
    assert approx["Close"].tolist() == [2640.90, 2655.70, 1334.35, 1340.00]
