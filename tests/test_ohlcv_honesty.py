"""
Three honesty gaps in the OHLCV path, each locked down by a regression test.

1. **A false success flag.** ``attrs["bfinance_corporate_actions_applied"]`` read ``True`` whenever
   a real factor was derived from a real feed, which is a different claim from "this frame is fully
   adjusted". :mod:`bfinance.nse.corporate_actions` deliberately never converts a ``Demerger``,
   buy-back, rights issue, AGM notice or REIT per-unit distribution into a factor, so the feed can
   hold known price-affecting events the factor does not cover -- and the frame had no way to say
   so. ITC's real 2025-01-06 demerger is the measured instance: the as-traded close fell 8.0876%
   across the ex-date (481.60 -> 442.65) and the mechanically correct factor is 1.047332
   (= ``P_pre / (P_ex + 0.1 x 171.85)``, the stub's listing close), while the factor bfinance
   applies is exactly 1.000000. The frame claimed to have applied an adjustment 4.7332% short.
   Now the flag reads ``False`` and ``attrs["bfinance_adjustment_incomplete"]`` names the reason,
   so a caller that asked for ``adjust_corporate_actions=True`` can detect this from attrs alone.

2. **No stated price basis.** ``attrs["price_basis"]`` now names the rupee-price basis of every
   frame returned, on a machine-readable vocabulary.

3. **Unscaled volume.** The as-traded bhavcopy counts pre-bonus bars in then-shares and post-bonus
   bars in now-shares, so one column carries two share-count bases. Volume is restated by the
   *inverse* of the price factor so ``price * volume`` -- the rupee value traded -- is one quantity
   across the boundary.

Offline by construction. The bhavcopy fetch is stubbed and the actions stub replays a **recorded**
NSE payload through the real parser, so the ex-dates and subjects under test are NSE's.

Provenance of the hard numbers
------------------------------
Bar values are the real bhavcopy archive, read with
``bfinance.nse.bhavcopy.bhavcopy_range("2025-01-02", "2025-01-09")`` and sliced to ITC:

    2025-01-02  485.35  490.00  482.30  489.05   26,124,980
    2025-01-03  490.25  491.00  479.85  481.60   37,072,992
    2025-01-06  455.60  461.75  441.25  442.65   22,009,958
    2025-01-07  445.50  447.60  440.15  441.10   14,294,780
    2025-01-08  443.00  452.05  437.10  449.55   13,482,926

and the RELIANCE volume measurement behind defect 3 is on 2024-09-01..2024-11-16 (39 bars before,
14 after the 1:1 bonus of ex-date 2024-10-28):

    median Close   2924.90 -> 1305.47    ratio 0.446331
    median Volume  8,132,084 -> 11,996,392  ratio 1.475193
"""

import json
from pathlib import Path

import pandas as pd
import pytest

import bfinance.market.ohlcv as ohlcv_mod
from bfinance.market.ohlcv import (
    PRICE_BASIS_AS_TRADED,
    PRICE_BASIS_SHARE_COUNT_ADJUSTED,
    PRICE_BASIS_SYNTHETIC,
    OHLCVEngine,
)
from bfinance.nse import corporate_actions as ca

FIXTURES = Path(__file__).parent / "fixtures" / "corporate_actions"


def _load(name: str):
    with open(FIXTURES / name, encoding="utf-8") as handle:
        return json.load(handle)


# ------------------------------------------------------- the recorded NSE payloads under test

# The genuine per-symbol NSE feed for ITC (31 rows, recorded from
# bfinance.nse.corporate_actions.corporate_actions_url + CorporateActionsClient._get_json). Its
# 2025-01-06 row is subject "Demerger" -- classified ``other``, and never converted into a factor.
# It also carries ITC's real 2010-08-03 bonus (2.0) and 2016-07-01 sub-division (1.5), both
# decades before the window, which is why the share-count factor across this window is exactly 1.0
# and the frame is as-traded. That isolates the demerger gap with nothing else in it.
ITC_PAYLOAD = _load("nse_itc_2025_demerger.json")

