"""
The equity-capital split fabrication, and the Screener.in fallback's remaining inferences.

The defect
----------
:meth:`CorporateActionsEngine.extract_splits` used to turn growth in the balance sheet's
``Equity Capital`` row into a stock split, with no split filing required. A capital raise read as a
bonus issue. On a real balance sheet whose equity went 4505 -> 23700 Cr it returned
``{2026-03-31: 5.26}``.

It was reachable from the default ``history()`` path, not just from a direct call:
``market/ohlcv.py`` calls ``resolve_actions_async`` with **no** ``fallback=`` argument, the default
is ``"screener"``, and that branch calls ``extract_splits``. So on the default ``ohlc="bhavcopy"``
path, a symbol the NSE corporate-filings feed does not index fell straight through to the
fabrication. Measured end to end, before the fix:

    Open      Close  Stock Splits
    1.901141  1.901141    0.00     <- 10.0 / 5.26
    1.939163  1.939163    0.00     <- 10.2 / 5.26
   10.200000 10.200000    5.26     <- fabricated, in a user-visible column
   10.400000 10.400000    0.00
    attrs['bfinance_actions_source']           = 'screener_fy_end_approximation'
    attrs['bfinance_corporate_actions_applied'] = True
    attrs['bfinance_synthetic_ohlc']            = False

Two harms at once: a fabricated number in a published column, **and** every bar before the
fabricated date silently rescaled by 1/5.26, while the frame's own provenance flags report real
bars and a complete adjustment. This is the historical "Vodafone Idea reports four fabricated
splits, never having had one" bug. The NSE feed path was fixed earlier; this fallback was not.

Why the fix is deletion and not calibration
-------------------------------------------
``Equity Capital`` is ``shares issued x face value``. At least six different corporate events move
it, and the balance sheet does not say which: a 1:1 bonus, a rights issue, a fresh issue /
preferential allotment, a warrant or ESOP or convertible conversion, a capital reduction or
buy-back cancellation, and -- worst of all -- a face-value sub-division, which multiplies the share
count by 10 while leaving equity capital **unchanged**. A bonus of 1:1 and a rights issue that
raised the same rupee amount are the *same number in the same field*. The signal is not noisy, it
is ambiguous, and no threshold separates the cases. Raising the old ``ratio >= 1.5`` cliff could
only choose which wrong answer to return.

This module is also the non-vacuity control for that deletion: it pins the fabricated result *and*
asserts that the NSE path -- the only source a split is now taken from -- still produces and still
applies a real one.

Fully offline. The NSE feed is a recorded fixture; the price bars are a whole-market bhavcopy stub.
"""

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from bfinance.market import ohlcv as ohlcv_mod
from bfinance.market.corporate import (
    ACTIONS_SOURCE_NONE,
    ACTIONS_SOURCE_SCREENER,
    CorporateActionsEngine,
)
from bfinance.market.ohlcv import OHLCVEngine
from bfinance.models.company import CompanyProfile
from bfinance.models.statements import FinancialStatement
from bfinance.nse import corporate_actions as ca

FIXTURES = Path(__file__).parent / "fixtures" / "corporate_actions"

#: The value the deleted inference produced for the equity-capital path below. Pinned so that a
#: future re-introduction of any equity-capital-to-split rule is caught even if it stops calling the
#: ratio "5.26": the test asserts the *absence* of a split, and this documents what is absent.
FABRICATED_RATIO = 23700.0 / 4505.0  # == 5.2608..., which the old code rounded to 5.26


# ------------------------------------------------------------------ offline stubs


class _StubActionsClient:
    """Stands in for ``CorporateActionsClient`` with a recorded payload. No transport."""

    def __init__(self, payload_file="nse_empty_feed.json"):
        self.payload = json.loads((FIXTURES / payload_file).read_text(encoding="utf-8"))
        self.calls = []

    async def actions_async(self, symbol, start=None, end=None, index="equities"):
        self.calls.append({"symbol": symbol, "start": start, "end": end, "index": index})
        events = ca.parse_corporate_actions(self.payload, symbol=symbol, url="fixture")
        return ca.actions_frame(events, symbol=symbol, url="fixture")


