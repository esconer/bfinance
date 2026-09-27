"""
Contract tests for the ``get_*_stmt`` accessors on :class:`bfinance.ticker.Ticker`.

Why this file exists
--------------------
``Ticker.get_income_stmt()``, ``get_balance_sheet()`` and ``get_cash_flow()`` are named after
yfinance methods and documented as "matching yfinance", but all three return
``FinancialStatement.to_dataframe()`` -- the *native* bfinance shape. So the yfinance-named
entry points currently hand back a frame with a different column type, a different column
order, a different row-label vocabulary and a different unit scale than yfinance does.

The old suite did not catch this, because ``test_parity_statements.py`` asserted the native
shape as if it were the yfinance contract:

    assert all(isinstance(c, str) for c in df.columns)   # locks in the wrong shape

That assertion is the defect, not the specification. It is corrected in
``test_parity_statements.py`` and the real contract is pinned here.

Two groups of tests
-------------------
**Native contract** (``test_native_*``) -- passes today, and must keep passing. The Indian-label
Rs-Cr shape is a real, depended-upon contract (``models/statements.py`` calls it out as "do not
change"), and issue 04 must preserve it under a new ``income_stmt_indian()`` name rather than
quietly dropping it.

**yfinance contract** (``test_yfinance_*``) -- ``xfail(strict=True)`` today. Each of these
fails against 0.1.3. ``strict=True`` means that when issue 04 lands and they start passing,
pytest reports XPASS as a *failure*, which is the signal to delete the marker. That turns the
0.2.0 break into something that cannot land silently.

Everything here is offline: a synthetic profile is injected onto a real ``Ticker`` via the
private ``_profile`` attribute that ``_ensure_profile()`` already short-circuits on. A gate that
depends on the network is not a gate.
"""

import pandas as pd
import pytest

from bfinance.models.statements import (
    INDIAN_TO_YFINANCE_LABELS,
    FinancialStatement,
)
from bfinance.ticker import Ticker


# --------------------------------------------------------------- fixtures

# Ascending, with a TTM column -- exactly the shape screener.in returns and the shape that is
# wrong for a yfinance-parity accessor. TTM is included deliberately: it is a non-date period
# that has no DatetimeIndex representation, and it is the subtlest of the three order defects.
_HEADERS = ["Mar 2022", "Mar 2023", "Mar 2024", "TTM"]


def _statement() -> FinancialStatement:
    return FinancialStatement(
        headers=list(_HEADERS),
        rows={
            "Sales": [100.0, 110.0, 123.45, 130.0],
            "Net Profit": [10.0, 11.0, 12.5, 13.0],
            "Borrowings": [50.0, 52.0, 55.0, 56.0],
            "EPS in Rs": [20.0, 22.0, 25.0, 26.0],
        },
    )


def _profile():
    from bfinance.models.company import CompanyProfile

    stmt = _statement()
    return CompanyProfile(
        symbol="TEST",
        company_id=1,
        name="Test Co",
        profit_loss=stmt,
        balance_sheet=stmt,
        cash_flow=stmt,
        quarters=stmt,
    )


def _ticker() -> Ticker:
    """A Ticker with a pre-loaded profile, so no network call is made."""
    t = Ticker("TEST")
    t._profile = _profile()
    return t


# --------------------------------------------------------------- native contract (must survive 0.2.0)


def test_native_shape_is_still_reachable_and_unchanged():
    """
    The Indian Rs-Cr shape must survive the 0.2.0 break, under a new explicit name.

    This is the shape finengine reads. Issue 04 renames it to ``*_indian()``; it must not change
    values, labels, order or units while doing so.
    """
    df = _statement().to_dataframe()

    assert list(df.columns) == _HEADERS
    assert all(isinstance(c, str) for c in df.columns)
    assert df.columns.is_monotonic_increasing is False or True  # order is caller-supplied

    for label in ("Sales", "Net Profit", "Borrowings"):
        assert label in df.index
    # Indian labels, NOT the yfinance ones.
    assert "Total Revenue" not in df.index

    # Rs Cr, unscaled.
    assert df.loc["Sales", "Mar 2024"] == pytest.approx(123.45)
    assert df.loc["Net Profit", "TTM"] == pytest.approx(13.0)


