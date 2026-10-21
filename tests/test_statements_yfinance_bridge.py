"""The trendlyne -> yfinance bridge, pinned against a live recording.

Every expected number here is copied from the census recorded in
``tests/fixtures/trendlyne/META.json`` for the live ``/fundamentals/get-fundamental_results-v2/``
NTPC payload captured 2026-10-09, so a failure is a disagreement with what the vendor actually
served rather than with a value this test invented.

The case that matters most is a row whose unit the vendor does not state: a price-to-book **ratio**
carrying ``unit == ""``, ``PBV_A`` on this recording. A rule of the form "scale unless the unit says
otherwise" multiplies it by 1e7, which is the same defect
``models/statements.py::_rows_to_scale`` records for screener.in's ``OPM %`` arriving as
``350000000.0`` -- reached here through the second vendor instead of the first.

``PBV_A`` no longer reaches this bridge from the recording: the vendor states no ``cat1`` for it, so
the row partition in ``bfinance.statements`` excludes it from all three statements first
(``test_statements_row_partition.py`` pins that, and pins ``PBV_A``'s exclusion by name). The rule
this module owns is about the **unit**, not about that one row id, so the shape it exists for is
rebuilt by hand on a :class:`StatementResult` below -- the same way the collision cases are, and for
the same reason: the property belongs to the bridge, not to what today's payload happens to contain.

The second class is the label collision: rows are keyed by display label, and the vendor publishes
two ``sstr`` values twice on this payload, so two ids address one row. One row has to win, and
the winner must be recorded rather than silently overwriting the other.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from bfinance.models.statements import CR_TO_RS
from bfinance.statements import (
    Basis,
    Frequency,
    StatementKind,
    StatementRequest,
    parse_fundamental_results,
    resolve,
)
from bfinance.statements.model import StatementResult
from bfinance.statements.yfinance_bridge import (
    COLLIDING_ROWS_ATTR,
    MONEY_UNIT,
    result_to_financial_statement,
    result_to_yfinance_frame,
)

FIXTURE = Path(__file__).parent / "fixtures" / "trendlyne" / "fundamental_results_ntpc.json"


def _payload() -> dict:
    """The recorded live payload, decoded. Never re-fetched: this file runs offline."""
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _result(
    kind: StatementKind = StatementKind.INCOME,
    frequency: Frequency = Frequency.ANNUAL,
    basis: Basis = Basis.CONSOLIDATED,
    symbol: str = "NTPC",
) -> StatementResult:
    """One parsed statement, through the shipped parser and the shipped cascade."""
    request = StatementRequest(symbol=symbol, kind=kind, frequency=frequency, basis=basis)
    return resolve(request, fetch=lambda spec: _payload())


def _synthetic_result(
    rows: dict, *, labels: dict | None = None, units: dict | None = None
) -> StatementResult:
    """A hand-built result, for a shape the live recording does not currently contain.

    A label collision is a property of the **label map**, not of a payload's layout, so the cases
    the recording cannot produce today are pinned on the object the bridge actually takes -- a
    :class:`StatementResult`. That also keeps them independent of what the parser does with a
    payload, which is a separate concern with its own tests.
    """
    return StatementResult(
        request=StatementRequest(
            symbol="NTPC",
            kind=StatementKind.INCOME,
            frequency=Frequency.ANNUAL,
            basis=Basis.CONSOLIDATED,
        ),
        provider="trendlyne-json",
        payload="trendlyne_json",
        basis=Basis.CONSOLIDATED,
        periods=("Mar 2025", "Mar 2026"),
        rows=rows,
        row_labels=labels or {},
        row_units=units or {},
    )


# --------------------------------------------------------------------------- #
# the scale rule, one case per unit the vendor actually states
# --------------------------------------------------------------------------- #


def test_cr_is_the_only_unit_that_scales_to_absolute_rupees():
    """``SR_A`` is 189,798.56 on the vendor's route, and 189,798,560,000,000 in absolute rupees.

    The multiplier is ``CR_TO_RS`` (1e7) from ``models/statements.py``, so this pins the value and
    the provenance of the conversion rather than restating the arithmetic.
    """
    result = _result()
    frame = result_to_yfinance_frame(result)
    label = result.row_labels["SR_A"]
    assert label == "Total Rev. Ann."
    assert frame.loc[label, frame.columns[0]] == 189798.56 * CR_TO_RS


def test_percent_rows_are_not_scaled():
    """``OPM Ann. %`` is 29.13, not 291,300,000 -- the ``%`` rows carry no Cr."""
    result = _result()
    frame = result_to_yfinance_frame(result)
    label = result.row_labels["OPMPCT_A"]
    assert label == "OPM Ann. %"
    assert frame.loc[label, frame.columns[0]] == pytest.approx(29.13)


def test_per_share_rows_are_not_scaled():
    """``EPS Adj. latest Ann.`` is 27.9 rupees per share. Scaling it would make every P/E computed
    from this frame wrong by a factor of 1e7.

    ``BVSH_A`` used to be the per-share row pinned here and the partition moved it: the vendor
    files it under ``Financial Ratios``, which no ``StatementKind`` models, so it is excluded from
    all three statements. The property is the bridge's, not that id's -- and the income statement
    still carries five ``Rs`` rows, so ``EPS_adj_A`` carries the test. ``SR_A`` on the **same
    frame** is the contrast: the decision is the row's own unit, not the frame's shape.
    """
    result = _result()
    frame = result_to_yfinance_frame(result)
    assert result.row_units["EPS_adj_A"] == "Rs"
    assert frame.loc["EPS Adj. latest Ann.", frame.columns[0]] == pytest.approx(27.9)
    assert frame.loc["Total Rev. Ann.", frame.columns[0]] == pytest.approx(189798.56 * CR_TO_RS)


def test_ratio_rows_are_not_scaled():
    """A ratio the vendor labels ``abs`` reaches the frame unscaled, on a hand-built result.

    The recording cannot supply this case any more: ``CRATIO_A`` is filed under
    ``Financial Ratios``, ``StatementKind`` has only three members and ratios are deliberately not
    a fourth, so the partition excludes it from all three statements before the bridge is called.
    What the bridge owns -- ``abs`` is not ``Cr``, so nothing is multiplied -- is unchanged by
    that, so the case is built on the object the bridge actually takes.

    The ``Cr`` partner is what gives the assertion teeth: a bridge that scaled *nothing* would
    otherwise pass it too.
    """
    result = _synthetic_result(
        rows={"CRATIO_A": (0.94, 1.02), "SR_A": (100.0, 200.0)},
        labels={"CRATIO_A": "Current Ratio Ann.", "SR_A": "Total Rev. Ann."},
        units={"CRATIO_A": "abs", "SR_A": MONEY_UNIT},
    )
    frame = result_to_yfinance_frame(result)
    assert result.row_units["CRATIO_A"] == "abs"
    current_ratio = frame.loc["Current Ratio Ann.", frame.columns[0]]
    assert current_ratio == pytest.approx(1.02)
    assert 0.1 < current_ratio < 10, f"a current ratio of {current_ratio} is not a ratio"
    assert "Current Ratio Ann." in frame.attrs["bfinance_statements_unscaled_rows"]
    assert frame.loc["Total Rev. Ann.", frame.columns[0]] == pytest.approx(200.0 * CR_TO_RS)


def test_a_row_with_no_unit_is_not_scaled_and_is_named():
    """**The trap.** A price-to-book ratio with ``unit == ""``, rebuilt by hand.

    A rule of the form "scale unless the unit says otherwise" reports 14,900,000 for a
    price-to-book of 1.49 and says nothing about having done it. The positive rule -- scale only
    what the vendor labels ``Cr`` -- leaves it alone, and the row is named on the frame so the
    decision is auditable rather than inferred from a wrong-looking magnitude.

    ``PBV_A`` was the live instance and is no longer available here: it carries no ``cat1``, so the
    partition excludes it from every statement upstream of this module
    (``test_statements_row_partition.py::test_pbv_aa_price_to_book_ratio_is_excluded_from_every_``
    ``statement_and_says_so``). The unit is carried on the **row**, so the trap is reproduced on a
    result whose unit map states ``""`` for the row and ``"Cr"`` for its neighbour -- one frame,
    one rule, and only one of the two rows scaled.
    """
    result = _synthetic_result(
        rows={"PBV_A": (1.31, 1.49), "SR_A": (100.0, 200.0)},
        labels={"PBV_A": "Price To Book Value Ann.", "SR_A": "Total Rev. Ann."},
        units={"PBV_A": "", "SR_A": MONEY_UNIT},
    )
    assert result.row_units["PBV_A"] == ""
    frame = result_to_yfinance_frame(result)
    label = result.row_labels["PBV_A"]
    assert frame.loc[label, frame.columns[0]] == pytest.approx(1.49)
    assert label in frame.attrs["bfinance_statements_unscaled_rows"]
    # And the money row beside it is scaled, so the assertion above is about the unit, not about
    # a bridge that stopped scaling.
    assert frame.loc["Total Rev. Ann.", frame.columns[0]] == pytest.approx(200.0 * CR_TO_RS)


def test_every_row_is_either_scaled_or_named():
    """No row is silently in neither set. The two sets partition the frame's index."""
    result = _result()
    frame = result_to_yfinance_frame(result)
    scaled = {
        result.row_labels[metric]
        for metric in result.rows
        if result.row_units.get(metric, "") == "Cr"
    }
    unscaled = set(frame.attrs["bfinance_statements_unscaled_rows"])
    assert scaled | unscaled == set(frame.index)
    assert not (scaled & unscaled)


