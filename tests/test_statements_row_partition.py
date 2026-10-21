"""One row per statement, measured on the committed recording.

The defect this file exists to stop
-----------------------------------
``parse_fundamental_results`` builds its rows from **every** ``_A``-suffixed id the dump holds, so
income, balance sheet and cash flow each returned the same 189 rows and a caller asking "what are
the total assets" got a frame containing revenue, operating margin and ROE. Every number in it was
individually right, which is what makes it dangerous: it reads as a verified answer.

Membership now comes from ``parameterMetadata[id]['cat1']`` — the vendor's own section label — and
this file pins the invariants that make the partition auditable rather than merely different:

* the three statements are pairwise disjoint, so no id lands in two;
* ``rows`` + ``excluded_rows`` + ``dropped_rows`` account for **every** id in the dump, so nothing
  disappears silently and no allow-list has to be trusted;
* labels are unique within a statement, which is the ``OPM_A``/``OPMPCT_A`` collision caught one
  layer earlier than the bridge catches it;
* the frame's ``attrs`` name the kind, so a mismatched statement is visible rather than inferred.

Nothing here asserts a row **count**. Counts move when the bridge re-keys units and when trendlyne
publishes; membership and disjointness are the invariants that have to hold either way.

Every id in the three allow-lists below was **read off**
``tests/fixtures/trendlyne/fundamental_results_ntpc.json`` — the live 2026-10-09 NTPC capture — by
looking at ``parameterMetadata[id]['cat1']`` and at which dump periods carry a number. None of them
is derived from the parser's own output, so a change in the parser cannot quietly edit the list it
is checked against.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from bfinance.statements import (
    Basis,
    Frequency,
    StatementKind,
    StatementRequest,
    parse_fundamental_results,
    resolve,
)
from bfinance.statements.errors import StatementEmptyError
from bfinance.statements.fundamentals_provider import (
    _JSON_RATIO_CATEGORY,
    _JSON_SECTION,
    _JSON_UNCATEGORISED,
)

FIXTURE = Path(__file__).parent / "fixtures" / "trendlyne" / "fundamental_results_ntpc.json"

# --------------------------------------------------------------------------- #
# measured allow-lists — the ids in the dump that belong to no statement
# --------------------------------------------------------------------------- #

#: 64 rows the vendor files under ``Financial Ratios``. ``StatementKind`` has three members and
#: ratios are deliberately not a fourth, so none of these belongs to income, balance sheet or cash
#: flow and all 64 are excluded from all three.
RATIO_ROWS: frozenset = frozenset({
    "ASETTO_A", "BVPerShareInclRevalReserve_A", "BVSH_A", "BusinessPerBranches_A",
    "BusinessPerEmployee_A", "CASA_A", "CASHTO_A", "CEPS1_A", "CEPS3_A", "CEPS5_A", "CFROA_A",
    "CRATIO_A", "CashEarningRetention_A", "CostToIncome_A", "DEBT_CE_A", "DividendPayoutNP_A",
    "DividendPerShare_A", "EVPerEBITDA_A", "EVPerNetSales_A", "EYield_A", "EarningRetention_A",
    "EnterpriseValue_A", "GoodwillOnConsolidation_A", "IC_A", "InterestCoveragePostTax_A",
    "InterestExpensesByEarningAssets_A", "InterestIncomeByEarningAssets_A",
    "InterestIncomePerBranch_A", "InterestIncomePerEmployee_A", "LTDE_A", "MarketCapPerSales_A",
    "NBTO_A", "NETPCT_A", "NI3_A", "NI5_A", "NIM_A", "NPAfterMIMargin_A", "NPAfterMIPerShare_A",
    "NPMAfterMI_A", "NPPerBranches_A", "NPPerEmployee_A", "NPPerShare_A",
    "NonInterestIncomeByEarningAssets_A", "OPCF3_A", "OPCF5_A", "OPM_A",
    "OperatingExpensesByEarningAssets_A", "OperatingProfitByEarningAssets_A",
    "OperatingRevenuePerShare_A", "PBDITMargin_A", "PBDITPerShare_A", "PBITMargin_A",
    "PBITPerShare_A", "PBTMargin_A", "PBTPerShare_A", "PriceToSales_A", "QuickRatio_A", "REV1_A",
    "REV2_A", "ROA_A", "ROCE_A", "ROE_A", "ROIC_A", "RetentionRatios_A",
})

#: 30 rows the vendor publishes in the annual dump and states **no category for at all**. There is
#: no measurement saying which statement any of them belongs to, so none of them is assigned to
#: one: ``DividendPayoutCP_A`` on a balance sheet is the same confident nonsense as ``ROE_A`` there.
UNCATEGORISED_ROWS: frozenset = frozenset({
    "AssetGroupShareInJointVentures_A", "AssetsHeldForSale_A", "ConstructionStores_A", "DIV_A",
    "DividendForThePreviousYear_A", "DividendPayoutCP_A", "EquityDividendRate_A",
    "EquityShareDividend_A", "ForeignCurrencyMonetaryItemTranslationDiffAcct_A",
    "ForexMonetaryItemTranslationDifferenceLiabilityAccount_A",
    "GroupShareInJointVenturesInExpenditure_A", "LessMATCredit_A",
    "LiabilityGroupShareInJointVentures_A", "MiningDevelopmentExpenditure_A",
    "NPAfterMIPerBranches_A", "NPAfterMIPerEmployee_A",
    "NetProfitLossBeforeExtraordinaryItemsAndTax_A", "NumberOfATMs_A", "PBV_A", "PCFO_A", "PE_A",
    "PS_A", "PriceToBV_A", "ServiceLineContributionFromConsumers_A", "ShareCapitalSuspense_A",
    "SpecialApprnTowardsProjectCost_A", "StatutoryConsumerReserves_A", "TOTAL_SR_A",
    "TransferToFromGeneralReserve_A", "TransferredOnAmalgamation_A",
})

#: 33 rows that **are** a statement's own, and which the vendor publishes with no number in any of
#: the eleven annual periods (27 Income Statement, 6 Balance Sheet, 0 Cash Flow). They are in the
#: statement's section, they carry nothing, and they are named on ``dropped_rows`` rather than
#: dropped without a number.
NO_NUMBER_ROWS: frozenset = frozenset({
    "Advances_A", "BalanceCarriedOverToBalanceSheet_A",
    "BalancesWithBanksMoneyAtCallAndShortNotice_A", "BillsForCollection_A", "Borrowings_A",
    "CapitalAdequacyRatios_A", "DepreciationOnLeasedAssets_A", "Deposits_A", "GNPARAT_A",
    "GrossNPARs_A", "IncomeFromInvestments_A", "InterestDiscountOnAdvancesBills_A",
    "InterestOnBalanceWithRBIAndOtherInterBankFunds_A", "Investments_A",
    "KeyPerformanceTier1_A", "KeyPerformanceTier2_A", "NNPARAT_A", "NetNPARs_A",
    "NetNPAToAdvancesPercentage_A", "NumberOfBranches_A", "NumberOfEmployees_A",
    "OperatingExpensesExcludesEmployeeCostAndDepreciation_A", "OtherProvisionsAndContingencies_A",
    "Others_A", "ProfitLossBroughtForward_A", "TotalAppropriations_A", "TotalProfitLoss_A",
    "TransferToFromCapitalReserve_A", "TransferToFromInvestmentReserve_A",
    "TransferToFromReserveFund_A", "TransferToFromRevenueAndOtherReserves_A",
    "TransferToFromSpecialReserve_A", "TransferToFromStatutoryReserve_A",
})

#: Everything in the dump that is in no statement's index, and why. The three sets are disjoint.
UNCLAIMED = RATIO_ROWS | UNCATEGORISED_ROWS | NO_NUMBER_ROWS

_KIND_BY_NAME = {
    "income": StatementKind.INCOME,
    "balance_sheet": StatementKind.BALANCE_SHEET,
    "cash_flow": StatementKind.CASH_FLOW,
}


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def payload() -> dict:
    """The recorded live payload, decoded. Never re-fetched: this file runs offline."""
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture
def annual_ids(payload: dict) -> set:
    """Every ``_A``-suffixed id in the annual dump, union over both bases."""
    ids: set = set()
    for dump in payload["body"]["annualDataDump"].values():
        for period in dump.values():
            ids |= {row_id for row_id in period if row_id.endswith("_A")}
    return ids


def _request(
    kind: StatementKind, frequency: Frequency = Frequency.ANNUAL
) -> StatementRequest:
    return StatementRequest(
        symbol="NTPC", kind=kind, frequency=frequency, basis=Basis.CONSOLIDATED
    )


def _result(payload: dict, kind: StatementKind, frequency: Frequency = Frequency.ANNUAL):
    return parse_fundamental_results(payload, _request(kind, frequency))


@pytest.fixture
def annual(payload: dict) -> dict:
    """The three annual statements, parsed through the shipped parser."""
    return {
        "income": _result(payload, StatementKind.INCOME),
        "balance_sheet": _result(payload, StatementKind.BALANCE_SHEET),
        "cash_flow": _result(payload, StatementKind.CASH_FLOW),
    }


# --------------------------------------------------------------------------- #
# 1. the partition itself
# --------------------------------------------------------------------------- #


def test_the_three_annual_statements_return_disjoint_row_sets(annual):
    """The bug in one assertion: before the partition all three returned the same 189 ids.

    ``StatementKind`` names three different things. A row id that appears in two of them means one
    of the two answers is wrong about the vendor, and the caller has no way to tell which.
    """
    income = set(annual["income"].rows)
    sheet = set(annual["balance_sheet"].rows)
    flow = set(annual["cash_flow"].rows)

    assert income & sheet == set(), f"income and balance sheet share {sorted(income & sheet)[:6]}"
    assert income & flow == set(), f"income and cash flow share {sorted(income & flow)[:6]}"
    assert sheet & flow == set(), f"balance sheet and cash flow share {sorted(sheet & flow)[:6]}"


def test_no_row_id_can_land_in_two_statements(annual):
    """Each id appears at most once across the three statements, counted rather than pairwise.

    Distinctness of :data:`_JSON_SECTION` is what makes this structural — two kinds mapped to the
    same ``cat1`` value would put one id in two statements on *every* payload — so the constant is
    checked as well as the outcome.
    """
    sections = list(_JSON_SECTION.values())
    assert len(set(sections)) == len(sections), (
        f"two StatementKinds share one cat1 section {sections}; a row id would land in both"
    )
    assert len(_JSON_SECTION) == len(list(StatementKind)), (
        "a StatementKind has no section; it would be served the whole dump or refused"
    )

    seen: dict = {}
    for name, result in annual.items():
        for row_id in result.rows:
            assert row_id not in seen, (
                f"{row_id!r} is in both {seen[row_id]!r} and {name!r}"
            )
            seen[row_id] = name
    assert len(seen) == sum(len(result.rows) for result in annual.values())


def test_the_balance_sheet_carries_assets_and_liabilities_and_nothing_else(annual):
    """``TA_A``/``CA_A``/``CL_A`` in; revenue, cash flow and ROE out.

    The third of those is the one the old row set got wrong in the most confident way: ``ROE_A``
    is a percentage, it is right about the company, and it is not part of a balance sheet.
    """
    sheet = set(annual["balance_sheet"].rows)
    assert {"TA_A", "CA_A", "CL_A"} <= sheet
    assert not ({"SR_A", "OPMPCT_A", "PAT_A"} & sheet), "a revenue/margin row is on the balance sheet"
    assert not ({"CFO_A", "CFI_A", "CFA_A"} & sheet), "a cash flow row is on the balance sheet"
    assert not ({"ROE_A", "ROCE_A", "BVSH_A"} & sheet), "a ratio row is on the balance sheet"


def test_the_cash_flow_carries_cash_flow_and_nothing_else(annual):
    """``CFO_A`` in, total assets and revenue out.

    Cash flow is legitimately the smallest of the three: the vendor files exactly 8 ``_A`` ids as
    ``Cash Flow``. A short result here is correct and is not padded.
    """
    flow = set(annual["cash_flow"].rows)
    assert "CFO_A" in flow
    assert not ({"TA_A", "CA_A", "CL_A"} & flow), "a balance sheet row is on the cash flow"
    assert not ({"SR_A", "PAT_A", "OPMPCT_A"} & flow), "a revenue/margin row is on the cash flow"


def test_the_income_statement_carries_the_p_and_l_and_nothing_else(annual):
    """``SR_A`` in, total assets and cash flow out."""
    income = set(annual["income"].rows)
    assert "SR_A" in income
    assert not ({"TA_A", "CA_A", "CL_A"} & income), "a balance sheet row is on the income statement"
    assert not ({"CFO_A", "CFI_A", "CFA_A"} & income), "a cash flow row is on the income statement"


def test_no_ratio_row_leaks_into_any_of_the_three_statements(annual):
    """``StatementKind`` has no ratios and the registry says so; this says the payload agrees.

    A ratio on an income statement is not a formatting problem. ``ROE_A`` is a percentage of two
    other rows in the same frame, and a caller reading it as a line item is wrong by construction.
    """
    ratio_ids = {row_id for result in annual.values() for row_id in result.rows
                 if row_id in RATIO_ROWS}
    assert ratio_ids == set(), f"ratio rows reached a statement: {sorted(ratio_ids)}"

    for name, result in annual.items():
        assert {"ROE_A", "ROCE_A", "BVSH_A"}.isdisjoint(result.rows), (
            f"{name} carries a ratio row"
        )


def test_pbv_aa_price_to_book_ratio_is_excluded_from_every_statement_and_says_so(annual):
    """``PBV_A`` is asserted explicitly because it is the row that anchors the unit-scale rule.

    It is a price-to-book **ratio** carrying ``unit == ""``, so it must never be scaled, and it is
    filed with no ``cat1`` at all — which is why it is excluded here rather than reaching a frame
    where the bridge's scale rule has to catch it. Its fate is stated rather than left to fall out
    of the allow-list, and it is **named** on ``excluded_rows`` so a caller can see it was a
    decision and not an absence.
    """
    for name, result in annual.items():
        assert "PBV_A" not in result.rows, f"PBV_A reached {name}"
        assert "PBV_A" in result.excluded_rows, (
            f"PBV_A is missing from {name}'s excluded_rows, so its exclusion is silent"
        )
        assert result.excluded_row_census.get(_JSON_UNCATEGORISED, 0) > 0


def test_the_quarterly_income_statement_keeps_its_thirteen_periods(payload):
    """The row partition must not cost a column.

    ``order.quarter`` advertises 13 quarters and this one holds them, which is the counterpart to
    trap 3: there the advertisement was a lie, here it is true, and the parser has no way to know
    in advance which case it is in — so the dump's own keys decide, not the advertisement.
    """
    quarterly = _result(payload, StatementKind.INCOME, Frequency.QUARTERLY)

    assert len(quarterly.periods) == 13
    assert quarterly.periods[0] == "Jun 2023"
    assert quarterly.periods[-1] == "Jun 2026"
    # The two rows that carry no frequency suffix and are genuinely rows of this table.
    assert {"NP_TTM", "EPS_TTM"} <= set(quarterly.rows)
    assert "TOTAL_SR_Q" in quarterly.rows
    assert quarterly.non_null_cells() > 0


def test_a_quarterly_balance_sheet_is_still_refused_rather_than_served_from_the_annual_dump(
    payload,
):
    """Trap 3 survives the partition.

    ``body.order.quarter.bs`` advertises 13 quarters and holds nothing for the balance sheet, so
    the row set has to come from the ids the **dump** holds. This is the request that proves it:
    no ``_A`` id appears in the quarterly dump at all, so no ``cat1`` lookup can rescue it.
    """
    with pytest.raises(StatementEmptyError) as excinfo:
        _result(payload, StatementKind.BALANCE_SHEET, Frequency.QUARTERLY)

    message = str(excinfo.value)
    assert "'_A'" in message and "260" in message
    assert "unasked question" in message or "not a published absence" in message


# --------------------------------------------------------------------------- #
# 2. nothing disappears: every id in the dump is accounted for
# --------------------------------------------------------------------------- #


def test_every_row_id_in_the_dump_is_accounted_for_on_attrs(annual, annual_ids):
    """``rows`` + ``excluded_rows`` + ``dropped_rows`` = the dump, and the three never overlap.

    This is the invariant that makes the exclusion auditable rather than merely different. A
    statement that quietly dropped rows is a false negative about the vendor — the exact failure
    :class:`~bfinance.statements.errors.StatementEmptyError` exists to prevent, reached by a
    quieter route.
    """
    for name, result in annual.items():
        rows = set(result.rows)
        excluded = set(result.excluded_rows)
        dropped = set(result.dropped_rows)

        assert not rows & excluded, f"{name}: a row is both returned and excluded"
        assert not rows & dropped, f"{name}: a row is both returned and dropped"
        assert not excluded & dropped, f"{name}: a row is both excluded and dropped"
        assert rows | excluded | dropped == annual_ids, (
            f"{name}: {sorted(annual_ids - (rows | excluded | dropped))[:6]} is in the dump and in "
            f"none of the three buckets"
        )
        assert result.omitted_rows == len(excluded) + len(dropped), (
            f"{name}: omitted_rows={result.omitted_rows} does not count what the result left out"
        )
        assert sum(result.excluded_row_census.values()) == len(excluded), (
            f"{name}: the per-reason census does not add up to the excluded ids"
        )


def test_the_remainder_of_the_dump_is_a_measured_allow_list(annual, annual_ids):
    """What the three statements do not return is a fixed, written-down set — not a growing gap.

    Any id in the dump outside the three statements' indexes is one of three kinds of thing, all
    of them measured on this fixture rather than derived from the parser:
    :data:`RATIO_ROWS` (64), :data:`UNCATEGORISED_ROWS` (30) and :data:`NO_NUMBER_ROWS` (33).
    A new id appearing outside that list means the vendor published something new, and the test
    fails and says which id — rather than the statement quietly growing a row.
    """
    claimed: set = set()
    for result in annual.values():
        claimed |= set(result.rows)

    assert annual_ids - claimed == UNCLAIMED, (
        "the dump holds ids in no statement that are not in the measured allow-list: "
        f"{sorted((annual_ids - claimed) - UNCLAIMED)[:8]}"
    )
    # The three reasons are disjoint, so no id is excluded for two contradictory reasons at once.
    assert RATIO_ROWS & UNCATEGORISED_ROWS == set()
    assert RATIO_ROWS & NO_NUMBER_ROWS == set()
    assert UNCATEGORISED_ROWS & NO_NUMBER_ROWS == set()


def test_ratios_are_excluded_as_a_category_and_not_as_a_shape(annual):
    """Six rows filed under ``Financial Ratios`` are money the vendor labels ``"Cr"``.

    ``CASHTO_A``, ``EnterpriseValue_A``, ``GoodwillOnConsolidation_A``, ``BusinessPerBranches_A``,
    ``InterestIncomePerBranch_A`` and ``NPPerBranches_A``. Any rule of the form "a percentage is
    not a statement row" keeps those six and drops the rest, which is exactly backwards, and
    "a ratio is not money" is the assumption the unit-scale rule exists to refuse.
    """
    money_filed_as_ratios = {
        "BusinessPerBranches_A", "CASHTO_A", "EnterpriseValue_A",
        "GoodwillOnConsolidation_A", "InterestIncomePerBranch_A", "NPPerBranches_A",
    }
    assert money_filed_as_ratios <= RATIO_ROWS, (
        "the six 'Cr' rows filed as ratios have moved; re-measure before assuming the shape rule "
        "would have been wrong"
    )
    for name, result in annual.items():
        assert money_filed_as_ratios <= set(result.excluded_rows), (
            f"{name} should exclude the ratio rows that happen to be money"
        )
        assert not money_filed_as_ratios & set(result.rows), (
            f"{name} carries a row that is filed as a ratio"
        )


def test_the_reason_each_row_was_excluded_is_recorded_per_category(annual):
    """The census names *why* a row was excluded, so a ratio exclusion is not an uncategorised one."""
    for name, result in annual.items():
        census = result.excluded_row_census
        assert census.get(_JSON_RATIO_CATEGORY, 0) > 0, f"{name} excluded no ratios by name"
        assert census.get(_JSON_UNCATEGORISED, 0) > 0, f"{name} excluded no uncategorised rows"
        # Every statement excludes the two buckets that belong to no statement, and the ones filed
        # under the other two statements, which is the partition working.
        other_two = set(_JSON_SECTION.values()) - {_JSON_SECTION[_KIND_BY_NAME[name]]}
        assert other_two <= set(census), f"{name} did not record the other statements' buckets"

        assert RATIO_ROWS <= set(result.excluded_rows)
        assert UNCATEGORISED_ROWS <= set(result.excluded_rows)
        assert RATIO_ROWS & set(result.rows) == set()


def test_the_one_self_contradicting_pair_does_not_produce_two_identical_labels(annual):
    """``OPM_A`` and ``OPMPCT_A`` share ``cat3`` and ``sstr`` but sit in different sections.

    Byte-identical ``cat3`` (``'Operating Profit Margin Annual'``) and byte-identical ``sstr``
    (``'OPM Ann. %'``), opposite ``cat1``. The vendor filed one number twice. Left standing rather
    than smoothed: the income statement keeps the copy filed as income and drops the ratio copy,
    so no frame ends up indexing two different rows under the same label.
    """
    metadata = json.loads(FIXTURE.read_text(encoding="utf-8"))["body"]["parameterMetadata"]
    assert metadata["OPM_A"]["cat1"] == _JSON_RATIO_CATEGORY
    assert metadata["OPMPCT_A"]["cat1"] == _JSON_SECTION[StatementKind.INCOME]
    assert metadata["OPM_A"]["cat3"] == metadata["OPMPCT_A"]["cat3"]
    assert metadata["OPM_A"]["sstr"] == metadata["OPMPCT_A"]["sstr"]

    income = annual["income"]
    assert "OPMPCT_A" in income.rows, "the income copy of the pair was dropped"
    assert "OPM_A" not in income.rows, "the ratio copy of the pair reached the income statement"


# --------------------------------------------------------------------------- #
# 3. the shape a caller actually holds
# --------------------------------------------------------------------------- #


def test_labels_are_unique_within_every_statement(annual):
    """``len(set(row_labels.values())) == len(rows)`` on all three.

    The bridge catches a colliding label and records it; this catches the same defect one layer
    earlier, where it can still be explained. ``OPMPCT_A``/``OPM_A`` is the live case.
    """
    for name, result in annual.items():
        labels = list(result.row_labels.values())
        assert len(set(labels)) == len(result.rows) == len(labels), (
            f"{name}: {len(labels) - len(set(labels))} row(s) share a label: "
            f"{[label for label in set(labels) if labels.count(label) > 1][:4]}"
        )
        assert set(result.row_labels) == set(result.rows), (
            f"{name}: row_labels and rows disagree"
        )


def test_the_frame_attrs_name_the_statement_they_came_from(payload):
    """End to end: what a caller holds says which of the three it is.

    A balance sheet that is quietly an income statement is the whole defect. When the numbers
    cannot be checked without re-fetching the payload, this flag is the check.
    """
    for kind in StatementKind:
        result = resolve(
            _request(kind), fetch=lambda spec: copy.deepcopy(payload)
        )
        attrs = result.attrs()
        assert attrs["bfinance_statements_kind"] == kind.value
        frame = result.to_dataframe()
        assert frame.attrs["bfinance_statements_kind"] == kind.value
        assert result.kind is kind


def test_the_three_frames_are_not_the_same_frame(payload):
    """The smallest possible version of the original bug report.

    Before the partition all three annual statements indexed the same 189 rows, so
    ``set(financials.index) == set(balance_sheet.index) == set(cashflow.index)``.
    """
    indexes = {}
    for kind in StatementKind:
        result = resolve(_request(kind), fetch=lambda spec: copy.deepcopy(payload))
        indexes[kind] = set(result.to_dataframe().index)

    assert indexes[StatementKind.INCOME] != indexes[StatementKind.BALANCE_SHEET]
    assert indexes[StatementKind.INCOME] != indexes[StatementKind.CASH_FLOW]
    assert indexes[StatementKind.BALANCE_SHEET] != indexes[StatementKind.CASH_FLOW]


def test_a_payload_that_categorises_nothing_is_refused_not_served_as_the_whole_dump(payload):
    """Strip every ``cat1`` and the honest answer is "this payload does not say", not 260 rows.

    The failure this guards is the quiet one: with no categorisation there is no basis for choosing
    a section, and serving everything reproduces the original bug rather than reporting the
    payload that caused it.
    """
    stripped = copy.deepcopy(payload)
    for record in stripped["body"]["parameterMetadata"].values():
        if isinstance(record, dict):
            record.pop("cat1", None)

    with pytest.raises(StatementEmptyError) as excinfo:
        parse_fundamental_results(stripped, _request(StatementKind.BALANCE_SHEET))

    message = str(excinfo.value)
    assert "Balance Sheet" in message
    assert "balance sheet carrying revenue and ROE" in message


def test_a_row_with_a_category_the_package_does_not_model_is_excluded_not_guessed(payload):
    """An unknown ``cat1`` value joins the ratios: excluded, counted, and visible on attrs.

    The vendor inventing a fifth category is a plausible future, and the response has to be the
    same as for ratios - not served, and not silently dropped either. Recategorising every
    balance-sheet row empties that statement, which is then refused with the arithmetic rather
    than served the whole dump.
    """
    renamed = copy.deepcopy(payload)
    metadata = renamed["body"]["parameterMetadata"]
    moved = 0
    for record in metadata.values():
        if isinstance(record, dict) and record.get("cat1") == "Balance Sheet":
            record["cat1"] = "Segment Reporting"
            moved += 1
    assert moved, "the fixture no longer has Balance Sheet rows; re-measure this test"

    with pytest.raises(StatementEmptyError) as excinfo:
        parse_fundamental_results(renamed, _request(StatementKind.BALANCE_SHEET))

    assert "Segment Reporting" in str(excinfo.value)

    # The income statement is untouched, and its exclusion census now *names* the unknown category
    # rather than swallowing it into the uncategorised bucket.
    income = parse_fundamental_results(renamed, _request(StatementKind.INCOME))
    assert "Segment Reporting" in income.excluded_row_census
    assert income.excluded_row_census["Segment Reporting"] == moved
    assert income.rows, "recategorising the balance sheet emptied the income statement too"