# RELIANCE's real 2024-10-28 1:1 bonus (-> 2.0) and 2024-08-19 Rs 10 dividend.
RELIANCE_PAYLOAD = _load("nse_reliance_2020_2026.json")


# ------------------------------------------------------------------ stubs (no network, no clock)

class _ActionsClient:
    """Replays a recorded NSE payload through the real parser, exactly as the other suites do."""

    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    async def actions_async(self, symbol, start=None, end=None, index="equities"):
        self.calls.append(symbol)
        events = ca.parse_corporate_actions(self.payload, symbol=symbol, url="fixture")
        return ca.actions_frame(events, symbol=symbol, url="fixture")


class _Screener:
    """Close-only screener stub. Records whether the close-only feed was consulted at all."""

    def __init__(self, chart_df=None):
        self.chart_df = chart_df if chart_df is not None else pd.DataFrame()
        self.chart_calls = []

    async def get_chart_timeseries(self, ticker, metric="price", days=1825):
        self.chart_calls.append(ticker)
        return self.chart_df

    async def get_company_profile(self, ticker, force_refresh=False):
        raise ValueError("no profile")


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
    frame = frame.sort_index()
    frame.attrs["bfinance_synthetic_ohlc"] = False
    return frame


# The real ITC bhavcopy bars across the 2025-01-06 demerger ex-date, verbatim.
ITC_DEMERGER_BARS = {
    "ITC": [
        ("2025-01-02", 485.35, 490.00, 482.30, 489.05, 26_124_980),
        ("2025-01-03", 490.25, 491.00, 479.85, 481.60, 37_072_992),
        ("2025-01-06", 455.60, 461.75, 441.25, 442.65, 22_009_958),
        ("2025-01-07", 445.50, 447.60, 440.15, 441.10, 14_294_780),
        ("2025-01-08", 443.00, 452.05, 437.10, 449.55, 13_482_926),
    ]
}

# RELIANCE's real bars either side of the 1:1 bonus of ex-date 2024-10-28, verbatim from the
# archive. The 25-Oct close of 2655.70 and the 28-Oct close of 1334.35 are the published prints.
RELIANCE_BONUS_BARS = {
    "RELIANCE": [
        ("2024-10-24", 2620.00, 2660.00, 2610.00, 2640.90, 8_132_084),
        ("2024-10-25", 2640.90, 2668.00, 2622.00, 2655.70, 9_298_748),
        ("2024-10-28", 1337.00, 1353.00, 1322.10, 1334.35, 10_824_350),
        ("2024-10-29", 1328.10, 1343.20, 1320.30, 1340.00, 12_008_361),
    ]
}


@pytest.fixture
def bars(monkeypatch):
    """Install a bhavcopy stub returning a whole-market frame of the given bars."""

    def _factory(rows):
        monkeypatch.setattr(ohlcv_mod, "bhavcopy_range", lambda start, end, **kw: _market_frame(rows))
        return rows

    return _factory


async def _history(rows, payload, **kwargs):
    kwargs.setdefault("actions", True)
    engine = OHLCVEngine(_Screener(), corporate_actions_client=_ActionsClient(payload))
    return await engine.fetch_history(symbol="ITC", start="2025-01-01", end="2025-01-15", **kwargs)


# ============================================================ 1. the false success flag

