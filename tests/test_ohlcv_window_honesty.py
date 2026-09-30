"""
``period="max"`` and ``period="5y"`` used to be clamped to the bhavcopy archive floor in SILENCE.

The defect
----------
:data:`bfinance.market.ohlcv.BHAVCOPY_FIRST_DATE` is 2024-01-01, the first date the cookie-free
NSE archive publishes a full O/H/L tape. A request reaching further back is clamped to it, which
is correct -- it is a real upstream limit and bfinance will not invent the missing bars. What was
wrong was that the clamp existed only as a ``logger.warning``.

The caller got back a frame with a perfect schema, a plausible bar count and a column list exactly
right for the instrument, with nothing anywhere in the returned object saying that two and a half
of the five years they asked for are simply absent. Nothing in the *values* reveals it either: a
2.7-year series is exactly what a 2.7-year series looks like. So a caller could compute a
five-year volatility, draw a five-year chart, or backtest across a decade, on 27% of the window,
and find nothing wrong.

That is the same defect class as the missing ``bfinance_bar_interval_mismatch`` label, and it is
fixed the same way and under the same rule: a real answer plus a truthful label, on **every**
frame, the empty one included.

The keys
--------
``attrs["bfinance_window_truncated"]``        bool -- the thing a consumer branches on
``attrs["bfinance_window_truncated_detail"]`` str  -- the sentence, empty when nothing was clamped

The detail is a ``str`` on every frame rather than a detail-or-``None``, following
``bfinance_price_source_degraded`` / ``_detail`` rather than ``bfinance_bar_interval_mismatch``:
a stable type means the reader never has to know that ``""`` means "fine", and the bool is right
there for the branch.

Offline by construction. The bhavcopy reader is stubbed and the corporate-actions client is
injected, so nothing here reaches the network -- which matters more than usual for this file,
because the whole subject is a request that reaches *further* back than the archive does.
"""

import logging

import pandas as pd
import pytest

import bfinance.market.ohlcv as ohlcv_mod
from bfinance.market.ohlcv import OHLCVEngine, PriceSourceRouter
from bfinance.sources.health import HealthLedger


# ------------------------------------------------------------------------------ stubs, no network

class _Screener:
    """Tripwire screener: the bhavcopy path must never consult it."""

    async def get_chart_timeseries(self, *args, **kwargs):
        raise AssertionError("the bhavcopy path must not consult the close-only chart API")

    async def get_company_profile(self, *args, **kwargs):
        raise AssertionError("the bhavcopy path must not consult the Screener profile")


#: Three real RELIANCE prints from 2024-10-24..2024-10-28, past the floor, so a clamped request and
#: an unclamped one return the SAME rows and the only difference between the two frames is the
#: label. That is deliberate: the test is about what the frame says, not about the bars.
PAST_FLOOR_BARS = {
    "RELIANCE": [
        ("2024-10-24", 2620.00, 2660.00, 2610.00, 2640.90, 8_132_084),
        ("2024-10-25", 2640.90, 2668.00, 2622.00, 2655.70, 9_298_748),
        ("2024-10-28", 1337.00, 1353.00, 1322.10, 1334.35, 10_824_350),
    ]
}

#: A second symbol, so "the archive held rows but not for this one" is reachable and the empty
#: frame under test is the ordinary no-rows-here case rather than an archive-wide outage.
OTHER_SYMBOL = {
    "TCS": [("2024-10-24", 4000.00, 4020.00, 3980.00, 3990.00, 500_000)]
}


def _market_frame(rows):
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
def archive(monkeypatch):
    """Install the bhavcopy stub, and record every (start, end) it was asked for."""

    calls = []

    def _factory(rows=PAST_FLOOR_BARS):
        def _bhavcopy_range(start, end, **kwargs):
            calls.append((start, end))
            return _market_frame(rows)

        monkeypatch.setattr(ohlcv_mod, "bhavcopy_range", _bhavcopy_range)
        return calls

    return _factory


def _engine(price_sources=None):
    return OHLCVEngine(_Screener(), price_sources=price_sources)


# =========================================================== the flag, on the populated frame

async def test_period_max_past_the_archive_floor_says_so_on_the_frame(archive):
    """
    The defect, written as an assertion.

    ``period="max"`` is 10000 days of lookback against an archive that starts 2024-01-01, so this
    request is clamped every single time it is made. The frame it returns has real bars, a correct
    schema and a plausible row count; before this change the ONLY evidence of the clamp was a log
    line a caller never sees. Now the frame carries it.
    """
    calls = archive()
    df = await _engine().fetch_history(symbol="RELIANCE", period="max", actions=False)

    assert calls[0][0] == ohlcv_mod.BHAVCOPY_FIRST_DATE, (
        "and it is still clamped -- the flag does not replace the clamp, it reports it"
    )
    assert df.attrs["bfinance_window_truncated"] is True
    detail = df.attrs["bfinance_window_truncated_detail"]
    assert isinstance(detail, str) and detail
    # The sentence must name BOTH ends of the disagreement, or it is not actionable: what was
    # asked for, and what the archive could actually deliver.
    assert calls[0][0] in detail, f"the detail must name the floor: {detail}"
    assert "requested window starts" in detail, detail


