"""
Parity tests for OHLCV history: tz, corporate-actions merge, provenance (offline)
plus bfinance-vs-yfinance ground-truth comparison (live, single ticker).

Offline tests use pure synthetic frames + stub screener (no conftest fixture).
Live tests are marked @pytest.mark.live and capped at RELIANCE.NS 5d.
"""

import pandas as pd
import pytest

from bfinance.market.corporate import CorporateActionsEngine
from bfinance.market.ohlcv import OHLCVEngine
from bfinance.models.company import CompanyProfile
from bfinance.models.statements import FinancialStatement

from conftest import StubCorporateActionsClient


def _synthetic_chart_frame():
    idx = pd.to_datetime(["2024-03-28", "2024-03-31", "2024-04-01"])
    return pd.DataFrame(
        {"Price": [100.0, 102.0, 101.0], "Volume": [1000, 2000, 1500]},
        index=pd.DatetimeIndex(idx, name="Date"),
    )


def _synthetic_profile():
    return CompanyProfile(
        symbol="TEST",
        company_id=1,
        name="Test Co",
        profit_loss=FinancialStatement(
            headers=["Mar 2023", "Mar 2024"],
            rows={
                "Dividend Payout %": [50.0, 40.0],
                "EPS in Rs": [20.0, 25.0],
            },
        ),
        balance_sheet=FinancialStatement(
            headers=["Mar 2023", "Mar 2024"],
            rows={"Equity Capital": [100.0, 200.0]},
        ),
    )


class _StubScreener:
    """Minimal stub: chart frame + optional profile, no network."""

    def __init__(self, chart_df, profile=None):
        self._chart_df = chart_df
        self._profile = profile

    async def get_chart_timeseries(self, ticker, metric="price", days=1825):
        return self._chart_df

    async def get_company_profile(self, ticker, force_refresh=False):
        if self._profile is None:
            raise ValueError("no profile")
        return self._profile


async def _fetch(chart_df, profile=None, actions_client=None, **kwargs):
    # The corporate-actions client is injected so this stays offline. Left as None,
    # CorporateActionsEngine.resolve_actions_async constructs a REAL client, and the tests that
    # pass actions=True then make a live DNS call to www.nseindia.com -- visible only when
    # ~/.bfinance is cold, which is the normal state on a CI runner.
    engine = OHLCVEngine(
        _StubScreener(chart_df, profile),
        corporate_actions_client=(
            actions_client if actions_client is not None else StubCorporateActionsClient()
        ),
    )
    # ohlc="synthetic": every test below describes the *close-only* feed, which is now an
    # explicit opt-in. The default source is the NSE bhavcopy (real O/H/L) -- see
    # tests/test_nse_bhavcopy_wiring.py for that path.
    # auto_adjust=False: these tests assert the unadjusted contract (Adj Close == Close when
    # no actions are supplied). auto_adjust=True is now rejected rather than faked.
    params = {
        "symbol": "TEST", "period": "5d", "actions": False,
        "auto_adjust": False, "ohlc": "synthetic",
    }
    params.update(kwargs)
    return await engine.fetch_history(**params)


# ------------------------------------------------------------- OFFLINE: tz
async def test_offline_history_index_tz_asia_kolkata():
    df = await _fetch(_synthetic_chart_frame())
    assert isinstance(df.index, pd.DatetimeIndex)
    assert str(df.index.tz) == "Asia/Kolkata"


# ------------------------------------------------------------- OFFLINE: provenance
#
# RC1: bfinance builds O/H/L that the source never sent. It reports this via
# ``df.attrs["bfinance_synthetic_ohlc"]``, which is the correct idea and the reason this is
# fixable without lying. But the flag is currently set *unconditionally* at
# ``market/ohlcv.py:249`` -- there is no code path in the package that sets it to False -- so it
# records "this build always fabricates", not "this frame was fabricated". finengine never reads
# it, so the fabrication is invisible downstream.
#
# The previous version of this test asserted the fabricated output as though it were the
# specification (``Adj Close == Close`` and the High/Low envelope). That is worse than no test:
# it makes the fix look like a regression. The three groups below separate the parts.


