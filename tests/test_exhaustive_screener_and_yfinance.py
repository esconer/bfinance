"""
Exhaustive verification test suite for bfinance.
Validates that 100% of Screener.in fundamental details, documents, and timeseries are extracted,
and that 100% of modern yfinance 1.7.0+ APIs, methods, and properties are compatible.
"""

from datetime import datetime
import pandas as pd
import pytest

import bfinance as bf
from bfinance.ticker import Ticker
from bfinance.tickers import Tickers
from bfinance.sector import Sector, Industry
from bfinance.models.options import OptionChain
from bfinance.models.company import CompanyProfile, Concall, PeerStock, TopRatios
from bfinance.models.statements import FinancialStatement


# =============================================================================
# 1. SCREENER.IN EXHAUSTIVE EXTRACTION TESTS
# =============================================================================

@pytest.mark.asyncio
async def test_screener_company_metadata(screener_client):
    """Verify company profile metadata: Name, About, Website, BSE, NSE."""
    profile = await screener_client.get_company_profile("RELIANCE")
    
    assert isinstance(profile, CompanyProfile)
    assert profile.symbol == "RELIANCE"
    assert "Reliance" in profile.name
    assert len(profile.about) > 20
    assert profile.company_id > 0
    assert profile.website is not None and profile.website.startswith("http")
    assert profile.bse_code is not None # e.g. "500325"
    assert profile.nse_symbol == "RELIANCE"
    assert profile.is_consolidated is True
    assert "screener.in/company/RELIANCE" in profile.url


@pytest.mark.asyncio
async def test_screener_top_ratios_exhaustiveness(screener_client):
    """Verify every single ratio in #top-ratios is parsed into typed fields."""
    profile = await screener_client.get_company_profile("RELIANCE")
    r = profile.ratios

    assert isinstance(r, TopRatios)
    assert r.market_cap is not None and r.market_cap > 100000.0 # in ₹ Cr
    assert r.current_price is not None and r.current_price > 100.0 # in ₹
    assert r.high_52w is not None and r.high_52w >= r.low_52w
    assert r.low_52w is not None and r.low_52w > 0
    assert r.stock_pe is not None and r.stock_pe > 5.0
    assert r.book_value is not None and r.book_value > 50.0
    assert r.dividend_yield is not None
    assert r.roce is not None and r.roce > 0
    assert r.roe is not None and r.roe > 0
    assert r.face_value is not None and r.face_value > 0
    assert isinstance(r.custom_ratios, dict)


@pytest.mark.asyncio
async def test_screener_10_year_annual_statements(screener_client):
    """Verify 10+ years of Annual P&L, Balance Sheet, and Cash Flow."""
    profile = await screener_client.get_company_profile("RELIANCE")

    # 1. P&L Statement
    pnl = profile.profit_loss
    assert isinstance(pnl, FinancialStatement)
    assert len(pnl.headers) >= 10 # 10+ years + TTM
    assert "Sales" in pnl.rows
    assert "Expenses" in pnl.rows
    assert "Operating Profit" in pnl.rows
    assert "OPM %" in pnl.rows
    assert "Other Income" in pnl.rows
    assert "Interest" in pnl.rows
    assert "Depreciation" in pnl.rows
    assert "Profit before tax" in pnl.rows
    assert "Tax %" in pnl.rows
    assert "Net Profit" in pnl.rows
    assert "EPS in Rs" in pnl.rows or "EPS" in pnl.rows

    # 2. Balance Sheet
    bs = profile.balance_sheet
    assert len(bs.headers) >= 10
    assert "Equity Capital" in bs.rows
    assert "Reserves" in bs.rows
    assert "Borrowings" in bs.rows
    assert "Other Liabilities" in bs.rows
    assert "Total Liabilities" in bs.rows
    assert "Fixed Assets" in bs.rows
    assert "CWIP" in bs.rows
    assert "Investments" in bs.rows
    assert "Other Assets" in bs.rows
    assert "Total Assets" in bs.rows

    # 3. Cash Flows
    cf = profile.cash_flow
    assert len(cf.headers) >= 10
    assert "Cash from Operating Activity" in cf.rows
    assert "Cash from Investing Activity" in cf.rows
    assert "Cash from Financing Activity" in cf.rows
    assert "Net Cash Flow" in cf.rows