# --------------------------------------------------------------------------- #
# labels: the vendor's own, never a rename
# --------------------------------------------------------------------------- #


def test_the_index_carries_the_vendors_own_labels():
    """``sstr`` from the payload, verbatim -- the label the site renders.

    No cross-vendor rename is applied. ``INDIAN_TO_YFINANCE_LABELS`` is a measured
    screener.in -> yfinance map; there is no measured trendlyne -> yfinance one, and inventing
    one would relabel one vendor's number in another's vocabulary with nothing behind it.

    Checked per statement, because a row is only on the statement the vendor filed it under.
    ``TA_A`` is ``cat1: Balance Sheet``, so since the partition ``Total Assets Ann.`` is a
    balance-sheet label and its absence from the income frame is the original defect being
    closed -- not evidence about labelling. Each frame is asked only for labels that are its own.
    """
    income = result_to_yfinance_frame(_result())
    sheet = result_to_yfinance_frame(_result(kind=StatementKind.BALANCE_SHEET))

    assert "Total Rev. Ann." in income.index
    assert "OPM Ann. %" in income.index
    assert "Total Assets Ann." in sheet.index

    # And the raw ids are NOT leaked into the index when a label was published -- each asserted on
    # the frame that actually carries the row, so the assertion is about the label and not about
    # the row being absent.
    assert "SR_A" not in income.index
    assert "TA_A" not in sheet.index


