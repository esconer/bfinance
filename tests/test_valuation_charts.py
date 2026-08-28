"""
Tests for Screener valuation multiples and price timeseries engine.
"""

import pytest
import pandas as pd
from bfinance.screener.client import ScreenerClient
from bfinance.ticker import Ticker


@pytest.mark.asyncio
async def test_historical_price_timeseries(screener_client):
    """Verify historical daily price timeseries."""
    df = await screener_client.get_chart_timeseries("RELIANCE", metric="price", days=365)
    
    assert isinstance(df, pd.DataFrame)
    assert not df.empty
    assert "Price" in df.columns
    assert isinstance(df.index, pd.DatetimeIndex)
    assert len(df) > 100


@pytest.mark.asyncio
async def test_historical_valuation_multiples(screener_client):
    """Verify historical P/E ratio and margins timeseries."""
    df_pe = await screener_client.get_chart_timeseries("RELIANCE", metric="pe", days=365)
    assert isinstance(df_pe, pd.DataFrame)
    assert not df_pe.empty
    assert "Price to Earning" in df_pe.columns or "Median PE" in df_pe.columns

    df_margins = await screener_client.get_chart_timeseries("RELIANCE", metric="margins", days=365)
    assert isinstance(df_margins, pd.DataFrame)
    assert not df_margins.empty
    assert "OPM" in df_margins.columns or "GPM" in df_margins.columns


def test_ticker_valuation_history():
    """Verify Ticker.valuation_history() synchronous wrapper."""
    t = Ticker("TCS")
    df = t.valuation_history(metric="pe", days=180)
    assert isinstance(df, pd.DataFrame)
    assert not df.empty
