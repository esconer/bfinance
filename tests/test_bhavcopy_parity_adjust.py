"""
The default history path is BACK-ADJUSTED, so bfinance matches yfinance.

The defect
----------
``Ticker.history()`` returned NSE bhavcopy bars AS-TRADED. yfinance's own ``auto_adjust=False``
series is not as-traded: it restates every past bar onto the current share count, so the two
disagreed by the full corporate-action factor across entire date ranges. Measured over
2024-09-01..2024-11-15 against yfinance 1.7.0:

    RELIANCE    14/53 bars match; 39 bars off by exactly 2.0x   (1:1 bonus, ex-date 2024-10-28)
    HDFCBANK     0/53 bars match; ALL off by exactly 2.0x
    ITC          0/53 bars match; ALL off by 1.039x
    TCS, INFY, SBIN, AXISBANK, TITAN   53/53 match  (no action in the window)

RELIANCE 2024-10-25 shows the mechanism: bhavcopy 2655.70 (what actually printed), yfinance
1327.85 (the same bar on today's share count). The 2024-10-28 bars agree, because that is where
the action lands. A third source, Trendlyne, agreed with yfinance on 7 of 8 symbols, so yfinance's
basis is the reference and a drop-in has to reproduce it.

What these tests lock down
-------------------------
1. **The default applies the share-count factor to all four price columns**, and only to those
   four. Dividends do not go on O/H/L/C, because yfinance's raw OHLC do not carry them -- TCS,
   with Rs 238 paid after the window, matches bhavcopy to 5e-8.
2. **The share-count factor is the whole-history product, not the window's.** HDFCBANK's 1:1
   bonus has a 2025-08-26 ex-date, so for a 2024-09..2024-11 request nothing in the frame shows
   it and a window-only product is 1.0 on every bar where yfinance halves all 53.
3. **Volume is never scaled.** A back-adjustment restates prices; it says nothing about how many
   shares changed hands that day. Scaling volume would invent a share count that never traded.
4. **As-traded is available by explicit opt-in** (``adjust_corporate_actions=False``), because
   trade reconstruction needs it and the bhavcopy natively is it.
5. **``Adj Close`` is the full adjusted series** (share count + dividends), so it equals ``Close``
   exactly when no dividend ex-date falls in the window, and does not when one does -- which is
   yfinance's own shape.
6. **Provenance is truthful in all four combinations** of (bar source) x (adjusted / not), and
   a degraded actions feed produces as-traded bars that SAY SO, never a silent unadjusted frame.

Offline by construction: the bhavcopy fetch and the corporate-actions client are both stubbed, and
the actions stub replays a recorded NSE payload through the real parser, so the ex-dates under
test are NSE's. No test here needs the network.
"""

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import bfinance.market.ohlcv as ohlcv_mod
from bfinance.market.corporate import ACTIONS_SOURCE_NONE, ACTIONS_SOURCE_NSE
from bfinance.market.ohlcv import OHLCVEngine
from bfinance.nse import corporate_actions as ca
from bfinance.ticker import Ticker

FIXTURES = Path(__file__).parent / "fixtures" / "corporate_actions"


def _load(name: str):
    with open(FIXTURES / name, encoding="utf-8") as handle:
        return json.load(handle)


class _ActionsClient:
    """Replays a recorded NSE payload through the real parser. No network.

    ``error``/``payload=[]`` model the two degradation shapes: the feed being down, and the feed
    not indexing the instrument at all.
    """

    def __init__(self, payload=None, error=None):
        self.payload = _load("nse_reliance_2020_2026.json") if payload is None else payload
        self.error = error
        self.calls = []

    async def actions_async(self, symbol, start=None, end=None, index="equities"):
        self.calls.append(symbol)
        if self.error is not None:
            raise self.error
        events = ca.parse_corporate_actions(self.payload, symbol=symbol, url="fixture")
        return ca.actions_frame(events, symbol=symbol, url="fixture")


class _Screener:
    """Close-only screener stub. Never consulted on the bhavcopy path; asserts that."""

    def __init__(self, chart_df=None, profile=None):
        self.chart_df = chart_df if chart_df is not None else pd.DataFrame()
        self.profile = profile
        self.chart_calls = []
        self.profile_calls = []

    async def get_chart_timeseries(self, ticker, metric="price", days=1825):
        self.chart_calls.append(ticker)
        return self.chart_df

    async def get_company_profile(self, ticker, force_refresh=False):
        self.profile_calls.append(ticker)
        if self.profile is None:
            raise ValueError("no profile")
        return self.profile