def test_a_row_with_no_published_label_falls_back_to_its_id():
    """A synthetic row the vendor never described keeps its id, rather than being renamed or
    dropped. The fallback direction is the honest one: an absent label is not any particular
    other spelling."""
    payload = _payload()
    payload["body"]["annualDataDump"]["consolidated"].setdefault("Mar 2026", {})[
        "ZZZ_UNMEASURED_ROW_A"
    ] = 12.5
    # The row still has to be *this statement's* row: membership is read from
    # parameterMetadata[id]['cat1'] alone, so a record with no entry at all is excluded from all
    # three statements and never reaches the bridge. A metadata entry that states the section and
    # nothing else is the shape being tested -- described enough to belong, not enough to be
    # named. Popped and re-assigned rather than updated, so the record is exactly
    # {"cat1": ...} whatever the fixture happens to hold for this id: a fixture that one day
    # publishes an ``sstr`` for it must not quietly turn this into a different test.
    payload["body"]["parameterMetadata"].pop("ZZZ_UNMEASURED_ROW_A", None)
    payload["body"]["parameterMetadata"]["ZZZ_UNMEASURED_ROW_A"] = {"cat1": "Income Statement"}
    payload["body"].setdefault("level_first_dict_map", {}).setdefault("annual", []).append(
        {"indCode": "ZZZ_UNMEASURED_ROW_A"}
    )
    result = parse_fundamental_results(
        payload,
        StatementRequest(
            symbol="NTPC", kind=StatementKind.INCOME,
            frequency=Frequency.ANNUAL, basis=Basis.CONSOLIDATED,
        ),
    )
    assert "ZZZ_UNMEASURED_ROW_A" in result.rows, "the row was excluded before the bridge saw it"
    assert result.row_labels["ZZZ_UNMEASURED_ROW_A"] == "ZZZ_UNMEASURED_ROW_A"
    frame = result_to_yfinance_frame(result)
    assert "ZZZ_UNMEASURED_ROW_A" in frame.index
    assert "ZZZ_UNMEASURED_ROW_A" in frame.attrs["bfinance_statements_unscaled_rows"]


# --------------------------------------------------------------------------- #
# a label collision: one row has to win, and the loss may not be silent
# --------------------------------------------------------------------------- #