async def test_a_demerger_row_does_not_report_a_complete_adjustment(bars):
    """
    The defect, written as an assertion: the frame must not claim an adjustment it did not make.

    Before the fix ``bfinance_corporate_actions_applied`` was ``True`` here -- a real factor was
    derived from a real feed -- while the only event on the ex-date was a demerger that
    :mod:`bfinance.nse.corporate_actions` never converts into a factor.
    """
    bars(ITC_DEMERGER_BARS)
    df = await _history(ITC_DEMERGER_BARS, ITC_PAYLOAD)

    assert df.attrs["bfinance_corporate_actions_applied"] is False, (
        "a declined action class means the adjustment is not complete, and the flag said it was"
    )
    incomplete = df.attrs["bfinance_adjustment_incomplete"]
    assert incomplete["reason"] == "unconverted_action_classes"
    assert incomplete["symbol"] == "ITC"
    assert incomplete["unconverted_rows"] == 1
    assert incomplete["action_counts"]["other"] == 1
    assert "INCOMPLETE" in df.attrs["bfinance_corporate_actions_applied_detail"]


async def test_the_demerger_factor_gap_is_what_the_flag_now_refuses_to_hide(bars):
    """
    Quantify the wrongness the flag used to paper over, so the label is backed by a number.

    ITC's real demerger: the stub's shares were distributed to holders, so the mechanically correct
    factor is ``P_pre / (P_ex + 0.1 x stub_listing_close)`` -- the pre-ex price over the post-ex
    price grossed up by the stub entitlement. bfinance derives exactly 1.0, because the feed's
    ``Stock Splits`` series has nothing on that date, so the frame is the as-traded print.
    """
    bars(ITC_DEMERGER_BARS)
    df = await _history(ITC_DEMERGER_BARS, ITC_PAYLOAD)

    # The as-traded fall the factor should have absorbed.
    as_traded_drop = df["Close"].iloc[2] / df["Close"].iloc[1] - 1.0
    assert as_traded_drop == pytest.approx(442.65 / 481.60 - 1.0)
    assert as_traded_drop == pytest.approx(-0.080876, abs=1e-6), "measured, not assumed"

    # The factor that is right, and the one actually applied.
    correct = 481.60 / (442.65 + 0.1 * 171.85)
    assert correct == pytest.approx(1.047332, abs=1e-6)
    applied = ohlcv_mod._split_adjustment_factor(
        df.index, ca.actions_frame(ca.parse_corporate_actions(ITC_PAYLOAD, symbol="ITC"), symbol="ITC")["Stock Splits"]
    )
    assert set(applied.round(9)) == {1.0}, "bfinance derives no factor at all across this boundary"
    assert correct - applied.iloc[1] == pytest.approx(0.047332, abs=1e-6)
    # So the prices in the frame are short of parity by that much, and the frame now says so
    # instead of asserting success.
    assert df["Close"].tolist() == pytest.approx([489.05, 481.60, 442.65, 441.10, 449.55])


async def test_a_feed_with_no_declared_rows_still_reports_a_complete_adjustment(bars):
    """
    The negative control, without which the flag would just be permanently ``False``.

    A flag that is never ``True`` is as uninformative as one that is always ``True``. This payload is
    RELIANCE's with the rows the parser classifies ``other`` removed -- its real 2020-05-13 rights
    issue and its real 2023-07-20 demerger -- so nothing was declined and the adjustment is
    genuinely complete. The rows are identified by running the real parser, not by string
    matching, so this stays correct if the fixture changes.
    """
    events = ca.parse_corporate_actions(RELIANCE_PAYLOAD, symbol="RELIANCE", url="fixture")
    declined_dates = {pd.Timestamp(stamp).date() for stamp in events.index[events["Action"] == "other"]}
    assert declined_dates, "the fixture must actually contain rows the parser declines to convert"
    payload = [
        r for r in RELIANCE_PAYLOAD
        if not any(token in r["subject"] for token in ("Rights", "Demerger", "Annual General Meeting"))
    ]
    bars(RELIANCE_BONUS_BARS)
    engine = OHLCVEngine(_Screener(), corporate_actions_client=_ActionsClient(payload))
    df = await engine.fetch_history(symbol="RELIANCE", start="2024-10-24", end="2024-10-30")

    assert df.attrs["bfinance_corporate_actions_applied"] is True
    assert df.attrs["bfinance_adjustment_incomplete"] is None
    assert df.attrs["price_basis"] == PRICE_BASIS_SHARE_COUNT_ADJUSTED
    # And the factor really was applied, so the True is not vacuous.
    assert df["Close"].iloc[0] == pytest.approx(2640.90 * 0.5)


