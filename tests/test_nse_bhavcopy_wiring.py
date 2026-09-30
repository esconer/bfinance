"""
Wiring tests for the real-OHLC (NSE bhavcopy) history path.

The defect these lock down: ``OHLCVEngine`` used to build Open/High/Low out of a close-only feed
(``Open = prev_close``, ``High/Low = max/min(O, C) +/- 0.2%``) and set
``attrs["bfinance_synthetic_ohlc"]`` to ``True`` on *both* return paths, so the flag recorded
"this build always fabricates" rather than "this frame was fabricated" and nothing downstream
could act on it.

Offline by construction: every test monkeypatches ``bfinance.market.ohlcv.bhavcopy_range``, so
these run with no network and with or without ``bfinance.nse.bhavcopy`` installed. The stubs
reproduce the real module's contract (whole-market ``(Date, Symbol)`` MultiIndex, columns
exactly Open/High/Low/Close/Volume) so a contract drift in that module is caught by
``test_real_bhavcopy_module_matches_the_contract_assumed_here``.
"""

import inspect
import json
from pathlib import Path

import pandas as pd
import pytest

import bfinance.market.ohlcv as ohlcv_mod
from bfinance.market.ohlcv import (
    AdjustmentNotAvailableError,
    OHLCVEngine,
    RealOHLCUnavailableError,
)
from bfinance.nse import corporate_actions as ca
from bfinance.ticker import Ticker
from bfinance.utils.exceptions import TickerNotFoundError

from conftest import StubCorporateActionsClient

_ACTIONS_FIXTURE = Path(__file__).parent / "fixtures" / "corporate_actions" / (
    "nse_reliance_2020_2026.json"
)

# A real intraday range a +/-0.2% envelope cannot represent: 15% high-low, 10% close-open.
REAL_BARS = {
    "RELIANCE": [
        ("2024-06-03", 100.0, 112.0, 97.5, 110.0, 1_000),
        ("2024-06-04", 110.0, 113.5, 108.0, 109.0, 2_000),
        ("2024-06-05", 109.0, 118.0, 108.5, 117.5, 3_000),
    ],
    "TCS": [
        ("2024-06-03", 400.0, 402.0, 390.0, 391.0, 500),
        ("2024-06-04", 391.0, 405.0, 390.5, 404.0, 600),
        ("2024-06-05", 404.0, 404.5, 396.0, 397.5, 700),
    ],
}


def _market_frame(bars=None):
    """Whole-market bhavcopy frame: (Date, Symbol) MultiIndex, exactly the 5 OHLCV columns."""
    bars = REAL_BARS if bars is None else bars
    rows, dates, symbols = [], [], []
    for symbol, day_rows in bars.items():
        for day, o, h, low, c, v in day_rows:
            rows.append((o, h, low, c, v))
            dates.append(pd.Timestamp(day))
            symbols.append(symbol)
    index = pd.MultiIndex.from_arrays(
        [pd.DatetimeIndex(dates), pd.Index(symbols, dtype=object, name="Symbol")],
        names=["Date", "Symbol"],
    )
    frame = pd.DataFrame(
        rows, columns=["Open", "High", "Low", "Close", "Volume"], index=index
    )
    frame["Volume"] = frame["Volume"].astype("int64")
    frame = frame.sort_index()
    frame.attrs["bfinance_synthetic_ohlc"] = False
    frame.attrs["bfinance_provenance"] = "NSE CM bhavcopy (test stub)"
    return frame


def _recorded_actions():
    """The recorded NSE corporate-actions payload for RELIANCE. No network."""
    with open(_ACTIONS_FIXTURE, encoding="utf-8") as handle:
        return json.load(handle)