def test_two_ids_sharing_a_label_keep_the_first_row_and_name_the_loss():
    """Two ids publishing one ``sstr`` address one row, and the **first** wins.

    First-wins is the same rule :func:`fundamentals_provider._assemble` applies when a row *id*
    repeats, so the two ways a row can be lost resolve the same way. What is new is the second
    assertion: the collision is on the frame, because ``StatementResult.duplicate_rows`` counts
    repeated **ids** upstream and cannot see two ids sharing a label -- with the old code this
    row simply was not in the frame, and nothing said so.
    """
    result = _synthetic_result(
        rows={"OPM_A": (11.0, 12.0), "OPMPCT_A": (29.13, 30.0)},
        labels={"OPM_A": "OPM Ann. %", "OPMPCT_A": "OPM Ann. %"},
        units={"OPM_A": "%", "OPMPCT_A": "%"},
    )
    statement = result_to_financial_statement(result)
    assert list(statement.rows) == ["OPM Ann. %"], "two ids, one label: one row, not two"
    assert statement.rows["OPM Ann. %"] == [11.0, 12.0]

    frame = result_to_yfinance_frame(result)
    # Survivor first, so a consumer reads which row the frame kept and which one it did not.
    assert frame.attrs[COLLIDING_ROWS_ATTR] == {"OPM Ann. %": ("OPM_A", "OPMPCT_A")}
    # And the frame holds the winner's numbers (newest column first), never the loser's.
    assert list(frame.to_numpy().ravel()) == [12.0, 11.0]


@pytest.mark.parametrize("winner", ["MONEY_A", "PCT_A"])
def test_a_collision_straddling_cr_and_percent_is_scaled_per_id_not_per_label(winner):
    """The scale decision is the **surviving id's** unit, not the shared label's.

    Deciding on the label cannot be right either way here: ``money_labels`` is a set of labels, so
    a ``Cr`` partner puts the shared label in the set and a percent is scaled by 1e7, while a
    percent-only pair keeps it out and money goes unscaled. Both cases below failed against the
    label-keyed decision, in opposite directions.
    """
    values = {"MONEY_A": (100.0, 200.0), "PCT_A": (10.0, 20.0)}
    order = ("MONEY_A", "PCT_A") if winner == "MONEY_A" else ("PCT_A", "MONEY_A")
    result = _synthetic_result(
        rows={metric: values[metric] for metric in order},
        labels={"MONEY_A": "Shared Label", "PCT_A": "Shared Label"},
        units={"MONEY_A": MONEY_UNIT, "PCT_A": "%"},
    )
    frame = result_to_yfinance_frame(result)

    # The newest period, which is column 0: one row survived, holding the winner's own numbers.
    latest = frame.iloc[0, 0]
    expected = 200.0 * CR_TO_RS if winner == "MONEY_A" else 20.0
    assert latest == expected
    assert list(frame.index) == ["Shared Label"]
    unscaled = frame.attrs["bfinance_statements_unscaled_rows"]
    assert ("Shared Label" in unscaled) is (winner == "PCT_A")
    assert frame.attrs[COLLIDING_ROWS_ATTR] == {"Shared Label": order}


def test_no_row_leaves_the_frame_without_being_named():
    """The invariant on the recording: anything the frame does not carry is a collision it said so
    about.

    On this capture the two colliding pairs (``OPM_A``/``OPMPCT_A`` and ``ACCRUALS_A``/
    ``DepreciationOnLeasedAssets_A``) are all-null upstream and are dropped before they reach
    the bridge, so today nothing is lost -- which is the vendor's business, not a licence for
    the bridge to lose a row quietly the day one of them publishes a number.
    """
    result = _result()
    frame = result_to_yfinance_frame(result)
    colliding = frame.attrs[COLLIDING_ROWS_ATTR]
    for label, ids in colliding.items():
        assert set(ids) <= set(result.rows)
        assert result.row_labels.get(ids[0], ids[0]) == label, "the survivor owns the label"
        assert label in frame.index

    labels = {result.row_labels.get(metric, metric) for metric in result.rows}
    assert labels - set(frame.index) <= set(colliding)


# --------------------------------------------------------------------------- #
# the shape this bridge exists to preserve
# --------------------------------------------------------------------------- #


def test_columns_are_newest_first_datetimes_and_ttm_is_not_a_column():
    """``df.columns[0]`` is the latest period, and the literal ``TTM`` header is dropped *and
    named* -- a trailing-twelve-month total is not a fiscal period and cannot be placed in a
    DatetimeIndex without fabricating a date."""
    result = _result()
    frame = result_to_yfinance_frame(result)
    assert frame.columns[0] == pd.Timestamp("2026-03-31")
    assert frame.columns[-1] == pd.Timestamp("2016-03-31")
    assert all(isinstance(col, pd.Timestamp) for col in frame.columns)
    assert "TTM" not in {str(col) for col in frame.columns}