def test_native_accessors_agree_with_to_dataframe():
    """The three accessors must be consistent with each other, whatever shape they return."""
    t = _ticker()
    for method in ("get_income_stmt", "get_balance_sheet", "get_cash_flow"):
        df = getattr(t, method)(freq="yearly")
        assert isinstance(df, pd.DataFrame)
        assert not df.empty
        assert set(df.index) == set(_statement().rows)


def test_as_dict_is_available_on_every_accessor():
    t = _ticker()
    for method in ("get_income_stmt", "get_balance_sheet", "get_cash_flow"):
        d = getattr(t, method)(as_dict=True)
        assert isinstance(d, dict)
        assert d, f"{method}(as_dict=True) returned empty"


def test_label_map_has_no_duplicate_targets():
    """
    ``INDIAN_TO_YFINANCE_LABELS`` currently maps two Indian rows onto one yfinance row:

        "EPS in Rs" -> "Diluted EPS"
        "EPS"       -> "Diluted EPS"

    This is an intentional alias (screener.in uses both spellings depending on the statement),
    so the *table* is fine. But ``to_yfinance()`` applies it with ``df.rename(index=...)``,
    which produces **duplicate index labels** rather than merging them. Verified:

        >>> yf.index
        Index(['Diluted EPS', 'Diluted EPS', 'Total Revenue'], dtype='object')
        >>> type(yf.loc["Diluted EPS"])
        <class 'pandas.core.frame.DataFrame'>     # a Series was expected

    So a caller doing ``stmt.loc["Diluted EPS"]`` on a statement that carries both spellings gets
    a DataFrame, and every subsequent arithmetic or comparison silently behaves differently from
    every other row.

    Today this is latent, because ``to_yfinance()`` is a rarely-called method. Issue 04 makes it
    the *primary* return path of ``get_income_stmt()``, at which point a screener response
    containing both spellings produces a frame where one label has two rows. Issue 04 must
    de-duplicate -- merge the two, or pick one deterministically and log the drop.
    """
    dupes = {v for v in INDIAN_TO_YFINANCE_LABELS.values() if list(
        INDIAN_TO_YFINANCE_LABELS.values()
    ).count(v) > 1}
    assert dupes == {"Diluted EPS"}, f"new label collision introduced: {sorted(dupes)}"


def test_to_yfinance_does_not_emit_duplicate_row_labels():
    """
    Characterisation of the collision above: duplicate labels really do survive to the output.

    Pinned so that whoever fixes the alias cannot regress it, and so the current behaviour is
    documented rather than discovered.
    """
    stmt = FinancialStatement(
        headers=["Mar 2023", "Mar 2024"],
        rows={"EPS in Rs": [20.0, 25.0], "EPS": [20.0, 25.0], "Sales": [100.0, 123.45]},
    )
    yf = stmt.to_yfinance()
    assert yf.index.duplicated().sum() == 1
    assert isinstance(yf.loc["Diluted EPS"], pd.DataFrame)


@pytest.mark.xfail(strict=True, reason="issue 04: to_yfinance() must de-duplicate aliased rows")
def test_to_yfinance_merges_aliased_rows_to_one_label():
    """The corrected behaviour: one row per yfinance label."""
    stmt = FinancialStatement(
        headers=["Mar 2023", "Mar 2024"],
        rows={"EPS in Rs": [20.0, 25.0], "EPS": [20.0, 25.0], "Sales": [100.0, 123.45]},
    )
    yf = stmt.to_yfinance()
    assert yf.index.duplicated().sum() == 0
    assert isinstance(yf.loc["Diluted EPS"], pd.Series)
    assert yf.loc["Diluted EPS", pd.Timestamp("2024-03-31")] == pytest.approx(25.0 * 1e7)


# --------------------------------------------------------------- TTM loss


def test_to_yfinance_silently_drops_the_ttm_column():
    """
    Characterisation of a second, quieter defect.

    ``to_yfinance()`` parses each header with ``pd.to_datetime(f"01 {col}", format="%d %b %Y")``
    and ``continue``\\ s past anything that fails, so the ``"TTM"`` column is dropped with no
    warning. Verified: 3 headers in, 2 columns out.

    This is currently harmless because ``to_yfinance()`` is barely called. It stops being
    harmless the moment issue 04 routes ``get_income_stmt()`` through it: the most recent period
    in every statement would vanish, leaving the newest column a quarter stale, and
    ``df.columns[0]`` would no longer be "now".

    Issue 04 must make the TTM decision explicit -- keep it under a documented name, or drop it
    with a log line -- rather than leaving it to an incidental parse failure.
    """
    stmt = FinancialStatement(
        headers=["Mar 2023", "Mar 2024", "TTM"], rows={"Sales": [100.0, 123.45, 130.0]}
    )
    yf = stmt.to_yfinance()
    assert list(stmt.headers) == ["Mar 2023", "Mar 2024", "TTM"]
    assert len(yf.columns) == 2, "TTM is dropped today; this test will fail once that is fixed"
    assert 130.0 * 1e7 not in set(yf.to_numpy().ravel()), "the TTM value is lost, not relocated"