class _RecordedActionsClient:
    """The real NSE corporate-actions payload, parsed by the real parser. No network.

    A recorded feed response is the only honest way to test an ex-date: the defect this work fixed
    was an ex-date that was 4-5 months wrong, and a hand-written dividend at a guessed date would
    reproduce exactly that mistake inside the test. ``parse_corporate_actions`` +
    ``actions_frame`` are the production code path, so the dates under test are NSE's.
    """

    def __init__(self, payload=None):
        self.payload = _recorded_actions() if payload is None else payload

    async def actions_async(self, symbol, start=None, end=None, index="equities"):
        events = ca.parse_corporate_actions(self.payload, symbol=symbol, url="fixture")
        return ca.actions_frame(events, symbol=symbol, url="fixture")


class _StubScreener:
    """Close-only screener stub. Records calls so a test can prove it was NOT used."""

    def __init__(self, chart_df=None, profile=None):
        self.chart_df = chart_df if chart_df is not None else pd.DataFrame()
        self.profile = profile
        self.chart_calls = []
        self.profile_calls = []

    async def get_chart_timeseries(self, ticker, metric="price", days=1825):
        self.chart_calls.append((ticker, metric, days))
        return self.chart_df

    async def get_company_profile(self, ticker, force_refresh=False):
        self.profile_calls.append(ticker)
        if self.profile is None:
            raise ValueError("no profile")
        return self.profile


@pytest.fixture
def stub_bhavcopy(monkeypatch):
    """Install a whole-market bhavcopy stub and expose the recorded range calls."""
    calls = []

    def _factory(result=None, error=None):
        def _bhavcopy_range(start, end, **kwargs):
            calls.append((start, end, kwargs))
            if error is not None:
                raise error
            return _market_frame() if result is None else result

        monkeypatch.setattr(ohlcv_mod, "bhavcopy_range", _bhavcopy_range)
        return calls

    return _factory


def _engine(chart_df=None, profile=None, actions_client=None):
    # Inject a stub corporate-actions client so the actions=True tests stay offline. Without it,
    # CorporateActionsEngine.resolve_actions_async builds a REAL client and calls
    # www.nseindia.com -- a live DNS request that only shows up when ~/.bfinance is cold, i.e. on
    # a fresh CI runner, and is invisible on a developer machine with a warm cache.
    return OHLCVEngine(
        _StubScreener(chart_df, profile),
        corporate_actions_client=(
            actions_client if actions_client is not None else StubCorporateActionsClient()
        ),
    )


# --------------------------------------------------------------- provenance of the bars
async def test_default_history_is_real_bhavcopy_bars(stub_bhavcopy):
    """
    The core of the fix: the default path returns the published O/H/L, not an envelope.

    The fixture's High is 12% above Open and its Low 2.5% below; a ``max/min +/- 0.2%``
    construction is arithmetically incapable of producing those numbers, so asserting them
    exactly is a real check rather than a tautology.
    """
    stub_bhavcopy()
    engine = _engine()

    df = await engine.fetch_history(symbol="RELIANCE", period="1mo", actions=False)

    assert list(df.columns) == ["Open", "High", "Low", "Close", "Adj Close", "Volume"]
    assert df["Open"].tolist() == [100.0, 110.0, 109.0]
    assert df["High"].tolist() == [112.0, 113.5, 118.0]
    assert df["Low"].tolist() == [97.5, 108.0, 108.5]
    assert df["Close"].tolist() == [110.0, 109.0, 117.5]
    assert df["Volume"].tolist() == [1_000, 2_000, 3_000]
    assert df.attrs["bfinance_synthetic_ohlc"] is False


async def test_default_path_never_touches_the_close_only_screener(stub_bhavcopy):
    """Real bars must come from the bhavcopy, never from Close + a derived envelope."""
    stub_bhavcopy()
    screener = _StubScreener(
        chart_df=pd.DataFrame(
            {"Price": [1.0, 2.0], "Volume": [10, 20]},
            index=pd.DatetimeIndex(pd.to_datetime(["2024-06-03", "2024-06-04"]), name="Date"),
        )
    )
    df = await OHLCVEngine(screener).fetch_history(symbol="RELIANCE", actions=False)

    assert screener.chart_calls == [], "the close-only chart API must not drive the default path"
    assert df["High"].tolist() == [112.0, 113.5, 118.0]


