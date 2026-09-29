"""
Row units on the yfinance-shaped statement: only money is converted from Rs Cr.

The defect
----------

:meth:`bfinance.models.statements.FinancialStatement.to_yfinance` multiplies a row by
:data:`~bfinance.models.statements.CR_TO_RS` (1e7) to turn Rs Cr into absolute rupees. That
is a statement about *money*, and the screener.in statement tables are not only money: the
recorded P&L also carries ``OPM %``, ``Tax %`` and ``Dividend Payout %``, the cash flow table
carries ``CFO/OP``, the ratios table carries day counts and ``ROCE %``, and the shareholding
table carries percentages and a share count.

The only exemption was :data:`PER_SHARE_LABELS` (``Diluted EPS``), so every other
non-monetary row was scaled too. Measured on the committed ITC recording in
``tests/fixtures/screener/company_itc.html``::

    screener.in prints    OPM % = 35.0
    to_yfinance() returned OPM % = 350000000.0

That is fabricated magnitude on the *primary* return path -- ``Ticker.get_income_stmt``,
``get_balance_sheet`` and ``get_cash_flow`` all return this frame -- and it is the class of
error this package exists to prevent. It also survives review, because the frame still looks
full and every label is still a real row.

The rule
--------

**The scale converts money, so only a row that screener.in prints in Rs Cr may be scaled.**
screener.in states a row's unit in its label, so the decision is readable rather than
heuristic: a ``%`` in the label is a percentage, a per-share row is already rupees, and a
ratio/days/count row is dimensionless. See
:func:`bfinance.models.statements._rows_to_scale`, which states it in the code next to the
constant tables it reads.

One subtlety is load-bearing and is pinned separately below: the decision has to be taken
*before* the label rename, because ``Tax %`` becomes ``Tax Rate`` and the output label no
longer carries a ``%``. A marker test that ran on the output label alone would quietly scale
that row too.

What this file pins
-------------------

* the exact corrected value for the percentage rows in the recordings -- ``OPM %`` comes back
  as **35.0**, not ``3.5e8``;
* the **control**: the money rows in the same frame are still scaled by 1e7, so deleting the
  scale does not satisfy this file;
* a whole-corpus sweep in which every value of every row of every recorded statement is
  either scaled by exactly ``CR_TO_RS`` or untouched, and the untouched set is exactly the
  documented non-monetary one -- so neither a silently-scaled percentage nor a
  silently-unscaled rupee amount survives;
* the premise, re-proved from the fixtures on every run, so the file cannot rot into
  asserting on rows that no longer exist.

Everything is offline: the recordings are committed and are read through the package's own
:class:`~bfinance.screener.parser.ScreenerHTMLParser`, the same way
:mod:`tests.test_ratios_honesty` re-proves its own premise.
"""

from __future__ import annotations

import pathlib
from typing import Dict, List, Tuple

import pandas as pd
import pytest
from bs4 import BeautifulSoup

from bfinance.models.statements import (
    CR_TO_RS,
    INDIAN_TO_YFINANCE_LABELS,
    NON_DATE_PERIOD_HEADERS,
    PER_SHARE_LABELS,
    RATIO_LABELS,
    FinancialStatement,
)
from bfinance.screener.parser import ScreenerHTMLParser

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "screener"

#: Both committed company recordings. Both are read because the label vocabulary is not
#: identical between them -- ``section#shareholding`` carries a ``Promoters`` row for TCS and
#: none for ITC -- and a rule checked against one company is not a rule.
SYMBOLS = ("ITC", "TCS")

#: ``section#<id>`` for the five statement tables, keyed the way the profile keys them.
SECTIONS = {
    "quarters": "quarters",
    "profit_loss": "profit-loss",
    "balance_sheet": "balance-sheet",
    "cash_flow": "cash-flow",
    "ratios_history": "ratios",
}

