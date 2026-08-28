"""
Tests for Screener fundamental ingestion, 10-year statements, concalls, sector hierarchy, and ratios.
"""

import pytest
import pandas as pd
from bfinance.screener.client import ScreenerClient
from bfinance.ticker import Ticker


@pytest.mark.asyncio
async def test_screener_profile_extraction(screener_client):
    """Verify live Screener extraction returns complete profile with 10Y financials."""
    profile = await screener_client.get_company_profile("RELIANCE")
    
    assert profile.symbol == "RELIANCE"
    assert "Reliance" in profile.name
    assert profile.ratios.market_cap is not None
    assert profile.ratios.market_cap > 100000.0 # Reliance market cap > 1 Lakh Cr
    assert profile.ratios.roce is not None
    assert profile.ratios.stock_pe is not None

    # Check 10-year Financial Statements
    pnl = profile.profit_loss
    assert len(pnl.headers) >= 8
    assert "Sales" in pnl.rows
    assert "Net Profit" in pnl.rows

    # Check Balance Sheet
    bs = profile.balance_sheet
    assert len(bs.headers) >= 8
    assert "Borrowings" in bs.rows or "Total Liabilities" in bs.rows

    # Check Shareholding (Quarterly and Annual)
    assert len(profile.shareholding.headers) >= 8
    assert len(profile.shareholding_yearly.headers) >= 6

    # Check Sector Hierarchy
    assert profile.sector == "Energy"
    assert len(profile.indices) > 0

    # Check Analysis Insights
    assert isinstance(profile.analysis.pros, list)
    assert isinstance(profile.analysis.cons, list)


@pytest.mark.asyncio
async def test_screener_concalls_and_reports(screener_client):
    """Verify conference call transcripts, audio MP3s, and annual reports."""
    profile = await screener_client.get_company_profile("TCS")
    
    assert isinstance(profile.concalls, list)
    if profile.concalls:
        first_call = profile.concalls[0]
        assert first_call.title != ""
        assert first_call.transcript_url is not None or first_call.audio_url is not None

    assert isinstance(profile.annual_reports, list)


def test_ticker_superpower_properties():
    """Verify high-level Ticker facade properties."""
    t = Ticker("RELIANCE")
    
    # 1. Financials DataFrames
    df_pnl = t.financials
    assert isinstance(df_pnl, pd.DataFrame)
    assert not df_pnl.empty

    df_bs = t.balance_sheet
    assert isinstance(df_bs, pd.DataFrame)
    assert not df_bs.empty

    # 2. Shareholding DataFrames
    df_sh = t.shareholding
    assert isinstance(df_sh, pd.DataFrame)
    assert not df_sh.empty

    df_sh_yr = t.shareholding_yearly
    assert isinstance(df_sh_yr, pd.DataFrame)
    assert not df_sh_yr.empty

    # 3. Sector & Indices
    assert t.sector == "Energy"
    assert "Nifty 50" in t.indices or "BSE Sensex" in t.indices

    # 4. Concalls
    concalls = t.concalls
    assert isinstance(concalls, list)

    # 5. Pros & Cons
    insights = t.pros_cons
    assert "pros" in insights
    assert "cons" in insights
