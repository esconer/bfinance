"""
A ragged statement row must be a typed bfinance error, not a raw pandas ValueError.

``FinancialStatement.to_dataframe`` delegated straight to
``pd.DataFrame(self.rows, index=self.headers).T``. When a row's cell count does not
match ``len(headers)`` — which happens whenever upstream changes a table shape, and
whenever a cached profile is round-tripped — pandas raises::

    ValueError: Length of values (2) does not match length of index (3)

A raw ``ValueError`` is not a ``BFinanceError``, so it escapes the
``except (TickerNotFoundError, UpstreamServiceError, BFinanceError)`` handlers in
``Ticker`` and reaches the user as an untyped crash with no indication of which
metric is malformed.

To be clear about what is *not* claimed here: pandas does **not** silently
misattribute periods. The ragged row raises; it does not slide values under the
wrong column. The defect is the exception *type* and the missing attribution.
"""

import pandas as pd
import pytest

from bfinance.models.statements import FinancialStatement
from bfinance.utils.exceptions import BFinanceError, TickerNotFoundError, UpstreamServiceError


HEADERS = ["Mar 2024", "Mar 2023", "Mar 2022"]


def test_short_row_raises_a_typed_bfinance_error():
    stmt = FinancialStatement(headers=HEADERS, rows={"Sales": [100.0, 90.0]})
    with pytest.raises(BFinanceError):
        stmt.to_dataframe()


def test_long_row_raises_a_typed_bfinance_error():
    stmt = FinancialStatement(headers=["Mar 2024", "Mar 2023"], rows={"Sales": [1.0, 2.0, 3.0]})
    with pytest.raises(BFinanceError):
        stmt.to_dataframe()


def test_no_raw_valueerror_escapes():
    """The exact pandas failure this replaces."""
    stmt = FinancialStatement(headers=HEADERS, rows={"Sales": [100.0, 90.0]})
    try:
        stmt.to_dataframe()
    except BFinanceError:
        pass
    except ValueError as exc:  # pragma: no cover - this is the failure being removed
        pytest.fail(f"raw ValueError still escapes: {exc}")
    else:  # pragma: no cover
        pytest.fail("ragged row was accepted")


def test_error_names_the_offending_metric():
    stmt = FinancialStatement(headers=HEADERS, rows={"Sales": [1.0, 2.0, 3.0], "Net Profit": [1.0]})
    with pytest.raises(BFinanceError) as excinfo:
        stmt.to_dataframe()
    message = str(excinfo.value)
    assert "Net Profit" in message
    assert "Sales" not in message, "only the offending metric should be named"


def test_error_reports_both_lengths():
    stmt = FinancialStatement(headers=HEADERS, rows={"Sales": [1.0, 2.0]})
    with pytest.raises(BFinanceError) as excinfo:
        stmt.to_dataframe()
    message = str(excinfo.value)
    assert "3" in message and "2" in message


def test_error_is_caught_by_the_ticker_guards():
    """ticker.py catches exactly this tuple; a new error class outside it is useless."""
    stmt = FinancialStatement(headers=HEADERS, rows={"Sales": [1.0]})
    try:
        stmt.to_dataframe()
    except (TickerNotFoundError, UpstreamServiceError, BFinanceError):
        caught = True
    except Exception:  # pragma: no cover - the regression being prevented
        caught = False
    else:  # pragma: no cover
        caught = False
    assert caught


def test_to_yfinance_raises_the_same_typed_error():
    """to_yfinance() delegates to to_dataframe(), so it must not leak a raw error either."""
    stmt = FinancialStatement(headers=HEADERS, rows={"Sales": [1.0, 2.0]})
    with pytest.raises(BFinanceError):
        stmt.to_yfinance()


def test_index_orient_also_validates():
    stmt = FinancialStatement(headers=HEADERS, rows={"Sales": [1.0, 2.0]})
    with pytest.raises(BFinanceError):
        stmt.to_dataframe(orient="index")


def test_only_one_bad_row_is_reported_at_a_time():
    """The first offender in iteration order is named; that is enough to fix the parse."""
    stmt = FinancialStatement(headers=HEADERS, rows={"Expenses": [1.0, 2.0]})
    with pytest.raises(BFinanceError) as excinfo:
        stmt.to_dataframe()
    assert "Expenses" in str(excinfo.value)


# --------------------------------------------------------------------------- no regressions


def test_well_formed_statement_is_unchanged():
    stmt = FinancialStatement(
        headers=["Mar 2024", "Mar 2023"],
        rows={"Sales": [100.0, 90.0], "Net Profit": [20.0, 18.0]},
    )
    df = stmt.to_dataframe()
    assert list(df.index) == ["Sales", "Net Profit"]
    assert list(df.columns) == ["Mar 2024", "Mar 2023"]
    assert df.loc["Sales", "Mar 2024"] == 100.0


def test_none_valued_cells_are_still_allowed():
    """Ragged means wrong *count*, not None. A None in a correctly sized row is normal."""
    stmt = FinancialStatement(headers=["Mar 2024", "Mar 2023"], rows={"Sales": [100.0, None]})
    df = stmt.to_dataframe()
    assert len(df) == 1 and len(df.columns) == 2


def test_empty_statement_still_returns_an_empty_frame():
    assert FinancialStatement().to_dataframe().empty
    assert FinancialStatement(headers=HEADERS).to_dataframe().empty
    assert FinancialStatement(rows={"Sales": [1.0]}).to_dataframe().empty


def test_row_with_zero_periods_is_a_degenerate_empty_statement_not_a_crash():
    stmt = FinancialStatement(headers=[], rows={"Sales": []})
    assert stmt.to_dataframe().empty


def test_get_metric_still_works_and_is_not_affected():
    stmt = FinancialStatement(headers=HEADERS, rows={"Sales": [100.0, 90.0, 80.0]})
    assert stmt.get_metric("Sales") == {"Mar 2024": 100.0, "Mar 2023": 90.0, "Mar 2022": 80.0}


def test_well_formed_to_yfinance_shape_is_unchanged():
    stmt = FinancialStatement(headers=["Mar 2024", "Mar 2023"], rows={"Sales": [1.0, 2.0]})
    df = stmt.to_yfinance()
    assert isinstance(df.columns, pd.DatetimeIndex)
    assert len(df.columns) == 2