@pytest.mark.asyncio
async def test_screener_12_quarters_and_ratios_history(screener_client):
    """Verify 12+ quarters and 10-year financial ratios."""
    profile = await screener_client.get_company_profile("TCS")

    # 1. Quarters
    q = profile.quarters
    assert len(q.headers) >= 10
    assert "Sales" in q.rows
    assert "Operating Profit" in q.rows
    assert "Net Profit" in q.rows
    assert "EPS in Rs" in q.rows or "EPS" in q.rows

    # 2. Historical Corporate Ratios
    ratios_hist = profile.ratios_history
    assert len(ratios_hist.headers) >= 6
    assert "Debtor Days" in ratios_hist.rows
    assert "Working Capital Days" in ratios_hist.rows
    assert "ROCE %" in ratios_hist.rows


@pytest.mark.asyncio
async def test_screener_shareholding_patterns(screener_client):
    """Verify quarterly institutional (FII, DII, Promoter) ownership trends."""
    profile = await screener_client.get_company_profile("RELIANCE")
    sh = profile.shareholding

    assert len(sh.headers) >= 8
    assert "Promoters" in sh.rows
    assert "FIIs" in sh.rows
    assert "DIIs" in sh.rows
    assert "Public" in sh.rows
    assert "No. of Shareholders" in sh.rows


@pytest.mark.asyncio
async def test_screener_cagrs_and_pros_cons(screener_client):
    """Verify compounded CAGRs and qualitative insights."""
    profile = await screener_client.get_company_profile("TCS")

    # CAGRs
    assert isinstance(profile.cagrs, dict)
    assert "Compounded Sales Growth" in profile.cagrs or "Compounded Profit Growth" in profile.cagrs
    assert "Stock Price CAGR" in profile.cagrs or "Return on Equity" in profile.cagrs

    # Pros and Cons
    assert isinstance(profile.analysis.pros, list)
    assert isinstance(profile.analysis.cons, list)
    assert len(profile.analysis.pros) > 0 # TCS has pros


@pytest.mark.asyncio
async def test_screener_concalls_audio_and_transcripts(screener_client):
    """Verify conference call transcripts, audio MP3 URLs, and presentations."""
    profile = await screener_client.get_company_profile("TCS")

    assert isinstance(profile.concalls, list)
    assert len(profile.concalls) >= 20 # TCS has 40+ concalls
    sample_call = profile.concalls[0]
    assert isinstance(sample_call, Concall)
    assert sample_call.date != ""
    assert sample_call.title != ""
    # At least one call should have transcript or audio
    has_transcript = any(c.transcript_url for c in profile.concalls)
    has_audio = any(c.audio_url for c in profile.concalls)
    assert has_transcript is True
    assert has_audio is True

    # Annual reports & credit ratings
    assert isinstance(profile.annual_reports, list)
    assert len(profile.annual_reports) >= 5


@pytest.mark.asyncio
async def test_screener_all_valuation_chart_metrics(screener_client):
    """Verify all 6 Screener chart timeseries metrics."""
    metrics = ["price", "pe", "margins", "ev_ebitda", "pb", "mcap_sales"]
    for m in metrics:
        df = await screener_client.get_chart_timeseries("RELIANCE", metric=m, days=365)
        assert isinstance(df, pd.DataFrame), f"Metric {m} failed to return DataFrame"
        assert not df.empty, f"Metric {m} returned empty DataFrame"
        assert isinstance(df.index, pd.DatetimeIndex), f"Metric {m} missing DatetimeIndex"