class _StubScreener:
    def __init__(self, profile=None):
        self.profile = profile
        self.profile_calls = []

    async def get_chart_timeseries(self, ticker, metric="price", days=1825):
        return pd.DataFrame()

    async def get_company_profile(self, ticker, force_refresh=False):
        self.profile_calls.append(ticker)
        if self.profile is None:
            raise ValueError("no profile")
        return self.profile


def _market_frame(rows):
    """Whole-market bhavcopy-shaped frame, exactly as ``nse.bhavcopy`` returns it."""
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


def _use_bars(monkeypatch, bars):
    monkeypatch.setattr(
        ohlcv_mod, "bhavcopy_range", lambda start, end, **kw: _market_frame(bars)
    )


# A capital raise and nothing else. The prices are the *published* bhavcopy prints, and the whole
# point of the "no bar is rescaled" test below is that they come back out bit for bit.
#
# Dates are chosen so the fabricated FY-end date 2026-03-31 falls INSIDE this window -- the case
# where the fabrication is at least visible in the ``Stock Splits`` column. The far worse case, the
# fabricated date falling outside the window so the column reads 0.0 while the prices are still
# rescaled, is its own test below.
CAPITAL_RAISE_BARS = {
    "IDEA": [
        ("2026-03-25", 10.0, 10.0, 10.0, 10.0, 100),
        ("2026-03-26", 10.0, 10.2, 10.0, 10.2, 200),
        ("2026-03-31", 10.2, 10.2, 10.2, 10.2, 300),
        ("2026-04-01", 10.2, 10.4, 10.2, 10.4, 400),
    ]
}

# A window containing RELIANCE's real 28-Oct-2024 1:1 bonus (-> 2.0). Every bar before the ex-date
# must be halved by it. This is the working path the fix must not break.
BONUS_BARS = {
    "RELIANCE": [
        ("2024-08-16", 2900.0, 2970.0, 2890.0, 2956.40, 1_000),
        ("2024-08-19", 2956.40, 2990.0, 2940.0, 2976.80, 2_000),
        ("2024-10-25", 2600.0, 2670.0, 2590.0, 2655.70, 4_000),
        ("2024-10-28", 2655.70, 2700.0, 1320.0, 1334.35, 5_000),
    ]
}


def _capital_raise_profile(headers=("Mar 2025", "Mar 2026"), capital=(4505.0, 23700.0)):
    """A profile whose only signal is that equity capital grew 5.26x. No split was ever filed."""
    return CompanyProfile(
        symbol="IDEA",
        company_id=1,
        name="Vodafone Idea",
        balance_sheet=FinancialStatement(
            headers=list(headers), rows={"Equity Capital": list(capital)}
        ),
    )


async def _history(actions_client, profile=None, symbol="IDEA.NS", **kwargs):
    engine = OHLCVEngine(_StubScreener(profile=profile), corporate_actions_client=actions_client)
    return await engine.fetch_history(
        symbol=symbol, period="5d", actions=True, auto_adjust=False, **kwargs
    )


# ------------------------------------------------- 1. THE REGRESSION: no split from equity capital


def test_equity_capital_growth_is_not_a_split():
    """The regression test. 4505 -> 23700 Cr of equity capital is a capital raise, not a 5.26x split.

    This is the exact input that made the historical report true: the deleted code returned
    ``{2026-03-31: 5.26}``.
    """
    splits = CorporateActionsEngine.extract_splits(_capital_raise_profile())

    assert splits.empty, (
        "growth in 'Equity Capital' produced a split series again: "
        f"{dict(splits)}. Equity capital moves for at least six different reasons "
        "(1:1 bonus, rights issue, fresh issue, warrant/ESOP/conversion, capital reduction, and a "
        "face-value sub-division that leaves equity capital UNCHANGED while the share count "
        "decuples), and the balance sheet cannot say which. The inference is deleted, not tuned."
    )


