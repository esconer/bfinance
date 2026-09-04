"""Offline verification for screens.py (fake ratio-dicts) + tiny live smoke.

Unit contract (from QuoteEngine.build_info_dict in market/quotes.py):
- returnOnCapitalEmployed is raw percent (20.0 == 20%)
- returnOnEquity / dividendYield are decimal fractions (0.18 == 18%)
- trailingPE / debtToEquity raw; cagrs values are '12%' strings.
"""
from types import SimpleNamespace

import pandas as pd
import pytest

from bfinance.market.quotes import QuoteEngine
from bfinance.models.company import CompanyProfile, TopRatios
from bfinance.screens import screens


def _fake(info):
    return SimpleNamespace(info=info, symbol="FAKE")


def _base_good_debt(de=0.1):
    return {
        "returnOnCapitalEmployed": 25.0,
        "returnOnEquity": 0.20,
        "marketCapInCr": 15000.0,
        "debtToEquity": de,
        "trailingPE": 18.0,
        "dividendYield": 0.01,
        "shortName": "Fake Ltd",
        "currentPrice": 100.0,
        "bookValue": 50.0,
    }


def _base_good_value(pe=15.0, roe=0.18, sales="15%", profit="12%"):
    return {
        "returnOnCapitalEmployed": 22.0,
        "returnOnEquity": roe,
        "marketCapInCr": 12000.0,
        "trailingPE": pe,
        "pegRatio": 1.2,
        "cagrs": {
            "Compounded Sales Growth": {"5 Years": sales, "3 Years": sales},
            "Compounded Profit Growth": {"5 Years": profit, "3 Years": profit},
        },
    }


def test_debt_free_rejects_high_de():
    info = _base_good_debt(de=0.5)
    assert screens.debt_free_compounders.filter_fn(_fake(info)) is False


def test_debt_free_rejects_missing_debt():
    info = _base_good_debt()
    info.pop("debtToEquity")
    assert screens.debt_free_compounders.filter_fn(_fake(info)) is False
    info2 = _base_good_debt()
    info2["debtToEquity"] = None
    assert screens.debt_free_compounders.filter_fn(_fake(info2)) is False


def test_debt_free_accepts_good():
    info = _base_good_debt(de=0.1)
    assert screens.debt_free_compounders.filter_fn(_fake(info)) is True


def test_undervalued_rejects_high_pe():
    info = _base_good_value(pe=25.0)
    assert screens.undervalued_growth.filter_fn(_fake(info)) is False


def test_undervalued_rejects_zero_growth():
    info = _base_good_value(pe=15.0, sales="0%", profit="0%")
    info.pop("pegRatio", None)
    assert screens.undervalued_growth.filter_fn(_fake(info)) is False


def test_undervalued_accepts_good():
    info = _base_good_value(pe=15.0, sales="15%", profit="12%")
    assert screens.undervalued_growth.filter_fn(_fake(info)) is True


def test_units_roe_decimal_fraction_via_builder():
    # Construct dict exactly as QuoteEngine.build_info_dict outputs.
    profile = CompanyProfile(
        symbol="FAKE",
        company_id=1,
        name="Fake Ltd",
        ratios=TopRatios(
            roe=18.0,
            roce=25.0,
            market_cap=15000.0,
            stock_pe=15.0,
            debt_to_equity=0.1,
            dividend_yield=1.0,
            book_value=50.0,
            current_price=100.0,
        ),
        cagrs={
            "Compounded Sales Growth": {"5 Years": "15%", "3 Years": "14%"},
            "Compounded Profit Growth": {"5 Years": "12%", "3 Years": "11%"},
        },
    )
    info = QuoteEngine.build_info_dict(profile)
    # Builder contract: ROE decimal fraction, ROCE raw percent.
    assert info["returnOnEquity"] == pytest.approx(0.18)
    assert info["returnOnCapitalEmployed"] == pytest.approx(25.0)
    # 0.18 must be treated as 18% (>=15 threshold), not 0.18%.
    assert (info["returnOnEquity"] * 100) == pytest.approx(18.0)
    assert screens.undervalued_growth.filter_fn(_fake(info)) is True
    assert screens.debt_free_compounders.filter_fn(_fake(info)) is True


def test_coffee_can_requires_history():
    # Point-in-time high ROCE/ROE/mcap alone must NOT pass.
    info = {
        "returnOnCapitalEmployed": 25.0,
        "returnOnEquity": 0.20,
        "marketCapInCr": 10000.0,
    }
    assert screens.coffee_can.filter_fn(_fake(info)) is False


def test_coffee_can_accepts_with_history():
    info = {
        "returnOnCapitalEmployed": 25.0,
        "returnOnEquity": 0.20,
        "marketCapInCr": 10000.0,
        "cagrs": {
            "Compounded Sales Growth": {"10 Years": "12%"},
            "Compounded Profit Growth": {"10 Years": "10%"},
        },
    }
    assert screens.coffee_can.filter_fn(_fake(info)) is True


def _fake_with_ratios(info, ratios):
    ns = SimpleNamespace(info=info, symbol="FAKE")
    ns.custom_ratios = ratios
    return ns


def test_debt_free_zero_debt_via_statement_fallback():
    # Zero-debt companies (e.g. INFY) report no screener "Debt to equity"
    # value; the statement-computed fallback (Borrowings==0) must PASS them.
    info = _base_good_debt()
    info.pop("debtToEquity")
    assert screens.debt_free_compounders.filter_fn(
        _fake_with_ratios(info, {"debt_to_equity": 0.0})) is True


def test_debt_free_high_de_via_statement_fallback():
    info = _base_good_debt()
    info.pop("debtToEquity")
    assert screens.debt_free_compounders.filter_fn(
        _fake_with_ratios(info, {"debt_to_equity": 0.45})) is False


@pytest.mark.live
def test_live_smoke_shape():
    from bfinance.utils.exceptions import RateLimitExceededError, UpstreamServiceError

    try:
        df = screens.debt_free_compounders.run(
            universe=["RELIANCE", "TCS", "INFY"], max_stocks=2
        )
    except (RateLimitExceededError, UpstreamServiceError) as e:
        pytest.skip(f"upstream throttled: {e}")
    assert isinstance(df, pd.DataFrame)
    expected = ["Symbol", "Name", "Price", "MarketCap_Cr", "PE", "ROCE_%", "ROE_%", "DivYield_%"]
    for col in expected:
        assert col in df.columns
    records = df.to_dict(orient="records")
    assert isinstance(records, list)
    assert len(records) <= 2
    for rec in records:
        for k in expected:
            assert k in rec
