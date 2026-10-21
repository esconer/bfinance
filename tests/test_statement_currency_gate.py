"""The currency gate in ``test_dropin_financials``, exercised with no network.

The live bridge test compares bfinance's absolute-rupee statement with a yfinance one, and gates
that comparison on ``Ticker.info["financialCurrency"]``. Yahoo reports INFY.NS's statements in
USD (inherited from its ADR/SEC filings) while the quote still trades in INR, so an ungated
comparison reads as an ~83x data bug that is really the exchange rate. A guard that only runs
under ``-m live`` is a guard the required offline job cannot see fail, so the gate itself is fed
fake ``info`` dicts here.

The gate is imported from the live module rather than reimplemented: a copy could drift from
the one the comparison actually uses and still pass. ``tests/`` is not a package and pytest
prepends its own directory to ``sys.path``, so the import is by bare module name -- the same
convention ``tests/test_screener_company_id_cache.py`` uses.
"""

import pytest

from test_dropin_financials import (
    STATEMENT_CURRENCY,
    _require_statement_currency,
    _statement_currency_skip_reason,
)

#: INFY.NS's measured shape: the quote trades in INR, the statements are filed in USD.
INFY_INFO = {"currency": "INR", "financialCurrency": "USD"}

#: A symbol whose statements and quote share the rupee.
RELIANCE_INFO = {"currency": "INR", "financialCurrency": "INR"}


def test_the_contract_currency_is_the_rupee_bfinance_reports_in():
    assert STATEMENT_CURRENCY == "INR"


def test_the_gate_skips_when_yahoo_reports_the_statements_in_usd():
    """The measured INFY.NS case. The reason must name USD -- a generic skip hides the cause."""
    assert _statement_currency_skip_reason("INFY.NS", INFY_INFO) is not None
    with pytest.raises(pytest.skip.Exception) as caught:
        _require_statement_currency("INFY.NS", INFY_INFO)
    reason = str(caught.value)
    assert "USD" in reason, f"skip reason does not name the currency: {reason!r}"
    assert "INFY.NS" in reason, f"skip reason does not name the ticker: {reason!r}"
    assert "INR" in reason, f"skip reason does not say what it refused to compare: {reason!r}"


def test_the_gate_proceeds_when_yahoo_reports_the_statements_in_inr():
    """RELIANCE.NS compares rupee to rupee, so nothing is skipped."""
    assert _statement_currency_skip_reason("RELIANCE.NS", RELIANCE_INFO) is None
    # No ``pytest.raises`` here on purpose: this call must return. A skip would raise
    # ``Skipped`` and fail the test body rather than pass it.
    _require_statement_currency("RELIANCE.NS", RELIANCE_INFO)


@pytest.mark.parametrize(
    "info",
    [
        pytest.param({"currency": "INR"}, id="key-absent"),
        pytest.param({"currency": "INR", "financialCurrency": None}, id="value-none"),
        pytest.param({"currency": "INR", "financialCurrency": ""}, id="value-empty"),
    ],
)
def test_the_gate_skips_when_the_statement_currency_is_unknown(info):
    """Absent is not INR. Assuming the rupee is the bug this gate exists to prevent."""
    reason = _statement_currency_skip_reason("INFY.NS", info)
    assert reason is not None, "a missing financialCurrency was read as INR"
    assert "financialCurrency" in reason, f"reason does not say what was missing: {reason!r}"
    assert "unknown" in reason, f"reason does not say the currency is unknown: {reason!r}"
    with pytest.raises(pytest.skip.Exception) as caught:
        _require_statement_currency("INFY.NS", info)
    assert str(caught.value) == reason


def test_the_gate_reads_financial_currency_not_the_quote_currency():
    """The exact trap: both dicts say ``currency: "INR"``; only the statement currency differs."""
    usd = {"currency": "INR", "financialCurrency": "USD"}
    inr = {"currency": "INR", "financialCurrency": "INR"}
    assert _statement_currency_skip_reason("X.NS", usd) is not None
    assert _statement_currency_skip_reason("X.NS", inr) is None
