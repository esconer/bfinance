"""
Test suite validating advanced extra features inspired by Screener.in:
Quantitative screens engine, multi-tab financial Excel model export, and document downloaders.
"""

from pathlib import Path
import tempfile
import pandas as pd
import pytest
import bfinance as bf


def test_prebuilt_screens():
    """Verify built-in institutional screeners run and return structured DataFrames."""
    # 1. Coffee Can screen
    df_coffee = bf.screens.coffee_can.run(max_stocks=3)
    assert isinstance(df_coffee, pd.DataFrame)
    if not df_coffee.empty:
        assert "Symbol" in df_coffee.columns
        assert "ROCE_%" in df_coffee.columns
        assert "MarketCap_Cr" in df_coffee.columns

    # 2. Magic Formula screen
    df_magic = bf.screens.magic_formula.run(max_stocks=3)
    assert isinstance(df_magic, pd.DataFrame)

    # 3. Custom Screener
    custom_screen = bf.screens.custom(
        name="Large Cap High ROE",
        filter_fn=lambda t: (t.info.get("marketCapInCr") or 0) > 50000 and ((t.info.get("returnOnEquity") or 0) * 100) > 15
    )
    df_custom = custom_screen.run(max_stocks=3)
    assert isinstance(df_custom, pd.DataFrame)


def test_export_to_excel(tmp_path):
    """Verify t.to_excel() creates a valid multi-tab Excel financial model workbook."""
    t = bf.Ticker("RELIANCE")
    excel_path = tmp_path / "RELIANCE_Financial_Model.xlsx"
    saved_path = t.to_excel(str(excel_path))

    assert Path(saved_path).exists()
    assert Path(saved_path).stat().st_size > 5000 # Valid non-empty workbook

    # Read back sheets safely with context manager
    with pd.ExcelFile(saved_path) as excel_file:
        expected_sheets = ["Overview", "Profit & Loss", "Quarters", "Balance Sheet", "Cash Flow", "Shareholding", "Ratios History"]
        for sheet in expected_sheets:
            assert sheet in excel_file.sheet_names, f"Missing sheet: {sheet}"


def test_document_download_url_resolution():
    """Verify document and concall transcript download URLs."""
    t = bf.Ticker("TCS", cache_ttl_hours=0.0)
    concalls = t.concalls
    assert len(concalls) > 0

    # Test that download helper detects transcript/audio URLs
    transcripts = [c for c in concalls if c.transcript_url]
    assert len(transcripts) > 0
    assert transcripts[0].transcript_url.startswith("http")

    # Annual reports
    reports = t.annual_reports
    assert len(reports) > 0
    assert reports[0]["url"].startswith("http")