@pytest.mark.parametrize(
    "capital,headers,note",
    [
        ((4505.0, 23700.0), ("Mar 2025", "Mar 2026"), "the measured capital raise (5.26x)"),
        ((100.0, 200.0), ("Mar 2025", "Mar 2026"), "exactly 2.0x -- what a 1:1 bonus looks like"),
        ((50.0, 150.0), ("Mar 2025", "Mar 2026"), "3.0x rights issue at a premium"),
        ((10.0, 15.0), ("Mar 2025", "Mar 2026"), "1.5x, sitting exactly on the old threshold"),
        ((10.0, 1000.0), ("Mar 2025", "Mar 2026"), "100x, a large preferential allotment"),
    ],
)
def test_no_equity_capital_shape_yields_a_split(capital, headers, note):
    """Calibrating the old ``ratio >= 1.5`` cliff cannot work, so no ratio is accepted at all.

    Note the second row: exactly 2.0x is what a genuine 1:1 bonus looks like on a balance sheet, and
    it is *indistinguishable* from a rights issue that raised the same amount. Any threshold that
    admits the bonus must admit the rights issue, and any that rejects it is already wrong. That is
    the whole argument for deletion in one parametrisation -- and the reason this test has no
    "acceptable ratio" row to balance the others against.
    """
    splits = CorporateActionsEngine.extract_splits(
        _capital_raise_profile(headers=headers, capital=capital)
    )
    assert splits.empty, f"{note}: {dict(splits)}"


async def test_equity_capital_growth_never_reaches_the_resolver():
    """Same thing one layer up: the fallback resolves no split series at all.

    Asserted on ``ResolvedActions.splits`` rather than on a frame, so the failure names the
    fallback rather than the price columns.
    """
    resolved = await CorporateActionsEngine.resolve_actions_async(
        "IDEA", client=_StubActionsClient(), profile=_capital_raise_profile()
    )
    assert resolved.splits.empty, dict(resolved.splits)
    assert resolved.source == ACTIONS_SOURCE_SCREENER  # the dividends really are the screener's


# ------------------------------------------------------- 2. THE WORKING PATH STILL WORKS


async def test_a_genuine_nse_filing_still_produces_its_split():
    """The NSE feed declares RELIANCE's real 1:1 bonus of 2024-10-28. It must still be read.

    This is the non-vacuity control for the deletion above: a split is a *declared* fact, read from
    a filing by :func:`bfinance.nse.corporate_actions.split_ratio`, and that is still the only
    place one comes from.
    """
    resolved = await CorporateActionsEngine.resolve_actions_async(
        "RELIANCE", client=_StubActionsClient("nse_reliance_2020_2026.json")
    )

    assert not resolved.splits.empty, "the recorded 1:1 bonus vanished from the NSE path"
    ex_date = pd.Timestamp("2024-10-28", tz="Asia/Kolkata")
    assert float(resolved.splits.loc[ex_date]) == pytest.approx(2.0)
    # And the price columns it drives are still there: a real split is never turned into a
    # restricted "not convertible" row.
    assert float(resolved.dividends.loc[pd.Timestamp("2024-08-19", tz="Asia/Kolkata")]) == (
        pytest.approx(10.0)
    )