async def test_the_detail_names_the_requested_window_start_not_just_the_floor(archive):
    """
    Naming only the floor would leave the caller unable to tell WHICH request was truncated.

    ``period="5y"`` and ``period="10y"`` both clamp to 2024-01-01 and both lose a different amount
    of history, so a sentence that said only "clamped to 2024-01-01" would be true of both and
    useless for either. The requested start date is in the sentence, which is what makes the two
    tellable apart.
    """
    archive()
    five = await _engine().fetch_history(symbol="RELIANCE", period="5y", actions=False)
    ten = await _engine().fetch_history(symbol="RELIANCE", period="10y", actions=False)

    assert five.attrs["bfinance_window_truncated"] is True
    assert ten.attrs["bfinance_window_truncated"] is True
    five_detail = five.attrs["bfinance_window_truncated_detail"]
    ten_detail = ten.attrs["bfinance_window_truncated_detail"]
    assert five_detail != ten_detail, (
        "two windows that lose different amounts of history must not share one sentence"
    )
    # 5y is 1825 days back and 10y is 3652, so the two requested starts are five years apart.
    assert "2021" in five_detail, five_detail
    assert "2016" in ten_detail, ten_detail


async def test_the_log_line_and_the_attrs_carry_the_same_sentence(archive, caplog):
    """
    One sentence, one function, so the two channels cannot drift apart.

    This is the rule ``_interval_mismatch_detail`` already follows for the bar-spacing case, and it
    is a rule rather than a preference: a log line and an attrs value written in two places are
    two facts, and the day they disagree the one a consumer reads is the one that is wrong.
    """
    archive()
    with caplog.at_level(logging.WARNING, logger="bfinance"):
        df = await _engine().fetch_history(symbol="RELIANCE", period="max", actions=False)

    detail = df.attrs["bfinance_window_truncated_detail"]
    logged = [
        record.getMessage() for record in caplog.records
        if record.name == "bfinance" and detail in record.getMessage()
    ]
    assert logged, (
        "the clamp is still logged -- losing the log line would trade a silent clamp for one a "
        "caller watching the log can no longer see"
    )


# ================================================================= the empty frame, and the control

async def test_the_two_keys_are_on_the_empty_frame_too(archive):
    """
    An empty frame is exactly the frame a consumer treats as "nothing went wrong".

    And it is the frame where the question matters most: a caller who asked for ``period="max"``
    and got nothing back needs to know whether the archive floor is why, which is a question the
    empty frame previously could not answer at all. The two keys are present with the same
    meaning they have on a populated frame.
    """
    archive({**PAST_FLOOR_BARS, **OTHER_SYMBOL})
    # The archive holds rows, but none for TCS, so the slice comes back empty through the ordinary
    # "this scrip has no rows here" path rather than the archive-wide outage refusal.
    empty = await _engine().fetch_history(symbol="NOTLISTED", period="max", actions=False)

    assert empty.empty
    assert empty.attrs["bfinance_window_truncated"] is True
    assert empty.attrs["bfinance_window_truncated_detail"]


async def test_the_empty_frame_carries_the_same_sentence_as_the_populated_one(archive):
    """
    One fact, one sentence, whatever the frame looks like.

    A consumer that special-cases the empty frame -- and the module's whole attrs philosophy says
    they should not have to -- would otherwise get a differently-worded version of the same clamp.
    """
    populated_calls = archive()
    populated = await _engine().fetch_history(symbol="RELIANCE", period="max", actions=False)
    archive({**PAST_FLOOR_BARS, **OTHER_SYMBOL})
    empty = await _engine().fetch_history(symbol="NOTLISTED", period="max", actions=False)

    assert populated_calls  # the stub really was consulted
    assert empty.attrs["bfinance_window_truncated_detail"] == (
        populated.attrs["bfinance_window_truncated_detail"]
    )


async def test_a_window_inside_the_floor_is_not_marked_truncated(archive):
    """
    The negative control, without which the flag is worthless.

    A flag that is always ``True`` is as uninformative as one that is never set, and this one has
    an obvious way to go that way: ``period="max"`` is the request everyone remembers, so marking
    every ``period`` as truncated -- or computing the comparison against "today" instead of against
    the requested window -- would pass every test above. An explicit ``start=`` inside the archive
    must read ``False`` with an empty sentence.
    """
    archive()
    inside = await _engine().fetch_history(
        symbol="RELIANCE", period="5d", start="2024-10-24", actions=False
    )
    assert inside.attrs["bfinance_window_truncated"] is False
    assert inside.attrs["bfinance_window_truncated_detail"] == ""
    # The short default period is inside the floor too, so the common case is not alarming.
    short = await _engine().fetch_history(symbol="RELIANCE", period="1mo", actions=False)
    assert short.attrs["bfinance_window_truncated"] is False


