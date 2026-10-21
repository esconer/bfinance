"""
The bar-spacing contract: a frame must say how far apart its bars actually are, and must
say so when that is coarser than what the caller asked for.

Why this test exists. The close-only Screener.in chart route changes granularity with
window length, and the request does not say which you got. Measured live on RELIANCE with
the identical ``interval="1d"`` call, 2026-09-28:

    period=1mo    20 rows  median gap 1 day    daily
    period=6mo   122 rows  median gap 1 day    daily
    period=1y    247 rows  median gap 1 day    daily
    period=2y    105 rows  median gap 7 days   weekly
    period=5y    262 rows  median gap 7 days   weekly

Before this, a caller requesting daily bars silently received weekly ones past roughly
eighteen months. Nothing recorded it: no column, no attribute, no warning. Every per-bar
indicator, ATR and backtest then changed meaning at that boundary while the frame kept
the same shape and the same column names, so the change was invisible downstream.

The published response is attrs plus a log line, deliberately NOT a raise: the values are
real, only the resolution is coarser, so a caller who wants five years at weekly
resolution is being served correctly. Refusing would break that legitimate use to prevent
a misuse the caller can now see for themselves.
"""
from __future__ import annotations

import pandas as pd
import pytest

from bfinance.market import ohlcv as ohlcv_mod
from bfinance.market.ohlcv import (
    OHLCVEngine,
    _infer_bar_interval,
    _interval_mismatch_detail,
)

from conftest import StubCorporateActionsClient

INTERVAL_KEY = "bfinance_bar_interval"
MISMATCH_KEY = "bfinance_bar_interval_mismatch"


class _Screener:
    """Close-only screener stub, matching the one in test_ohlcv_honesty.py."""

    def __init__(self, chart_df=None):
        self.chart_df = chart_df if chart_df is not None else pd.DataFrame()
        self.chart_calls = []

    async def get_chart_timeseries(self, ticker, metric="price", days=1825):
        self.chart_calls.append(ticker)
        return self.chart_df

    async def get_company_profile(self, ticker, force_refresh=False):
        raise ValueError("no profile")


def _chart(index: pd.DatetimeIndex) -> pd.DataFrame:
    """A close-only chart frame shaped the way the Screener route returns one."""
    n = len(index)
    frame = pd.DataFrame(
        {
            "Price": [100.0 + i for i in range(n)],
            "Volume": [1_000 * (i + 1) for i in range(n)],
        },
        index=pd.DatetimeIndex(index, name="Date"),
    )
    return frame


DAILY = pd.DatetimeIndex(
    ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09"]
)
WEEKLY = pd.DatetimeIndex(
    ["2025-06-02", "2025-06-09", "2025-06-16", "2025-06-23", "2025-06-30"]
)


# ------------------------------------------------------------------ the inference itself
class TestInference:
    def test_consecutive_trading_days_read_as_daily(self):
        assert _infer_bar_interval(DAILY) == "1d"

    def test_seven_day_spacing_reads_as_weekly(self):
        assert _infer_bar_interval(WEEKLY) == "1wk"

    def test_weekend_gaps_do_not_make_a_daily_series_look_weekly(self):
        """
        The load-bearing edge. A Friday-to-Monday pair is 3 days, so a minimum-gap or
        first-gap reading calls any daily equity series weekly. A real NSE week contains
        exactly this pattern every week, so the error would fire on essentially every
        frame and the key would be worthless.
        """
        fri_monday = pd.DatetimeIndex(
            ["2026-01-02", "2026-01-05", "2026-01-06", "2026-01-07",
             "2026-01-08", "2026-01-09"]
        )
        assert _infer_bar_interval(fri_monday) == "1d"

    def test_one_anomalous_gap_does_not_reclassify_the_series(self):
        """
        This is the fixture that separates a MEDIAN reading from a MINIMUM reading, and it
        was added after a mutation check showed the weekend test above could not tell them
        apart: a daily series has gaps of 1 and 3, so its minimum and its median agree and
        both implementations pass it.

        Here the series is weekly with one pair of adjacent bars. The median is 7 and the
        answer is "1wk"; the minimum is 1 and a min-based reading calls it "1d". Real
        feeds carry exactly this kind of irregularity -- an extra or missing bar around a
        holiday -- so a classifier keyed on the minimum would silently mislabel a whole
        series from a single anomalous row.
        """
        irregular = pd.DatetimeIndex(
            ["2025-06-02", "2025-06-09", "2025-06-10", "2025-06-16", "2025-06-23"]
        )
        assert _infer_bar_interval(irregular) == "1wk"

    def test_too_few_bars_is_untellable_rather_than_guessed(self):
        """
        Two bars carry one gap, which cannot distinguish a daily pair from a weekly pair.
        Guessing here would publish a confident wrong spacing, so None is returned and
        the caller sees "unknown" instead.
        """
        assert _infer_bar_interval(DAILY[:2]) is None

    def test_empty_index_is_untellable(self):
        assert _infer_bar_interval(pd.DatetimeIndex([])) is None

    def test_monthly_spacing_reads_as_monthly(self):
        monthly = pd.DatetimeIndex(
            ["2025-01-31", "2025-02-28", "2025-03-31", "2025-04-30"]
        )
        assert _infer_bar_interval(monthly) == "1mo"