async def test_a_genuine_split_is_still_applied_to_the_bars(monkeypatch):
    """End to end: the recorded 1:1 bonus still halves every bar before its ex-date.

    Deliberately the default path (``adjust_corporate_actions`` left on), so it exercises the real
    ``resolve_actions_async`` -> ``_split_adjustment_factor`` -> column scaling chain rather than
    the as-traded view.
    """
    _use_bars(monkeypatch, BONUS_BARS)
    df = await _history(
        _StubActionsClient("nse_reliance_2020_2026.json"),
        profile=_capital_raise_profile(),          # loaded but must be ignored: NSE indexes RELIANCE
        symbol="RELIANCE.NS",
    )

    assert float(df["Stock Splits"].sum()) == pytest.approx(2.0)
    published = _market_frame(BONUS_BARS).xs("RELIANCE", level="Symbol")["Close"].to_numpy()
    # Bars 0-2 precede the 2024-10-28 ex-date and carry 1/2. The ex-date bar itself carries 1.0.
    np.testing.assert_allclose(df["Close"].to_numpy()[:3], published[:3] * 0.5, rtol=1e-12)
    assert float(df["Close"].iloc[3]) == pytest.approx(published[3])
    # Volume takes the inverse factor, so price * volume -- the rupee value traded -- is the same
    # quantity on both sides of the share-count event. Tolerance covers the rint back to int64.
    raw_volume = _market_frame(BONUS_BARS).xs("RELIANCE", level="Symbol")["Volume"].to_numpy()
    np.testing.assert_allclose(
        df["Close"].to_numpy()[:3] * df["Volume"].to_numpy()[:3],
        (published * raw_volume)[:3],
        rtol=1e-3,
    )


# ------------------------------------------- 3. NO BAR IS RESCALED BY THE REMOVED INFERENCE


async def test_no_bar_is_rescaled_by_the_removed_inference(monkeypatch):
    """The published prints come back bit-identical, and ``Stock Splits`` is 0.0 throughout.

    Before the fix these four bars were returned as 1.901141 / 1.939163 / 10.2 / 10.4 with
    ``Stock Splits`` reading 5.26 on the third. The first two are 10.0 and 10.2 divided by the
    fabricated ratio, i.e. the capital raise silently restated the price series.
    """
    _use_bars(monkeypatch, CAPITAL_RAISE_BARS)
    df = await _history(_StubActionsClient(), profile=_capital_raise_profile())

    published = _market_frame(CAPITAL_RAISE_BARS).xs("IDEA", level="Symbol")
    for column in ("Open", "High", "Low", "Close"):
        np.testing.assert_array_equal(
            df[column].to_numpy(),
            published[column].to_numpy(),
            err_msg=f"{column} was rescaled by a fabricated split",
        )
    np.testing.assert_array_equal(
        df["Volume"].to_numpy(), published["Volume"].to_numpy(),
        err_msg="Volume was rescaled by the inverse of a fabricated split",
    )
    assert df["Stock Splits"].tolist() == [0.0, 0.0, 0.0, 0.0]
    assert df["Close"].tolist() == [10.0, 10.2, 10.2, 10.4]
    assert df.attrs["bfinance_synthetic_ohlc"] is False, "the bars are the real bhavcopy prints"


async def test_a_fabrication_outside_the_bar_window_no_longer_rescales_anything(monkeypatch):
    """The silent case, and the worse of the two.

    When the fabricated date falls *after* every bar, ``Stock Splits`` reads 0.0 on every row -- so
    the column looks clean -- while the cumulative share-count factor still restated the whole
    price series. A consumer reading the ``Stock Splits`` column had no way to see it. Here the
    fabricated date is 2027-03-31, well after the 2026-03 window.
    """
    _use_bars(monkeypatch, CAPITAL_RAISE_BARS)
    profile = _capital_raise_profile(
        headers=("Mar 2026", "Mar 2027"), capital=(4505.0, 23700.0)
    )
    df = await _history(_StubActionsClient(), profile=profile)

    published = _market_frame(CAPITAL_RAISE_BARS).xs("IDEA", level="Symbol")
    assert df["Stock Splits"].tolist() == [0.0, 0.0, 0.0, 0.0], "the column already looked clean"
    np.testing.assert_array_equal(df["Close"].to_numpy(), published["Close"].to_numpy())
    np.testing.assert_array_equal(df["Volume"].to_numpy(), published["Volume"].to_numpy())


# ------------------------------- 4. THE FALLBACK SIGNALS WHAT IT CANNOT DETERMINE