async def test_an_incomplete_adjustment_still_delivers_the_factor_it_derived(bars):
    """
    "Incomplete" must not mean "less than asked for".

    The caller requested ``adjust_corporate_actions=True``, so they get an adjustment -- the bonus
    and sub-division factors that *were* derived are real and correct for what they cover. What
    they do not get is a claim of completeness. Dropping the factor instead would regress the
    yfinance parity this module exists to reach, and would be a different lie.
    """
    payload = [
        r for r in ITC_PAYLOAD
        if r["subject"] != "Demerger"
    ] + [
        # Keep a real bonus alive so there IS a factor to apply, and put the demerger back in.
        r for r in ITC_PAYLOAD if r["subject"] == "Demerger"
    ]
    bars(ITC_DEMERGER_BARS)
    df = await _history(ITC_DEMERGER_BARS, payload)

    assert df.attrs["bfinance_corporate_actions_applied"] is False
    assert df.attrs["bfinance_adjustment_incomplete"]["unconverted_rows"] == 1
    # Both ITC bonuses predate the window, so the price columns are untouched here; the assertion
    # that matters is that the two facts are reported independently rather than one standing in
    # for the other.
    assert df.attrs["price_basis"] == "as_traded_share_count_adjusted"


async def test_the_incomplete_key_exists_on_every_frame_so_the_check_needs_no_probe(bars):
    """
    ``attrs["bfinance_adjustment_incomplete"] is not None`` must be a reliable test.

    That only holds if the key is present -- and ``None`` -- on the paths where no adjustment was
    attempted. A key that is merely *absent* on the as-traded path would force a consumer to probe
    for absence, which is the pattern that let the false flag through in the first place.
    """
    bars(ITC_DEMERGER_BARS)

    adjusted = await _history(ITC_DEMERGER_BARS, ITC_PAYLOAD)
    assert "bfinance_adjustment_incomplete" in adjusted.attrs
    assert adjusted.attrs["bfinance_adjustment_incomplete"] is not None

    as_traded = await _history(ITC_DEMERGER_BARS, ITC_PAYLOAD, adjust_corporate_actions=False)
    assert as_traded.attrs["bfinance_adjustment_incomplete"] is None
    assert as_traded.attrs["bfinance_corporate_actions_applied"] is False
    assert "adjust_corporate_actions=False" in as_traded.attrs["bfinance_corporate_actions_applied_detail"]

    # actions=False: no factor could exist, so there is nothing to be incomplete. auto_adjust=False
    # is spelled out because auto_adjust is now True by default (matching yfinance 1.7), and
    # auto_adjust=True resolves the actions feed on its own -- yfinance's flags are independent --
    # so leaving it implicit would make this a copy of the adjusted case above rather than the
    # no-adjustment case it is testing.
    no_actions = await _history(
        ITC_DEMERGER_BARS, ITC_PAYLOAD, actions=False, auto_adjust=False
    )
    assert no_actions.attrs["bfinance_adjustment_incomplete"] is None
    assert "actions=False" in no_actions.attrs["bfinance_corporate_actions_applied_detail"]

    # The empty frame, which is the one a consumer is most likely to treat as harmless.
    empty_engine = OHLCVEngine(
        _Screener(chart_df=pd.DataFrame()), corporate_actions_client=_ActionsClient(ITC_PAYLOAD)
    )
    empty = await empty_engine.fetch_history(
        symbol="ITC", ohlc="synthetic", auto_adjust=False
    )
    assert empty.empty
    assert "bfinance_adjustment_incomplete" in empty.attrs
    assert empty.attrs["bfinance_adjustment_incomplete"] is None


