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
change"), and it is reached through ``FinancialStatement.to_dataframe()``, which is untouched by
the 0.2.0 change.

**yfinance contract** (``test_yfinance_*``) -- these were ``xfail(strict=True)`` until issue 04
landed. Each failed against 0.1.3, because the accessors named after yfinance methods returned
the native shape: plain-string period columns, oldest-first, a trailing ``"TTM"`` label that has no
``DatetimeIndex`` representation, crore values and Indian row labels. They are now real passes
against a real ``Ticker``, and :func:`test_the_seven_yfinance_properties_all_hold` re-checks all
seven in one place so deleting any single one of them is a failure rather than a silent loss.

Everything here is offline: a synthetic profile is injected onto a real ``Ticker`` via the
private ``_profile`` attribute that ``_ensure_profile()`` already short-circuits on. A gate that
depends on the network is not a gate.
"""

import ast
import pathlib

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

#: A deliberately NON-ascending header order, so that "the library does not impose an
#: order" is a claim with teeth: any implementation that sorted or reversed the
#: caller's headers would reorder this and fail. ``TTM`` is kept in the list because
#: moving it to the front is what breaks monotonicity most obviously.
_UNSORTED_HEADERS = ["TTM", "Mar 2022", "Mar 2023", "Mar 2024"]


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

    # "Order is caller-supplied" is a claim about what the library does *not* do, so it
    # is asserted as a negative: feed a header list that is not in any natural order and
    # require it back verbatim. A sorting or reversing implementation fails here.
    #
    # This replaces a line that read:
    #     assert df.columns.is_monotonic_increasing is False or True
    # `is` binds tighter than `or`, so that parsed as ``(x is False) or True`` -- True
    # for every possible value of x, i.e. it could never fail. It was a live, offline
    # no-op sitting in the one file that is supposed to make the 0.2.0 break loud.
    reordered = FinancialStatement(
        headers=list(_UNSORTED_HEADERS), rows={"Sales": [1.0, 2.0, 3.0, 4.0]}
    )
    got = list(reordered.to_dataframe().columns)
    assert got == _UNSORTED_HEADERS, f"to_dataframe() imposed an order on caller headers: {got}"
    # Guards the guard: if the fixture ever became ascending this check would be
    # asserting something trivially true, so assert that it is not ascending.
    assert not pd.Index(got).is_monotonic_increasing, f"fixture is no longer a real test: {got}"

    for label in ("Sales", "Net Profit", "Borrowings"):
        assert label in df.index
    # Indian labels, NOT the yfinance ones.
    assert "Total Revenue" not in df.index

    # Rs Cr, unscaled.
    assert df.loc["Sales", "Mar 2024"] == pytest.approx(123.45)
    assert df.loc["Net Profit", "TTM"] == pytest.approx(13.0)


def test_native_accessors_agree_with_to_dataframe():
    """The three accessors must be consistent with each other, whatever shape they return.

    **Updated when issue 04 landed.** It read ``set(df.index) == set(_statement().rows)`` --
    i.e. the accessors' index was the *native* Indian labels, which is the defect this whole
    file exists to end. The accessors now return the yfinance shape, so the invariant has to
    be stated against ``to_yfinance()`` instead: the three accessors must agree with each
    other, and with the serializer. The native shape is not reachable from the accessors any
    more, and pretending otherwise is what this assertion used to do -- it is pinned on
    ``to_dataframe()`` itself, in
    :func:`test_native_shape_is_still_reachable_and_unchanged`.
    """
    expected = _statement().to_yfinance()
    t = _ticker()
    for method in ("get_income_stmt", "get_balance_sheet", "get_cash_flow"):
        df = getattr(t, method)(freq="yearly")
        assert isinstance(df, pd.DataFrame)
        assert not df.empty
        assert set(df.index) == set(expected.index)
        assert list(df.columns) == list(expected.columns)
        # ...and none of them leaked an Indian label back into the yfinance path.
        assert "Sales" not in df.index, f"{method} still returns the native label vocabulary"


def test_as_dict_is_available_on_every_accessor():
    t = _ticker()
    for method in ("get_income_stmt", "get_balance_sheet", "get_cash_flow"):
        d = getattr(t, method)(as_dict=True)
        assert isinstance(d, dict)
        assert d, f"{method}(as_dict=True) returned empty"


def test_label_map_has_no_duplicate_targets():
    """
    ``INDIAN_TO_YFINANCE_LABELS`` maps two Indian rows onto one yfinance row:

        "EPS in Rs" -> "Diluted EPS"
        "EPS"       -> "Diluted EPS"

    This is an intentional alias (screener.in uses both spellings depending on the statement),
    so the *table* is fine. ``to_yfinance()`` used to apply it with a bare
    ``df.rename(index=...)``, which produced **duplicate index labels** rather than merging
    them. Verified against 0.1.3:

        >>> yf.index
        Index(['Diluted EPS', 'Diluted EPS', 'Total Revenue'], dtype='object')
        >>> type(yf.loc["Diluted EPS"])
        <class 'pandas.core.frame.DataFrame'>     # a Series was expected

    ``to_yfinance()`` now coalesces aliased rows (see
    :func:`test_to_yfinance_merges_aliased_rows_to_one_label`), so the alias is no longer
    reachable in the output. What this test keeps is the *table* invariant: a new collision
    added to ``INDIAN_TO_YFINANCE_LABELS`` has to be a deliberate decision here, not a
    side effect, and the assertion below is what makes that decision visible. It is the
    first half of the property -- the second half, that no collision reaches the output, is
    asserted on the output itself and cannot be satisfied by editing this table.
    """
    dupes = {v for v in INDIAN_TO_YFINANCE_LABELS.values() if list(
        INDIAN_TO_YFINANCE_LABELS.values()
    ).count(v) > 1}
    assert dupes == {"Diluted EPS"}, f"new label collision introduced: {sorted(dupes)}"


def test_a_label_collision_in_the_table_still_never_reaches_the_output():
    """
    The general form of the alias rule, so the de-duplication is not special-cased to EPS.

    ``to_yfinance()`` coalesces by *output* label, not by a hard-coded EPS exception, so a
    second collision added to ``INDIAN_TO_YFINANCE_LABELS`` tomorrow is de-duplicated too.
    Proven by monkeypatching a second collision into the table and requiring the output to
    stay unique. The earlier version of this test asserted the *opposite*
    (``duplicated().sum() == 1`` and ``loc["Diluted EPS"]`` is a DataFrame); it was a
    characterisation of the 0.1.3 defect and it contradicted the xfail two lines below it.
    """
    import bfinance.models.statements as stmts

    stmt = FinancialStatement(
        headers=["Mar 2023", "Mar 2024"],
        rows={"EPS in Rs": [20.0, 25.0], "EPS": [20.0, 25.0], "Sales": [100.0, 123.45]},
    )
    original = stmts.INDIAN_TO_YFINANCE_LABELS
    try:
        stmts.INDIAN_TO_YFINANCE_LABELS = {**original, "Sales": "Total Revenue"}
        yf = stmt.to_yfinance()
    finally:
        stmts.INDIAN_TO_YFINANCE_LABELS = original
    assert yf.index.is_unique, f"a label collision reached the output: {list(yf.index)}"
    assert isinstance(yf.loc["Total Revenue"], pd.Series)


def test_to_yfinance_merges_aliased_rows_to_one_label():
    """The corrected behaviour: one row per yfinance label.

    Was ``xfail(strict=True, reason="issue 04: to_yfinance() must de-duplicate aliased rows")``;
    a strict xfail that starts passing is reported as a *failure*, which is the signal that
    the fix landed and the marker must go.

    The value assertion is ``25.0``, not ``25.0 * 1e7``. The old text asserted the scaled
    value, which was itself the second half of the defect issue 04 had to fix: ``"EPS in Rs"``
    is already rupees *per share*, so the ``1e7`` crore scale turned 25.0 into 250,000,000 --
    a per-share number in absolute-rupee units, in no unit at all. yfinance's ``Diluted EPS``
    is rupees per share, so the row is exempt from the scale
    (``models/statements.PER_SHARE_LABELS``). The scaled counterpart is asserted on a row the
    scale *does* apply to, in :func:`test_yfinance_values_are_absolute_rs_not_cr`.
    """
    stmt = FinancialStatement(
        headers=["Mar 2023", "Mar 2024"],
        rows={"EPS in Rs": [20.0, 25.0], "EPS": [20.0, 25.0], "Sales": [100.0, 123.45]},
    )
    yf = stmt.to_yfinance()
    assert yf.index.duplicated().sum() == 0
    assert isinstance(yf.loc["Diluted EPS"], pd.Series)
    assert yf.loc["Diluted EPS", pd.Timestamp("2024-03-31")] == pytest.approx(25.0)
    # ...and the merge is published, not silent: which labels collapsed, into what.
    assert yf.attrs["bfinance_merged_row_labels"] == {"Diluted EPS": ["Diluted EPS", "Diluted EPS"]}
    # The coalesce is per period, not winner-take-all: a blank in one spelling must not erase
    # what the other carries for that period.
    gappy = FinancialStatement(
        headers=["Mar 2023", "Mar 2024"],
        rows={"EPS in Rs": [None, 25.0], "EPS": [20.0, None], "Sales": [100.0, 123.45]},
    )
    assert gappy.to_yfinance().loc["Diluted EPS", pd.Timestamp("2023-03-31")] == pytest.approx(20.0)
    assert gappy.to_yfinance().loc["Diluted EPS", pd.Timestamp("2024-03-31")] == pytest.approx(25.0)


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


def test_the_ttm_drop_is_a_decision_that_is_stated_not_an_incidental_parse_failure():
    """
    The decision ``to_yfinance()`` makes about TTM, and the fact that it says so.

    **This is the resolution of the characterisation above, and the answer was "keep dropping
    it, but say so".** A trailing-twelve-months total is not a fiscal period: it has no
    month-end date, so it cannot occupy a ``DatetimeIndex`` without a date being invented for
    it, and inventing one is worse than omitting the column. So it is dropped -- but by
    *name*, in ``NON_DATE_PERIOD_HEADERS``, and the dropped header is published on the frame
    as ``attrs["bfinance_dropped_period_columns"]``, so a caller reading a statement whose
    newest column is a quarter stale can see that it was a choice rather than infer it from
    a column that is not there.

    Two things this pins that a bare "the column is gone" cannot:

    * the native statement still carries TTM, so the values are not lost, only relocated to
      the shape that can represent them;
    * the drop is *published*. An attr that is always ``[]`` would pass the same assertions,
      so the negative case is here too: a statement with no TTM reports an empty list.

    The alternative -- keeping TTM under a documented name -- was rejected: it would put a
    string back into a ``DatetimeIndex`` and reintroduce the very defect the column-order
    work removes.
    """
    stmt = FinancialStatement(
        headers=["Mar 2023", "Mar 2024", "TTM"], rows={"Sales": [100.0, 123.45, 130.0]}
    )
    yf = stmt.to_yfinance()
    assert yf.attrs["bfinance_dropped_period_columns"] == ["TTM"]
    assert "TTM" in stmt.to_dataframe().columns, "the native shape must still carry TTM"
    assert 130.0 in set(stmt.to_dataframe().to_numpy().ravel())

    dated_only = FinancialStatement(
        headers=["Mar 2023", "Mar 2024"], rows={"Sales": [100.0, 123.45]}
    )
    assert dated_only.to_yfinance().attrs["bfinance_dropped_period_columns"] == []


# --------------------------------------------------------------- yfinance contract (shipped in 0.2.0)
#
# These six were ``xfail(strict=True, reason=_ISSUE_04)`` and the seventh
# (``test_to_yfinance_merges_aliased_rows_to_one_label``) was
# ``xfail(strict=True, reason="issue 04: to_yfinance() must de-duplicate aliased rows")``.
# A strict xfail that starts passing is reported as a *failure*, so the marker had to go
# when the accessor was wired to ``to_yfinance()``; the properties themselves are unchanged
# and are re-checked together by
# :func:`test_the_seven_yfinance_properties_all_hold`.


def test_yfinance_columns_are_a_datetime_index():
    """
    Was xfail: columns were ``Index`` of ``str`` (``'Mar 2024'``), not ``DatetimeIndex``.

    Measured against live yfinance for RELIANCE.NS:
        bfinance  -> Index(['Mar 2015', ... 'Mar 2026', 'TTM'])
        yfinance  -> DatetimeIndex(['2026-03-31', ... '2022-03-31'])
    """
    df = _ticker().get_income_stmt(freq="yearly")
    assert isinstance(df.columns, pd.DatetimeIndex), f"got {type(df.columns).__name__}"


def test_yfinance_columns_are_newest_first():
    """
    Was xfail: bfinance returned oldest-first (ascending), yfinance returns newest-first.

    This is the *period-order invariant*. It is the one that silently corrupts downstream
    analysis: a caller doing ``df.iloc[:, 0]`` to get "the latest period" gets the **oldest**
    one, and every growth/trend number computed from it is wrong in a way that looks plausible.

    Asserted on a three-period frame, because a single column is monotonically *both* ways and
    would pass against an implementation that never ordered anything.
    """
    df = _ticker().get_income_stmt(freq="yearly")
    assert len(df.columns) >= 3, f"a {len(df.columns)}-column frame cannot show an order"
    assert df.columns.is_monotonic_decreasing, f"columns are {list(df.columns)}"
    assert df.columns[0] == pd.Timestamp("2024-03-31"), f"newest first, not {list(df.columns)}"


def test_yfinance_has_no_non_date_period_column():
    """
    Was xfail: the trailing ``'TTM'`` column was a label, not a date.

    It cannot be represented in a ``DatetimeIndex``. The decision made in 0.2.0 is to drop it
    by name and publish the drop -- see
    :func:`test_the_ttm_drop_is_a_decision_that_is_stated_not_an_incidental_parse_failure`.
    """
    df = _ticker().get_income_stmt(freq="yearly")
    non_dates = [c for c in df.columns if not isinstance(c, pd.Timestamp)]
    assert not non_dates, f"non-date period columns present: {non_dates}"
    assert df.attrs["bfinance_dropped_period_columns"] == ["TTM"]


def test_yfinance_row_labels_are_generic():
    """
    Was xfail: rows were Indian labels (``'Sales'``), not yfinance's (``'Total Revenue'``).
    """
    df = _ticker().get_income_stmt(freq="yearly")
    assert "Total Revenue" in df.index
    assert "Net Income" in df.index
    assert "Total Debt" in df.index
    assert "Sales" not in df.index


def test_yfinance_values_are_absolute_rs_not_cr():
    """
    Was xfail: values were Rs Cr. yfinance reports absolute rupees.

    The 1e7 gap is the kind of error that survives review: 123.45 Cr and 1,234,500,000 Rs are
    the same company, and a ratio computed from mixed units is off by 1e7.

    The second assertion is the other half of the scale, and the half that is easy to get
    wrong in the other direction: a per-share row is rupees *per share* and must NOT be
    multiplied. ``"EPS in Rs"`` at 25.0 is 25.0, not 250,000,000.
    """
    df = _ticker().get_income_stmt(freq="yearly")
    assert df.loc["Total Revenue", pd.Timestamp("2024-03-31")] == pytest.approx(123.45 * 1e7)
    assert df.loc["Diluted EPS", pd.Timestamp("2024-03-31")] == pytest.approx(25.0)


def test_to_yfinance_is_what_the_accessor_returns():
    """
    The accessor and the serializer must not diverge.

    ``to_yfinance()`` was already correct -- it was the accessor that failed to call it. This pins
    the relationship so the two cannot drift apart again, now over all three accessors and over
    the *values* too, not just the two label lists a regression would most obviously show up in.
    """
    stmt = _statement()
    expected = stmt.to_yfinance()
    t = _ticker()
    for method in ("get_income_stmt", "get_balance_sheet", "get_cash_flow"):
        from_accessor = getattr(t, method)(freq="yearly")
        assert list(from_accessor.columns) == list(expected.columns), method
        assert list(from_accessor.index) == list(expected.index), method
        pd.testing.assert_frame_equal(from_accessor, expected)


# --------------------------------------------------------------- the seven properties
#
# The file used to carry 7 ``xfail(strict=True)`` markers -- one per property -- plus two
# self-checks that the markers were still all there and all still armed. Both halves of that
# arrangement are gone now, and a blanket removal would leave the properties unguarded, so
# this section is what replaced it.
#
# What was lost, stated plainly: an ``xfail(strict=True)`` marker is *self-arming*. It fails
# the moment its property becomes true, and a non-strict one reports XPASS as a pass, so
# neither an ordinary regression nor a silently-dropped marker can go unnoticed. A plain
# assertion is not self-arming. These are not equivalent, and the difference is the whole
# reason the block below exists.
#
# So the guard is the other half of the trade: it re-checks all seven properties **in one
# place**, against a real ``Ticker``, so that deleting or neutering any single test above
# removes a redundant copy rather than the only copy. It is deliberately written without
# reference to the tests it covers -- it re-derives each frame from
# ``_ticker().get_income_stmt(...)`` and asserts the property itself, so it cannot pass by
# agreeing with a test that has been weakened in the same edit.

#: The seven properties, in the order they are checked. Each was one ``xfail(strict=True)``
#: alarm while it was false; the name is the test that pins it individually.
_YFINANCE_PROPERTIES = (
    "test_to_yfinance_merges_aliased_rows_to_one_label",
    "test_yfinance_columns_are_a_datetime_index",
    "test_yfinance_columns_are_newest_first",
    "test_yfinance_has_no_non_date_period_column",
    "test_yfinance_row_labels_are_generic",
    "test_yfinance_values_are_absolute_rs_not_cr",
    "test_to_yfinance_is_what_the_accessor_returns",
)


def test_the_seven_named_tests_are_still_present_in_this_file():
    """
    The seven property tests exist, by name, in this file's source.

    Read with ``ast``, like the ``_EXPECTED_XFAIL_COUNT`` check it replaces. The reason it
    matters: the guard below re-checks the properties itself, so it stays green no matter how
    many of the individual tests are removed. This is what makes removing one of them a
    *failure* rather than a quiet loss of a named check.
    """
    defined = {
        node.name
        for node in ast.walk(ast.parse(pathlib.Path(__file__).read_text(encoding="utf-8-sig")))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    missing = [name for name in _YFINANCE_PROPERTIES if name not in defined]
    assert missing == [], (
        f"the seven yfinance-property tests must all exist: missing {missing}. Deleting one "
        f"to make a run green removes the named check for that property."
    )


def test_the_seven_yfinance_properties_all_hold():
    """
    All seven yfinance-shape properties, checked together, independently of the tests above.

    Each block is a plain re-assertion of one property against a real ``Ticker``'s accessor
    output. Offline: ``_ticker()`` injects a synthetic profile that ``_ensure_profile()``
    short-circuits on, so nothing here touches the network.

    The statements are deliberately repetitive rather than factored into a helper. A helper
    would be one edit away from being weakened in the same commit that weakens its callers,
    and the point of this function is to be the copy that cannot be edited along with them.
    """
    df = _ticker().get_income_stmt(freq="yearly")

    # 1. one row per yfinance label -- no alias survives as a duplicate index entry.
    aliased = FinancialStatement(
        headers=["Mar 2023", "Mar 2024"],
        rows={"EPS in Rs": [20.0, 25.0], "EPS": [20.0, 25.0], "Sales": [100.0, 123.45]},
    ).to_yfinance()
    assert aliased.index.duplicated().sum() == 0, f"aliased rows reached the output: {list(aliased.index)}"
    assert isinstance(aliased.loc["Diluted EPS"], pd.Series)

    # 2. columns are a DatetimeIndex, not an Index of period strings.
    assert isinstance(df.columns, pd.DatetimeIndex), f"got {type(df.columns).__name__}"

    # 3. newest first. Three dated periods, because a one-column frame is monotonically both
    #    ways and would pass against an implementation that ordered nothing.
    assert len(df.columns) >= 3, f"a {len(df.columns)}-column frame cannot show an order"
    assert df.columns.is_monotonic_decreasing, f"columns are {list(df.columns)}"
    assert df.columns[0] == pd.Timestamp("2024-03-31"), f"not newest-first: {list(df.columns)}"

    # 4. no non-date period column survives, and the TTM drop is published.
    non_dates = [c for c in df.columns if not isinstance(c, pd.Timestamp)]
    assert non_dates == [], f"non-date period columns present: {non_dates}"
    assert df.attrs["bfinance_dropped_period_columns"] == ["TTM"]

    # 5. generic row labels, and no Indian label left behind.
    for label in ("Total Revenue", "Net Income", "Total Debt"):
        assert label in df.index, f"missing yfinance row {label}"
    for indian in ("Sales", "Net Profit", "Borrowings", "EPS in Rs"):
        assert indian not in df.index, f"the Indian label {indian!r} leaked into the yfinance shape"

    # 6. absolute rupees on the rows the scale applies to; rupees per share on the one it does not.
    assert df.loc["Total Revenue", pd.Timestamp("2024-03-31")] == pytest.approx(123.45 * 1e7)
    assert df.loc["Diluted EPS", pd.Timestamp("2024-03-31")] == pytest.approx(25.0)

    # 7. the accessor returns the serializer's frame and not a lookalike.
    expected = _statement().to_yfinance()
    assert list(df.columns) == list(expected.columns)
    assert list(df.index) == list(expected.index)
    pd.testing.assert_frame_equal(df, expected)