async def test_the_fallback_states_that_the_share_count_is_not_observable(monkeypatch):
    """The specific key and value a caller reads when the fallback cannot determine a split.

    A Screener.in P&L has no share-count field at all, so its silence on splits is not "none
    happened" -- it is "this source cannot see them". That is what these keys say, and they are
    deliberately emitted only on the path where a real source is named but cannot underwrite a
    share-count factor: on the ``ACTIONS_SOURCE_NONE`` path the source tag already says it.
    """
    _use_bars(monkeypatch, CAPITAL_RAISE_BARS)
    df = await _history(_StubActionsClient(), profile=_capital_raise_profile())

    assert df.attrs["bfinance_actions_share_count_observable"] is False
    assert (
        df.attrs["bfinance_actions_share_count_source"] == "not_observable_from_screener_pnl"
    )
    # The dividends are still attributed to the screener, and the frame still says why it is here.
    assert df.attrs["bfinance_actions_source"] == ACTIONS_SOURCE_SCREENER
    assert "does not index this instrument" in df.attrs["bfinance_actions_fallback_reason"]


async def test_the_share_count_blindness_is_carried_on_the_resolution_too():
    """Same fact at the API boundary, for callers that never touch ``ohlcv``."""
    resolved = await CorporateActionsEngine.resolve_actions_async(
        "IDEA", client=_StubActionsClient(), profile=_capital_raise_profile()
    )
    assert resolved.extra["bfinance_actions_share_count_observable"] is False
    assert resolved.extra["bfinance_actions_share_count_source"] == (
        "not_observable_from_screener_pnl"
    )


async def test_a_source_that_can_see_splits_does_not_claim_blindness():
    """The negative control on the flag itself: it is not permanently ``False``.

    The NSE branch carries no such key, because the feed demonstrably indexes the symbol -- that is
    the precondition for reading a share-count event off it. A flag that is always set would carry
    no information.
    """
    resolved = await CorporateActionsEngine.resolve_actions_async(
        "RELIANCE", client=_StubActionsClient("nse_reliance_2020_2026.json")
    )
    assert "bfinance_actions_share_count_observable" not in resolved.extra


async def test_no_source_at_all_reports_no_source_rather_than_a_blind_one(monkeypatch):
    """With the fallback switched off, the tag says ``none`` and the blindness key is absent.

    Redundant with the tag, and that is the point: the flag exists for the case the tag cannot
    express, so it must not be spread onto the case the tag already gets right.
    """
    _use_bars(monkeypatch, CAPITAL_RAISE_BARS)
    resolved = await CorporateActionsEngine.resolve_actions_async(
        "IDEA", client=_StubActionsClient(), fallback="none"
    )
    assert resolved.source == ACTIONS_SOURCE_NONE
    assert "bfinance_actions_share_count_observable" not in resolved.extra


@pytest.mark.xfail(
    reason=(
        "needs market/ohlcv.py, owned by another agent. The screener fallback names a real source "
        "and cannot supply a share-count factor, so _resolve_adjustment returns applied=True and "
        "price_basis reports 'as_traded_share_count_adjusted' for a frame whose O/H/L/C were never "
        "rescaled (factor exactly 1.0). Fix: have _resolve_adjustment consult "
        "extra['bfinance_actions_share_count_observable'] and return applied=False with a reason "
        "naming the blindness, so bfinance_corporate_actions_applied is False and "
        "bfinance_adj_close_source / price_basis stop claiming a share-count factor."
    ),
    strict=False,
)
async def test_the_screener_frame_does_not_claim_a_share_count_adjustment_it_never_made(
    monkeypatch,
):
    _use_bars(monkeypatch, CAPITAL_RAISE_BARS)
    df = await _history(_StubActionsClient(), profile=_capital_raise_profile())
    assert df.attrs["bfinance_corporate_actions_applied"] is False, (
        "still claims a complete adjustment while carrying no share-count factor at all"
    )


# --------------------------------- 5. THE OTHER FALLBACK INFERENCE: PAYOUT % x EPS, PINNED