async def test_one_range_fetch_serves_the_symbol_and_filters_the_market(stub_bhavcopy):
    """
    Efficiency shape, asserted: a daily bhavcopy file is the whole exchange (~3,600 rows), so
    one range fetch is sliced to the requested symbol. A per-symbol request would ask NSE for
    the same file N times.
    """
    calls = stub_bhavcopy()
    engine = _engine()

    df = await engine.fetch_history(symbol="RELIANCE", actions=False)

    assert len(calls) == 1, "one history() call must not fan out per symbol or per day"
    assert df["Close"].tolist() == [110.0, 109.0, 117.5], "TCS rows leaked into RELIANCE"
    assert not any("TCS" in str(v) for v in df.attrs.values())


async def test_synthetic_path_is_retained_behind_explicit_opt_in(stub_bhavcopy):
    """The legacy behaviour still exists, but only when asked for by name, and is flagged."""
    stub_bhavcopy()
    chart = pd.DataFrame(
        {"Price": [100.0, 102.0], "Volume": [10, 20]},
        index=pd.DatetimeIndex(pd.to_datetime(["2024-06-03", "2024-06-04"]), name="Date"),
    )
    screener = _StubScreener(chart_df=chart)
    df = await OHLCVEngine(screener).fetch_history(
        symbol="RELIANCE", period="1mo", actions=False, ohlc="synthetic"
    )

    assert screener.chart_calls, "the opt-in path must still use the screener feed"
    assert df.attrs["bfinance_synthetic_ohlc"] is True
    # The envelope is still the envelope -- documented, not silently the default any more.
    assert df["High"].round(4).tolist() == [100.2, 102.204]


def test_ohlc_parameter_defaults_to_bhavcopy_everywhere():
    """The default is a public contract: history() must not quietly change shape again."""
    for target in (
        OHLCVEngine.fetch_history,
        Ticker.history,
        Ticker.history_async,
    ):
        param = inspect.signature(target).parameters["ohlc"]
        assert param.default == "bhavcopy", f"{target.__qualname__} default changed"
        assert inspect.signature(target).parameters["auto_adjust"].default is False


async def test_unknown_ohlc_source_is_rejected(stub_bhavcopy):
    stub_bhavcopy()
    with pytest.raises(ValueError, match="ohlc must be one of"):
        await _engine().fetch_history(symbol="RELIANCE", ohlc="magic")


# --------------------------------------------------------------- index/column contract
async def test_bhavcopy_frame_keeps_the_yfinance_shape(stub_bhavcopy):
    stub_bhavcopy()
    df = await _engine().fetch_history(symbol="RELIANCE", actions=False)

    assert isinstance(df.index, pd.DatetimeIndex)
    assert str(df.index.tz) == "Asia/Kolkata"
    assert df.index.name == "Date"
    assert df.index.is_monotonic_increasing
    assert df["Volume"].dtype.kind == "i"


async def test_start_is_inclusive_and_end_exclusive(stub_bhavcopy):
    """yfinance semantics: start inclusive, end exclusive. Kept on the new source."""
    stub_bhavcopy()
    df = await _engine().fetch_history(
        symbol="RELIANCE", start="2024-06-04", end="2024-06-05", actions=False
    )
    assert df.index.strftime("%Y-%m-%d").tolist() == ["2024-06-04"]


async def test_weekly_resample_uses_the_real_extremes(stub_bhavcopy):
    """A resampled bar must be the max of real highs, not of a synthetic envelope."""
    stub_bhavcopy()
    df = await _engine().fetch_history(
        symbol="RELIANCE", interval="1wk", actions=False
    )
    assert len(df) == 1
    row = df.iloc[0]
    assert row["High"] == 118.0 and row["Low"] == 97.5
    assert row["Open"] == 100.0 and row["Close"] == 117.5
    assert row["Volume"] == 6_000