# RELIANCE's real 2024-08-19 Rs 10 dividend (prior close 2956.40) and the real 2024-10-28 1:1
# bonus (-> 2.0). Four bars, exactly as tests/test_corporate_actions_wiring.py records them.
BARS = {
    "RELIANCE": [
        ("2024-08-16", 2900.0, 2970.0, 2890.0, 2956.40, 1_000),
        ("2024-08-19", 2956.40, 2990.0, 2940.0, 2976.80, 2_000),
        ("2024-10-25", 2600.0, 2670.0, 2590.0, 2655.70, 4_000),
        ("2024-10-28", 2655.70, 2700.0, 1320.0, 1334.35, 5_000),
    ]
}

# The acceptance-test shape: bars that ALL sit before the 2024-10-28 bonus ex-date, and a window
# that ends before it. This is the HDFCBANK case -- the event restates every bar in the frame and
# the frame never mentions it, so a window-only factor is 1.0 on all of them.
PRE_BONUS_BARS = {
    "RELIANCE": [
        ("2024-10-24", 2620.0, 2660.0, 2610.0, 2640.90, 11_000),
        ("2024-10-25", 2640.90, 2668.0, 2622.0, 2655.70, 9_298_748),
    ]
}

# The two factors, kept apart because yfinance keeps them apart.
#
# SHARE COUNT: the recorded payload's only later share-count event is the 2024-10-28 1:1 bonus, so
# the three bars before it carry 1/2 and the ex-date bar carries 1.0.
SPLIT_FACTORS = [0.5, 0.5, 0.5, 1.0]
# DIVIDEND: only the 2024-08-19 Rs 10 falls inside the window, and its prev_close is the 16-Aug
# bar's own close of 2956.40. The 2025-08-14 and 2026-06-05 dividends are outside it and are
# deliberately NOT included -- there is no prev_close to divide by (see _dividend_adjustment_factor).
DIVIDEND_FACTORS = [1.0 - 10.0 / 2956.40, 1.0, 1.0, 1.0]

PUBLISHED_OPEN = [2900.0, 2956.40, 2600.0, 2655.70]
PUBLISHED_HIGH = [2970.0, 2990.0, 2670.0, 2700.0]
PUBLISHED_LOW = [2890.0, 2940.0, 2590.0, 1320.0]
PUBLISHED_CLOSE = [2956.40, 2976.80, 2655.70, 1334.35]
PUBLISHED_VOLUME = [1_000, 2_000, 4_000, 5_000]


def _market_frame(rows=None):
    """Whole-market bhavcopy-shaped frame: (Date, Symbol) MultiIndex, 5 OHLCV columns."""
    rows = BARS if rows is None else rows
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
    frame.attrs["bfinance_synthetic_ohlc"] = False
    frame.attrs["bfinance_provenance"] = "NSE CM bhavcopy (test stub)"
    return frame


@pytest.fixture
def bhavcopy(monkeypatch):
    """Install the bhavcopy stub. Records the range calls so a test can assert the fetch count."""
    calls = []

    def _factory(result=None):
        def _range(start, end, **kwargs):
            calls.append((start, end))
            return _market_frame() if result is None else result

        monkeypatch.setattr(ohlcv_mod, "bhavcopy_range", _range)
        return calls

    return _factory


def _engine(actions_client=None, screener=None):
    return OHLCVEngine(
        screener if screener is not None else _Screener(),
        corporate_actions_client=actions_client,
    )


async def _history(actions_client=None, screener=None, **kwargs):
    kwargs.setdefault("actions", True)
    return await _engine(actions_client, screener).fetch_history(
        symbol="RELIANCE.NS", period="5d", **kwargs
    )


# ---------------------------------------------------------- 1. the default applies the factor


async def test_default_ohlc_carries_the_share_count_factor(bhavcopy):
    """The defect, written as an assertion: every price column is restated, not just Adj Close.

    Before the fix ``Close`` came back at 2655.70 for 25-Oct, where yfinance reports 1327.85 --
    the full 2.0x bonus factor missing across the whole range. 2655.70 * 0.5 = 1327.85, which is
    the live measurement, not a tidy number chosen to divide.
    """
    bhavcopy()
    df = await _history(_ActionsClient())

    for column, published in (
        ("Open", PUBLISHED_OPEN),
        ("High", PUBLISHED_HIGH),
        ("Low", PUBLISHED_LOW),
        ("Close", PUBLISHED_CLOSE),
    ):
        expected = [p * f for p, f in zip(published, SPLIT_FACTORS)]
        assert df[column].tolist() == pytest.approx(expected), f"{column} was not back-adjusted"
    assert df["Close"].iloc[2] == pytest.approx(1327.85)


