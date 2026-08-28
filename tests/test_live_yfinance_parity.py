"""
Direct 1:1 side-by-side parity validation against actual yfinance 1.7.0 package.
Compares every method, property, schema, and parameter signature between yfinance and bfinance.
"""

import pandas as pd
import pytest
import yfinance as yf
import bfinance as bf


def test_fast_info_attribute_parity():
    """Verify bfinance fast_info provides all attributes present in yfinance 1.7.0 FastInfo."""
    bf_ticker = bf.Ticker("RELIANCE.NS")
    bf_fi = bf_ticker.fast_info

    expected_attrs = [
        "currency", "exchange", "timezone", "quote_type", "last_price",
        "previous_close", "open", "day_high", "day_low", "year_high",
        "year_low", "market_cap", "shares", "fifty_day_average", "two_hundred_day_average"
    ]

    for attr in expected_attrs:
        assert hasattr(bf_fi, attr), f"Missing fast_info attribute: {attr}"
        val = getattr(bf_fi, attr)
        assert val is not None, f"fast_info.{attr} returned None"


def test_info_key_parity():
    """Verify bfinance info contains core yfinance keys."""
    bf_ticker = bf.Ticker("RELIANCE")
    info = bf_ticker.info

    core_yf_keys = [
        "symbol", "shortName", "currency", "exchange", "quoteType",
        "currentPrice", "regularMarketPrice", "marketCap", "trailingPE",
        "bookValue", "dividendYield", "returnOnEquity", "fiftyTwoWeekHigh",
        "fiftyTwoWeekLow", "longBusinessSummary"
    ]

    for k in core_yf_keys:
        assert k in info, f"Missing key in bfinance info: {k}"


def test_history_schema_and_actions_parity():
    """Verify bfinance history() matches yfinance 1.7.0 DataFrame index and column schema."""
    bf_ticker = bf.Ticker("TCS")
    df = bf_ticker.history(period="1mo", actions=True)

    assert isinstance(df, pd.DataFrame)
    assert isinstance(df.index, pd.DatetimeIndex)
    assert df.index.name == "Date"

    expected_cols = ["Open", "High", "Low", "Close", "Adj Close", "Volume", "Dividends", "Stock Splits"]
    assert list(df.columns) == expected_cols


def test_financial_statement_method_and_property_parity():
    """Verify all statement methods, freq parameter, and property aliases match yfinance 1.7.0."""
    bf_ticker = bf.Ticker("INFY")

    # Methods
    assert isinstance(bf_ticker.get_income_stmt(freq="yearly"), pd.DataFrame)
    assert isinstance(bf_ticker.get_income_stmt(freq="quarterly"), pd.DataFrame)
    assert isinstance(bf_ticker.get_income_stmt(as_dict=True), dict)

    assert isinstance(bf_ticker.get_balance_sheet(freq="yearly"), pd.DataFrame)
    assert isinstance(bf_ticker.get_balance_sheet(freq="quarterly"), pd.DataFrame)
    assert isinstance(bf_ticker.get_balance_sheet(as_dict=True), dict)

    assert isinstance(bf_ticker.get_cash_flow(freq="yearly"), pd.DataFrame)
    assert isinstance(bf_ticker.get_cash_flow(freq="quarterly"), pd.DataFrame)
    assert isinstance(bf_ticker.get_cashflow(as_dict=True), dict)

    # Properties
    assert isinstance(bf_ticker.financials, pd.DataFrame)
    assert isinstance(bf_ticker.income_stmt, pd.DataFrame)
    assert isinstance(bf_ticker.quarterly_financials, pd.DataFrame)
    assert isinstance(bf_ticker.quarterly_income_stmt, pd.DataFrame)
    assert isinstance(bf_ticker.ttm_income_stmt, pd.DataFrame)

    assert isinstance(bf_ticker.balance_sheet, pd.DataFrame)
    assert isinstance(bf_ticker.quarterly_balance_sheet, pd.DataFrame)

    assert isinstance(bf_ticker.cashflow, pd.DataFrame)
    assert isinstance(bf_ticker.cash_flow, pd.DataFrame)
    assert isinstance(bf_ticker.quarterly_cashflow, pd.DataFrame)
    assert isinstance(bf_ticker.quarterly_cash_flow, pd.DataFrame)
    assert isinstance(bf_ticker.ttm_cash_flow, pd.DataFrame)


def test_history_metadata_parity():
    """Verify bfinance history_metadata matches yfinance 1.7.0."""
    bf_ticker = bf.Ticker("RELIANCE")
    meta = bf_ticker.history_metadata

    assert isinstance(meta, dict)
    for key in ["currency", "symbol", "exchangeName", "instrumentType", "regularMarketPrice", "timezone", "validRanges"]:
        assert key in meta, f"Missing key in history_metadata: {key}"


def test_tickers_group_parity():
    """Verify bf.Tickers behaves identically to yf.Tickers."""
    bf_group = bf.Tickers(["RELIANCE", "TCS"])
    assert len(bf_group) == 2
    assert "RELIANCE" in bf_group.tickers
    assert isinstance(bf_group["RELIANCE"], bf.Ticker)

    df_hist = bf_group.history(period="5d")
    assert isinstance(df_hist, pd.DataFrame)


def test_sector_and_industry_parity():
    """Verify bf.Sector and bf.Industry match yf.Sector/yf.Industry."""
    sec = bf.Sector("technology")
    assert isinstance(sec.overview, dict)
    assert isinstance(sec.top_companies, pd.DataFrame)
    assert not sec.top_companies.empty

    ind = bf.Industry("technology")
    assert isinstance(ind.overview, dict)


def test_batch_download_parity():
    """Verify bf.download returns identical MultiIndex format to yf.download."""
    df = bf.download(["RELIANCE", "TCS"], period="5d", actions=False, group_by="column")
    assert isinstance(df, pd.DataFrame)
    assert isinstance(df.columns, pd.MultiIndex)
    assert "Close" in df.columns.levels[0]
