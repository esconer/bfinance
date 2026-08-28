"""
Tests validating 1:1 drop-in compatibility with yfinance 1.7.0+ Ticker, Tickers, and Sector API.
"""

import pandas as pd
import pytest
from bfinance.ticker import Ticker
from bfinance.tickers import Tickers
from bfinance.sector import Sector, Industry


def test_yfinance_fast_info():
    """Verify ticker.fast_info matches modern yfinance 0.2+ FastInfo attributes."""
    t = Ticker("RELIANCE.NS")
    fi = t.fast_info

    assert fi.currency == "INR"
    assert fi.exchange == "NSE"
    assert fi.quote_type == "EQUITY"
    assert fi.last_price > 0
    assert fi.previous_close > 0
    assert fi.open > 0
    assert fi.day_high >= fi.day_low
    assert fi.market_cap is not None and fi.market_cap > 1e11
    assert fi.year_high is not None
    assert fi.year_low is not None

    d = fi.to_dict()
    assert isinstance(d, dict)
    assert "last_price" in d


def test_yfinance_info_schema():
    """Verify ticker.info contains standard yfinance keys."""
    t = Ticker("RELIANCE.NS")
    info = t.info

    assert isinstance(info, dict)
    assert info["symbol"] == "RELIANCE.NS"
    assert info["currency"] == "INR"
    assert info["exchange"] == "NSE"
    assert info["quoteType"] == "EQUITY"
    assert "currentPrice" in info and info["currentPrice"] > 0
    assert "marketCap" in info and info["marketCap"] > 0
    assert "fiftyTwoWeekHigh" in info
    assert "fiftyTwoWeekLow" in info
    assert "trailingPE" in info
    assert "bookValue" in info
    assert "dividendYield" in info
    assert "returnOnEquity" in info
    assert "longBusinessSummary" in info


def test_yfinance_history_actions():
    """Verify ticker.history() with actions=True includes Dividends & Stock Splits."""
    t = Ticker("TCS")
    df = t.history(period="6mo", actions=True)

    assert isinstance(df, pd.DataFrame)
    assert not df.empty
    expected_cols = ["Open", "High", "Low", "Close", "Adj Close", "Volume", "Dividends", "Stock Splits"]
    assert list(df.columns) == expected_cols
    assert isinstance(df.index, pd.DatetimeIndex)
    assert df.index.name == "Date"
    assert len(df) > 50


def test_yfinance_statement_methods_and_aliases():
    """Verify modern get_income_stmt, get_balance_sheet, get_cash_flow and property aliases."""
    t = Ticker("INFY")

    # get_income_stmt / income_stmt / quarterly_income_stmt
    df_inc = t.get_income_stmt(freq="yearly")
    assert isinstance(df_inc, pd.DataFrame)
    assert not df_inc.empty
    assert t.income_stmt.equals(df_inc)
    assert t.financials.equals(df_inc)

    df_q_inc = t.get_income_stmt(freq="quarterly")
    assert isinstance(df_q_inc, pd.DataFrame)
    assert not df_q_inc.empty
    assert t.quarterly_income_stmt.equals(df_q_inc)
    assert t.quarterly_financials.equals(df_q_inc)

    # get_balance_sheet / balance_sheet
    df_bs = t.get_balance_sheet(freq="yearly")
    assert isinstance(df_bs, pd.DataFrame)
    assert not df_bs.empty
    assert t.balance_sheet.equals(df_bs)

    # get_cash_flow / cash_flow / cashflow
    df_cf = t.get_cash_flow(freq="yearly")
    assert isinstance(df_cf, pd.DataFrame)
    assert not df_cf.empty
    assert t.cash_flow.equals(df_cf)
    assert t.cashflow.equals(df_cf)


def test_yfinance_calendar_and_targets():
    """Verify calendar and analyst_price_targets properties."""
    t = Ticker("RELIANCE")

    cal = t.calendar
    assert isinstance(cal, dict)
    assert "Earnings Date" in cal

    targets = t.analyst_price_targets
    assert isinstance(targets, dict)
    assert "current" in targets
    assert "mean" in targets


