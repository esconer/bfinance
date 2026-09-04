"""Parity tests for statement native shape vs yfinance converter.

Native (bfinance): metric rows with TitleCase Indian labels, period-string
columns ("Mar YYYY"), values in Rs Cr.
YFinance (to_yfinance): same orientation, DatetimeIndex columns, absolute Rs.
"""

import pandas as pd
import pytest

import bfinance.models.statements as stmt_mod
from bfinance.models.statements import FinancialStatement


def _annual_fixture() -> FinancialStatement:
    return FinancialStatement(
        headers=["Mar 2023", "Mar 2024"],
        rows={
            "Sales": [100.0, 123.45],
            "Net Profit": [10.0, 12.5],
            "Borrowings": [50.0, 55.0],
            "Custom Metric XYZ": [1.0, 2.0],
        },
    )


def test_native_shape_contract():
    stmt = _annual_fixture()
    df = stmt.to_dataframe()
    # rows are metrics incl. required labels
    for label in ("Sales", "Net Profit", "Borrowings"):
        assert label in df.index
    # columns are period strings (not datetimes)
    assert list(df.columns) == ["Mar 2023", "Mar 2024"]
    assert all(isinstance(c, str) for c in df.columns)
    # native units are Rs Cr (no scaling applied)
    assert df.loc["Sales", "Mar 2024"] == pytest.approx(123.45)


def test_native_units_documented():
    doc = (stmt_mod.__doc__ or "") + (FinancialStatement.to_dataframe.__doc__ or "")
    assert "Cr" in doc
    assert "Mar YYYY" in doc or "Mar 2024" in doc


def test_to_yfinance_shape_and_scaling():
    stmt = _annual_fixture()
    yf_df = stmt.to_yfinance()
    assert isinstance(yf_df.columns, pd.DatetimeIndex)
    assert pd.Timestamp("2024-03-31") in yf_df.columns
    assert pd.Timestamp("2023-03-31") in yf_df.columns
    # Indian -> generic mapping applied
    assert "Total Revenue" in yf_df.index  # Sales
    assert "Net Income" in yf_df.index  # Net Profit
    assert "Total Debt" in yf_df.index  # Borrowings
    # values scaled x1e7 to absolute Rs
    assert yf_df.loc["Total Revenue", pd.Timestamp("2024-03-31")] == pytest.approx(123.45 * 1e7)
    assert yf_df.loc["Net Income", pd.Timestamp("2024-03-31")] == pytest.approx(12.5 * 1e7)
    assert yf_df.loc["Total Debt", pd.Timestamp("2024-03-31")] == pytest.approx(55.0 * 1e7)
    # unmapped rows kept as-is
    assert "Custom Metric XYZ" in yf_df.index
    assert yf_df.loc["Custom Metric XYZ", pd.Timestamp("2024-03-31")] == pytest.approx(2.0 * 1e7)


def test_to_yfinance_quarterly_periods():
    stmt = FinancialStatement(
        headers=["Dec 2023", "Mar 2024"],
        rows={"Sales": [90.0, 95.0], "Net Profit": [9.0, 9.5]},
    )
    yf_df = stmt.to_yfinance()
    assert isinstance(yf_df.columns, pd.DatetimeIndex)
    assert pd.Timestamp("2023-12-31") in yf_df.columns
    assert pd.Timestamp("2024-03-31") in yf_df.columns


def test_to_yfinance_does_not_mutate_native():
    stmt = _annual_fixture()
    native_before = stmt.to_dataframe()
    stmt.to_yfinance()
    pd.testing.assert_frame_equal(stmt.to_dataframe(), native_before)


@pytest.mark.live
def test_live_annual_income_statement():
    from bfinance.ticker import Ticker

    t = Ticker("RELIANCE.NS")
    df = t.get_income_stmt(freq="yearly")
    assert isinstance(df, pd.DataFrame)
    assert not df.empty
    assert "Sales" in df.index
    assert "Net Profit" in df.index
    assert all(isinstance(c, str) for c in df.columns)


@pytest.mark.live
def test_live_quarterly_income_data_or_raise():
    from bfinance.ticker import Ticker

    t = Ticker("RELIANCE.NS")
    try:
        qdf = t.get_income_stmt(freq="quarterly")
    except NotImplementedError:
        return
    ydf = t.get_income_stmt(freq="yearly")
    assert isinstance(qdf, pd.DataFrame)
    assert not qdf.empty, "quarterly returned empty; must raise NotImplementedError instead"
    assert "Sales" in qdf.index
    # fail on silent-yearly: quarterly must differ from yearly
    assert list(qdf.columns) != list(ydf.columns) or not qdf.equals(ydf)