async def test_offline_history_flags_fabricated_bars():
    """
    The invariant issue 03 depends on: bars built from a close-only source must be flagged.

    This is a real, valuable contract. If issue 03 lands a bhavcopy path with genuine O/H/L, this
    must keep holding for the *screener* path while the bhavcopy path sets the flag to False.
    """
    df = await _fetch(_synthetic_chart_frame())
    assert df.attrs.get("bfinance_synthetic_ohlc") is True


async def test_offline_history_synthetic_flag_is_present_even_when_empty():
    """
    An empty result must also carry the flag, so callers can tell "no data" from "real data".

    ``ohlcv.py:165`` sets the attribute on the empty-frame early return for exactly this reason.
    """
    engine = OHLCVEngine(_StubScreener(pd.DataFrame(columns=["Price", "Volume"])))
    df = await engine.fetch_history(
        symbol="TEST", period="5d", actions=False, auto_adjust=False, ohlc="synthetic"
    )
    assert df.empty
    assert df.attrs.get("bfinance_synthetic_ohlc") is True


# NOTE: `test_synthetic_flag_has_no_false_path_yet` used to live here. It asserted that NO
# `attrs["bfinance_synthetic_ohlc"] = False` assignment existed in market/ohlcv.py, and its own
# docstring said it "must be deleted, not rewritten" once a real-OHLC source landed. That source
# is the NSE bhavcopy and it sets the flag to False, so the assertion is now false and the test
# is gone. The replacement contract is in tests/test_nse_bhavcopy_wiring.py: real bars carry
# the flag as False, the close-only path carries it as True, and `df.attrs` also carries the
# source's provenance record.


async def test_offline_history_characterises_the_fabrication():
    """
    CHARACTERISATION, NOT A SPECIFICATION. Documents the magnitude of the RC1 defect.

    The assertions below are the *bug*, written down so they can be deleted with confidence:

    - ``Adj Close`` is a copy of ``Close``; the feed is never dividend-adjusted, so every
      return computed from it is a price return, and any backtest using it silently omits
      dividends.
    - ``High``/``Low`` are a synthetic ~0.2% envelope around ``Open``/``Close``, not observed
      extremes. Every volatility estimate, ATR, drawdown and Bollinger band derived from them is
      fabricated -- and because the envelope is a fixed proportion, the error is *correlated
      across symbols*, which cross-sectional screens cannot detect.
    """
    df = await _fetch(_synthetic_chart_frame())

    # Adj Close is unadjusted: must equal Close exactly.
    pd.testing.assert_series_equal(df["Adj Close"], df["Close"], check_names=False)

    # High/Low envelope must bound Open/Close.
    assert bool((df["High"] >= df[["Open", "Close"]].max(axis=1)).all())
    assert bool((df["Low"] <= df[["Open", "Close"]].min(axis=1)).all())

    # ...and it really is a fixed proportion, not observed data. This is the part that makes the
    # error systematic rather than random.
    hi_exc = (df["High"] / df[["Open", "Close"]].max(axis=1) - 1.0).abs()
    lo_exc = (1.0 - df["Low"] / df[["Open", "Close"]].min(axis=1)).abs()
    assert float(hi_exc.max()) < 0.01, "envelope should be ~0.2%; larger means something else"
    assert float(lo_exc.max()) < 0.01


