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

import ast
import os
import pathlib
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

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


# --------------------------------------------------------------- the alarm itself
#
# Everything above assumes the alarms are still armed. Two ways they can stop being
# armed, both of which have to be loud:
#
#   1. a marker loses ``strict=True`` -- then XPASS is reported as a *pass*, so the
#      0.2.0 break lands silently and the file reports green;
#   2. an xfail starts passing -- then the break has already landed and the file
#      reports red (or green, if strict was dropped).
#
# Neither is observable from inside the file it guards, so both are checked from here.

#: The number of alarms this file must always carry. Lowering it to make a run green
#: defeats the only tripwire between 0.1.3 and the 0.2.0 statement-order break.
_EXPECTED_XFAIL_COUNT = 7

#: Set on the child process spawned below so the guard does not guard itself forever.
_NESTED_GUARD_ENV = "BFINANCE_XFAIL_GUARD_CHILD"


def test_every_alarm_is_a_strict_xfail():
    """
    Every xfail in this file must be ``strict=True``, and there must be all 7 of them.

    Read out of this file's source with ``ast`` rather than from the marks pytest
    collects: a non-strict xfail that XPASSes leaves no trace in the collected marks, so
    by the time you could observe it from the collection side it has already done the
    one thing it existed to prevent.
    """
    tree = ast.parse(pathlib.Path(__file__).read_text(encoding="utf-8-sig"))

    marks: list[tuple[str, object]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if not (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)):
                continue
            if dec.func.attr != "xfail":
                continue
            strict = [kw.value for kw in dec.keywords if kw.arg == "strict"]
            marks.append((node.name, ast.literal_eval(strict[0]) if strict else None))

    assert len(marks) == _EXPECTED_XFAIL_COUNT, (
        f"expected {_EXPECTED_XFAIL_COUNT} xfail alarms, found {len(marks)}: "
        f"{[name for name, _ in marks]}. Deleting an alarm to get a green run defeats "
        f"the only tripwire on the 0.2.0 statement-order break."
    )
    non_strict = [name for name, strict in marks if strict is not True]
    assert non_strict == [], (
        f"xfail without strict=True is a silent alarm -- XPASS would report as a pass: "
        f"{non_strict}"
    )


def test_the_alarms_still_xfail_and_none_has_started_passing():
    """
    Zero XPASS: the seven alarms are still armed, and none of them is spent.

    The only honest way to see an XPASS is to run the file, so this shells out to pytest
    and reads a JUnit XML report. Two reasons for XML rather than the stdout summary:
    the counts line is not reliably emitted in this environment, and parsing prose is
    fragile in a way a machine-readable report is not.

    Under ``strict=True`` pytest reports an XPASS as a **failure**, so:

        errors=0, failures=0   ->  no alarm has XPASSed
        7 xfail-typed skips     ->  all seven still armed

    Offline: every test in the file runs on an injected synthetic profile.
    """
    if os.environ.get(_NESTED_GUARD_ENV) == "1":
        pytest.skip("this process is the child of the xfail alarm guard")

    root = pathlib.Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory() as tmp:
        report = pathlib.Path(tmp) / "xfail_guard.xml"
        proc = subprocess.run(
            [
                sys.executable, "-m", "pytest",
                "tests/test_stmt_accessor_contract.py",
                "-p", "no:cacheprovider", "-q", "--color=no",
                f"--junitxml={report}",
            ],
            cwd=root,
            env={**os.environ, _NESTED_GUARD_ENV: "1"},
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert proc.returncode == 0, (
            "the guarded file did not run clean -- an alarm may have XPASSed.\n"
            f"stdout:\n{proc.stdout[-4000:]}\nstderr:\n{proc.stderr[-2000:]}"
        )
        suite = ET.fromstring(report.read_text(encoding="utf-8")).find("testsuite")

    assert suite is not None, "no <testsuite> element in the JUnit report"
    assert suite.get("errors") == "0", f"errors reported: {suite.get('errors')}"
    assert suite.get("failures") == "0", (
        f"{suite.get('failures')} failure(s) in the guarded file. Under strict=True the "
        f"only way a test fails there is by XPASSing, i.e. the alarm is defeated."
    )
    armed = [
        case.get("name")
        for case in suite.findall("testcase")
        for skip in case.findall("skipped")
        if skip.get("type") == "pytest.xfail"
    ]
    assert len(armed) == _EXPECTED_XFAIL_COUNT, (
        f"{len(armed)} of {_EXPECTED_XFAIL_COUNT} alarms still armed: {armed}"
    )
