"""
``auto_adjust=True`` must WORK, in yfinance's shape, or be REFUSED for a named reason.

The defect this replaces
------------------------
``auto_adjust=True`` used to raise :class:`AdjustmentNotAvailableError` unconditionally
(``market/ohlcv.py``, the top of ``fetch_history``). The stated reason -- "bfinance derives no
adjustment factor" -- had stopped being true some time earlier: the NSE ex-dated
corporate-actions feed supplies real dividends (true ex-date, rupees actually payable) and real
bonus / face-value-sub-division ratios, they already drive ``Adj Close`` on the default path, and
``Adj Close`` is by construction the share-count factor times the dividend factor. Meanwhile
yfinance 1.7 **defaults** ``auto_adjust`` to ``True``, so the package died on the value its
closest competitor reaches by saying nothing -- which is the opposite of drop-in.

So ``True`` is now implemented, using yfinance's own arithmetic rather than a bfinance one
(``yfinance.utils.auto_adjust``): ``ratio = "Adj Close" / "Close"``, scale Open/High/Low/Close by
it, drop ``Adj Close``, leave Volume and the action columns alone. The **default stays ``False``**
-- pinned by ``tests/test_adjust_default_drift.py`` and documented in ``docs/API_REFERENCE.md`` --
because ``False`` is the strictly richer answer, and a caller who wants yfinance's literal column
set is the one who should have to ask for it.

What is still refused
---------------------
Scaling four price columns by an ``Adj Close`` that is not an adjusted series is the one output
this package exists to not produce: it would restate history by a number that never existed. The
refusal therefore became a gate on the PROVENANCE of the actions, not on the keyword, and it
still names which of the cases it is. Each case has a test below; a refusal nobody can act on is
just a different failure.

Offline by construction. The bhavcopy reader is stubbed and the actions stub replays a
**recorded** NSE payload through the real parser, so the ex-date and rupee amount under test are
NSE's and not a plausible-looking guess.

Provenance of the hard numbers
------------------------------
RELIANCE's real 19-Aug-2024 ex-date carried Rs 10 against the close on the trading day before it,
2956.40, so ``1 - D / prev_close`` is exactly 0.99661764 and the two bars before the ex-date are
restated by it while the ex-date bar itself is not. That ratio is not 1.0, which is what makes the
central test non-vacuous: a column drop that scaled nothing would pass a ratio-1.0 fixture, and
this one would not.
"""

import json
from pathlib import Path

import pandas as pd
import pytest

import bfinance.market.ohlcv as ohlcv_mod
from bfinance.market.corporate import ACTIONS_SOURCE_NSE
from bfinance.market.ohlcv import (
    AdjustmentNotAvailableError,
    OHLCVEngine,
)
from bfinance.models.company import CompanyProfile
from bfinance.models.statements import FinancialStatement
from bfinance.nse import corporate_actions as ca

FIXTURES = Path(__file__).parent / "fixtures" / "corporate_actions"

#: RELIANCE's real per-symbol NSE feed, recorded. Carries the genuine 2024-08-19 Rs 10 dividend,
#: the 2024-10-28 1:1 bonus (-> share-count factor 2.0, so the raw bars come back halved) and the
#: 2020-05-13 rights issue / 2023-07-20 demerger this engine deliberately declines to convert.
RELIANCE_PAYLOAD = json.load(
    open(FIXTURES / "nse_reliance_2020_2026.json", encoding="utf-8")
)

#: Real published bhavcopy prints either side of the 19-Aug-2024 dividend ex-date, verbatim from
#: the archive (the same three bars tests/test_nse_bhavcopy_wiring.py uses). The ex-date bar is not
#: restated by its own dividend; the bar before it is, which is the whole point of the fixture.
RELIANCE_DIVIDEND_BARS = {
    "RELIANCE": [
        ("2024-08-16", 2900.00, 2970.00, 2890.00, 2956.40, 8_132_084),
        ("2024-08-19", 2956.40, 2990.00, 2940.00, 2976.80, 9_298_748),
        ("2024-08-20", 2976.80, 3010.00, 2960.00, 3000.00, 10_824_350),
    ]
}

WINDOW = {"start": "2024-08-16", "end": "2024-08-21"}

#: Spelled out as a literal so the message assertion reads the way the message does, rather than
#: comparing two references to the same constant.
ACTIONS_SOURCE_NONE_TXT = "none"

