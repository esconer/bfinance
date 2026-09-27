"""Offline verification for screens.py (fake ratio-dicts) + tiny live smoke.

Unit contract (from QuoteEngine.build_info_dict in market/quotes.py):
- returnOnCapitalEmployed is raw percent (20.0 == 20%)
- returnOnEquity is a decimal fraction (0.18 == 18%)
- dividendYield is a raw PERCENT (1.5 == 1.5%), matching yfinance
- trailingPE / debtToEquity raw; cagrs values are '12%' strings.

CORRECTED 2026-09-27. This file previously claimed "returnOnEquity / dividendYield are decimal
fractions" and used a ``dividendYield: 0.01`` fixture. That is false, and it was the root cause
of two live 100x bugs in ``screens.py``:

  - ``DivYield_%`` multiplied an already-percent value by 100, so a 0.15% payer reported 15.00.
  - ``high_dividend_yield`` compared a percent against a fraction threshold (``>= 0.025``),
    so the advertised 2.5% cut was never applied and every dividend payer was admitted.

The falsehood survived because ``tests/test_dropin_info.py`` asserts the *opposite* convention
against live yfinance ("dividendYield must not be 100x off"). The suite contradicted itself and
the offline half won, because it is the half that runs without a network.
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
        # PERCENT, not a fraction. 1.5 == 1.5%. A genuine dividend payer.
        "dividendYield": 1.5,
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


# ---------------------------------------------------------------------------
# Regression tests for the dividend-yield 100x bug (fixed 2026-09-27).
#
# Before the fix, `high_dividend_yield` compared a PERCENT against a FRACTION threshold
# (`dy >= 0.025`), so its advertised 2.5% cut was never applied, and `Screen.run` multiplied
# the already-percent value by 100 again. Measured against the live universe, the screen
# returned 34 rows whose lowest reported "DivYield_%" was 15.00 -- Trent, whose real yield is
# 0.15%. The screen was a no-op that mislabelled every dividend payer in India by 100x.
#
# These tests pin both halves. The existing tests above did not catch either, which is why the
# bug shipped.


def test_dividend_yield_is_a_percent_not_a_fraction():
    """
    The convention, asserted at the source rather than described in a docstring.

    A docstring claiming the wrong convention is what caused this bug, so the contract is
    asserted against a real ``QuoteEngine.build_info_dict`` output.
    """
    profile = CompanyProfile(
        symbol="FAKE",
        company_id=1,
        name="Fake Ltd",
        ratios=TopRatios(dividend_yield=1.5, roce=25.0, market_cap=15000.0),
    )
    info = QuoteEngine.build_info_dict(profile)
    # 1.5 means 1.5%. If this ever becomes 0.015, every screen threshold below is wrong again.
    assert info["dividendYield"] == pytest.approx(1.5)


@pytest.mark.parametrize(
    "dividend_yield,expected",
    [
        (0.15, False),   # Trent-like: a real payer, nowhere near a dividend champion
        (0.31, False),   # Titan-like
        (2.4, False),    # just under the advertised 2.5% cut
        (2.5, True),     # exactly at the cut
        (3.12, True),    # TCS-like
        (6.0, True),
        (0.0, False),    # non-payer
        (None, False),   # missing must not crash or pass
    ],
)
def test_high_dividend_yield_threshold_is_actually_applied(dividend_yield, expected):
    """
    The 2.5% cut must be a real cut.

    Every one of the False cases was admitted by the pre-fix filter, because ``dy >= 0.025``
    on a percent value means "yielding above 0.025%", which every dividend payer clears.
    """
    info = {
        "dividendYield": dividend_yield,
        "returnOnCapitalEmployed": 30.0,  # comfortably above the 12% gate
    }
    assert screens.high_dividend_yield.filter_fn(_fake(info)) is expected


def test_high_dividend_yield_still_gates_on_roce():
    """A high yield must not rescue a company that fails the ROCE gate."""
    info = {"dividendYield": 9.0, "returnOnCapitalEmployed": 5.0}
    assert screens.high_dividend_yield.filter_fn(_fake(info)) is False


def test_screen_run_reports_yield_uninflated(monkeypatch):
    """
    ``DivYield_%`` must be the percent itself, not 100x it.

    ``Screen.run`` is driven with a stubbed ``Ticker`` so the whole output row is checked, not
    just the filter predicate -- the 100x lived in the row-building, which the filter tests
    above never touch.
    """
    import importlib

    # NB: `import bfinance.screens as m` does NOT give the module. `bfinance/__init__.py` does
    # `from .screens import screens`, so the attribute `bfinance.screens` is the *registry
    # instance*, and the `as` form resolves via getattr. Same shadowing applies to
    # `bfinance.download` (a function) and is an easy source of confusing AttributeErrors.
    screens_mod = importlib.import_module("bfinance.screens")

    class _StubTicker:
        def __init__(self, symbol):
            self.symbol = symbol
            self.info = {
                "shortName": "Fake Ltd",
                "currentPrice": 100.0,
                "marketCapInCr": 15000.0,
                "trailingPE": 18.0,
                "returnOnCapitalEmployed": 30.0,
                "returnOnEquity": 0.20,
                "dividendYield": 1.53,
                "bookValue": 50.0,
            }

    monkeypatch.setattr(screens_mod, "Ticker", _StubTicker)

    df = screens_mod.Screen(
        name="probe", description="probe", filter_fn=lambda t: True
    ).run(universe=["FAKE"], show_progress=False)

    assert len(df) == 1
    assert df.loc[0, "DivYield_%"] == pytest.approx(1.53)
    # ROE is a genuine fraction and must still be converted.
    assert df.loc[0, "ROE_%"] == pytest.approx(20.0)
    # ROCE is already a percent and must still pass through untouched.
    assert df.loc[0, "ROCE_%"] == pytest.approx(30.0)


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