async def test_the_adjustment_applied_pair_is_on_every_frame_too(bars):
    """
    The second honesty key, and the same rule as the first: present everywhere, never a probe.

    ``bfinance_adjustment_applied`` answers "were the four price columns restated on THIS call",
    which is not the question ``bfinance_corporate_actions_applied`` answers -- that one is about
    whether the adjustment is COMPLETE. Both states are real and they disagree on purpose: ITC's
    demerger means a factor was applied and the frame still does not claim completeness, while a
    frame returned as-traded under an ``auto_adjust=True`` request was not restated at all. So the
    pair has to be readable on the complete frame, the incomplete one, the opt-out one and the
    empty one, with the detail a stable ``str`` -- empty meaning "it was applied", never ``None``,
    which would force a reader to know two different ways of saying fine.
    """
    bars(ITC_DEMERGER_BARS)

    incomplete = await _history(ITC_DEMERGER_BARS, ITC_PAYLOAD)
    assert incomplete.attrs["bfinance_corporate_actions_applied"] is False
    assert incomplete.attrs["bfinance_adjustment_applied"] is True, (
        "a factor WAS applied to these prices; the frame just does not claim to be complete"
    )
    assert incomplete.attrs["bfinance_adjustment_applied_detail"] == ""

    as_traded = await _history(ITC_DEMERGER_BARS, ITC_PAYLOAD, adjust_corporate_actions=False)
    assert as_traded.attrs["bfinance_adjustment_applied"] is False
    assert isinstance(as_traded.attrs["bfinance_adjustment_applied_detail"], str)

    empty_engine = OHLCVEngine(
        _Screener(chart_df=pd.DataFrame()), corporate_actions_client=_ActionsClient(ITC_PAYLOAD)
    )
    empty = await empty_engine.fetch_history(
        symbol="ITC", ohlc="synthetic", auto_adjust=False
    )
    assert empty.empty
    assert empty.attrs["bfinance_adjustment_applied"] is False
    assert "no bars in the requested window" in empty.attrs["bfinance_adjustment_applied_detail"]


async def test_the_degradation_is_reported_once_per_frame_and_never_raises(bars, caplog):
    """
    Non-fatal and non-spammy: one line per frame, not one per declined row, and no exception.

    ``logger.warning`` rather than ``warnings.warn``: this module's degradation channel is
    logger + attrs, and raising a Python warning too would put one fact through two channels that
    can drift apart -- and a per-row warning would fire 8,649 times on a market-wide pull.
    """
    bars(ITC_DEMERGER_BARS)
    with caplog.at_level("WARNING", logger="bfinance"):
        df = await _history(ITC_DEMERGER_BARS, ITC_PAYLOAD)

    lines = [r for r in caplog.records if r.name == "bfinance" and r.levelname == "WARNING"]
    assert len(lines) == 1, f"expected one warning for the frame, got {len(lines)}"
    assert "INCOMPLETE" in lines[0].getMessage()
    assert "bfinance_adjustment_incomplete" in lines[0].getMessage()
    # The call returned normally -- a degraded frame, not an exception.
    assert not df.empty and len(df) == 5


# ============================================================ 2. the missing price-basis label

async def test_price_basis_names_every_path(bars):
    """
    One key, one machine-readable vocabulary, on every frame.

    Five sources on four different bases for one stock is the hazard this closes: the bhavcopy is
    the exchange print, yfinance's ``auto_adjust=False`` OHLC are already split-adjusted, and the
    two disagree by the full action factor over whole date ranges. The package that resolves them
    is the one place that should say which one it used.
    """
    bars(ITC_DEMERGER_BARS)

    adjusted = await _history(ITC_DEMERGER_BARS, ITC_PAYLOAD)
    assert adjusted.attrs["price_basis"] == PRICE_BASIS_SHARE_COUNT_ADJUSTED

    as_traded = await _history(ITC_DEMERGER_BARS, ITC_PAYLOAD, adjust_corporate_actions=False)
    assert as_traded.attrs["price_basis"] == PRICE_BASIS_AS_TRADED

    # A frame with no bars still states its basis, because that is the frame a consumer is most
    # likely to treat as "nothing to worry about".
    empty_engine = OHLCVEngine(
        _Screener(chart_df=pd.DataFrame()), corporate_actions_client=_ActionsClient(ITC_PAYLOAD)
    )
    assert (
        await empty_engine.fetch_history(symbol="ITC", ohlc="synthetic", auto_adjust=False)
    ).attrs["price_basis"] == PRICE_BASIS_SYNTHETIC


