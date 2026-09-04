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


async def _fetch(chart_df, profile=None, **kwargs):
    engine = OHLCVEngine(_StubScreener(chart_df, profile))
    # auto_adjust=False: these tests assert the unadjusted contract (Adj Close == Close).
    # The yfinance-1.7 default (auto_adjust=True drops Adj Close) is locked in below.
    params = {"symbol": "TEST", "period": "5d", "actions": False, "auto_adjust": False}
    params.update(kwargs)
    return await engine.fetch_history(**params)


# ------------------------------------------------------------- OFFLINE: tz
async def test_offline_history_index_tz_asia_kolkata():
    df = await _fetch(_synthetic_chart_frame())
    assert isinstance(df.index, pd.DatetimeIndex)
    assert str(df.index.tz) == "Asia/Kolkata"


# ------------------------------------------------------------- OFFLINE: provenance
async def test_offline_history_provenance_synthetic_ohlc():
    df = await _fetch(_synthetic_chart_frame())
    assert df.attrs.get("bfinance_synthetic_ohlc") is True
    # Adj Close is unadjusted: must equal Close exactly
    pd.testing.assert_series_equal(df["Adj Close"], df["Close"], check_names=False)
    # High/Low envelope must bound Open/Close
    assert bool((df["High"] >= df[["Open", "Close"]].max(axis=1)).all())
    assert bool((df["Low"] <= df[["Open", "Close"]].min(axis=1)).all())


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


async def test_offline_auto_adjust_true_drops_adj_close():
    """yfinance-1.7 default: auto_adjust=True drops Adj Close. Must match."""
    df = await _fetch(_synthetic_chart_frame(), auto_adjust=True)
    assert "Adj Close" not in df.columns
    assert list(df.columns) == ["Open", "High", "Low", "Close", "Volume"]
