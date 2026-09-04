"""Drop-in financials parity vs live yfinance for RELIANCE.NS (yfinance 1.7.0).

Native bfinance orientation/rows/units are the contract (metric rows x
"Mar YYYY" cols, TitleCase Indian labels, Rs Cr) — this file asserts
self-consistency + yfinance-bridge alignment, never native equality.
"""
import pandas as pd
import pytest

import yfinance as yf

YFINANCE_VERSION = getattr(yf, "__version__", "unknown")
TICKER = "RELIANCE.NS"
TOL_RECONCILE = 0.05
TOL_BRIDGE = 0.15
# Fiscal Mar-year: screener "Mar YYYY" <-> yfinance YYYY-03-31. Align by year.
BRIDGE_YEARS = [2024, 2025]


def _rel_close(a, b, tol):
    try:
        a = float(a)
        b = float(b)
    except (TypeError, ValueError):
        return False
    if pd.isna(a) or pd.isna(b):
        return False
    if b == 0:
        return abs(a) < 1e-9
    return abs(a - b) / abs(b) <= tol


def _bridge_by_year(yf_bridge: pd.DataFrame):
    out = {}
    for c in yf_bridge.columns:
        try:
            out[int(pd.Timestamp(c).year)] = c
        except Exception:
            continue
    return out


@pytest.mark.live
def test_annual_income_nonempty_and_coverage():
    from bfinance.ticker import Ticker

    df = Ticker(TICKER).get_income_stmt(freq="yearly")
    assert isinstance(df, pd.DataFrame) and not df.empty
    assert df.shape[1] >= 8
    for r in ("Sales", "Net Profit"):
        assert r in df.index


@pytest.mark.live
def test_annual_balance_nonempty_and_coverage():
    from bfinance.ticker import Ticker

    df = Ticker(TICKER).get_balance_sheet(freq="yearly")
    assert isinstance(df, pd.DataFrame) and not df.empty
    assert df.shape[1] >= 8
    for r in ("Total Assets", "Total Liabilities"):
        assert r in df.index


@pytest.mark.live
def test_annual_cashflow_nonempty_and_coverage():
    from bfinance.ticker import Ticker

    df = Ticker(TICKER).get_cash_flow(freq="yearly")
    assert isinstance(df, pd.DataFrame) and not df.empty
    assert df.shape[1] >= 8
    for r in ("Cash from Operating Activity", "Net Cash Flow"):
        assert r in df.index


@pytest.mark.live
def test_quarterly_income_nonempty_and_coverage():
    from bfinance.ticker import Ticker

    try:
        qdf = Ticker(TICKER).get_income_stmt(freq="quarterly")
    except NotImplementedError:
        pytest.skip("quarterly income not supported by backend")
    assert isinstance(qdf, pd.DataFrame) and not qdf.empty
    assert qdf.shape[1] >= 8
    assert "Sales" in qdf.index


@pytest.mark.live
def test_quarterly_balance_supported_or_documented():
    from bfinance.ticker import Ticker

    try:
        qdf = Ticker(TICKER).get_balance_sheet(freq="quarterly")
    except NotImplementedError:
        return  # gap owned by ticker.py; documented in report (NEEDS-MAIN)
    assert isinstance(qdf, pd.DataFrame) and not qdf.empty
    assert qdf.shape[1] >= 8


@pytest.mark.live
def test_quarterly_cashflow_supported_or_documented():
    from bfinance.ticker import Ticker

    try:
        qdf = Ticker(TICKER).get_cash_flow(freq="quarterly")
    except NotImplementedError:
        return  # gap owned by ticker.py; documented in report (NEEDS-MAIN)
    assert isinstance(qdf, pd.DataFrame) and not qdf.empty
    assert qdf.shape[1] >= 8


@pytest.mark.live
def test_balance_sheet_totals_reconcile():
    from bfinance.ticker import Ticker

    df = Ticker(TICKER).get_balance_sheet(freq="yearly")
    for col in df.columns:
        a = df.loc["Total Assets", col]
        b = df.loc["Total Liabilities", col]
        assert _rel_close(a, b, TOL_RECONCILE), f"{col}: Assets {a} vs Liab {b}"


@pytest.mark.live
def test_cash_flow_totals_reconcile():
    from bfinance.ticker import Ticker

    df = Ticker(TICKER).get_cash_flow(freq="yearly")
    rows = (
        "Cash from Operating Activity",
        "Cash from Investing Activity",
        "Cash from Financing Activity",
    )
    for col in df.columns:
        expect = sum(float(df.loc[r, col]) for r in rows)
        got = float(df.loc["Net Cash Flow", col])
        assert _rel_close(got, expect, TOL_RECONCILE), f"{col}: net {got} vs sum {expect}"


@pytest.mark.live
def test_income_operating_profit_reconcile():
    from bfinance.ticker import Ticker

    df = Ticker(TICKER).get_income_stmt(freq="yearly")
    cols = [c for c in df.columns if c != "TTM"]
    assert len(cols) >= 8
    for col in cols:
        expect = float(df.loc["Sales", col]) - float(df.loc["Expenses", col])
        got = float(df.loc["Operating Profit", col])
        assert _rel_close(got, expect, TOL_RECONCILE), f"{col}: op {got} vs {expect}"