async def test_source_provenance_is_carried_onto_the_result(stub_bhavcopy):
    """attrs is a provenance record, not one boolean: the source identifies itself."""
    stub_bhavcopy()
    df = await _engine().fetch_history(symbol="RELIANCE", actions=False)
    assert df.attrs["bfinance_provenance"] == "NSE CM bhavcopy (test stub)"
    assert df.attrs["bfinance_synthetic_ohlc"] is False


# --------------------------------------------------------------- unavailable, not fabricated
async def test_bhavcopy_module_missing_raises_a_typed_error(monkeypatch):
    """
    A build without the bhavcopy module must fail loudly. Falling back would hand back
    fabricated bars; returning empty would look like "this ticker has no data".
    """
    monkeypatch.setattr(ohlcv_mod, "bhavcopy_range", None)
    with pytest.raises(RealOHLCUnavailableError) as exc:
        await _engine().fetch_history(symbol="RELIANCE", actions=False)
    assert "synthetic" in str(exc.value), "the error must name the opt-in escape hatch"


async def test_archive_failure_raises_and_does_not_fall_back(stub_bhavcopy):
    stub_bhavcopy(error=OSError("connection reset"))
    screener = _StubScreener(
        chart_df=pd.DataFrame({"Price": [100.0], "Volume": [1]},
                              index=pd.DatetimeIndex(pd.to_datetime(["2024-06-03"]), name="Date"))
    )
    with pytest.raises(RealOHLCUnavailableError) as exc:
        await OHLCVEngine(screener).fetch_history(symbol="RELIANCE", actions=False)
    assert "connection reset" in str(exc.value)
    assert screener.chart_calls == [], "an outage must not silently trigger the envelope path"


async def test_empty_archive_raises_rather_than_returning_nothing(stub_bhavcopy):
    """An empty archive is an outage or an unsupported window, not 'the ticker has no data'."""
    stub_bhavcopy(result=pd.DataFrame())
    with pytest.raises(RealOHLCUnavailableError, match="no data"):
        await _engine().fetch_history(symbol="RELIANCE", actions=False)


async def test_window_before_the_archive_explains_itself(stub_bhavcopy):
    calls = stub_bhavcopy()
    await _engine().fetch_history(symbol="RELIANCE", period="max", actions=False)
    start, _end, _kwargs = calls[0]
    assert start >= ohlcv_mod.BHAVCOPY_FIRST_DATE, "must not request pre-2024 dead URLs"


async def test_window_is_clamped_to_the_archive_start_with_a_warning(stub_bhavcopy, caplog):
    calls = stub_bhavcopy()
    with caplog.at_level("WARNING", logger="bfinance"):
        await _engine().fetch_history(symbol="RELIANCE", period="10y", actions=False)
    assert calls[0][0] == ohlcv_mod.BHAVCOPY_FIRST_DATE
    assert any(
        ohlcv_mod.BHAVCOPY_FIRST_DATE in r.getMessage() for r in caplog.records
    ), "truncating a 10y request to the archive must be said out loud"


async def test_typed_bhavcopy_errors_are_not_blurred(stub_bhavcopy):
    """A precise upstream diagnosis must survive, not be wrapped in a generic message."""
    from bfinance.utils.exceptions import NotAnNseSymbolError

    stub_bhavcopy(error=NotAnNseSymbolError("BSE scrip codes are not in the NSE CM bhavcopy"))
    with pytest.raises(NotAnNseSymbolError):
        await _engine().fetch_history(symbol="500112.BO", actions=False)


# --------------------------------------------------------------- leniency, without fabrication
async def test_absent_symbol_is_empty_and_flagged_real(stub_bhavcopy):
    """A genuinely missing instrument stays lenient: empty, never filled with invented bars."""
    stub_bhavcopy()
    df = await _engine().fetch_history(symbol="NOSUCHSCRIP", actions=False)

    assert df.empty
    assert list(df.columns) == ["Open", "High", "Low", "Close", "Adj Close", "Volume"]
    assert df.attrs["bfinance_synthetic_ohlc"] is False