# ------------------------------------------------------------- the mismatch discriminator
class TestMismatchDiscriminates:
    """
    The comparison must return a sentence for one pair and None for the other. Asserting
    only that a mismatch is detected would pass against a function that always returns a
    string, which is why both directions are pinned here.
    """

    def test_coarser_than_requested_is_flagged(self):
        detail = _interval_mismatch_detail("1d", "1wk")
        assert detail is not None
        assert "1wk" in detail and "1d" in detail

    @pytest.mark.parametrize(
        "requested,delivered",
        [
            ("1d", "1d"),    # asked daily, got daily
            ("1wk", "1d"),   # asked weekly, got daily: FINER, not a lie
            ("1wk", "1wk"),  # asked weekly, got weekly
            ("1mo", "1wk"),  # asked monthly, got weekly: finer
            ("1mo", "1mo"),
        ],
    )
    def test_equal_or_finer_is_not_flagged(self, requested, delivered):
        assert _interval_mismatch_detail(requested, delivered) is None

    def test_undeterminable_delivery_is_not_flagged(self):
        """
        "Cannot tell" is a third state, not "matches". It must not manufacture a
        mismatch, and equally must not be reported as a confirmed match.
        """
        assert _interval_mismatch_detail("1d", None) is None

    def test_unknown_request_is_not_flagged(self):
        assert _interval_mismatch_detail("5m", "1wk") is None