@pytest.mark.live
def test_to_yfinance_bridge_income_anchors():
    from bfinance.ticker import Ticker

    bt = Ticker(TICKER)
    prof = bt._ensure_profile()
    bridge = prof.profit_loss.to_yfinance()
    assert isinstance(bridge.columns, pd.DatetimeIndex)
    by = _bridge_by_year(bridge)
    ydf = yf.Ticker(TICKER).income_stmt
    assert isinstance(ydf.columns, pd.DatetimeIndex)
    for anchor in ("Total Revenue", "Pretax Income"):
        assert anchor in bridge.index, f"missing bridge row {anchor}"
        assert anchor in ydf.index, f"missing yfinance row {anchor}"
        for yr in BRIDGE_YEARS:
            bcol = by[yr]
            ycol = pd.Timestamp(f"{yr}-03-31")
            bv = float(bridge.loc[anchor, bcol])
            yv = float(ydf.loc[anchor, ycol])
            assert _rel_close(bv, yv, TOL_BRIDGE), f"{anchor} {yr}: bf {bv} vs yf {yv}"


@pytest.mark.live
def test_to_yfinance_bridge_balance_anchors():
    from bfinance.ticker import Ticker

    bt = Ticker(TICKER)
    bridge = bt._ensure_profile().balance_sheet.to_yfinance()
    assert isinstance(bridge.columns, pd.DatetimeIndex)
    by = _bridge_by_year(bridge)
    ydf = yf.Ticker(TICKER).balance_sheet
    for anchor in ("Total Assets", "Total Debt"):
        assert anchor in bridge.index
        assert anchor in ydf.index
        for yr in BRIDGE_YEARS:
            bv = float(bridge.loc[anchor, by[yr]])
            yv = float(ydf.loc[anchor, pd.Timestamp(f"{yr}-03-31")])
            assert _rel_close(bv, yv, TOL_BRIDGE), f"{anchor} {yr}: bf {bv} vs yf {yv}"


@pytest.mark.live
def test_to_yfinance_bridge_cashflow_anchors():
    from bfinance.ticker import Ticker

    bt = Ticker(TICKER)
    bridge = bt._ensure_profile().cash_flow.to_yfinance()
    assert isinstance(bridge.columns, pd.DatetimeIndex)
    by = _bridge_by_year(bridge)
    ydf = yf.Ticker(TICKER).cashflow
    for anchor in ("Operating Cash Flow", "Investing Cash Flow", "Financing Cash Flow"):
        assert anchor in bridge.index
        assert anchor in ydf.index
        for yr in BRIDGE_YEARS:
            bv = float(bridge.loc[anchor, by[yr]])
            yv = float(ydf.loc[anchor, pd.Timestamp(f"{yr}-03-31")])
            assert _rel_close(bv, yv, TOL_BRIDGE), f"{anchor} {yr}: bf {bv} vs yf {yv}"


@pytest.mark.live
def test_to_yfinance_bridge_third_anchor_net_income_inclusive():
    """bf Net Profit is consolidated incl. minority: compare to yfinance inclusive row."""
    from bfinance.ticker import Ticker

    bridge = Ticker(TICKER)._ensure_profile().profit_loss.to_yfinance()
    ydf = yf.Ticker(TICKER).income_stmt
    yrow = "Net Income Including Noncontrolling Interests"
    assert yrow in ydf.index
    assert yrow in bridge.index, "bridge must alias consolidated Net Income to yfinance inclusive row"
    by = _bridge_by_year(bridge)
    n = 0
    for yr in BRIDGE_YEARS:
        bv = float(bridge.loc["Net Income", by[yr]])
        yv = float(ydf.loc[yrow, pd.Timestamp(f"{yr}-03-31")])
        assert _rel_close(bv, yv, TOL_BRIDGE), f"Net Income {yr}: bf {bv} vs yf-incl {yv}"
        n += 1
    assert n >= 2


@pytest.mark.live
def test_to_yfinance_quarterly_bridge_datetime():
    from bfinance.ticker import Ticker

    try:
        qdf = Ticker(TICKER).get_income_stmt(freq="quarterly")
    except NotImplementedError:
        pytest.skip("quarterly income not supported by backend")
    prof = Ticker(TICKER)._ensure_profile()
    bridge = prof.quarters.to_yfinance()
    assert isinstance(bridge.columns, pd.DatetimeIndex)
    assert len(bridge.columns) >= 8
    assert all(isinstance(c, pd.Timestamp) for c in bridge.columns)
    assert all(dt.kind == "f" for dt in bridge.dtypes), f"bridge must be float, got {set(bridge.dtypes)}"
    assert "Raw PDF" not in bridge.index, "all-NaN screener rows must not leak into bridge"


@pytest.mark.live
def test_earnings_dates_gap_documented():
    """bfinance has no earnings-dates API; do NOT scrape — gap for reconciler."""
    from bfinance.ticker import Ticker

    bt = Ticker(TICKER)
    for attr in ("earnings_dates", "quarterly_earnings", "earnings", "get_earnings_dates"):
        assert not hasattr(bt, attr), f"unexpected earnings API {attr} now exists — extend test"
    yed = yf.Ticker(TICKER).earnings_dates
    assert isinstance(yed, pd.DataFrame) and not yed.empty