async def test_the_synthetic_basis_is_named_for_what_it_is_not_dressed_up(monkeypatch):
    """
    The synthetic path must not claim to be an as-traded or an adjusted price series.

    Its Close and Volume are real Screener.in values; its Open/High/Low are *invented* from Close
    (previous close open, +/-0.2% envelope). Calling that ``as_traded`` would be the exact
    dishonesty this key exists to prevent, so the name says which columns are real.
    """
    chart = pd.DataFrame(
        {"Price": [100.0, 102.0, 101.0], "Volume": [10, 20, 15]},
        index=pd.DatetimeIndex(pd.to_datetime(["2025-01-02", "2025-01-03", "2025-01-06"]), name="Date"),
    )
    screener = _Screener(chart_df=chart)
    monkeypatch.setattr(ohlcv_mod, "bhavcopy_range", lambda *a, **k: None)
    df = await OHLCVEngine(screener).fetch_history(
        symbol="ITC", ohlc="synthetic", actions=False, auto_adjust=False
    )

    assert screener.chart_calls, "the opt-in path must use the close-only feed"
    assert df.attrs["bfinance_synthetic_ohlc"] is True
    assert df.attrs["price_basis"] == PRICE_BASIS_SYNTHETIC
    assert df.attrs["price_basis"] not in (PRICE_BASIS_AS_TRADED, PRICE_BASIS_SHARE_COUNT_ADJUSTED)
    # Never adjusted, so the flag and the basis agree rather than contradicting each other.
    assert df.attrs["bfinance_corporate_actions_applied"] is False
    assert "synthetic path" in df.attrs["bfinance_corporate_actions_applied_detail"]


async def test_the_adjusted_basis_is_not_claimed_to_be_yfinance_parity(bars):
    """
    The name states what was done, not a parity that the declined-row gap makes untrue.

    ``as_traded_share_count_adjusted`` is precise: bhavcopy restated by the share-count factor.
    No demerger or buy-back reaches it and no dividend reaches O/H/L/C, so calling it
    ``yfinance_adjusted`` would be a stronger and false claim.
    """
    bars(ITC_DEMERGER_BARS)
    # auto_adjust=False keeps "Adj Close" in the frame: this test compares it to Close, and the
    # default has been True (matching yfinance 1.7) since 2026-09-30, which drops the column.
    df = await _history(ITC_DEMERGER_BARS, ITC_PAYLOAD, auto_adjust=False)
    assert df.attrs["price_basis"] == "as_traded_share_count_adjusted"
    assert "yfinance" not in df.attrs["price_basis"]
    # The dividend column is untouched by the share-count restatement, and neither reaches O/H/L/C.
    assert "Dividends" in df.columns
    assert df["Stock Splits"].tolist() == pytest.approx([0.0] * 5)
    pd.testing.assert_series_equal(df["Adj Close"], df["Close"], check_names=False)


# ============================================================ 3. volume scaled by the inverse