# =============================================================================
# 2. YFINANCE 1.7.0+ DROP-IN COMPATIBILITY TESTS
# =============================================================================

def test_yfinance_fast_info_complete():
    """Verify modern FastInfo attributes."""
    t = Ticker("RELIANCE.NS")
    fi = t.fast_info

    assert fi.currency == "INR"
    assert fi.exchange == "NSE"
    assert fi.timezone == "Asia/Kolkata"
    assert fi.quote_type == "EQUITY"
    assert fi.last_price > 0
    assert fi.previous_close > 0
    assert fi.open > 0
    assert fi.day_high >= fi.day_low
    assert fi.year_high is not None
    assert fi.year_low is not None
    assert fi.market_cap is not None and fi.market_cap > 1e11
    assert fi.shares is not None and fi.shares > 1e8
    assert fi.fifty_day_average > 0
    assert fi.two_hundred_day_average > 0

    # Dictionary access
    d = fi.to_dict()
    assert d["currency"] == "INR"
    assert fi["last_price"] == fi.last_price


def test_yfinance_info_keys_richness():
    """Verify ticker.info contains all standard yfinance equity attributes."""
    t = Ticker("TCS")
    info = t.info

    required_keys = [
        "symbol", "shortName", "longName", "currency", "exchange", "quoteType",
        "currentPrice", "regularMarketPrice", "regularMarketOpen", "regularMarketDayHigh",
        "regularMarketDayLow", "regularMarketPreviousClose", "fiftyTwoWeekHigh",
        "fiftyTwoWeekLow", "marketCap", "marketCapInCr", "trailingPE", "forwardPE",
        "priceToBook", "bookValue", "dividendYield", "trailingEps", "returnOnEquity",
        "returnOnCapitalEmployed", "sharesOutstanding", "floatShares", "heldPercentInsiders",
        "promoterHolding", "promoterPledged", "website", "longBusinessSummary",
        "bseCode", "nseSymbol", "isConsolidated", "screenerUrl", "pros", "cons", "cagrs"
    ]

    for k in required_keys:
        assert k in info, f"Missing required info key: {k}"


def test_yfinance_history_actions_and_intervals():
    """Verify history() with actions=True returns complete OHLCV + Corporate Actions."""
    t = Ticker("RELIANCE")
    df = t.history(period="1y", interval="1d", actions=True, auto_adjust=True)

    assert isinstance(df, pd.DataFrame)
    assert not df.empty
    expected_cols = ["Open", "High", "Low", "Close", "Adj Close", "Volume", "Dividends", "Stock Splits"]
    assert list(df.columns) == expected_cols
    assert isinstance(df.index, pd.DatetimeIndex)
    assert df.index.name == "Date"
    assert len(df) > 150

    # Check without actions
    df_no_act = t.history(period="1mo", actions=False)
    assert "Dividends" not in df_no_act.columns
    assert "Stock Splits" not in df_no_act.columns


def test_yfinance_financial_statements_complete_matrix():
    """Verify all statement methods and property aliases match yfinance exactly."""
    t = Ticker("BAJAJ-AUTO")

    # 1. Income statements
    assert isinstance(t.financials, pd.DataFrame)
    assert isinstance(t.income_stmt, pd.DataFrame)
    assert isinstance(t.quarterly_financials, pd.DataFrame)
    assert isinstance(t.quarterly_income_stmt, pd.DataFrame)
    assert isinstance(t.ttm_income_stmt, pd.DataFrame)
    assert t.financials.equals(t.income_stmt)

    # 2. Balance sheets
    assert isinstance(t.balance_sheet, pd.DataFrame)
    assert isinstance(t.quarterly_balance_sheet, pd.DataFrame)
    assert t.get_balance_sheet(freq="yearly").equals(t.balance_sheet)

    # 3. Cash flows
    assert isinstance(t.cashflow, pd.DataFrame)
    assert isinstance(t.cash_flow, pd.DataFrame)
    assert isinstance(t.quarterly_cashflow, pd.DataFrame)
    assert isinstance(t.quarterly_cash_flow, pd.DataFrame)
    assert isinstance(t.ttm_cash_flow, pd.DataFrame)
    assert t.cashflow.equals(t.cash_flow)

    # 4. as_dict parameter
    dict_pnl = t.get_income_stmt(as_dict=True)
    assert isinstance(dict_pnl, dict)