async def test_a_share_count_event_outside_the_window_still_restates_it(bhavcopy):
    """
    The HDFCBANK case, and the one a window-only factor cannot survive.

    HDFCBANK's 1:1 bonus has a 2025-08-26 ex-date, so for a 2024-09..2024-11 request not one bar
    in the frame carries it -- yet yfinance halves all 53. Here both bars and the whole window
    precede RELIANCE's real 2024-10-28 bonus, so reading the factor off the frame gives 1.0 and
    leaves both bars at their as-traded price, which is the bug.
    """
    calls = bhavcopy(result=_market_frame(PRE_BONUS_BARS))
    df = await _history(_ActionsClient(), start="2024-10-24", end="2024-10-26")

    assert calls, "the bars were fetched"
    assert df.index.strftime("%Y-%m-%d").tolist() == ["2024-10-24", "2024-10-25"]
    # No action appears in the merged columns -- that is the whole point of this test.
    assert bool((df["Dividends"] == 0.0).all())
    assert bool((df["Stock Splits"] == 0.0).all())
    assert df["Close"].tolist() == pytest.approx([2640.90 * 0.5, 2655.70 * 0.5])
    # ...and Adj Close == Close, because with no dividend ex-date in the window the two factors
    # collapse into one. This is the shape of the acceptance window.
    pd.testing.assert_series_equal(df["Adj Close"], df["Close"], check_names=False)


async def test_dividends_never_reach_the_price_columns(bhavcopy):
    """
    The load-bearing negative. yfinance's raw OHLC do not carry dividends, so neither may ours:
    TCS matches bhavcopy to 5e-8 with Rs 238 of dividends paid after the window.

    Here the 19-Aug Rs 10 dividend is inside the window. Applying its factor to O/H/L/C is exactly
    what put TCS 7.6% from yfinance, so the price columns carry the share count only.
    """
    bhavcopy()
    df = await _history(_ActionsClient())

    assert float(df.loc[df["Dividends"] != 0.0, "Dividends"].iloc[0]) == pytest.approx(10.0)
    assert df["Close"].tolist() == pytest.approx(
        [p * f for p, f in zip(PUBLISHED_CLOSE, SPLIT_FACTORS)]
    )
    # Adj Close is where the dividend belongs, and it is the only column that moves on it.
    assert df["Adj Close"].iloc[0] == pytest.approx(2956.40 * 0.5 * (1.0 - 10.0 / 2956.40))
    assert df["Adj Close"].iloc[1] == pytest.approx(2976.80 * 0.5)


async def test_the_bonus_ex_date_bar_is_never_adjusted(bhavcopy):
    """
    A factor that also moves the ex-date bar itself is wrong, and the error is silent: it looks
    like a price move. On 28-Oct the share count had already changed, so that bar -- and the
    yfinance value it is compared against -- carries 1.0.
    """
    bhavcopy()
    df = await _history(_ActionsClient())

    last = df.iloc[-1]
    assert last["Close"] == pytest.approx(1334.35)
    assert last["Open"] == pytest.approx(2655.70)
    assert last["High"] == pytest.approx(2700.0)
    assert last["Low"] == pytest.approx(1320.0)


async def test_adj_close_is_the_full_adjusted_series(bhavcopy):
    """
    ``Adj Close`` = Close x dividend factor, and it equals ``Close`` only when no dividend
    ex-date falls in the window. A dividend in the window is the whole difference, and that is
    yfinance's own shape: its raw OHLC carry splits, its "Adj Close" carries both.
    """
    bhavcopy()
    df = await _history(_ActionsClient())

    expected = [
        close * split * dividend
        for close, split, dividend in zip(PUBLISHED_CLOSE, SPLIT_FACTORS, DIVIDEND_FACTORS)
    ]
    assert df["Adj Close"].tolist() == pytest.approx(expected)
    # The dividend ex-date is the only bar where the two columns differ.
    differing = (df["Adj Close"] != df["Close"]).to_numpy()
    assert list(np.nonzero(differing)[0]) == [0]