async def test_the_synthetic_path_reports_no_truncation(archive):
    """
    The archive floor is a property of the bhavcopy tape, and this path does not read that tape.

    So the keys are still present -- a consumer reads one key on every frame -- and both say
    "nothing was clamped". Omitting them here would make the check a probe for absence, which is
    the pattern the whole attrs layer exists to avoid.
    """
    archive()
    chart = pd.DataFrame(
        {"Price": [100.0, 102.0, 101.0], "Volume": [10, 20, 15]},
        index=pd.DatetimeIndex(
            pd.to_datetime(["2024-10-24", "2024-10-25", "2024-10-28"]), name="Date"
        ),
    )
    df = await OHLCVEngine(_ChartScreener(chart)).fetch_history(
        symbol="RELIANCE", ohlc="synthetic", period="max", actions=False
    )

    assert df.attrs["bfinance_window_truncated"] is False
    assert df.attrs["bfinance_window_truncated_detail"] == ""


class _ChartScreener:
    """A screener that serves a close-only chart, which is all the synthetic path reads."""

    def __init__(self, chart_df):
        self.chart_df = chart_df

    async def get_chart_timeseries(self, *args, **kwargs):
        return self.chart_df

    async def get_company_profile(self, *args, **kwargs):
        raise AssertionError("actions=False means no corporate actions are requested at all")


# ==================================================== the cascade clamps too, and says so as well

async def test_the_cascade_path_clamps_and_reports(archive):
    """
    The clamp is duplicated -- deliberately -- in the cascade's own reader, so the cascade must
    report it too. A flag wired into only one of the two readers would report "not truncated" for
    a request that was in fact clamped, which is worse than reporting nothing at all.

    The engine is built with a ``PriceSourceRouter`` and a stubbed fetch, so no vendor is reached.
    """
    archive()
    engine = OHLCVEngine(
        _Screener(),
        price_sources=PriceSourceRouter(fetch=_stub_fetch(), health=HealthLedger()),
    )
    df = await engine.fetch_history(
        symbol="RELIANCE", period="max", actions=False, sources=["nse-bhavcopy"]
    )

    assert not df.empty
    assert df.attrs["bfinance_window_truncated"] is True
    assert "2024-01-01" in df.attrs["bfinance_window_truncated_detail"]


async def test_the_cascade_path_reports_no_truncation_inside_the_floor(archive):
    """The control for the control: the cascade's flag is not stuck on either."""
    archive()
    engine = OHLCVEngine(
        _Screener(),
        price_sources=PriceSourceRouter(fetch=_stub_fetch(), health=HealthLedger()),
    )
    df = await engine.fetch_history(
        symbol="RELIANCE", period="5d", start="2024-10-24", actions=False,
        sources=["nse-bhavcopy"],
    )
    assert df.attrs["bfinance_window_truncated"] is False
    assert df.attrs["bfinance_window_truncated_detail"] == ""


def _stub_fetch():
    """A cascade fetch that serves the same bars, and records the range it was asked for."""

    def _fetch(spec, request):
        rows = _market_frame(PAST_FLOOR_BARS)
        return rows[
            (rows.index.get_level_values("Date") >= pd.Timestamp(request["start"]))
            & (rows.index.get_level_values("Date") < pd.Timestamp(request["end"]))
        ]

    return _fetch


# ============================================ no bar was invented to make the window look complete

async def test_a_truncated_window_never_contains_a_bar_from_before_the_floor(archive):
    """
    The clamp is a refusal to fabricate, and this is the assertion that it stayed one.

    The obvious wrong fix is to pad the frame -- emit rows for the missing years at zero, or carry
    the earliest available close backwards -- so that ``len(df)`` matches the request. The check
    is structural: nothing in the frame can predate the floor, and the frame is exactly the rows
    the archive returned, unaltered.
    """
    archive()
    df = await _engine().fetch_history(symbol="RELIANCE", period="max", actions=False)

    floor = pd.Timestamp(ohlcv_mod.BHAVCOPY_FIRST_DATE, tz=df.index.tz)
    assert (df.index >= floor).all(), "no bar may predate the archive floor"
    # Exactly the archive's published prints, unscaled and unpadded. The 25-Oct -> 28-Oct step is
    # the real 1:1 bonus, halved on nothing: actions=False, so no share-count factor is applied and
    # what the exchange printed is what comes back.
    assert df["Close"].tolist() == pytest.approx([2640.90, 2655.70, 1334.35])
    assert len(df) == 3, "the frame is the archive's rows and not a padded approximation of them"
    # price * volume is still the traded value, i.e. the numbers were not rescaled to look fuller.
    assert (df["Close"] * df["Volume"]).iloc[0] == pytest.approx(2640.90 * 8_132_084, rel=1e-9)


async def test_the_truncation_flag_is_readable_without_touching_the_log(archive, caplog):
    """
    The whole point: a library consumer, in a program, with logging switched off.

    If the clamp were only a log line then this test could not be written at all -- and the
    ``caplog`` context is what proves it: the assertion below holds with nothing captured, so the
    frame carried the fact on its own.
    """
    archive()
    with caplog.at_level(logging.WARNING, logger="bfinance"):
        df = await _engine().fetch_history(symbol="RELIANCE", period="max", actions=False)
    assert df.attrs["bfinance_window_truncated"] is True
    assert caplog.text, "the log line is still expected; this asserts it is not the ONLY channel"