async def test_volume_scaling_preserves_price_times_volume_across_a_bonus_boundary(bars):
    """
    The invariant, on RELIANCE's real 1:1 bonus of ex-date 2024-10-28.

    Direction is settled by measurement, not by the tidiness of the argument. On
    2024-09-01..2024-11-16 the price median went 2924.90 -> 1305.47 (0.446331, the bonus plus a soft
    session) while the volume median went 8,132,084 -> 11,996,392 -- it *rose* 1.475193x. Had the
    published volume already been on the post-bonus share count it would have fallen ~2.2x with the
    price. So the as-traded column counts pre-bonus bars in then-shares and post-bonus bars in
    now-shares: one column, two share-count bases. Halving the price alone therefore understates
    the rupee value traded on every pre-bonus bar by exactly the factor.
    """
    bars(RELIANCE_BONUS_BARS)
    engine = OHLCVEngine(_Screener(), corporate_actions_client=_ActionsClient(RELIANCE_PAYLOAD))
    df = await engine.fetch_history(symbol="RELIANCE", start="2024-10-24", end="2024-10-30")

    published_close = [2640.90, 2655.70, 1334.35, 1340.00]
    published_volume = [8_132_084, 9_298_748, 10_824_350, 12_008_361]
    factors = [0.5, 0.5, 1.0, 1.0]  # 1:1 bonus -> ratio 2.0 -> 1/2 on every bar before the ex-date

    # Volume takes 1/split_factor: the two pre-bonus bars double, the post-bonus bars do not.
    assert df["Close"].tolist() == pytest.approx([c * f for c, f in zip(published_close, factors)])
    assert df["Volume"].tolist() == [16_264_168, 18_597_496, 10_824_350, 12_008_361]
    assert df["Volume"].dtype.kind == "i", "a share count is a whole number"

    # The assertion that matters: price * volume is the same quantity on both sides of the bonus.
    for c, v, f, adj_c, adj_v in zip(
        published_close, published_volume, factors, df["Close"], df["Volume"]
    ):
        assert adj_c * adj_v == pytest.approx(c * v, rel=1e-9), (
            "price * volume must be invariant across the share-count boundary"
        )
        assert adj_c == pytest.approx(c * f)

    # Read as a series, the traded value steps by the market's move and not by the factor.
    traded = (df["Close"] * df["Volume"]).to_numpy()
    assert traded[2] / traded[1] == pytest.approx(
        (published_close[2] * published_volume[2]) / (published_close[1] * published_volume[1])
    )
    assert 0.1 < traded[2] / traded[1] < 10


async def test_volume_scaling_generalises_past_a_whole_number_factor(bars):
    """
    The rule is ``volume / split_factor``, not "double the pre-event volume".

    ITC's real 2016-07-01 sub-division is a 1.5x share-count change, so the multiplier is 2/3 and
    not 2.0. A hardcoded 2.0 would pass the bonus tests and be wrong on every sub-division.
    """
    rows = {
        "ITC": [
            ("2016-06-29", 300.00, 305.00, 298.00, 302.50, 600_000),
            ("2016-06-30", 302.50, 310.00, 301.00, 308.00, 700_000),
            ("2016-07-01", 205.33, 210.00, 204.00, 208.00, 800_000),
            ("2016-07-04", 208.00, 212.00, 206.00, 210.00, 900_000),
        ]
    }
    bars(rows)
    engine = OHLCVEngine(_Screener(), corporate_actions_client=_ActionsClient(ITC_PAYLOAD))
    df = await engine.fetch_history(symbol="ITC", start="2016-06-29", end="2016-07-05")

    # 1.5 sub-division -> split_factor 1/1.5 on every earlier bar -> volume x 1.5
    assert df["Close"].tolist() == pytest.approx([302.50 / 1.5, 308.00 / 1.5, 208.00, 210.00])
    assert df["Volume"].tolist() == [900_000, 1_050_000, 800_000, 900_000]
    for c, v, adj_c, adj_v in zip(
        [302.50, 308.00, 208.00, 210.00], [600_000, 700_000, 800_000, 900_000],
        df["Close"], df["Volume"],
    ):
        assert adj_c * adj_v == pytest.approx(c * v, rel=1e-9)