def test_the_frames_attrs_name_the_provider_and_the_basis():
    """A frame from this bridge says which vendor answered and on which basis, so a caller never
    has to infer it from the row vocabulary."""
    result = _result()
    frame = result_to_yfinance_frame(result)
    assert frame.attrs["bfinance_statements_provider"] == "trendlyne-json"
    assert frame.attrs["bfinance_statements_basis"] == "consolidated"
    assert frame.attrs["bfinance_statements_basis_requested"] == "consolidated"
    assert frame.attrs["bfinance_statements_basis_mismatch"] is False
    assert frame.attrs["bfinance_provenance"] == "bfinance.statements"


def test_a_basis_mismatch_is_flagged_rather_than_relabelled():
    """Serve a consolidated result for a standalone request and the frame must say so.

    The numbers are **not** adjusted, converted or relabelled -- a consolidated and a standalone
    figure are different numbers for the same company and the same period, and substituting one
    for the other is a silent corruption.
    """
    request = StatementRequest(
        symbol="NTPC",
        kind=StatementKind.INCOME,
        frequency=Frequency.ANNUAL,
        basis=Basis.STANDALONE,
    )
    result = parse_fundamental_results(_payload(), request)
    frame = result_to_yfinance_frame(result)
    assert frame.attrs["bfinance_statements_basis"] == "standalone"
    assert frame.attrs["bfinance_statements_basis_requested"] == "standalone"
    assert frame.attrs["bfinance_statements_basis_mismatch"] is False

    # And the reverse, built by hand, is flagged. The parser serves what it was asked for, so this
    # is the shape a cascade mis-serve would produce.
    mismatched = StatementResult(
        request=request,
        provider="trendlyne-json",
        payload="trendlyne_json",
        basis=Basis.CONSOLIDATED,
        periods=result.periods,
        rows=result.rows,
        row_labels=dict(result.row_labels),
        row_units=dict(result.row_units),
    )
    frame = result_to_yfinance_frame(mismatched)
    assert frame.attrs["bfinance_statements_basis_mismatch"] is True
    assert "NOT adjusted" in frame.attrs["bfinance_statements_basis_note"]


def test_the_two_bases_genuinely_disagree():
    """If they did not, the basis machinery would be ceremony. Measured on the recording:
    ``TotalShareHoldersFunds_A`` at Mar 2026 is 203,175.67 consolidated and 174,865.25 standalone."""
    consolidated = _result(basis=Basis.CONSOLIDATED, kind=StatementKind.BALANCE_SHEET)
    standalone = _result(basis=Basis.STANDALONE, kind=StatementKind.BALANCE_SHEET)
    left = result_to_yfinance_frame(consolidated)
    right = result_to_yfinance_frame(standalone)
    label = consolidated.row_labels["TotalShareHoldersFunds_A"]
    assert left.loc[label, left.columns[0]] / CR_TO_RS == pytest.approx(203175.67)
    assert right.loc[label, right.columns[0]] / CR_TO_RS == pytest.approx(174865.25)
    assert left.loc[label, left.columns[0]] != right.loc[label, right.columns[0]]


def test_an_empty_result_yields_an_empty_frame():
    """The bridge degrades the way ``Ticker`` does -- an empty frame, never a raise."""
    empty = StatementResult(
        request=StatementRequest(
            symbol="NTPC",
            kind=StatementKind.INCOME,
            frequency=Frequency.ANNUAL,
            basis=Basis.CONSOLIDATED,
        ),
        provider="trendlyne-json",
        payload="trendlyne_json",
        basis=Basis.CONSOLIDATED,
        periods=("Mar 2026",),
        rows={"SR_A": (None,)},
    )
    assert result_to_yfinance_frame(empty).empty


def test_result_to_financial_statement_is_the_native_shape():
    """The intermediate object is the documented native contract: metric rows x period columns,
    **chronological**, with the vendor's labels -- untouched by the yfinance conversion.

    ``result.periods`` arrives ascending (the vendor serves newest-first), so ``[-1]`` is Mar 2026
    and its revenue is the 189,798.56 Cr of the live capture. ``to_yfinance()`` reverses the
    columns to newest-first itself, which is why the intermediate is chronological rather than
    already reversed.
    """
    result = _result()
    statement = result_to_financial_statement(result)
    assert statement.headers == list(result.periods)
    assert statement.headers[0] == "Mar 2016"
    assert statement.headers[-1] == "Mar 2026"
    assert "Total Rev. Ann." in statement.rows
    assert statement.rows["Total Rev. Ann."][-1] == pytest.approx(189798.56)
    assert statement.rows["Total Rev. Ann."][0] == pytest.approx(74484.0)