#: Every non-monetary label the committed recordings emit, mapped to one value the recording
#: actually prints for it. Transcribed from ``company_itc.html`` (``Promoters`` from
#: ``company_tcs.html``) and re-proved by the first test below on every run, so this table is
#: evidence rather than assertion. It is written out here rather than derived from the
#: constant the code reads, so the sweep at the end of this file cannot agree with the
#: implementation by construction.
NON_MONETARY_IN_RECORDINGS: Dict[str, float] = {
    # section#quarters and section#profit-loss: percentages, printed with a trailing "%"
    "OPM %": 35.0,
    "Tax %": 25.0,
    # section#quarters and section#profit-loss: per share, already rupees
    "EPS in Rs": 16.51,
    # section#profit-loss only
    "Dividend Payout %": 88.0,
    # section#cash-flow: cash from operations as a share of operating profit, no "%" marker
    "CFO/OP": 93.0,
    # section#ratios: day counts, and the one percentage
    "Debtor Days": 23.0,
    "Inventory Days": 178.0,
    "Days Payable": 55.0,
    "Cash Conversion Cycle": 146.0,
    "Working Capital Days": 45.0,
    "ROCE %": 37.0,
    # section#shareholding: percent of shares held (no "%" marker) plus one plain count
    "Promoters": 71.77,
    "FIIs": 34.83,
    "DIIs": 49.15,
    "Government": 0.04,
    "Public": 15.96,
    "No. of Shareholders": 4041653.0,
}


def _parser() -> ScreenerHTMLParser:
    return ScreenerHTMLParser()


def _soup(symbol: str) -> BeautifulSoup:
    return BeautifulSoup(
        (FIXTURES / f"company_{symbol.lower()}.html").read_text(encoding="utf-8"), "lxml"
    )


def _recorded(symbol: str, section_id: str) -> FinancialStatement:
    """One statement table, parsed out of a committed recording by the package's own parser."""
    return _parser()._parse_table_section(_soup(symbol), section_id)


def _recorded_shareholding(symbol: str, second_table: bool) -> FinancialStatement:
    """``section#shareholding`` carries two tables (quarterly, yearly); both are pattern data."""
    quarterly, yearly = _parser()._parse_shareholding_sections(_soup(symbol))
    return yearly if second_table else quarterly


def _every_recorded_statement() -> List[Tuple[str, FinancialStatement]]:
    """Every statement table of every committed recording: 7 per company, 14 in all."""
    out: List[Tuple[str, FinancialStatement]] = []
    for symbol in SYMBOLS:
        for name, section_id in SECTIONS.items():
            out.append((f"{symbol}.{name}", _recorded(symbol, section_id)))
        out.append((f"{symbol}.shareholding", _recorded_shareholding(symbol, second_table=False)))
        out.append(
            (f"{symbol}.shareholding_yearly", _recorded_shareholding(symbol, second_table=True))
        )
    return out


def _month_end(period: str) -> pd.Timestamp:
    """``"Mar 2026"`` -> ``Timestamp("2026-03-31")``, the way ``to_yfinance`` builds a column."""
    return pd.to_datetime(f"01 {period}", format="%d %b %Y") + pd.offsets.MonthEnd(1)


def _observed_values() -> Dict[str, set]:
    """Every non-null value each recorded row carries, across both recordings."""
    observed: Dict[str, set] = {}
    for _name, statement in _every_recorded_statement():
        for label, values in statement.rows.items():
            observed.setdefault(label, set()).update(v for v in values if v is not None)
    return observed


# --------------------------------------------------------------- the premise


def test_the_recordings_still_print_every_non_monetary_value_this_file_asserts_on():
    """
    The premise, re-proved from the committed fixtures rather than from a comment.

    The values in :data:`NON_MONETARY_IN_RECORDINGS` are transcribed from the recordings. If
    screener.in ever renames or changes one of these rows, this fails and the file must be
    re-read -- rather than the expectations quietly going on describing a table that no longer
    exists. The reverse direction is covered by the corpus sweep below, which fails on any
    non-monetary row that is not in this table.
    """
    observed = _observed_values()

    missing = {k: v for k, v in NON_MONETARY_IN_RECORDINGS.items() if v not in observed.get(k, ())}
    assert not missing, (
        f"the recordings no longer print these values for these rows: {sorted(missing)}"
    )