async def test_absent_symbol_raises_under_raise_errors(stub_bhavcopy):
    stub_bhavcopy()
    with pytest.raises(TickerNotFoundError):
        await _engine().fetch_history(
            symbol="NOSUCHSCRIP", actions=False, raise_errors=True
        )


async def test_symbol_suffixes_are_normalised(stub_bhavcopy):
    stub_bhavcopy()
    for requested in ("RELIANCE", "RELIANCE.NS", "reliance.ns", "  RELIANCE-EQ  "):
        df = await _engine().fetch_history(symbol=requested, actions=False)
        assert not df.empty, f"{requested!r} did not resolve to the bhavcopy symbol"
        assert df["Close"].tolist() == [110.0, 109.0, 117.5]


# --------------------------------------------------------------- auto_adjust
async def test_auto_adjust_true_now_works_and_is_not_a_column_drop(stub_bhavcopy):
    """
    This test used to assert that ``auto_adjust=True`` raises. It asserted a real defect, but the
    defect it named had stopped being true: the NSE ex-dated corporate-actions feed supplies real
    dividends and real bonus ratios, they already drive ``Adj Close`` on the default path, and
    ``Adj Close`` is by construction the share-count factor times the dividend factor. Meanwhile
    yfinance 1.7 DEFAULTS ``auto_adjust`` to ``True``, so the package died on the value its closest
    competitor reaches by saying nothing.

    So the assertion is replaced rather than deleted, and it is replaced with the opposite claim:
    ``True`` must WORK. The first version of a fix like this is to accept the keyword and let
    ``_history_columns`` do its already-implemented column drop -- the tests about columns go
    green and every price is untouched -- so the assertion here is deliberately about VALUES. This
    stub carries RELIANCE's real 19-Aug-2024 Rs 10 dividend against a prior close of 2956.40, so
    the ratio is 0.99661764 and not 1.0, and a column drop cannot satisfy it.
    """
    stub_bhavcopy(result=_market_frame({
        "RELIANCE": [
            ("2024-08-16", 2900.0, 2970.0, 2890.0, 2956.40, 1_000),
            ("2024-08-19", 2956.40, 2990.0, 2940.0, 2976.80, 2_000),
            ("2024-08-20", 2976.80, 3010.0, 2960.0, 3000.00, 3_000),
        ]
    }))
    window = {"symbol": "RELIANCE", "start": "2024-08-16", "end": "2024-08-21", "actions": True}
    # The recorded NSE payload through the real parser, so the ex-date and the rupee amount under
    # test are NSE's. The default StubCorporateActionsClient carries nothing, which is the other
    # half of this file's story and is asserted in the refusal test just below.
    engine = _engine(actions_client=_RecordedActionsClient())

    adjusted = await engine.fetch_history(auto_adjust=False, **window)
    auto = await engine.fetch_history(auto_adjust=True, **window)

    assert "Adj Close" not in auto.columns
    assert list(auto.columns) == [
        "Open", "High", "Low", "Close", "Volume", "Dividends", "Stock Splits"
    ]
    # The load-bearing assertion: True returns the ADJUSTED prices, so its Close is the other
    # frame's Adj Close. The fixture's ratio is not 1.0, so a pure column drop fails this.
    assert auto["Close"].tolist() == pytest.approx(adjusted["Adj Close"].tolist(), rel=1e-12)
    ratio = (adjusted["Adj Close"] / adjusted["Close"]).tolist()
    assert ratio == pytest.approx([1.0 - 10.0 / 2956.40, 1.0, 1.0], rel=1e-12)
    # Volume and the action columns are not prices and do not move.
    assert auto["Volume"].tolist() == adjusted["Volume"].tolist()
    assert auto["Stock Splits"].tolist() == adjusted["Stock Splits"].tolist()