# ---------------------------------------------------------------- end to end through fetch
class TestPublishedOnEveryFrame:
    @pytest.mark.asyncio
    async def test_daily_feed_for_a_daily_request_publishes_daily_and_no_mismatch(self, monkeypatch):
        monkeypatch.setattr(ohlcv_mod, "bhavcopy_range", lambda *a, **k: None)
        engine = OHLCVEngine(_Screener(chart_df=_chart(DAILY)))
        # auto_adjust=False is explicit on every call in this class because auto_adjust is now
        # True by default (matching yfinance 1.7) and this path refuses True: it derives
        # Open/High/Low from Close and is never back-adjusted, so there is no real Adj Close to
        # scale by. Every assertion below is about the bar interval, not about the adjustment,
        # and none of them predates that default flip.
        df = await engine.fetch_history(
            symbol="ITC", ohlc="synthetic", actions=False, auto_adjust=False
        )

        assert df.attrs[INTERVAL_KEY] == "1d"
        assert df.attrs[MISMATCH_KEY] is None

    @pytest.mark.asyncio
    async def test_weekly_feed_for_a_daily_request_is_flagged_and_says_which(self, monkeypatch):
        """
        The defect this test pins. A weekly chart frame with interval="1d" requested must
        produce a non-None mismatch naming BOTH intervals, so the caller can tell what
        they got and what they asked for.
        """
        monkeypatch.setattr(ohlcv_mod, "bhavcopy_range", lambda *a, **k: None)
        engine = OHLCVEngine(_Screener(chart_df=_chart(WEEKLY)))
        df = await engine.fetch_history(
            symbol="ITC", ohlc="synthetic", actions=False, auto_adjust=False
        )

        assert len(df) == 5, "the frame is returned, not refused: the values are real"
        assert df.attrs[INTERVAL_KEY] == "1wk"
        detail = df.attrs[MISMATCH_KEY]
        assert detail is not None, "a daily request answered with weekly bars must say so"
        assert "1wk" in detail
        assert "interval='1d'" in detail

    @pytest.mark.asyncio
    async def test_weekly_feed_for_a_weekly_request_is_not_flagged(self, monkeypatch):
        """
        The negative control at the integration level. Same weekly feed, but the caller
        asked for weekly, so there is nothing to report. Without this, a function that
        always returned a sentence would pass every other test in this file.
        """
        monkeypatch.setattr(ohlcv_mod, "bhavcopy_range", lambda *a, **k: None)
        engine = OHLCVEngine(_Screener(chart_df=_chart(WEEKLY)))
        df = await engine.fetch_history(
            symbol="ITC", ohlc="synthetic", actions=False, auto_adjust=False, interval="1wk"
        )
        assert df.attrs[INTERVAL_KEY] == "1wk"
        assert df.attrs[MISMATCH_KEY] is None

    @pytest.mark.asyncio
    async def test_a_warning_is_logged_when_the_delivery_is_coarser(self, monkeypatch, caplog):
        """Published in attrs AND logged, carrying the same sentence, so they cannot drift."""
        monkeypatch.setattr(ohlcv_mod, "bhavcopy_range", lambda *a, **k: None)
        engine = OHLCVEngine(_Screener(chart_df=_chart(WEEKLY)))
        with caplog.at_level("WARNING", logger="bfinance"):
            df = await engine.fetch_history(
                symbol="ITC", ohlc="synthetic", actions=False, auto_adjust=False
            )

        logged = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
        assert any(df.attrs[MISMATCH_KEY] in m for m in logged), (
            "the mismatch sentence must reach the log, not only the attrs"
        )

    @pytest.mark.asyncio
    async def test_no_warning_when_the_delivery_matches(self, monkeypatch, caplog):
        monkeypatch.setattr(ohlcv_mod, "bhavcopy_range", lambda *a, **k: None)
        engine = OHLCVEngine(_Screener(chart_df=_chart(DAILY)))
        with caplog.at_level("WARNING", logger="bfinance"):
            await engine.fetch_history(
                symbol="ITC", ohlc="synthetic", actions=False, auto_adjust=False
            )

        offenders = [
            r.getMessage() for r in caplog.records
            if r.levelname == "WARNING" and "delivered" in r.getMessage()
        ]
        assert not offenders, f"a matched delivery must stay quiet, got: {offenders}"

    @pytest.mark.asyncio
    async def test_the_keys_exist_on_an_empty_frame_too(self, monkeypatch):
        """
        A frame with no bars is the one a consumer is most likely to treat as harmless,
        so the keys must be present there as well, and None rather than absent.
        """
        monkeypatch.setattr(ohlcv_mod, "bhavcopy_range", lambda *a, **k: None)
        engine = OHLCVEngine(_Screener(chart_df=pd.DataFrame()))
        empty = await engine.fetch_history(
            symbol="ITC", ohlc="synthetic", auto_adjust=False
        )

        assert empty.empty
        assert INTERVAL_KEY in empty.attrs
        assert MISMATCH_KEY in empty.attrs
        assert empty.attrs[INTERVAL_KEY] is None
        assert empty.attrs[MISMATCH_KEY] is None

    @pytest.mark.asyncio
    async def test_bhavcopy_publishes_daily_and_never_flags(self, monkeypatch):
        """
        The bhavcopy archive is daily by construction, so the real-OHLC path must report
        "1d" and stay silent. If this ever flagged, every honest caller would be trained
        to ignore the key.
        """
        rows = {
            "ITC": [
                ("2026-01-05", 480.0, 486.0, 478.0, 485.0, 1_000),
                ("2026-01-06", 485.0, 490.0, 482.0, 489.0, 1_100),
                ("2026-01-07", 489.0, 493.0, 487.0, 492.0, 1_200),
                ("2026-01-08", 492.0, 495.0, 490.0, 494.0, 1_300),
                ("2026-01-09", 494.0, 497.0, 491.0, 496.0, 1_400),
            ]
        }
        market = ohlcv_mod.__dict__["OHLCVEngine"]  # name kept explicit for clarity
        del market
        monkeypatch.setattr(ohlcv_mod, "bhavcopy_range", _fake_bhavcopy(rows))
        # The stub is injected and auto_adjust is pinned to False, so this call cannot consult the
        # corporate-actions feed at all: with the default (True since 2026-09-30) the engine
        # resolves it whenever EITHER flag asks for it (``actions or auto_adjust``), which built a
        # REAL client and looked up www.nseindia.com -- answered from a warm ~/.bfinance on a
        # developer machine, and made for real on a cold CI runner.
        engine = OHLCVEngine(_Screener(), corporate_actions_client=StubCorporateActionsClient())
        df = await engine.fetch_history(
            symbol="ITC", ohlc="bhavcopy", actions=False, auto_adjust=False
        )

        assert df.attrs[INTERVAL_KEY] == "1d"
        assert df.attrs[MISMATCH_KEY] is None
        assert df.attrs["bfinance_synthetic_ohlc"] is False


def _fake_bhavcopy(rows):
    """
    Build a whole-market (Date, Symbol) frame and return a CALLABLE standing in for
    bhavcopy_range, which the engine invokes rather than reads as a value.
    """
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
    frame = frame.sort_index()

    def _range(start, end):
        return frame

    return _range


# ------------------------------------------------------------------- non-vacuity of the file
def test_the_inference_actually_discriminates_three_widths():
    """
    A single end-of-file guard. If every one of these collapsed to the same answer the
    class above would still contain passing tests, because they each assert one value.
    Reading three different spacings and demanding three different answers is what proves
    the function varies with its input.
    """
    answers = {
        _infer_bar_interval(DAILY),
        _infer_bar_interval(WEEKLY),
        _infer_bar_interval(pd.DatetimeIndex(["2025-01-31", "2025-02-28", "2025-03-31"])),
    }
    assert answers == {"1d", "1wk", "1mo"}