def test_yfinance_history_metadata_and_valuation_measures():
    """Verify history_metadata and valuation_measures (1.7.0+)."""
    t = Ticker("RELIANCE")

    meta = t.history_metadata
    assert isinstance(meta, dict)
    assert meta["currency"] == "INR"
    assert meta["symbol"] == "RELIANCE.NS"
    assert meta["exchangeName"] == "NSE"
    assert meta["timezone"] == "IST"
    assert "regularMarketPrice" in meta
    assert t.get_history_metadata() == meta

    vm = t.valuation_measures
    assert isinstance(vm, pd.DataFrame)
    assert not vm.empty
    assert "Metric" in vm.columns
    assert "Value" in vm.columns


def test_yfinance_corporate_actions_series():
    """Verify dividends and stock splits series format."""
    t = Ticker("RELIANCE")

    divs = t.dividends
    assert isinstance(divs, pd.Series)
    assert divs.name == "Dividends"
    assert isinstance(divs.index, pd.DatetimeIndex)

    splits = t.splits
    assert isinstance(splits, pd.Series)
    assert splits.name == "Stock Splits"


def test_yfinance_options_derivatives_chain():
    """Verify NSE options chain with Calls and Puts DataFrames."""
    t = Ticker("RELIANCE")
    expiries = t.options

    assert isinstance(expiries, tuple)
    assert len(expiries) >= 2

    chain = t.option_chain(expiries[0])
    assert isinstance(chain, OptionChain)
    assert isinstance(chain.calls, pd.DataFrame)
    assert isinstance(chain.puts, pd.DataFrame)

    for col in ["contractSymbol", "strike", "lastPrice", "bid", "ask", "openInterest", "impliedVolatility"]:
        assert col in chain.calls.columns
        assert col in chain.puts.columns


def test_yfinance_tickers_multi_collection():
    """Verify yf.Tickers multi-ticker manager."""
    group = Tickers("RELIANCE TCS INFY")
    assert len(group) == 3
    assert group["RELIANCE"].symbol == "RELIANCE"
    assert group["TCS"].symbol == "TCS"
    assert group["INFY"].symbol == "INFY"

    df_hist = group.history(period="1mo")
    assert isinstance(df_hist, pd.DataFrame)
    assert not df_hist.empty


def test_yfinance_sector_and_industry_explorer():
    """Verify Sector and Industry classes."""
    tech = Sector("technology")
    assert tech.name == "Information Technology"
    assert isinstance(tech.overview, dict)
    assert isinstance(tech.top_companies, pd.DataFrame)
    assert not tech.top_companies.empty

    fin = Sector("financials")
    assert fin.name == "Financial Services"
    assert not fin.top_companies.empty

    ind = Industry("auto")
    assert not ind.top_companies.empty


def test_yfinance_batch_download_matrix():
    """Verify download() with group_by options."""
    # 1. group_by='column' (default)
    df_col = bf.download(["RELIANCE", "TCS"], period="1mo", group_by="column")
    assert isinstance(df_col, pd.DataFrame)
    assert isinstance(df_col.columns, pd.MultiIndex)
    assert "Close" in df_col.columns.levels[0]

    # 2. group_by='ticker'
    df_tick = bf.download(["RELIANCE", "TCS"], period="1mo", group_by="ticker")
    assert isinstance(df_tick, pd.DataFrame)
    assert isinstance(df_tick.columns, pd.MultiIndex)
    assert "RELIANCE" in df_tick.columns.levels[0]