async def test_offline_real_ohlc_source_reports_synthetic_false(monkeypatch):
    """
    The post-issue-03 contract: genuine bars must be distinguishable from fabricated ones.

    Until this passes, no consumer -- finengine included -- can trust the flag, because there is
    no way to obtain a frame that claims to be real. The engine exposes the real-OHLC source
    through the ``ohlc`` parameter, so this drives that path and reads the flag off the result
    instead of looking for a method name.
    """
    import bfinance.market.ohlcv as ohlcv_mod

    index = pd.MultiIndex.from_arrays(
        [pd.DatetimeIndex(pd.to_datetime(["2024-06-03", "2024-06-04"])),
         pd.Index(["RELIANCE", "RELIANCE"], dtype=object, name="Symbol")],
        names=["Date", "Symbol"],
    )
    market = pd.DataFrame(
        [[100.0, 112.0, 97.5, 110.0, 1000], [110.0, 113.5, 108.0, 109.0, 2000]],
        columns=["Open", "High", "Low", "Close", "Volume"], index=index,
    )
    monkeypatch.setattr(ohlcv_mod, "bhavcopy_range", lambda start, end, **kw: market)

    engine = OHLCVEngine(_StubScreener(_synthetic_chart_frame()))
    df = await engine.fetch_history(symbol="RELIANCE", actions=False, auto_adjust=False)

    assert df.attrs.get("bfinance_synthetic_ohlc") is False
    # The bars are the published ones, not a max/min envelope around Close.
    assert df["High"].tolist() == [112.0, 113.5]
    assert df["Low"].tolist() == [97.5, 108.0]


async def test_offline_adj_close_differs_from_close_when_dividends_exist():
    """
    ``Adj Close`` is a real back-adjustment by the corporate actions merged into the frame, so a
    dividend-paying stock no longer shows a zero dividend contribution.
    """
    df = await _fetch(
        _synthetic_chart_frame(), _synthetic_profile(), symbol="TEST", actions=True
    )
    assert "Dividends" in df.columns
    assert float(df["Dividends"].abs().sum()) > 0, "fixture must carry a real dividend"
    assert not df["Adj Close"].equals(df["Close"]), "Adj Close is still a copy of Close"
    # The actions sit on 2024-03-31: a 10.0 dividend against a 100.0 previous close and a 2:1
    # split, so the single bar before it carries factor (1 - 10/100) / 2 = 0.45. The ex-date bar
    # and everything after are unadjusted.
    assert float(df["Adj Close"].iloc[0]) == pytest.approx(45.0)
    assert float(df["Adj Close"].iloc[1]) == pytest.approx(102.0)


# ------------------------------------------------------------- OFFLINE: actions merge via fetch_history
async def test_offline_actions_merge_real_dividends_splits():
    df = await _fetch(
        _synthetic_chart_frame(), _synthetic_profile(), symbol="TEST", actions=True
    )
    assert list(df.columns) == [
        "Open", "High", "Low", "Close", "Adj Close", "Volume",
        "Dividends", "Stock Splits",
    ]
    # 2024-03-31 carries a real dividend (40% of EPS 25 = 10.0) and split (200/100 = 2.0)
    keys = df.index.normalize().tz_localize(None)
    row = df.loc[keys == pd.Timestamp("2024-03-31")]
    assert len(row) == 1
    assert float(row["Dividends"].iloc[0]) == pytest.approx(10.0)
    assert float(row["Stock Splits"].iloc[0]) == pytest.approx(2.0)
    # Non-action dates stay zero
    rest = df.loc[keys != pd.Timestamp("2024-03-31")]
    assert bool((rest["Dividends"] == 0.0).all())
    assert bool((rest["Stock Splits"] == 0.0).all())


async def test_offline_actions_zeros_when_unavailable():
    empty_profile = CompanyProfile(symbol="TEST", company_id=0, name="Empty")
    df = await _fetch(_synthetic_chart_frame(), empty_profile, actions=True)
    assert bool((df["Dividends"] == 0.0).all())
    assert bool((df["Stock Splits"] == 0.0).all())


async def test_offline_actions_false_omits_columns():
    df = await _fetch(_synthetic_chart_frame(), _synthetic_profile(), actions=False)
    assert "Dividends" not in df.columns
    assert "Stock Splits" not in df.columns