async def test_auto_adjust_true_still_refuses_when_no_real_actions_exist(stub_bhavcopy):
    """
    The refusal that was KEPT, deliberately, and the case that makes it necessary.

    With no ex-dated corporate-actions feed, ``Adj Close`` is not an adjusted series, and scaling
    Open/High/Low/Close by it would return unadjusted prices under an adjusted column list -- the
    exact defect ``auto_adjust=True`` is supposed to mean "not". The stub feeds a single 2024-06
    bar and the client carries no recorded actions, so no factor is derived and the call refuses.
    """
    stub_bhavcopy()
    with pytest.raises(AdjustmentNotAvailableError) as exc:
        await _engine().fetch_history(symbol="RELIANCE", auto_adjust=True, actions=True)
    message = str(exc.value)
    assert "no corporate-actions feed with real" in message, message
    assert "would fabricate history" in message, message


def test_auto_adjust_is_typed_and_derived_from_the_package_base():
    assert issubclass(AdjustmentNotAvailableError, ohlcv_mod.BFinanceError)
    assert issubclass(RealOHLCUnavailableError, ohlcv_mod.BFinanceError)


# --------------------------------------------------------------- Adj Close is a real adjustment
async def test_adj_close_reflects_dividends_on_the_real_bars(stub_bhavcopy):
    """The back-adjustment must land on the O/H/L/C, not only on ``Adj Close`` (issue-03 alarm).

    This test used to manufacture its dividend from a Screener profile and date it at fiscal year
    end, which put the ex-date on 2024-06-30 — a date on which NSE records **no action at all**
    for RELIANCE (the nearest are the 19-Aug dividend and the 28-Oct bonus). It was therefore
    asserting the removed approximation rather than the real feed, and the whole point of the NSE
    feed is that the ex-date is the true one. It now stubs the recorded NSE payload through the
    real parser, so it tests the path that actually runs.

    RELIANCE's real 19-Aug-2024 ex-date carried Rs 10 against a prior close of 2956.40, and the
    three bars are 16, 19 and 20 Aug, so the factor must touch only the 16-Aug bar.
    """
    stub_bhavcopy(
        result=_market_frame(
            {
                "RELIANCE": [
                    ("2024-08-16", 2900.0, 2970.0, 2890.0, 2956.40, 1_000),
                    ("2024-08-19", 2956.40, 2990.0, 2940.0, 2976.80, 2_000),
                    ("2024-08-20", 2976.80, 3010.0, 2960.0, 3000.00, 3_000),
                ]
            }
        )
    )
    engine = OHLCVEngine(_StubScreener(), corporate_actions_client=_RecordedActionsClient())

    df = await engine.fetch_history(symbol="RELIANCE", actions=True)

    assert df.attrs["bfinance_actions_source"] == "nse_corporate_actions"
    assert float(df.loc[df["Dividends"] != 0.0, "Dividends"].iloc[0]) == pytest.approx(10.0)
    assert df.index[df["Dividends"] != 0.0].tolist() == [
        pd.Timestamp("2024-08-19", tz="Asia/Kolkata")
    ], "the dividend must land on NSE's true ex-date, not the fiscal-year-end approximation"

    # The default path is back-adjusted. Two factors, kept apart, because yfinance keeps them
    # apart: the recorded payload's 2024-10-28 1:1 bonus halves all three bars (a share-count
    # event restates every earlier price, and its ex-date is outside these three bars), while the
    # Rs 10 dividend lands only on Adj Close. The price columns never see the dividend.
    assert df["Open"].iloc[0] == pytest.approx(2900.0 * 0.5)
    assert df["High"].iloc[0] == pytest.approx(2970.0 * 0.5)
    assert df["Low"].iloc[0] == pytest.approx(2890.0 * 0.5)
    assert df["Close"].iloc[0] == pytest.approx(2956.40 * 0.5)
    # The ex-date bar and everything after it carry no dividend factor.
    assert df["Close"].iloc[1:].tolist() == pytest.approx([2976.80 * 0.5, 3000.00 * 0.5])
    # Adj Close is the full adjustment: only the bar before the ex-date differs from Close.
    assert df["Adj Close"].iloc[0] == pytest.approx(2956.40 * 0.5 * (1.0 - 10.0 / 2956.40))
    assert df["Adj Close"].iloc[1:].tolist() == pytest.approx([2976.80 * 0.5, 3000.00 * 0.5])
    # The recorded payload holds rows bfinance declines to convert (RELIANCE's real 2023-07-20
    # demerger among them), so the frame reports an incomplete adjustment rather than a success.
    assert df.attrs["bfinance_corporate_actions_applied"] is False
    assert df.attrs["bfinance_adjustment_incomplete"]["unconverted_rows"] == 2
    assert df.attrs["price_basis"] == "as_traded_share_count_adjusted"
    # Volume is a share count, so it takes the INVERSE of the price factor: the two pre-bonus bars
    # double, the post-bonus bars are unchanged. price * volume is then invariant across the bonus,
    # which is the whole point of putting every bar on one share count.
    assert df["Volume"].tolist() == [2_000, 4_000, 6_000]
    for published_close, published_volume, adj_close, adj_volume in zip(
        [2956.40, 2976.80, 3000.00], [1_000, 2_000, 3_000], df["Close"], df["Volume"]
    ):
        assert adj_close * adj_volume == pytest.approx(
            published_close * published_volume, rel=1e-9
        )