def test_the_recordings_carry_money_rows_and_non_money_rows_in_the_same_tables():
    """
    The other half of the premise, and what stops the sweep below from being vacuous: the
    recorded statements mix Rupees-Cr rows with percentage, per-share, ratio and count rows,
    so the sweep has both kinds to classify in every table.
    """
    pnl = _recorded("ITC", "profit-loss")
    assert pnl.rows["Sales"][0] == pytest.approx(78868.0)
    assert pnl.rows["Net Profit"][0] == pytest.approx(21018.0)
    assert pnl.rows["OPM %"][0] == pytest.approx(35.0)
    assert pnl.rows["EPS in Rs"][0] == pytest.approx(16.51)

    cash = _recorded("ITC", "cash-flow")
    assert cash.rows["Cash from Operating Activity"][0] == pytest.approx(17627.0)
    assert cash.rows["CFO/OP"][0] == pytest.approx(93.0)


# --------------------------------------------------------------- the defect: percentages


def test_a_percentage_row_comes_back_as_its_reported_number_not_1e7_times_it():
    """
    The headline. ``OPM %`` is reported as 35.0 and must come back as 35.0.

    Against the code as it stood this assertion reported ``350000000.0``. It is written as an
    equality against the reported figure rather than against "whatever the converter
    produces", because a characterisation assertion would have stated the defect as the
    contract.
    """
    pnl = _recorded("ITC", "profit-loss")

    yf = pnl.to_yfinance()

    assert yf.loc["OPM %", _month_end("Mar 2026")] == pytest.approx(35.0)


def test_a_percentage_row_keeps_its_value_in_every_table_that_carries_one():
    """
    The same property across the quarterly, annual and ratios tables, so "fix one row" is not
    what a reader takes away. Each case names the screener.in period it reads, because the
    yfinance frame is newest-first and a bare position would silently change subject.
    """
    cases = (
        ("quarters", "OPM %", "Mar 2026", 39.0),
        ("quarters", "OPM %", "Jun 2026", 27.0),
        ("quarters", "Tax %", "Mar 2026", 24.0),
        ("profit-loss", "OPM %", "Mar 2026", 35.0),
        ("profit-loss", "Dividend Payout %", "Mar 2026", 88.0),
        ("ratios", "ROCE %", "Mar 2025", 37.0),
        ("ratios", "ROCE %", "Mar 2026", 39.0),
    )
    for section_id, label, period, value in cases:
        yf = _recorded("ITC", section_id).to_yfinance()
        out = INDIAN_TO_YFINANCE_LABELS.get(label, label)
        assert out in yf.index, f"{section_id}: {label!r} did not survive as {out!r}"
        assert yf.loc[out, _month_end(period)] == pytest.approx(value), (
            f"{section_id}[{label}] {period}: {yf.loc[out, _month_end(period)]!r} != {value!r}"
        )


def test_the_rename_cannot_hide_a_percentage_behind_an_output_label_with_no_marker():
    """
    ``Tax %`` is the row that makes this a design constraint rather than a lookup table.

    ``INDIAN_TO_YFINANCE_LABELS`` renames it to ``Tax Rate``, which carries no ``%``. The
    non-monetary test therefore cannot run on the *output* label alone: ``Tax Rate`` would be
    scaled to 250,000,000 and nothing would say so. ``_rows_to_scale`` decides on the
    screener.in label, before the rename, and this is the assertion that keeps that ordering
    from being quietly swapped.

    Pinned on the mapped name because that is the row a caller can reach. If the rename were
    removed this fails on a missing ``Tax Rate``, which is the right outcome -- the row would
    have changed identity.
    """
    yf = _recorded("ITC", "profit-loss").to_yfinance()

    assert "Tax %" not in yf.index, "the row is no longer renamed; the constraint is gone"
    assert yf.loc["Tax Rate", _month_end("Mar 2026")] == pytest.approx(25.0)


# --------------------------------------------------------------- the other two kinds