# ------------------------------------------------------------- OFFLINE: merge helper directly
def test_offline_merge_helper_direct():
    idx = pd.DatetimeIndex(
        pd.to_datetime(["2024-03-28", "2024-03-31", "2024-04-01"]), tz="Asia/Kolkata",
        name="Date",
    )
    df = pd.DataFrame(
        {
            "Open": [100.0, 100.0, 102.0],
            "High": [101.0, 103.0, 103.0],
            "Low": [99.0, 99.0, 100.0],
            "Close": [100.0, 102.0, 101.0],
            "Adj Close": [100.0, 102.0, 101.0],
            "Volume": [1000, 2000, 1500],
        },
        index=idx,
    )
    divs = pd.Series(
        [10.0], index=pd.DatetimeIndex(pd.to_datetime(["2024-03-31"]), name="Date"),
        name="Dividends",
    )
    splits = pd.Series(
        [2.0], index=pd.DatetimeIndex(pd.to_datetime(["2024-03-31"]), name="Date"),
        name="Stock Splits",
    )
    merge_fn = CorporateActionsEngine.merge_actions_into_history
    out = merge_fn(df, dividends=divs, splits=splits)
    assert float(out.loc[out.index.normalize() == "2024-03-31", "Dividends"].iloc[0]) == pytest.approx(10.0)
    assert float(out.loc[out.index.normalize() == "2024-03-31", "Stock Splits"].iloc[0]) == pytest.approx(2.0)
    rest = out.loc[out.index.normalize() != "2024-03-31"]
    assert bool((rest["Dividends"] == 0.0).all())
    assert bool((rest["Stock Splits"] == 0.0).all())


# ------------------------------------------------------------- LIVE: ground truth
@pytest.mark.live
def test_live_yfinance_tz_is_asia_kolkata():
    yf = pytest.importorskip("yfinance")
    ydf = yf.Ticker("RELIANCE.NS").history(period="5d", auto_adjust=False, actions=True)
    assert not ydf.empty
    assert str(ydf.index.tz) == "Asia/Kolkata"


@pytest.mark.live
def test_live_history_parity_reliance():
    yf = pytest.importorskip("yfinance")
    import bfinance as bf

    bdf = bf.Ticker("RELIANCE.NS", cache_ttl_hours=0).history(
        period="5d", actions=True, auto_adjust=False
    )
    ydf = yf.Ticker("RELIANCE.NS").history(period="5d", auto_adjust=False, actions=True)
    assert not bdf.empty and not ydf.empty

    # Same columns
    assert list(bdf.columns) == list(ydf.columns)
    # Index tz matches yfinance convention
    assert str(bdf.index.tz) == str(ydf.index.tz) == "Asia/Kolkata"
    # Volume present and non-null
    assert "Volume" in bdf.columns
    assert bdf["Volume"].notna().all()

    # Close within 2% on overlapping dates
    b = bdf.copy()
    y = ydf.copy()
    b.index = b.index.normalize()
    y.index = y.index.normalize()
    common = b.index.intersection(y.index)
    assert len(common) >= 1
    pct = (b.loc[common, "Close"] - y.loc[common, "Close"]).abs() / y.loc[common, "Close"]
    assert bool((pct <= 0.02).all()), f"max pct diff {float(pct.max()):.4f}"


async def test_offline_auto_adjust_true_is_rejected_not_faked():
    """
    yfinance's default is auto_adjust=True, which drops "Adj Close" and rescales OHLC. bfinance
    used to copy that signature while rescaling nothing at all (frames were byte-identical either
    way, factor 1.0 vs yfinance's measured 0.9954) -- a column list claiming an adjustment that
    never happened. There is no authoritative factor to apply, so the request is refused loudly.
    """
    from bfinance.market.ohlcv import AdjustmentNotAvailableError

    with pytest.raises(AdjustmentNotAvailableError) as exc:
        await _fetch(_synthetic_chart_frame(), auto_adjust=True)
    assert "adjustment factor" in str(exc.value).lower()


async def test_offline_auto_adjust_false_keeps_the_adjusted_column():
    """auto_adjust=False is the default now, and keeps the real back-adjusted 'Adj Close'."""
    df = await _fetch(_synthetic_chart_frame())
    assert "Adj Close" in df.columns
    assert list(df.columns) == ["Open", "High", "Low", "Close", "Adj Close", "Volume"]