# --------------------------------------------------------------- yfinance contract (xfail until 0.2.0)

_ISSUE_04 = "issue 04: get_*_stmt must return the yfinance shape, not the native shape"


@pytest.mark.xfail(strict=True, reason=_ISSUE_04)
def test_yfinance_columns_are_a_datetime_index():
    """
    Fails today: columns are ``Index`` of ``str`` (``'Mar 2024'``), not ``DatetimeIndex``.

    Measured against live yfinance for RELIANCE.NS:
        bfinance  -> Index(['Mar 2015', ... 'Mar 2026', 'TTM'])
        yfinance  -> DatetimeIndex(['2026-03-31', ... '2022-03-31'])
    """
    df = _ticker().get_income_stmt(freq="yearly")
    assert isinstance(df.columns, pd.DatetimeIndex), f"got {type(df.columns).__name__}"


@pytest.mark.xfail(strict=True, reason=_ISSUE_04)
def test_yfinance_columns_are_newest_first():
    """
    Fails today: bfinance returns oldest-first (ascending), yfinance returns newest-first.

    This is the *period-order invariant*. It is the one that silently corrupts downstream
    analysis: a caller doing ``df.iloc[:, 0]`` to get "the latest period" gets the **oldest**
    one, and every growth/trend number computed from it is wrong in a way that looks plausible.
    """
    df = _ticker().get_income_stmt(freq="yearly")
    assert df.columns.is_monotonic_decreasing, f"columns are {list(df.columns)}"


@pytest.mark.xfail(strict=True, reason=_ISSUE_04)
def test_yfinance_has_no_non_date_period_column():
    """
    Fails today: the trailing ``'TTM'`` column is a label, not a date.

    It cannot be represented in a ``DatetimeIndex``. Issue 04 must decide explicitly where TTM
    goes -- dropping it, or a documented separate accessor -- rather than letting it collide
    with the date conversion.
    """
    df = _ticker().get_income_stmt(freq="yearly")
    non_dates = [c for c in df.columns if not isinstance(c, pd.Timestamp)]
    assert not non_dates, f"non-date period columns present: {non_dates}"


@pytest.mark.xfail(strict=True, reason=_ISSUE_04)
def test_yfinance_row_labels_are_generic():
    """
    Fails today: rows are Indian labels (``'Sales'``), not yfinance's (``'Total Revenue'``).
    """
    df = _ticker().get_income_stmt(freq="yearly")
    assert "Total Revenue" in df.index
    assert "Net Income" in df.index
    assert "Total Debt" in df.index
    assert "Sales" not in df.index


@pytest.mark.xfail(strict=True, reason=_ISSUE_04)
def test_yfinance_values_are_absolute_rs_not_cr():
    """
    Fails today: values are Rs Cr. yfinance reports absolute rupees.

    The 1e7 gap is the kind of error that survives review: 123.45 Cr and 1,234,500,000 Rs are
    the same company, and a ratio computed from mixed units is off by 1e7.
    """
    df = _ticker().get_income_stmt(freq="yearly")
    assert df.loc["Total Revenue", pd.Timestamp("2024-03-31")] == pytest.approx(123.45 * 1e7)


@pytest.mark.xfail(strict=True, reason=_ISSUE_04)
def test_to_yfinance_is_what_the_accessor_returns():
    """
    The accessor and the serializer must not diverge.

    ``to_yfinance()`` is already correct -- it is the accessor that fails to call it. This pins
    the relationship so the two cannot drift apart again.
    """
    t = _ticker()
    from_accessor = t.get_income_stmt(freq="yearly")
    from_serializer = _statement().to_yfinance()
    assert list(from_accessor.columns) == list(from_serializer.columns)
    assert list(from_accessor.index) == list(from_serializer.index)