def test_a_per_share_row_is_rupees_per_share_and_a_ratio_row_is_dimensionless():
    """
    The same rule applied to the other two recorded kinds, in the statements that carry them.

    ``EPS in Rs`` 4.3 is rupees per share, and ``CFO/OP`` 93.0 is cash from operations as a
    share of operating profit -- a number near 100, not an amount in Cr. Both are read out of
    the recorded tables rather than from a hand-written fixture, so the assertion cannot drift
    away from what the parser actually produces.
    """
    quarters = _recorded("ITC", "quarters").to_yfinance()
    assert quarters.loc["Diluted EPS", _month_end("Mar 2026")] == pytest.approx(4.3)

    cash = _recorded("ITC", "cash-flow").to_yfinance()
    assert cash.loc["CFO/OP", _month_end("Mar 2025")] == pytest.approx(93.0)
    assert cash.loc["CFO/OP", _month_end("Mar 2026")] == pytest.approx(91.0)


def test_the_days_and_share_count_rows_are_not_scaled_either():
    """
    The ``ratios_history`` and ``shareholding`` tables, reachable through the public
    ``to_yfinance()`` even though no ``Ticker`` accessor serves them. ``No. of Shareholders``
    is the plainest case in the corpus: a count has no rupee unit to convert at all.
    """
    ratios = _recorded("ITC", "ratios").to_yfinance()
    assert ratios.loc["Debtor Days", _month_end("Mar 2026")] == pytest.approx(18.0)
    assert ratios.loc["Cash Conversion Cycle", _month_end("Mar 2026")] == pytest.approx(164.0)

    holding = _recorded_shareholding("ITC", second_table=False).to_yfinance()
    assert holding.loc["FIIs", _month_end("Mar 2026")] == pytest.approx(34.83)
    assert holding.loc["No. of Shareholders", _month_end("Mar 2026")] == pytest.approx(4041653.0)


# --------------------------------------------------------------- the control


def test_the_money_rows_in_the_same_frame_are_still_scaled_by_1e7():
    """
    The control that stops the tests above from being satisfied by deleting the scale.

    ``OPM %`` and ``Total Revenue`` are two rows of one recorded statement: 35.0 and
    788,680,000,000 in the same frame. Removing :data:`CR_TO_RS` fails here, so "percentage
    rows are exempt" cannot be implemented as "nothing is scaled".
    """
    yf = _recorded("ITC", "profit-loss").to_yfinance()
    mar_2026 = _month_end("Mar 2026")

    assert yf.loc["OPM %", mar_2026] == pytest.approx(35.0)
    assert yf.loc["Total Revenue", mar_2026] == pytest.approx(78868.0 * CR_TO_RS)
    assert yf.loc["Net Income", mar_2026] == pytest.approx(21018.0 * CR_TO_RS)
    assert yf.loc["Total Expenses", mar_2026] == pytest.approx(51562.0 * CR_TO_RS)
    assert yf.loc["Pretax Income", mar_2026] == pytest.approx(28033.0 * CR_TO_RS)
    # ...and the scale leaves ratios *between* money rows alone, which is the point of it.
    assert yf.loc["Total Revenue", mar_2026] / yf.loc["Total Expenses", mar_2026] == pytest.approx(
        78868.0 / 51562.0
    )


def test_the_native_shape_still_carries_the_unscaled_percentage_and_the_crore_amount():
    """
    ``to_dataframe()`` is the contract finengine reads and this change does not touch it, so
    the fix is scoped to the yfinance shape alone -- checked on one statement, on both sides,
    so a later change that "helpfully" normalises the native frame fails here.
    """
    pnl = _recorded("ITC", "profit-loss")

    native = pnl.to_dataframe()
    assert native.loc["OPM %", "Mar 2026"] == pytest.approx(35.0)
    assert native.loc["Sales", "Mar 2026"] == pytest.approx(78868.0)

    yf = pnl.to_yfinance()
    assert yf.loc["OPM %", _month_end("Mar 2026")] == pytest.approx(35.0)
    assert yf.loc["Total Revenue", _month_end("Mar 2026")] == pytest.approx(78868.0 * CR_TO_RS)


# --------------------------------------------------------------- the whole corpus