async def test_adj_close_equals_close_when_there_are_no_actions(stub_bhavcopy):
    """No actions => factor 1.0. That equality is then a fact, not an unfinished adjustment."""
    stub_bhavcopy()
    df = await _engine().fetch_history(symbol="RELIANCE", actions=True)
    pd.testing.assert_series_equal(df["Adj Close"], df["Close"], check_names=False)


# --------------------------------------------------------------- public Ticker surface
def _ticker(stub_bhavcopy, profile=None):
    stub_bhavcopy()
    t = Ticker("RELIANCE", cache_ttl_hours=0)
    screener = _StubScreener(profile=profile)
    t.screener_client = screener
    t.ohlcv_engine = OHLCVEngine(screener)
    return t


def test_ticker_history_returns_real_bars_and_flags_them(stub_bhavcopy):
    df = _ticker(stub_bhavcopy).history(period="1mo", actions=False)
    assert df["High"].tolist() == [112.0, 113.5, 118.0]
    assert df.attrs["bfinance_synthetic_ohlc"] is False


def test_ticker_history_forwards_auto_adjust_without_dying_on_it(stub_bhavcopy):
    """
    This test used to assert that ``Ticker.history(auto_adjust=True)`` raises and spends no request.

    Both halves of that were consequences of the unconditional raise, and both are now wrong. The
    public entry point must not die on a keyword yfinance accepts by default, and the call must
    spend its request -- the refusal that is still correct (``actions=False`` has no factor to
    apply) is decided locally and is covered in
    ``tests/test_ohlcv_auto_adjust_true.py::test_the_refusal_still_fires_when_a_request_would_have_been_wasted``.

    What is asserted here is the forwarding, which is this layer's only job: the flag reaches
    ``fetch_history`` and the frame that comes back is the adjusted one.
    """
    calls = stub_bhavcopy(result=_market_frame({
        "RELIANCE": [
            ("2024-08-16", 2900.0, 2970.0, 2890.0, 2956.40, 1_000),
            ("2024-08-19", 2956.40, 2990.0, 2940.0, 2976.80, 2_000),
        ]
    }))
    t = _ticker(stub_bhavcopy)
    t.ohlcv_engine = OHLCVEngine(
        t.screener_client, corporate_actions_client=_RecordedActionsClient()
    )
    df = t.history(
        period="5d", start="2024-08-16", end="2024-08-20", actions=True, auto_adjust=True
    )
    assert calls, "the request is now made, because the call is answered rather than refused"
    assert "Adj Close" not in df.columns
    # The two-frame identity: the same window with auto_adjust=False puts exactly these closes in
    # its Adj Close column. That is the whole of yfinance's auto_adjust=True.
    plain = t.history(
        period="5d", start="2024-08-16", end="2024-08-20", actions=True, auto_adjust=False
    )
    assert df["Close"].tolist() == pytest.approx(plain["Adj Close"].tolist(), rel=1e-12)