#: 1 - 10 / 2956.40, measured off NSE's own ex-date and its own prior close. Asserted rather than
#: left implicit, because every claim in the central test below is downstream of this number.
EXPECTED_DIVIDEND_FACTOR = 1.0 - 10.0 / 2956.40


# ------------------------------------------------------------------------- stubs (no network)

class _NseActions:
    """Replays the recorded NSE payload through the real parser. No network, no clock."""

    def __init__(self, payload=None):
        self.payload = RELIANCE_PAYLOAD if payload is None else payload
        self.calls = []

    async def actions_async(self, symbol, start=None, end=None, index="equities"):
        self.calls.append(symbol)
        events = ca.parse_corporate_actions(self.payload, symbol=symbol, url="fixture")
        return ca.actions_frame(events, symbol=symbol, url="fixture")


class _NoRowsActions:
    """The feed answered, and it does not index this instrument. ``actions_source`` -> fallback."""

    async def actions_async(self, symbol, start=None, end=None, index="equities"):
        return pd.DataFrame(columns=["Dividends", "Stock Splits", "Action"])


class _Screener:
    """Close-only screener stub. ``profile`` decides which corporate-actions fallback is reached."""

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


def _screener_profile():
    """A profile with a dividend payout, so the FY-end approximation has something to date."""
    return CompanyProfile(
        symbol="RELIANCE", company_id=1, name="Reliance",
        profit_loss=FinancialStatement(
            headers=["Mar 2024"], rows={"Dividend Payout %": [50.0]}
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
    """Install the bhavcopy stub, or install an EMPTY archive for the empty-frame case."""

    def _factory(rows=RELIANCE_DIVIDEND_BARS):
        monkeypatch.setattr(
            ohlcv_mod, "bhavcopy_range", lambda start, end, **kw: _market_frame(rows)
        )

    return _factory


def _engine(actions=None, screener=None):
    return OHLCVEngine(
        screener or _Screener(),
        corporate_actions_client=actions if actions is not None else _NseActions(),
    )


async def _history(**kwargs):
    kwargs.setdefault("actions", True)
    return await _engine().fetch_history(symbol="RELIANCE", **WINDOW, **kwargs)


# =================================================== the behaviour: True now works, in yfinance's shape

async def test_auto_adjust_true_lands_on_the_adjusted_series_and_drops_adj_close(bars):
    """
    The central claim, as an assertion: ``auto_adjust=True`` returns the ADJUSTED prices, not the
    unadjusted ones under a shorter column list.

    Before this change the call raised. The first version of the fix that people reach for first
    is to simply accept the keyword -- ``_history_columns`` already omits ``Adj Close`` when
    ``auto_adjust`` is True, so a one-line change makes the tests about columns go green while
    every price is untouched. That version is what this test is written to kill: it asserts the
    VALUES moved, and by the exact factor NSE's ex-date implies.
    """
    bars()
    adjusted = await _history(auto_adjust=False)
    auto = await _history(auto_adjust=True)

    assert "Adj Close" not in auto.columns, "yfinance's auto_adjust=True has no Adj Close column"
    # yfinance's column set, in yfinance's order. Pinned exactly, because a column list is a
    # contract a drop-in caller indexes positionally as often as by name.
    assert list(auto.columns) == ["Open", "High", "Low", "Close", "Volume", "Dividends", "Stock Splits"]

    # The load-bearing assertion: auto_adjust=True's Close IS the former Adj Close. rel=1e-12
    # rather than exact because this is `Close * (AdjClose / Close)`, which is Adj Close up to one
    # float rounding. The dividend factor under test moves the price by 0.34%, so a 1e-12 relative
    # tolerance is ten orders of magnitude tighter than the effect and cannot hide its absence.
    assert auto["Close"].tolist() == pytest.approx(
        adjusted["Adj Close"].tolist(), rel=1e-12
    ), "auto_adjust=True must return the adjusted series, not the raw one"

    # And the ratio is NOT 1.0, so the assertion above cannot be satisfied by a column drop. The
    # bar before the ex-date is restated by NSE's real Rs 10 dividend; the ex-date bar and the one
    # after it are not, which is what makes the fixture a test rather than a tautology.
    ratio = (adjusted["Adj Close"] / adjusted["Close"]).tolist()
    assert ratio == pytest.approx([EXPECTED_DIVIDEND_FACTOR, 1.0, 1.0], rel=1e-12), (
        f"the dividend factor is not the one NSE's ex-date implies: {ratio}"
    )
    assert any(abs(r - 1.0) > 1e-6 for r in ratio), (
        "a fixture whose ratio is 1.0 everywhere would let a column drop pass as an adjustment"
    )


async def test_all_four_price_columns_are_scaled_by_the_same_ratio(bars):
    """
    Not just Close. A fix that moved Close alone would satisfy the test above and leave a bar whose
    high is below its open after an adjustment -- an impossible bar that no feed has ever printed.
    """
    bars()
    adjusted = await _history(auto_adjust=False)
    auto = await _history(auto_adjust=True)

    ratio = (adjusted["Adj Close"] / adjusted["Close"]).astype(float)
    for column in ("Open", "High", "Low", "Close"):
        assert auto[column].tolist() == pytest.approx(
            (adjusted[column].astype(float) * ratio).tolist(), rel=1e-12
        ), f"{column} was not scaled by Adj Close / Close"
    # The bar is still a bar: high >= low, and high >= both open and close, after the adjustment.
    assert (auto["High"] >= auto["Low"]).all()
    assert (auto["High"] >= auto[["Open", "Close"]].max(axis=1)).all()


async def test_volume_and_the_action_columns_are_left_exactly_as_they_were(bars):
    """
    Volume is a share count and the action columns describe the actions; neither is a price.

    Volume has already been restated by the INVERSE share-count factor on this path, which is
    what keeps ``price * volume`` continuous across a bonus. Multiplying it by the dividend ratio
    as well would make it a share count times a yield, which is not a quantity that exists.
    ``Dividends`` is a rupee amount payable per share and ``Stock Splits`` is a share ratio --
    restating either would corrupt the very numbers that describe the event.
    """
    bars()
    adjusted = await _history(auto_adjust=False)
    auto = await _history(auto_adjust=True)

    assert auto["Volume"].tolist() == adjusted["Volume"].tolist()
    assert auto["Dividends"].tolist() == adjusted["Dividends"].tolist()
    assert auto["Stock Splits"].tolist() == adjusted["Stock Splits"].tolist()
    assert "Volume" in ohlcv_mod.VOLUME_COLUMN
    assert "Volume" not in ohlcv_mod.ADJUSTED_PRICE_COLUMNS


async def test_the_as_traded_path_also_lands_on_the_adjusted_series(bars):
    """
    ``adjust_corporate_actions=False`` plus ``auto_adjust=True`` is a legitimate combination and it
    must give the SAME answer as the default path, not a different one.

    On the as-traded path Close carries no share-count factor, so ``Adj Close / Close`` there is
    the full share-count x dividend factor; on the adjusted path Close already carries the
    share-count factor and the ratio reduces to the dividend factor. Both spellings are the same
    number, and the caller asked the same question either way -- so writing the scaling as the
    measured ratio rather than as ``split_factor * dividend_factor`` is what makes the two paths
    agree instead of drifting apart by however much the share-count factor happened to be.
    """
    bars()
    on_default = await _history(auto_adjust=True)
    on_as_traded = await _history(auto_adjust=True, adjust_corporate_actions=False)

    assert on_as_traded["Close"].tolist() == pytest.approx(
        on_default["Close"].tolist(), rel=1e-9
    )
    # And the as-traded frame really was as-traded, or the comparison above is vacuous. On this
    # path the price columns are left as the exchange printed them (2956.40) while "Adj Close"
    # still carries the FULL adjusted series, share-count factor and dividend factor both -- which
    # is what makes the measured ratio on it the whole factor rather than just the dividend's.
    as_traded = await _history(auto_adjust=False, adjust_corporate_actions=False)
    assert as_traded.attrs["price_basis"] == "as_traded"
    assert as_traded["Close"].iloc[0] == pytest.approx(2956.40)
    assert as_traded["Adj Close"].iloc[0] == pytest.approx(
        2956.40 * 0.5 * EXPECTED_DIVIDEND_FACTOR, rel=1e-9
    ), "the 2024-10-28 1:1 bonus halves the share count and the Rs 10 dividend restates on top"
    assert on_as_traded["Close"].iloc[0] == pytest.approx(
        as_traded["Adj Close"].iloc[0], rel=1e-9
    )


async def test_omitting_the_keyword_returns_yfinances_own_frame(bars):
    """
    The default, asserted rather than assumed: no kwarg means ``True``, exactly as in yfinance 1.7.

    It used to mean ``False``, on the argument that ``False`` is the strictly richer answer --
    it keeps ``Adj Close``, and every adjusted price in the frame is recoverable from it. That
    argument was real and it lost anyway: measured live against yfinance 1.7.0 in this venv, a
    bare ``history()`` returns ``['Open', 'High', 'Low', 'Close', 'Volume', 'Dividends',
    'Stock Splits']`` where bfinance returned eight columns including ``Adj Close``. A drop-in
    that hands back columns its closest competitor never returns is not a drop-in, and the
    adjustment ``True`` asks for was already real and already applied on that path. So ``False``
    is still supported -- it is how you ask for ``Adj Close`` -- it is simply no longer what
    saying nothing gets you.
    """
    bars()
    implicit = await _history()
    explicit = await _history(auto_adjust=True)

    assert list(implicit.columns) == list(explicit.columns)
    assert "Adj Close" not in implicit.columns
    assert implicit["Close"].tolist() == explicit["Close"].tolist()
    # The richer frame is still one keyword away, and it still carries the full adjusted series.
    richer = await _history(auto_adjust=False)
    assert "Adj Close" in richer.columns
    assert implicit["Close"].tolist() == pytest.approx(richer["Adj Close"].tolist(), rel=1e-12)
    # Pinned by reflection across every public entry point too; see
    # tests/test_adjust_default_drift.py. This is the behavioural half of that guard.
    assert "Adj Close" not in (await _history(auto_adjust=True)).columns


# ================================================ the refusals that were KEPT, each with its reason

async def test_auto_adjust_true_is_refused_when_no_ex_dated_feed_answered(bars):
    """
    The refusal this change must NOT remove, and the reason it exists.

    With no ex-dated corporate-actions feed, ``Adj Close`` is not an adjusted series -- it is
    ``Close`` with a factor of 1.0 baked in, or nothing at all. Scaling four price columns by it
    would return the unadjusted prices under an "adjusted" column list, which is the precise
    defect the unconditional raise was originally written to prevent, and the first version of
    this fix would have shipped it. The screener here has NO profile, so the FY-end approximation
    is unreachable too and ``bfinance_actions_source`` reads ``none``.
    """
    bars()
    engine = OHLCVEngine(_Screener(), corporate_actions_client=_NoRowsActions())

    # First prove the fixture is in the state the test claims: the same call WITHOUT auto_adjust
    # returns a frame that says its actions source is "none". Without this, a stub that quietly
    # started answering would make the refusal pass for the wrong reason. auto_adjust=False is
    # spelled out because auto_adjust is now True by default and this line is about the SOURCE,
    # not about the adjustment.
    plain = await engine.fetch_history(
        symbol="RELIANCE", actions=True, auto_adjust=False, **WINDOW
    )
    assert plain.attrs["bfinance_actions_source"] == "none"

    with pytest.raises(AdjustmentNotAvailableError) as exc:
        await engine.fetch_history(symbol="RELIANCE", actions=True, auto_adjust=True, **WINDOW)
    message = str(exc.value)
    assert "no corporate-actions feed with real" in message, message
    assert ACTIONS_SOURCE_NONE_TXT in message, message


async def test_auto_adjust_true_is_refused_on_the_screener_fiscal_year_end_approximation(bars):
    """
    The other feed that answers and is still not an adjustment.

    The Screener.in fallback dates dividends at fiscal year end, 4-5 months after the true
    ex-date, and carries no share-count data at all. The ratio it implies is therefore a guess
    with the right shape, and a guess is exactly what must not be multiplied into four price
    columns. This is a *different* refusal from the one above with a *different* reason, which is
    why it is a separate test: a single "it raises" test would pass whichever of the two the code
    happened to implement.
    """
    bars()
    engine = OHLCVEngine(
        _Screener(profile=_screener_profile()), corporate_actions_client=_NoRowsActions()
    )

    plain = await engine.fetch_history(
        symbol="RELIANCE", actions=True, auto_adjust=False, **WINDOW
    )
    assert plain.attrs["bfinance_actions_source"] == "screener_fy_end_approximation"
    assert plain.attrs["bfinance_actions_share_count_observable"] is False

    with pytest.raises(AdjustmentNotAvailableError) as exc:
        await engine.fetch_history(symbol="RELIANCE", actions=True, auto_adjust=True, **WINDOW)
    message = str(exc.value)
    assert "fiscal-year-end approximation" in message, message
    assert "4-5 months late" in message, message
    assert "no share-count data at all" in message, message


async def test_auto_adjust_true_is_not_refused_for_actions_false(bars, monkeypatch):
    """
    This test used to pin the opposite. It asserted that ``actions=False`` combined with
    ``auto_adjust=True`` refused before a request was spent, on the reasoning that no actions
    series means no factor derived means a ratio of exactly 1.0 and therefore a column drop
    pretending to be an adjustment.

    That reasoning was right about the arithmetic and wrong about the gate. yfinance's
    ``auto_adjust`` does not depend on its own ``actions`` flag -- there, ``actions`` decides
    only whether ``Dividends`` / ``Stock Splits`` are published, and the Adjusted Close the
    scaling reads is fetched either way -- and bfinance used ``actions`` to gate the FEED as
    well as the columns. So the two batch entry points that default ``actions=False``
    (``download()`` and ``Tickers.history()``) answered their own new ``auto_adjust=True``
    default with a silently EMPTY frame: measured, ``bfinance.download(["NTPC", "RELIANCE"],
    period="5d")`` returned shape ``(0, 0)`` where the same call with ``actions=True`` returned
    seven columns. The refusal was bought with a silent empty DataFrame, which is the one
    outcome worse than an unadjusted price.

    So the feed is now resolved whenever EITHER flag asks for it, and this test pins what
    replaced the refusal: a real request, a real factor, yfinance's exact column set, and the
    two action columns absent because the caller asked for them to be.
    """
    calls = []
    monkeypatch.setattr(
        ohlcv_mod, "bhavcopy_range",
        lambda start, end, **kw: calls.append((start, end)) or _market_frame(
            RELIANCE_DIVIDEND_BARS
        ),
    )
    frame = await _engine().fetch_history(
        symbol="RELIANCE", actions=False, auto_adjust=True, **WINDOW
    )

    assert calls != [], "a real ex-dated feed answered, so a request was spent -- as it must be"
    assert frame.attrs["bfinance_actions_source"] == ACTIONS_SOURCE_NSE
    # yfinance's columns for actions=False: no Adj Close (auto_adjust=True) and no action columns.
    assert list(frame.columns) == ["Open", "High", "Low", "Close", "Volume"]
    # And the prices are genuinely adjusted rather than merely relabelled: the same dividend
    # factor the auto_adjust=True + actions=True call applies is on O/H/L/C here.
    with_actions = await _engine().fetch_history(
        symbol="RELIANCE", actions=True, auto_adjust=True, **WINDOW
    )
    for column in ("Open", "High", "Low", "Close"):
        assert frame[column].tolist() == pytest.approx(
            with_actions[column].tolist(), rel=1e-12
        ), f"{column} differs from the actions=True answer"


async def test_auto_adjust_true_is_refused_on_the_synthetic_path(bars):
    """
    The synthetic path is never back-adjusted, and its Open/High/Low are derived from Close.

    Scaling an invented high and low by an invented ratio would invent the rest of the bar too.
    Refused before any request as well, for the same local-vocabulary reason.
    """
    bars()
    chart = pd.DataFrame(
        {"Price": [100.0, 102.0, 101.0], "Volume": [10, 20, 15]},
        index=pd.DatetimeIndex(
            pd.to_datetime(["2024-08-16", "2024-08-19", "2024-08-20"]), name="Date"
        ),
    )
    engine = OHLCVEngine(_Screener(chart_df=chart), corporate_actions_client=_NseActions())

    with pytest.raises(AdjustmentNotAvailableError) as exc:
        await engine.fetch_history(
            symbol="RELIANCE", ohlc="synthetic", actions=True, auto_adjust=True, **WINDOW
        )
    message = str(exc.value)
    assert "ohlc='synthetic'" in message, message
    assert "DERIVES Open/High/Low from Close" in message, message


async def test_auto_adjust_true_is_refused_on_an_empty_frame(bars):
    """
    No bars means no actions were resolved, so there is no adjustment to apply -- and returning an
    empty frame instead would be worse than refusing, because an empty frame with an adjusted
    column list is indistinguishable from a successful empty answer.

    The archive holds a real bar for a DIFFERENT symbol, so the whole-market read succeeds and the
    slice to RELIANCE is what comes back empty. That is the ordinary "this scrip has no rows here"
    path, not the separate "the archive itself was empty" refusal, which is a different exception
    entirely and is covered by tests/test_nse_bhavcopy_wiring.py.
    """
    bars({"TCS": [("2024-08-16", 400.0, 402.0, 390.0, 391.0, 500)]})
    with pytest.raises(AdjustmentNotAvailableError) as exc:
        await _history(auto_adjust=True)
    assert "no corporate-actions feed with real" in str(exc.value)


async def test_the_refusal_still_fires_when_a_request_would_have_been_wasted(bars, monkeypatch):
    """
    The locally-decidable refusal spends no request, which is a measurable property and not a
    stylistic one: ``download()`` runs the same call per symbol, so a refusal that fetched first
    would spend a request per name before saying no.

    ``ohlc="synthetic"`` is now the ONLY such refusal. It used to be paired with
    ``actions=False``, which moved to the resolved-feed path -- see
    ``test_auto_adjust_true_is_not_refused_for_actions_false`` for why, and for the measured
    ``(0, 0)`` frame that refusal was costing.
    """
    bars()
    calls = []
    monkeypatch.setattr(
        ohlcv_mod, "bhavcopy_range",
        lambda start, end, **kw: calls.append((start, end)) or _market_frame(
            RELIANCE_DIVIDEND_BARS
        ),
    )
    with pytest.raises(AdjustmentNotAvailableError):
        await _engine().fetch_history(
            symbol="RELIANCE", auto_adjust=True, ohlc="synthetic", **WINDOW
        )
    assert calls == [], f"a local refusal must not reach the network: {calls}"


async def test_a_refused_call_leaves_no_state_the_next_call_can_inherit(bars):
    """
    A refusal is a clean exit, and the next call on the same engine is the evidence.

    The ``auto_adjust`` gate is placed BEFORE any column is scaled, not after, and that placement
    is not something a caller can observe directly: they get an exception either way. What they
    *can* observe is whether the engine is still itself afterwards. If the gate sat after the
    scaling, the frame would have been built and its prices multiplied before the raise, and the
    only way that shows up is in the state it leaves behind.
    """
    bars()
    engine = OHLCVEngine(_Screener(), corporate_actions_client=_NoRowsActions())
    with pytest.raises(AdjustmentNotAvailableError):
        await engine.fetch_history(symbol="RELIANCE", actions=True, auto_adjust=True, **WINDOW)
    # The same engine still answers correctly on the next call, and the unadjusted path is
    # unaffected: a refusal that had already done the arithmetic would not leave it clean. The
    # closes are the raw published prints because this engine's feed produced no factor, which is
    # also the state that made the refusal correct in the first place.
    ok = await engine.fetch_history(symbol="RELIANCE", actions=True, auto_adjust=False, **WINDOW)
    assert list(ok.columns) == [
        "Open", "High", "Low", "Close", "Adj Close", "Volume", "Dividends", "Stock Splits"
    ]
    assert ok["Close"].tolist() == pytest.approx([2956.40, 2976.80, 3000.00])


# ------------------------------------------------------------- the column order, pinned once more
#
# A prior review reported that ``_history_columns`` built Adj Close at index 4, i.e. AFTER Volume.
# It does not: the list it inserts into is ["Open", "High", "Low", "Close", "Volume"], so index 4
# is the gap BETWEEN Close and Volume, and the result is yfinance's order. Re-asserted here
# because the method whose output it is got a comment saying so, and a comment is not a test.


async def test_the_column_order_is_yfinances_and_not_the_reported_one(bars):
    """
    ``["Open", "High", "Low", "Close", "Adj Close", "Volume"]`` -- Adj Close at index 4 of SIX.

    The reported defect was a misreading of ``cols.insert(4, "Adj Close")`` as an index into the
    finished list rather than a position in the five-element one it is called on. The claim is
    load-bearing enough (it is the column order of the most-used method in the package) that it
    gets a test of its own rather than a code comment.
    """
    bars()
    adjusted = await _history(auto_adjust=False)
    assert list(adjusted.columns) == [
        "Open", "High", "Low", "Close", "Adj Close", "Volume", "Dividends", "Stock Splits"
    ]
    assert list(adjusted.columns)[:6] == [
        "Open", "High", "Low", "Close", "Adj Close", "Volume"
    ]
    assert list(adjusted.columns).index("Adj Close") == 4
    assert list(adjusted.columns).index("Close") == 3
    assert list(adjusted.columns).index("Volume") == 5
    # And the actions-free form, which is the one yfinance's own auto_adjust=False frame has.
    assert list(OHLCVEngine._history_columns(False, False, False)) == [
        "Open", "High", "Low", "Close", "Adj Close", "Volume"
    ]