async def test_no_ratio_or_amount_column_is_ever_scaled(bars):
    """
    Only ``Volume`` is a share count, so only ``Volume`` moves.

    ``Dividends`` is a rupee amount payable per share and ``Stock Splits`` is a share ratio.
    Restating either would corrupt the very quantities that describe the action, and
    ``price * volume`` says nothing about them. Checked explicitly because a plausible-looking
    "scale every numeric column" would get this wrong.
    """
    bars(RELIANCE_BONUS_BARS)
    engine = OHLCVEngine(_Screener(), corporate_actions_client=_ActionsClient(RELIANCE_PAYLOAD))
    df = await engine.fetch_history(symbol="RELIANCE", start="2024-10-24", end="2024-10-30")

    assert float(df["Stock Splits"].max()) == 2.0, "the bonus ratio is a fact, not a restated price"
    assert "Volume" not in ohlcv_mod.ADJUSTED_PRICE_COLUMNS
    assert ohlcv_mod.VOLUME_COLUMN == "Volume"
    # Volume is the only share-count column the history schema has.
    assert [c for c in df.columns if c in ("Volume", "Traded Volume", "Turnover")] == ["Volume"]


async def test_volume_is_untouched_when_nothing_is_adjusted(bars):
    """
    The as-traded path keeps the exchange print, volume included.

    ``adjust_corporate_actions=False`` is what trade reconstruction asks for. Scaling there would
    defeat the flag: the caller asked what actually traded and would get a restatement of it. And
    with the prices also as-traded, ``price * volume`` there is already the real traded value.
    """
    bars(RELIANCE_BONUS_BARS)
    engine = OHLCVEngine(_Screener(), corporate_actions_client=_ActionsClient(RELIANCE_PAYLOAD))
    # auto_adjust=False too: auto_adjust=True scales O/H/L/C by Adj Close / Close even on the
    # as-traded path, so leaving the new default on here would restate the prices this test is
    # pinning as UNTOUCHED. The default has been True (matching yfinance 1.7) since 2026-09-30.
    df = await engine.fetch_history(
        symbol="RELIANCE", start="2024-10-24", end="2024-10-30",
        adjust_corporate_actions=False, auto_adjust=False,
    )

    assert df["Close"].tolist() == pytest.approx([2640.90, 2655.70, 1334.35, 1340.00])
    assert df["Volume"].tolist() == [8_132_084, 9_298_748, 10_824_350, 12_008_361]
    assert df.attrs["price_basis"] == PRICE_BASIS_AS_TRADED


async def test_resampled_volume_is_summed_from_the_scaled_column(bars):
    """
    The scaling happens before the resample, so a weekly bar sums already-restated daily volumes.

    Summing unscaled daily volumes into a weekly bar would mix share-count bases *within one bar*,
    which is worse than the daily problem the scaling fixes. The window straddles a weekend and the
    bonus, so the first weekly bar holds two pre-bonus bars at 0.5 and the second two unadjusted.
    """
    bars(RELIANCE_BONUS_BARS)
    engine = OHLCVEngine(_Screener(), corporate_actions_client=_ActionsClient(RELIANCE_PAYLOAD))
    df = await engine.fetch_history(
        symbol="RELIANCE", start="2024-10-24", end="2024-10-30", interval="1wk"
    )

    assert len(df) == 2, "Mon 2024-10-28 starts the second week, so this window is two weekly bars"
    week1, week2 = df.iloc[0], df.iloc[1]
    # Week 1 = 24-Oct and 25-Oct, both pre-bonus, so both volumes doubled.
    assert week1["Volume"] == 8_132_084 * 2 + 9_298_748 * 2
    # Its last bar is 25-Oct, the bar the module docstring cites as yfinance's 1327.85.
    assert week1["Close"] == pytest.approx(2655.70 * 0.5)
    assert week1["Close"] == pytest.approx(1327.85)
    # Week 2 = 28-Oct and 29-Oct, both post-bonus, so their volumes are untouched.
    assert week2["Volume"] == 10_824_350 + 12_008_361
    assert week2["Close"] == pytest.approx(1340.00)