async def test_no_action_at_all_means_a_factor_of_exactly_one(bhavcopy):
    """
    Equal to Close, because nothing happened -- a fact, not an unfinished adjustment.

    The payload is the recorded RELIANCE feed with its two bonus rows dropped, so the instrument
    has real dividends and no share-count event at all. With nothing after the window, both
    factors are exactly 1.0 and every column is the published value.
    """
    bhavcopy(
        result=_market_frame(
            {"RELIANCE": [("2024-05-02", 2400.0, 2450.0, 2390.0, 2440.00, 7),
                          ("2024-05-03", 2440.0, 2470.0, 2420.0, 2460.00, 8)]}
        )
    )
    client = _ActionsClient(
        payload=[r for r in _load("nse_reliance_2020_2026.json") if "Bonus" not in r["subject"]]
    )
    df = await _history(client)

    pd.testing.assert_series_equal(df["Adj Close"], df["Close"], check_names=False)
    assert df["Close"].tolist() == [2440.00, 2460.00]
    assert df.attrs["bfinance_actions_source"] == ACTIONS_SOURCE_NSE, "the feed was consulted"
    assert df.attrs["bfinance_corporate_actions_applied"] is True


async def test_a_later_bonus_still_restates_a_window_with_no_events_in_it(bhavcopy):
    """
    The quiet-window case, and the one that is easy to get wrong in the other direction.

    Nothing happened in May 2024, so the merged action columns are all zero -- and yet the
    2024-10-28 bonus restates both bars, because a share-count event rewrites every earlier price
    whether or not it falls in the window. Treating an empty in-window answer as "no adjustment
    needed" is the same defect in a different guise.
    """
    bhavcopy(
        result=_market_frame(
            {"RELIANCE": [("2024-05-02", 2400.0, 2450.0, 2390.0, 2440.00, 7),
                          ("2024-05-03", 2440.0, 2470.0, 2420.0, 2460.00, 8)]}
        )
    )
    df = await _history(_ActionsClient())

    assert bool((df["Dividends"] == 0.0).all())
    assert bool((df["Stock Splits"] == 0.0).all())
    assert df["Close"].tolist() == pytest.approx([1220.00, 1230.00])
    pd.testing.assert_series_equal(df["Adj Close"], df["Close"], check_names=False)


async def test_dividends_after_the_window_are_named_rather_than_silently_dropped(bhavcopy):
    """
    The one adjustment this call cannot make, reported instead of hidden.

    ``1 - D / prev_close`` needs the close on the trading day before the ex-date, and a bhavcopy
    range fetch stops at the requested ``end``. The recorded RELIANCE payload pays Rs 5.5 on
    2025-08-14 and Rs 6.0 on 2026-06-05, both after these four bars. Measured against yfinance,
    that omission makes bfinance's ``Adj Close`` a uniform 0.86% above yfinance's -- a real,
    bounded, *discoverable* gap rather than a divergence nobody can explain. It does not touch
    O/H/L/C, because dividends are not on them.
    """
    bhavcopy()
    df = await _history(_ActionsClient())

    pending = df.attrs["bfinance_actions_unapplied_dividends"]
    assert pending == [
        {"ex_date": "2025-08-14", "amount": 5.5},
        {"ex_date": "2026-06-05", "amount": 6.0},
    ]
    # The share-count factor has no such gap, because it needs no price.
    assert df["Close"].iloc[2] == pytest.approx(1327.85)


# ---------------------------------------------------------- 2. volume is a share count


async def test_volume_is_never_scaled(bhavcopy):
    """
    "Scale everything by the factor" is the obvious wrong move and this is where it is refused.

    A back-adjustment restates *prices* onto the current share count. The shares that traded on
    25-Oct did not halve; the share count did. Multiplying volume by 1/2 would report a traded
    share count that never existed, and would do it silently -- nobody checks volume against
    bhavcopy, because volume is not supposed to need adjusting.
    """
    bhavcopy()
    df = await _history(_ActionsClient())

    assert df["Volume"].tolist() == PUBLISHED_VOLUME
    assert df["Volume"].dtype.kind == "i"
    assert "Volume" not in ohlcv_mod.ADJUSTED_PRICE_COLUMNS