def _dividend_profile(payout=50.0, eps=20.0, headers=("Jun 2024",)):
    return CompanyProfile(
        symbol="RELIANCE",
        company_id=1,
        name="Reliance",
        profit_loss=FinancialStatement(
            headers=list(headers),
            rows={"Dividend Payout %": [payout] * len(headers), "EPS in Rs": [eps] * len(headers)},
        ),
    )


def test_the_dividend_approximation_is_an_identity_not_a_guess():
    """Why this one is KEPT, while the equity-capital ratio is deleted.

    ``Dividend Payout %`` is *defined* as dividend per share over EPS. Inverting it recovers DPS
    from two fields Screener publishes for the same period, so the amount is algebraically sound
    rather than a correlation -- the amount tracks yfinance within ~5%. The equity-capital ratio has
    no such identity: it is not a restatement of anything, it merely happens to coincide for one of
    six event types. That is the entire difference between the two inferences.
    """
    dividends = CorporateActionsEngine.extract_dividends(_dividend_profile(payout=50.0, eps=20.0))
    assert float(dividends.iloc[0]) == pytest.approx(10.0)  # 0.50 * 20
    assert str(dividends.index.tz) == "Asia/Kolkata"


def test_the_dividend_approximation_pins_its_failure_mode_the_date():
    """The approximation is kept, so its failure mode is pinned and not just its happy path.

    It is dated at the **fiscal year end**, which is 4-5 months before the true ex-date. For
    RELIANCE's real 2024-08-19 ex-date it produces 2024-06-30, so the ex-date itself is absent from
    the series. That is the reason the path is tagged ``screener_fy_end_approximation`` and never
    merged with the NSE series.
    """
    dividends = CorporateActionsEngine.extract_dividends(_dividend_profile(headers=("Jun 2024",)))
    assert pd.Timestamp(dividends.index[0]) == pd.Timestamp("2024-06-30", tz="Asia/Kolkata")

    true_ex_date = pd.Timestamp("2024-08-19", tz="Asia/Kolkata")
    assert true_ex_date not in dividends.index, "if this ever fails, the approximation improved"


async def test_and_the_under_adjustment_that_missing_date_causes(monkeypatch):
    """The consequence of that missing date, measured: a window on the ex-date gets nothing.

    ``Adj Close`` comes back a copy of ``Close``. To a consumer reading only the prices that is
    indistinguishable from "this company paid no dividend" -- which is precisely why the path is
    labelled and never presented as parity.
    """
    _use_bars(
        monkeypatch, {"RELIANCE": [("2024-08-19", 2956.40, 2990.0, 2940.0, 2976.80, 2_000)]}
    )
    df = await _history(
        _StubActionsClient(),
        profile=_dividend_profile(headers=("Jun 2024",)),
        symbol="RELIANCE.NS",
    )

    assert float(df["Dividends"].iloc[0]) == 0.0
    assert float(df["Adj Close"].iloc[0]) == pytest.approx(float(df["Close"].iloc[0]))
    # And the frame says which source produced that non-answer, so it cannot be read as parity.
    assert df.attrs["bfinance_actions_source"] == ACTIONS_SOURCE_SCREENER


def test_a_negative_eps_can_never_become_a_dividend():
    """A second, provable guard on the kept approximation.

    ``0.40 * -5.0 == -2.0``: a payout ratio left positive from an earlier profitable year, paired
    with a loss year's EPS, yields a *negative* dividend. No company pays a negative dividend, so
    that number is not a noisy estimate -- it is not a dividend at all. Left in, it would reach the
    user-visible ``Dividends`` column as -2.0, and ``_dividend_adjustment_factor`` would turn it
    into a step of ``1 - (-2.0)/prev_close > 1`` and inflate the price history.
    """
    assert 0.40 * -5.0 == -2.0, "documents the arithmetic the guard rejects"
    assert CorporateActionsEngine.extract_dividends(_dividend_profile(payout=40.0, eps=-5.0)).empty