def test_yfinance_history_metadata_and_valuation():
    """Verify history_metadata and valuation_measures matching yfinance 1.7.0."""
    t = Ticker("RELIANCE")

    meta = t.history_metadata
    assert isinstance(meta, dict)
    assert meta["currency"] == "INR"
    assert meta["instrumentType"] == "EQUITY"
    assert "regularMarketPrice" in meta
    assert "validRanges" in meta

    vm = t.valuation_measures
    assert isinstance(vm, pd.DataFrame)
    assert not vm.empty
    assert "Metric" in vm.columns


def test_yfinance_tickers_group():
    """Verify yf.Tickers multi-ticker collection matching yfinance 1.7.0."""
    group = Tickers(["RELIANCE", "TCS"])
    assert len(group) == 2
    assert "RELIANCE" in group.tickers
    assert group["TCS"].symbol == "TCS"

    df_hist = group.history(period="1mo")
    assert isinstance(df_hist, pd.DataFrame)
    assert not df_hist.empty


def test_yfinance_sector_and_industry():
    """Verify yf.Sector and yf.Industry matching yfinance 1.4.0+."""
    sec = Sector("technology")
    assert sec.name == "Information Technology"
    df_top = sec.top_companies
    assert isinstance(df_top, pd.DataFrame)
    assert not df_top.empty
    assert "symbol" in df_top.columns


def test_yfinance_corporate_actions():
    """Verify dividends and stock splits series."""
    t = Ticker("RELIANCE")

    divs = t.dividends
    assert isinstance(divs, pd.Series)
    assert divs.name == "Dividends"

    splits = t.splits
    assert isinstance(splits, pd.Series)
    assert splits.name == "Stock Splits"


def test_yfinance_options_chain():
    """Verify options chain expiries and calls/puts structure."""
    t = Ticker("RELIANCE")

    expiries = t.options
    assert isinstance(expiries, tuple)
    assert len(expiries) > 0

    chain = t.option_chain(expiries[0])
    assert hasattr(chain, "calls")
    assert hasattr(chain, "puts")
    assert isinstance(chain.calls, pd.DataFrame)
    assert isinstance(chain.puts, pd.DataFrame)
    assert "strike" in chain.calls.columns
    assert "lastPrice" in chain.calls.columns
    assert "impliedVolatility" in chain.calls.columns
    assert "openInterest" in chain.calls.columns


def test_invalid_ticker_graceful_handling():
    """Verify invalid/delisted tickers do not crash the application and return clean empty structures."""
    # 1. Default mode (raise_errors=False)
    t = Ticker("NONEXISTENT_TICKER_9999", raise_errors=False)
    assert t.financials.empty
    assert t.balance_sheet.empty
    assert t.cash_flow.empty
    assert t.shareholding.empty
    assert t.history(period="5d").empty
    assert isinstance(t.info, dict)

    # 2. Strict mode (raise_errors=True)
    with pytest.raises(Exception):
        t_strict = Ticker("NONEXISTENT_TICKER_9999", raise_errors=True)
        _ = t_strict.financials


def test_stock_vs_etf_structural_parity():
    """Verify distinct behavioral differences between individual equities (MOTHERSON) and ETFs (MIDCAPIETF)."""
    # 1. Stock: MOTHERSON
    motherson = Ticker("MOTHERSON")
    assert motherson.info["quoteType"] == "EQUITY"
    assert motherson.fast_info.last_price > 0
    assert not motherson.financials.empty
    assert not motherson.shareholding.empty
    assert len(motherson.concalls) > 0

    # 2. ETF: MIDCAPIETF
    etf = Ticker("MIDCAPIETF")
    assert etf.info["quoteType"] == "ETF"
    assert etf.fast_info.last_price > 0
    assert etf.financials.empty  # ETFs do not have company P&L statements
    hist_e = etf.history(period="5d")
    assert not hist_e.empty
    assert "Close" in hist_e.columns
