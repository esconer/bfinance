"""
Test suite validating Custom Ratios Engine and Screener Ratio Search.
"""

import pytest
import bfinance as bf


def test_screener_ratio_search():
    """Verify searching ratio catalog returns official definitions and units from Screener."""
    results = bf.ratios.search("Piotroski")
    assert isinstance(results, list)
    assert len(results) > 0
    first = results[0]
    assert "name" in first
    assert "description" in first


def test_calculated_custom_ratios():
    """Verify calculation of advanced institutional ratios."""
    t = bf.Ticker("RELIANCE")
    r = t.custom_ratios

    assert isinstance(r, dict)
    assert "piotroski_score" in r
    assert 0 <= r["piotroski_score"] <= 9

    assert "graham_number" in r
    assert r["graham_number"] is not None
    assert r["graham_number"] > 100.0

    assert "enterprise_value_cr" in r
    assert r["enterprise_value_cr"] > 100000.0

    # Test individual facade properties
    assert t.piotroski_score == r["piotroski_score"]
    assert t.graham_number == r["graham_number"]
    assert t.enterprise_value == r["enterprise_value_cr"]
