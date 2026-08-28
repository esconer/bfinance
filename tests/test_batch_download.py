"""
Tests for concurrent multi-ticker batch download matching yfinance.download().
"""

import pandas as pd
import pytest
from bfinance.download import download


def test_batch_download_single_ticker():
    """Verify single ticker download returns standard OHLCV DataFrame."""
    df = download("RELIANCE", period="1mo")
    assert isinstance(df, pd.DataFrame)
    assert not df.empty
    assert "Close" in df.columns
    assert "Volume" in df.columns


def test_batch_download_multi_tickers():
    """Verify multi-ticker download returns MultiIndex DataFrame."""
    df = download(["RELIANCE", "TCS"], period="1mo")
    assert isinstance(df, pd.DataFrame)
    assert not df.empty
    # Columns are MultiIndex (Metric, Ticker) e.g. ('Close', 'RELIANCE')
    assert isinstance(df.columns, pd.MultiIndex)
    assert "Close" in df.columns.levels[0]
