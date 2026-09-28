"""
Test suite validating Custom Ratios Engine and Screener Ratio Search.
"""

import pytest
import bfinance as bf


@pytest.mark.live
def test_screener_ratio_search():
    """Verify searching ratio catalog returns official definitions and units from Screener."""
    results = bf.ratios.search("Piotroski")
    assert isinstance(results, list)
    assert len(results) > 0
    first = results[0]
    assert "name" in first
    assert "description" in first


@pytest.mark.live
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

    # DO NOT "RESTORE" `r["enterprise_value_cr"] > 100000.0`.
    #
    # That assertion encoded the defect this file's fix removed. It demanded a very large
    # enterprise value, and a very large value is precisely what a *fabricated zero cash leg*
    # guarantees: the old code computed `mcap + debt - cash_hold` with `cash_hold` pinned to a
    # literal `0.0` and never otherwise set, because the upstream balance sheet does not itemise
    # cash (measured on all three fixtures in `tests/fixtures/screener/`, ten rows, no cash row --
    # re-proved every run by `tests/test_ratios_honesty.py`). So EV came back as bare market cap
    # plus debt, always large, for every company, forever. The assertion was satisfied *by the
    # bug*, so it could not fail: it is the fifth unfalsifiable assertion this project shipped.
    #
    # Enterprise Value is now reported only when all three legs of `mcap + debt - cash` are
    # actually in hand. For a real company the upstream shape omits it, so the correct live
    # expectation is the omission and its published reason -- not a number.
    assert "enterprise_value_cr" in r, "the key must be present even when the value is absent"
    ev = r["enterprise_value_cr"]
    omitted = r["bfinance_enterprise_value_omitted"]
    if ev is None:
        assert omitted, "an absent enterprise value must name the leg that is missing"
        assert isinstance(omitted, str)
    else:
        # A number is only acceptable when every leg is in hand, and then it must be the
        # arithmetic rather than something merely large.
        assert omitted is None
        assert r["bfinance_cash_cr"] is not None
        assert r["bfinance_debt_cr"] is not None
        assert ev == pytest.approx(
            t.info["marketCap"] / 1e7 + r["bfinance_debt_cr"] - r["bfinance_cash_cr"], rel=1e-6
        )

    # Test individual facade properties
    assert t.piotroski_score == r["piotroski_score"]
    assert t.graham_number == r["graham_number"]
    assert t.enterprise_value == r["enterprise_value_cr"]
    # Same omission, on the multiple built from it.
    assert t.ev_to_ebitda == r.get("ev_to_ebitda")