async def test_the_dividend_and_split_columns_are_amounts_not_prices(bhavcopy):
    """Rs payable and share ratios are not restated either: a rupee dividend stays a rupee."""
    bhavcopy()
    df = await _history(_ActionsClient())

    assert float(df.loc[df["Dividends"] != 0.0, "Dividends"].iloc[0]) == pytest.approx(10.0)
    assert float(df.loc[df["Stock Splits"] != 0.0, "Stock Splits"].iloc[0]) == pytest.approx(2.0)


# ---------------------------------------------------------- 3. as-traded, by explicit opt-in


async def test_opt_out_returns_the_published_prices(bhavcopy):
    """Trade reconstruction needs what printed on the exchange, and it must stay reachable."""
    bhavcopy()
    df = await _history(_ActionsClient(), adjust_corporate_actions=False)

    assert df["Close"].tolist() == PUBLISHED_CLOSE
    assert df["Open"].tolist() == PUBLISHED_OPEN
    assert df.attrs["bfinance_corporate_actions_applied"] is False
    assert "adjust_corporate_actions=False" in df.attrs["bfinance_corporate_actions_applied_detail"]
    # Adj Close still carries the full adjustment, so the adjusted series stays recoverable from
    # the published prices. It just is not what the price columns report.
    assert df["Adj Close"].iloc[0] == pytest.approx(2956.40 * 0.5 * (1.0 - 10.0 / 2956.40))
    assert df["Adj Close"].iloc[-1] == pytest.approx(1334.35)


async def test_the_default_is_parity_not_as_traded():
    """A default that drifts back to as-traded would silently re-break yfinance parity."""
    import inspect

    for target in (
        OHLCVEngine.fetch_history,
        Ticker.history,
        Ticker.history_async,
    ):
        param = inspect.signature(target).parameters["adjust_corporate_actions"]
        assert param.default is True, f"{target.__qualname__} default changed"


# ---------------------------------------------------------- 4. provenance, all four combinations


async def test_bhavcopy_adjusted_is_recorded_as_both(bhavcopy):
    bhavcopy()
    df = await _history(_ActionsClient())

    assert df.attrs["bfinance_ohlc_source"] == "bhavcopy"
    assert df.attrs["bfinance_synthetic_ohlc"] is False
    assert df.attrs["bfinance_corporate_actions_applied"] is True
    assert df.attrs["bfinance_actions_source"] == ACTIONS_SOURCE_NSE
    assert ACTIONS_SOURCE_NSE in df.attrs["bfinance_corporate_actions_applied_detail"]


async def test_bhavcopy_unadjusted_is_recorded_as_both(bhavcopy):
    bhavcopy()
    df = await _history(_ActionsClient(), adjust_corporate_actions=False)

    assert df.attrs["bfinance_ohlc_source"] == "bhavcopy"
    assert df.attrs["bfinance_synthetic_ohlc"] is False
    assert df.attrs["bfinance_corporate_actions_applied"] is False
    # The action source is still reported: the factor was computed, it just was not applied.
    assert df.attrs["bfinance_actions_source"] == ACTIONS_SOURCE_NSE
    assert "back-adjusted Close by NSE" in df.attrs["bfinance_adj_close_source"]


async def test_synthetic_path_is_never_adjusted_and_says_so(bhavcopy):
    """
    The opt-in close-only path keeps its old contract untouched: a derived Open/High/Low envelope
    that the real feed has no adjustment for. It is already flagged as fabricated; it now also
    declares that it is not on the adjusted basis, so the two facts cannot be conflated.
    """
    bhavcopy()
    chart = pd.DataFrame(
        {"Price": [100.0, 102.0], "Volume": [10, 20]},
        index=pd.DatetimeIndex(pd.to_datetime(["2024-08-16", "2024-08-19"]), name="Date"),
    )
    df = await _history(_ActionsClient(), screener=_Screener(chart_df=chart), ohlc="synthetic")

    assert df.attrs["bfinance_ohlc_source"] == "synthetic"
    assert df.attrs["bfinance_synthetic_ohlc"] is True
    assert df.attrs["bfinance_corporate_actions_applied"] is False
    assert "synthetic path" in df.attrs["bfinance_corporate_actions_applied_detail"]
    # Untouched contract: the raw envelope survives. Close is the screener price verbatim, and
    # the +/-0.2% High is still derived from it rather than rescaled.
    assert df["Close"].tolist() == [100.0, 102.0]
    assert df["High"].round(4).tolist() == [100.2, 102.204]
    # Adj Close still derives from the in-window actions only, byte-for-byte as before this change.
    assert df["Adj Close"].iloc[0] == pytest.approx(100.0 * (1.0 - 10.0 / 100.0))