async def test_ticker_history_async_reports_real_bars(stub_bhavcopy):
    t = _ticker(stub_bhavcopy)
    df = await t.history_async(period="1mo", actions=False)
    assert df.attrs["bfinance_synthetic_ohlc"] is False
    assert df["Low"].tolist() == [97.5, 108.0, 108.5]


def test_history_metadata_previous_close_is_a_real_bar(stub_bhavcopy):
    """
    Regression on the fabricated constant: ``chartPreviousClose`` was ``currentPrice * 0.995``,
    a number that lands inside the real adjustment factor and so looks plausible while never
    being a traded price. It must now be an actual close, or None when there is no history.

    The actions client is stubbed here, which it was not before: the engine used to build its real
    client on demand, so this "offline" test was quietly making a live NSE request. The payload
    is the recorded RELIANCE feed with the share-count rows removed, so the June-2024 bars carry no
    factor and the published close is the expected answer. With the real payload the 2024-10-28
    1:1 bonus would halve them, correctly.
    """
    from bfinance.models.company import CompanyProfile

    t = _ticker(stub_bhavcopy, profile=CompanyProfile(symbol="RELIANCE", company_id=1, name="Reliance"))
    t.ohlcv_engine = OHLCVEngine(
        t.screener_client,
        corporate_actions_client=_RecordedActionsClient(
            payload=[
                row for row in _recorded_actions()
                if "Bonus" not in row["subject"] and "Sub-Division" not in row["subject"]
            ]
        ),
    )
    meta = t.history_metadata

    assert meta["chartPreviousClose"] == pytest.approx(109.0)
    assert meta["previousClose"] == pytest.approx(109.0)
    assert meta["chartPreviousClose"] != pytest.approx(meta["regularMarketPrice"] * 0.995)


def test_history_metadata_says_none_when_there_is_no_history(monkeypatch):
    """No history must report None, not a plausible-looking number."""
    from bfinance.models.company import CompanyProfile

    monkeypatch.setattr(ohlcv_mod, "bhavcopy_range", None)
    t = Ticker("RELIANCE", cache_ttl_hours=0)
    screener = _StubScreener(
        profile=CompanyProfile(symbol="RELIANCE", company_id=1, name="Reliance")
    )
    t.screener_client = screener
    t.ohlcv_engine = OHLCVEngine(screener)

    meta = t.history_metadata
    assert meta["chartPreviousClose"] is None
    assert meta["previousClose"] is None


# --------------------------------------------------------------- real-module contract
def test_real_bhavcopy_module_matches_the_contract_assumed_here():
    """
    The stub above is only trustworthy while it matches the real module. This runs the real
    parser (pure function, no network) and asserts the shapes the wiring depends on.
    """
    bhavcopy = pytest.importorskip("bfinance.nse.bhavcopy")

    assert ohlcv_mod.bhavcopy_range is not None
    # Turnover value is part of the frame contract now, so the stub has to carry it.
    assert list(bhavcopy.COLUMNS) == [
        "Open", "High", "Low", "Close", "Volume", "TurnoverValue"
    ]

    frame = bhavcopy.parse_bhavcopy(
        "SYMBOL,SERIES,TIMESTAMP,OPEN,HIGH,LOW,CLOSE,TOTTRDQTY,TOTTRDVAL\n"
        "RELIANCE,EQ,03-Jun-2024,100.0,112.0,97.5,110.0,1000,105000.0\n"
        "TCS,EQ,03-Jun-2024,400.0,402.0,390.0,391.0,500,198000.0\n"
    )
    assert list(frame.columns) == ["Open", "High", "Low", "Close", "Volume", "TurnoverValue"]
    assert frame.index.names == ["Date", "Symbol"]
    assert frame.attrs["bfinance_synthetic_ohlc"] is False

    # ...and the wiring turns that exact frame into a real history frame.
    selected = ohlcv_mod._select_symbol_rows(frame, "RELIANCE")
    assert isinstance(selected.index, pd.DatetimeIndex)
    assert selected["High"].tolist() == [112.0]