def test_every_recorded_row_is_either_scaled_by_1e7_or_left_alone_and_the_split_is_the_documented_one():
    """
    The general form, so no row of any recorded statement escapes the sweep.

    For every value of every row of every recorded statement table, the yfinance value is
    *exactly* the native value scaled by ``CR_TO_RS``, or *exactly* the native value. There is
    no third possibility: no row silently dropped from the count, no row scaled by some other
    factor, no row exempted for a reason nobody wrote down.

    The untouched rows are then required to be **exactly**
    :data:`NON_MONETARY_IN_RECORDINGS`, in both directions -- every non-monetary row is
    untouched (so scaling cannot creep back in), and nothing else is (so the exemption cannot
    widen into the money rows by accident).
    """
    untouched: set = set()
    checked = 0

    for name, statement in _every_recorded_statement():
        native = statement.to_dataframe()
        yf = statement.to_yfinance()
        for label, row in native.iterrows():
            out = INDIAN_TO_YFINANCE_LABELS.get(label, label)
            if out not in yf.index:
                # All-NaN rows (screener.in's "Raw PDF" label row) are dropped; a row with no
                # value has no unit to convert and no number to fabricate.
                assert row.isna().all(), (
                    f"{name}[{label!r}] vanished from the yfinance shape but is not all-NaN"
                )
                continue
            for period, nv in row.items():
                if nv is None or (isinstance(nv, float) and pd.isna(nv)):
                    continue
                if period in NON_DATE_PERIOD_HEADERS:
                    # "TTM" is a trailing-twelve-months total, not a fiscal period; it has no
                    # month-end date and is dropped by name. Not this file's subject.
                    continue
                ts = _month_end(str(period))
                if ts not in yf.columns:
                    continue
                yv = float(yf.loc[out, ts])
                scaled, plain = nv * CR_TO_RS, nv
                if yv == pytest.approx(scaled):
                    continue
                assert yv == pytest.approx(plain), (
                    f"{name}[{label!r}] {period}: {yv!r} is neither the reported {plain!r} "
                    f"nor its scaled {scaled!r}"
                )
                untouched.add(label)
                checked += 1

    assert checked >= 20, f"the sweep only classified {checked} value(s); it is too thin to bite"
    assert untouched == set(NON_MONETARY_IN_RECORDINGS), (
        "the set of rows left unscaled is not the documented non-monetary one; "
        f"unexpectedly scaled: {sorted(untouched - set(NON_MONETARY_IN_RECORDINGS))}; "
        f"unexpectedly exempt: {sorted(set(NON_MONETARY_IN_RECORDINGS) - untouched)}"
    )


def test_the_exemption_sets_cover_every_non_monetary_label_in_the_corpus():
    """
    :data:`PER_SHARE_LABELS` and :data:`RATIO_LABELS` must not fall behind the recordings.

    Percentages are covered by the ``%`` rule rather than by a name, so the labels that need a
    name are the rest. Both directions are checked, because either one alone is satisfiable by
    a no-op: a recorded non-monetary row in neither set would be scaled again, and a set entry
    no recording supports is a claim about markup this repo has never seen.
    """
    recorded = _observed_values()
    assert recorded, "the recordings parsed to no rows at all; the sweep would be vacuous"

    # The sets are keyed on the label a caller reads, so the expected labels are renamed too.
    expected_out = {INDIAN_TO_YFINANCE_LABELS.get(label, label) for label in NON_MONETARY_IN_RECORDINGS}
    # Percentages are covered by the "%" rule rather than by a name, so the labels that need a
    # name are the rest.
    named_out = {
        INDIAN_TO_YFINANCE_LABELS.get(label, label)
        for label in NON_MONETARY_IN_RECORDINGS
        if "%" not in label
    }

    missing = named_out - (PER_SHARE_LABELS | RATIO_LABELS)
    assert not missing, (
        "recorded non-monetary rows that are in neither exemption set, so they would be "
        f"scaled again: {sorted(missing)}"
    )
    stale = (PER_SHARE_LABELS | RATIO_LABELS) - expected_out
    assert not stale, f"exemption entries no recording supports: {sorted(stale)}"