# ---------------------------------------------------------- 5. degradation: never silent


async def test_unreachable_feed_returns_as_traded_and_flags_it(bhavcopy, caplog):
    """
    (a), not (c). The bhavcopy prices are still perfectly good as-traded prices, and throwing
    them away because a second endpoint had a bad minute would be its own kind of lying. So they
    are returned -- unadjusted, and labelled, in attrs and in a log warning. The frame does not
    claim parity and a consumer can see that without reading the numbers.
    """
    bhavcopy()
    with caplog.at_level("WARNING", logger="bfinance"):
        df = await _history(_ActionsClient(error=RuntimeError("nse blocked")))

    assert df.attrs["bfinance_corporate_actions_applied"] is False
    assert df.attrs["bfinance_actions_source"] == ACTIONS_SOURCE_NONE
    assert "parity is NOT established" in df.attrs["bfinance_corporate_actions_applied_detail"]
    # Unadjusted, and honestly so: the published close, not a half-applied one.
    assert df["Close"].tolist() == PUBLISHED_CLOSE
    assert any(
        "parity" in record.getMessage() for record in caplog.records
    ), "a degradation this consequential must be said out loud, not only recorded"


async def test_a_symbol_the_feed_does_not_index_is_flagged_not_guessed(bhavcopy):
    """Zero rows for the whole symbol is a coverage gap, and the label must not read as parity."""
    bhavcopy()
    df = await _history(_ActionsClient(payload=[]), screener=_Screener())

    assert df.attrs["bfinance_corporate_actions_applied"] is False
    assert "parity is NOT established" in df.attrs["bfinance_corporate_actions_applied_detail"]


async def test_actions_false_is_recorded_rather_than_assumed(bhavcopy):
    """
    The factor is derived from the actions, so ``actions=False`` means there is nothing to apply.
    The caller asked for the default basis and did not get it, so the frame says so instead of
    letting a missing key imply the default.
    """
    bhavcopy()
    df = await _history(_ActionsClient(), actions=False)

    assert df.attrs["bfinance_corporate_actions_applied"] is False
    assert "actions=False" in df.attrs["bfinance_corporate_actions_applied_detail"]
    assert df["Close"].tolist() == PUBLISHED_CLOSE


async def test_an_empty_result_still_carries_the_provenance_contract(bhavcopy):
    bhavcopy(result=_market_frame({"TCS": [("2024-08-16", 1.0, 1.0, 1.0, 1.0, 1)]}))
    df = await _history(_ActionsClient())

    assert df.empty
    assert df.attrs["bfinance_ohlc_source"] == "bhavcopy"
    assert df.attrs["bfinance_corporate_actions_applied"] is False


# ---------------------------------------------------------- 6. one source of truth per field


async def test_close_is_the_published_price_times_the_factor_and_nothing_else(bhavcopy):
    """
    The anti-blending guarantee, restated for the adjusted path and therefore STRONGER than the
    as-traded version: ``Close`` is exactly ``published * factor``, single-source, with no second
    price averaged in. A blend, a correction from another venue, or a synthetic fallback would all
    miss this identity; scaling alone hits it exactly.
    """
    bhavcopy()
    df = await _history(_ActionsClient())

    known = np.array(SPLIT_FACTORS)
    # Divide the factor back out and the bhavcopy value must reappear, bit for bit.
    np.testing.assert_allclose(
        (df["Close"] / known).to_numpy(), np.array(PUBLISHED_CLOSE), rtol=1e-12
    )
    assert df["Close"].tolist() == pytest.approx((np.array(PUBLISHED_CLOSE) * known).tolist())


async def test_the_actions_feed_is_never_asked_for_a_price(bhavcopy):
    bhavcopy()
    client = _ActionsClient()
    await _history(client)

    assert client.calls == ["RELIANCE"]


async def test_the_screener_is_not_consulted_on_the_adjusted_path(bhavcopy):
    screener = _Screener()
    bhavcopy()
    await _history(_ActionsClient(), screener=screener)

    assert screener.chart_calls == []
    assert screener.profile_calls == []


async def test_one_bhavcopy_range_fetch_serves_the_window(bhavcopy):
    """The adjustment adds no requests of its own: one bars fetch, one actions fetch."""
    calls = bhavcopy()
    client = _ActionsClient()
    await _history(client)

    assert len(calls) == 1
    assert len(client.calls) == 1
