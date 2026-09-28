"""
Regression tests for the enterprise-value honesty defect in :mod:`bfinance.market.ratios`.

The defect
----------

``market/ratios.py`` computed ``enterprise_value_cr`` as ``mcap + debt - cash`` with the cash leg
pinned to a literal ``0.0``::

    cash_hold = 0.0
    if not bs.empty:
        if "Cash Equivalents" in bs.index:            # dead
            ...
        elif "Cash and Bank Balance" in bs.index:      # dead
            ...
        # cash stays zero when neither cash row is present

Both branches are **dead**, and that is a measured fact rather than an opinion. Parsing the real
recorded balance sheets in this repo -- ``tests/fixtures/screener/company_itc.html``,
``company_tcs.html`` and ``company_itc_full_columns.html`` -- through the package's own
``ScreenerHTMLParser._parse_table_section`` yields exactly ten rows, identically in all three::

    Equity Capital, Reserves, Borrowings, Other Liabilities, Total Liabilities,
    Fixed Assets, CWIP, Investments, Other Assets, Total Assets

No cash row. Cash sits inside the undifferentiated ``Other Assets`` bucket, and
``models/statements.py:24-50``'s ``INDIAN_TO_YFINANCE_LABELS`` has no cash entry either.
:func:`test_the_upstream_balance_sheet_really_has_no_cash_row` re-proves that from the fixtures
on every run, so this file's premise cannot rot silently.

So ``cash_hold`` has always been ``0.0`` in production, while the code *read* as though it
handled cash. Consequence: ``enterprise_value_cr`` was overstated by the entire cash balance for
every net-cash company, on every call, with no exception ever raised. It backs three public
surfaces -- ``Ticker.enterprise_value``, ``Ticker.ev_to_ebitda`` and ``Ticker.custom_ratios`` --
so all three were wrong together.

The same block also read debt with ``_safe_float(..., default=0.0)``, which made the upstream's
``-`` for a debt-free company and a borrowings row that could not be read the same ``0.0``.

What this file pins
-------------------

The contract is "never a confidently wrong number; returning nothing is acceptable", and it is the
same contract :mod:`tests.test_valuation_honesty` pins for the other EV code path, with the same
wording and the same provenance key names, so a caller sees one behaviour across
``valuation_measures`` and ``custom_ratios`` rather than two:

* a net-cash profile whose statement does not itemise cash yields ``None``, not ``mcap + debt``;
* a statement that *does* carry a cash row still has it subtracted -- the fix is not "always omit";
* the three debt states -- debt-free, row-absent, failed-parse -- stay distinguishable;
* a genuine ``BFinanceError`` carries its own message and a non-``BFinanceError`` propagates;
* no absent metric can come back as ``NaN``;
* the exact correct number is asserted for the well-formed case, so none of the above can pass by
  breaking EV everywhere.

Everything here is offline. A synthetic ``CompanyProfile`` is injected onto a real ``Ticker``
through the private ``_profile`` attribute that ``_ensure_profile()`` short-circuits on, and
``cache_ttl_hours=0`` means no cache file is created. A gate that depends on the network, or on a
warm ``~/.bfinance``, is not a gate.
"""

import ast
import inspect
import math
import pathlib
import textwrap

import pandas as pd
import pytest

from bfinance.market import ratios as ratios_mod
from bfinance.market.ratios import CustomRatiosCalculator
from bfinance.models.company import CompanyProfile, TopRatios
from bfinance.models.statements import FinancialStatement
from bfinance.ticker import Ticker
from bfinance.utils.exceptions import BFinanceError, ParsingError


_HEADERS = ["Mar 2023", "Mar 2024"]

#: The three legs, chosen so the arithmetic is checkable by hand and the cash term dominates.
#: mcap 5000 + debt 40 - cash 250 = 4790, which is BELOW bare market cap. The old code returned
#: 5040 -- above it, by the full cash balance, and equal to bare market cap plus debt.
_MCAP_CR = 5000.0
_DEBT_CR = 40.0
_CASH_CR = 250.0
_EV_CR = _MCAP_CR + _DEBT_CR - _CASH_CR  # 4790.0
_FABRICATED_EV_CR = _MCAP_CR + _DEBT_CR  # 5040.0 -- what `cash_hold = 0.0` produced

_SALES_CR = 1200.0
_OP_PROFIT_CR = 300.0
_DEPRECIATION_CR = 60.0
_EBITDA_CR = _OP_PROFIT_CR + _DEPRECIATION_CR  # 360.0
_EV_TO_EBITDA = round(_EV_CR / _EBITDA_CR, 2)


def _pnl() -> FinancialStatement:
    return FinancialStatement(
        headers=list(_HEADERS),
        rows={
            "Sales": [1000.0, _SALES_CR],
            "Operating Profit": [200.0, _OP_PROFIT_CR],
            "Depreciation": [50.0, _DEPRECIATION_CR],
            "Other Income": [8.0, 10.0],
            "Interest": [20.0, 20.0],
            "Net Profit": [80.0, 100.0],
            "EPS in Rs": [4.0, 5.0],
        },
    )


def _cf() -> FinancialStatement:
    return FinancialStatement(
        headers=list(_HEADERS),
        rows={
            "Cash from Operating Activity": [200.0, 250.0],
            "Cash from Investing Activity": [-60.0, -80.0],
        },
    )


def _bs_with_cash(label: str = "Cash Equivalents") -> FinancialStatement:
    """A well-formed balance sheet that DOES itemise cash, plus borrowings."""
    return FinancialStatement(
        headers=list(_HEADERS),
        rows={
            "Equity Capital": [100.0, 100.0],
            "Reserves": [2000.0, 2100.0],
            "Borrowings": [_DEBT_CR, _DEBT_CR],
            label: [100.0, _CASH_CR],
            "Total Assets": [2400.0, 2450.0],
        },
    )


def _bs_like_upstream() -> FinancialStatement:
    """
    The real screener.in balance sheet: exactly the ten rows the fixtures carry, no cash.

    This is the shape nearly every live caller gets, so it is the case that used to return a
    confidently wrong number.
    """
    return FinancialStatement(
        headers=list(_HEADERS),
        rows={
            "Equity Capital": [1251.0, 1253.0],
            "Reserves": [68779.0, 71254.0],
            "Borrowings": [285.0, 2399.0],
            "Other Liabilities": [17688.0, 18731.0],
            "Total Liabilities": [88003.0, 93637.0],
            "Fixed Assets": [21955.0, 22443.0],
            "CWIP": [1091.0, 1602.0],
            "Investments": [34720.0, 38128.0],
            "Other Assets": [30237.0, 31464.0],
            "Total Assets": [88003.0, 93637.0],
        },
    )


def _bs_debt_free() -> FinancialStatement:
    """Debt reported as a real 0.0, and cash present. Not a parse failure, not a missing row."""
    return FinancialStatement(
        headers=list(_HEADERS),
        rows={
            "Equity Capital": [100.0, 100.0],
            "Borrowings": [0.0, 0.0],
            "Cash Equivalents": [80.0, _CASH_CR],
        },
    )


def _bs_no_borrowings_row() -> FinancialStatement:
    """Readable, but the statement simply has no Borrowings row. A third, distinct state."""
    return FinancialStatement(
        headers=list(_HEADERS),
        rows={"Equity Capital": [100.0, 100.0], "Cash Equivalents": [80.0, _CASH_CR]},
    )


def _bs_null_borrowings() -> FinancialStatement:
    """
    What a debt-free company actually produces, via the package's own parser.

    screener.in prints ``-`` for a company with no borrowings. ``parse_indian_number`` maps that to
    ``None`` (``utils/formatting.py:26``), and ``FinancialStatement.rows`` is typed
    ``Dict[str, List[Optional[float]]]`` so ``None`` is what the cell carries -- the frame shows it
    as NaN. The old ``_safe_float(..., default=0.0)`` read that NaN as ``0.0``, so "this company has
    no debt" and "we could not read the borrowings figure" were the same number.
    """
    return FinancialStatement(
        headers=list(_HEADERS),
        rows={
            "Equity Capital": [100.0, 100.0],
            "Borrowings": [None, None],
            "Cash Equivalents": [80.0, _CASH_CR],
        },
    )


def _object_frame_with_dashes() -> pd.DataFrame:
    """
    A frame carrying the literal ``-`` string, as a cache round-trip or an ``astype(object)``
    can produce. Not reachable through the pydantic model -- which rejects it -- so it is built
    directly. Same failure mode as :func:`_bs_null_borrowings`: an absent number that a
    ``default=0.0`` coercion would report as a reported zero.
    """
    # Rows are metrics and columns are periods -- the shape `to_dataframe(orient="columns")`
    # produces, which is why the transpose is needed on a dict-of-lists.
    return pd.DataFrame(
        {
            "Equity Capital": [100.0, 100.0],
            "Borrowings": ["-", "-"],
            "Cash Equivalents": [80.0, _CASH_CR],
        },
        index=_HEADERS,
    ).T.astype(object)


def _bs_ragged() -> FinancialStatement:
    """The measured failure: one value too few for the number of periods -> ``ParsingError``."""
    return FinancialStatement(
        headers=list(_HEADERS),
        rows={
            "Equity Capital": [100.0, 100.0],
            "Borrowings": [30.0, 40.0, 50.0],  # ragged by one cell
            "Cash Equivalents": [100.0, _CASH_CR],
        },
    )


def _ticker(balance_sheet: FinancialStatement) -> Ticker:
    """A Ticker with a pre-loaded profile. No network, and no cache file."""
    t = Ticker("TEST", cache_ttl_hours=0)
    t._profile = CompanyProfile(
        symbol="TEST",
        company_id=1,
        name="Test Co",
        ratios=TopRatios(
            market_cap=_MCAP_CR,
            current_price=250.0,
            high_52w=300.0,
            low_52w=180.0,
            stock_pe=20.0,
            book_value=500.0,
        ),
        profit_loss=_pnl(),
        cash_flow=_cf(),
        balance_sheet=balance_sheet,
    )
    return t


def _ratios(balance_sheet: FinancialStatement, market_cap_cr=_MCAP_CR) -> dict:
    """``calculate_all_custom_ratios`` over synthetic frames -- no Ticker involved."""
    return CustomRatiosCalculator.calculate_all_custom_ratios(
        market_cap_cr=market_cap_cr,
        current_price=250.0,
        trailing_pe=20.0,
        book_value=500.0,
        eps=5.0,
        pnl=_pnl().to_dataframe(orient="columns"),
        bs=balance_sheet.to_dataframe(orient="columns"),
        cf=_cf().to_dataframe(orient="columns"),
    )


def _frame(balance_sheet: FinancialStatement) -> pd.DataFrame:
    return balance_sheet.to_dataframe(orient="columns")


# ------------------------------------------------------- the premise, proved from fixtures


def test_the_upstream_balance_sheet_really_has_no_cash_row():
    """
    The factual basis for the whole fix, asserted rather than asserted-in-a-comment.

    Read straight from the three committed screener.in fixtures through the package's own parser.
    If this ever fails, the upstream does itemise cash, and the EV gate can be relaxed to a
    working lookup instead of a hook.
    """
    from bs4 import BeautifulSoup

    from bfinance.screener.parser import ScreenerHTMLParser

    parser = ScreenerHTMLParser()
    fixtures = pathlib.Path(__file__).parent / "fixtures" / "screener"
    for name in ("company_itc", "company_tcs", "company_itc_full_columns"):
        path = fixtures / f"{name}.html"
        if not path.exists():
            continue
        soup = BeautifulSoup(path.read_text(encoding="utf-8", errors="replace"), "lxml")
        statement = parser._parse_table_section(soup, "balance-sheet")
        rows = set(statement.rows)

        assert len(rows) == 10, f"{name} balance sheet is no longer the measured ten rows"
        assert "Borrowings" in rows
        assert not rows & set(ratios_mod._CASH_ROW_LABELS), (
            f"{name} DOES itemise cash -- if this fails, _CASH_ROW_LABELS can be promoted from a "
            "hook to a working lookup and the EV gate relaxed."
        )


def test_the_cash_hook_is_the_same_hook_in_both_modules():
    """
    ``market/ratios.py`` and ``ticker.py`` each hold a copy of the label tuple, because the facade
    must not become a dependency of the engine it sits on. This is the tripwire that keeps the two
    from drifting: the honest fix is for ``ticker.py`` to import the constant from here.
    """
    from bfinance.ticker import _CASH_ROW_LABELS as TICKER_CASH

    assert ratios_mod._CASH_ROW_LABELS == TICKER_CASH


# ------------------------------------------------- the headline: a net-cash profile is not mcap+debt


def test_a_profile_without_an_itemised_cash_row_does_not_yield_mcap_plus_debt():
    """
    The core assertion. The upstream shape has no cash row, so the cash leg is unsourceable and
    the honest answer is "not computed, and here is why" -- not ``mcap + debt``.

    ``1300.0``-style assertions in the old suite were satisfied *by the defect*: a fabricated
    zero cash leg is exactly what produces ``mcap + debt``.
    """
    out = _ratios(_bs_like_upstream())

    assert out["enterprise_value_cr"] is None, (
        "an unsourceable cash leg must not be reported as a zero cash leg"
    )
    assert out["enterprise_value_cr"] != pytest.approx(_FABRICATED_EV_CR)
    assert out["enterprise_value_cr"] != pytest.approx(_MCAP_CR)
    assert out["cash_and_equivalents_cr"] is None
    # The reason is published, and it names the missing leg.
    omitted = out["bfinance_enterprise_value_omitted"]
    assert omitted and "cash" in omitted, f"the reason must name the missing leg, got {omitted!r}"
    # The legs that ARE readable are published, rather than discarded along with the EV.
    assert out["bfinance_debt_cr"] == pytest.approx(2399.0)
    assert out["bfinance_cash_cr"] is None
    # And this is an absent input, not a failure: nothing is recorded as broken.
    assert not any("failed" in note for note in out["bfinance_omitted_metrics"])


def test_the_dead_cash_branch_is_gone_from_the_source():
    """
    A mechanical guard, because the branch was *plausible-looking* dead code rather than obviously
    dead code. Restoring ``cash_hold = 0.0`` puts that name back and this goes red.

    The bare-handler check is made on the parsed syntax tree, not on the source text, so a comment
    that merely mentions ``except Exception: pass`` -- as several comments here do, to explain what
    was removed -- cannot satisfy or trip it. Only a real handler counts.
    """
    source = inspect.getsource(CustomRatiosCalculator.calculate_all_custom_ratios)
    tree = ast.parse(textwrap.dedent(source))

    assert "cash_hold" not in source, "the fabricated cash accumulator is back"

    bare: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        if node.type is None:
            bare.append(f"line {node.lineno} (bare `except:`)")
            continue
        names = [n.id for n in ast.walk(node.type) if isinstance(n, ast.Name)]
        if "Exception" in names or "BaseException" in names:
            bare.append(f"line {node.lineno}")
    assert not bare, f"a bare handler is back in calculate_all_custom_ratios at {bare}"

    # No leg may be defaulted to zero any more. `Depreciation` is the one deliberate exception and
    # it is stated in a comment at the site, because "no depreciation line" is a real modelling
    # choice for EBITDA rather than an unmeasured input.
    assert "latest_debt = 0.0" not in source
    assert 'bs.loc["Borrowings"].iloc[-1]' not in source, "the leg is read through _cell now"


def test_no_bare_handler_survives_anywhere_in_the_ev_path():
    """
    Wider than the one method: the four handlers that used to swallow sit either in
    ``calculate_all_custom_ratios`` or in the module-level cell reader it delegates to, and a fix
    that only cleaned up the first would leave the second one guarding every leg read.
    """
    for target in (
        CustomRatiosCalculator.calculate_all_custom_ratios,
        ratios_mod._cell,
    ):
        tree = ast.parse(textwrap.dedent(inspect.getsource(target)))
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler) and node.type is not None:
                names = [n.id for n in ast.walk(node.type) if isinstance(n, ast.Name)]
                assert "Exception" not in names, (
                    f"{target.__name__}: bare `except Exception` at line {node.lineno}"
                )


# ------------------------------------------- the three public surfaces must not drift from each other


def test_enterprise_value_ev_to_ebitda_and_custom_ratios_agree_on_the_omission():
    """
    Three public surfaces over one computation. When the cash leg is unsourceable all three must
    withhold, and none of them may hand back a number the others refuse.
    """
    t = _ticker(_bs_like_upstream())
    out = t.custom_ratios

    assert t.enterprise_value is None, (
        "Ticker.enterprise_value reads custom_ratios with .get(..., 0.0); an ABSENT key would hand "
        "this caller a fabricated 0.0, so the key must be present and None"
    )
    assert t.enterprise_value == out["enterprise_value_cr"]
    assert t.ev_to_ebitda is None, "EV/EBITDA must follow the same omission as EV"
    assert "ev_to_ebitda" not in out
    assert out["enterprise_value_cr"] is None
    # The EV/EBITDA omission is explained too, not just the EV one.
    assert any("ev_to_ebitda" in note for note in out["bfinance_omitted_metrics"])


def test_the_three_surfaces_agree_on_the_real_number_too():
    """The same agreement on the happy path, so the test above cannot pass by breaking EV."""
    t = _ticker(_bs_with_cash())
    out = t.custom_ratios

    assert out["enterprise_value_cr"] == pytest.approx(_EV_CR)
    assert t.enterprise_value == pytest.approx(_EV_CR)
    assert t.ev_to_ebitda == pytest.approx(_EV_TO_EBITDA)
    assert t.enterprise_value == out["enterprise_value_cr"]
    assert out["bfinance_enterprise_value_omitted"] is None
    assert not any("cash leg" in note for note in out["bfinance_omitted_metrics"])


# ------------------------------------------------- the hook must be live, not "always omit"


@pytest.mark.parametrize("label", ["Cash Equivalents", "Cash and Bank Balance"])
def test_a_balance_sheet_that_does_carry_cash_is_honoured(label):
    """
    The fix is "omit when unsourceable", not "always omit". Both labels in the hook are read, so a
    synthetic or alternate source that itemises cash produces a real number with no code change.
    """
    out = _ratios(_bs_with_cash(label))

    assert out["bfinance_cash_cr"] == pytest.approx(_CASH_CR)
    assert out["cash_and_equivalents_cr"] == pytest.approx(_CASH_CR)
    assert out["enterprise_value_cr"] == pytest.approx(_EV_CR)
    assert out["enterprise_value_cr"] != pytest.approx(_FABRICATED_EV_CR)
    assert out["bfinance_enterprise_value_omitted"] is None


def test_the_first_present_cash_label_wins():
    """The hook is ordered, so the order has to be a decision rather than an accident."""
    statement = _bs_with_cash("Cash Equivalents")
    statement.rows["Cash and Bank Balance"] = [100.0, 999.0]

    out = _ratios(statement)

    assert out["bfinance_cash_cr"] == pytest.approx(_CASH_CR), (
        "'Cash Equivalents' is first in the hook, so it must be the one that is read"
    )
    assert out["enterprise_value_cr"] == pytest.approx(_EV_CR)


def test_happy_path_control_the_exact_number():
    """
    The control for every omission test in this file: the well-formed case produces the precise
    expected value, computed by hand from the three legs. Without this, "return None for EV
    always" would turn this file entirely green.
    """
    out = _ratios(_bs_with_cash())

    assert out["enterprise_value_cr"] == 4790.0
    assert out["ev_to_ebitda"] == round(4790.0 / 360.0, 2)
    assert out["cash_and_equivalents_cr"] == 250.0
    assert out["bfinance_debt_cr"] == pytest.approx(_DEBT_CR)
    assert out["bfinance_cash_cr"] == pytest.approx(_CASH_CR)
    # Every other ratio still computes, so the EV gate did not take the rest of the dict with it.
    assert out["interest_coverage"] == pytest.approx((300.0 + 10.0) / 20.0)
    assert out["debt_to_equity"] == pytest.approx(round(_DEBT_CR / (2000.0 + 100.0), 2))
    assert out["cfo_to_pat"] == pytest.approx(2.5)
    assert out["free_cash_flow_cr"] == pytest.approx(170.0)
    assert out["graham_number"] is not None
    assert out["piotroski_score"] >= 0


# -------------------------------------------------- debt-free vs row-absent vs failed-parse


def test_the_three_debt_states_are_distinguishable():
    """
    ``_safe_float(..., default=0.0)`` made "no borrowings" and "the borrowings figure could not be
    read" the same number. Three inputs, three outcomes.
    """
    debt_free = _ratios(_bs_debt_free())
    row_absent = _ratios(_bs_no_borrowings_row())

    # 1. Debt-free: a real 0.0, and a real EV.
    assert debt_free["bfinance_debt_cr"] == pytest.approx(0.0)
    assert debt_free["bfinance_debt_cr"] is not None, "a reported zero is not the same as no row"
    assert debt_free["enterprise_value_cr"] == pytest.approx(_MCAP_CR - _CASH_CR)
    assert debt_free["bfinance_enterprise_value_omitted"] is None

    # 2. Row absent: the debt leg is unknown, so no EV, and the reason says so. No error -- nothing
    #    failed, the statement simply does not carry the line.
    assert row_absent["bfinance_debt_cr"] is None
    assert row_absent["enterprise_value_cr"] is None
    assert "Borrowings" in row_absent["bfinance_enterprise_value_omitted"]
    assert not any("failed" in note for note in row_absent["bfinance_omitted_metrics"])

    # 3. Failed parse: see the next test -- it raises rather than returning a dict, which is a
    #    third and loudest signal. Asserted here so the set is read as a set.
    assert debt_free["bfinance_enterprise_value_omitted"] != (
        row_absent["bfinance_enterprise_value_omitted"]
    )
    assert debt_free["bfinance_omitted_metrics"] != row_absent["bfinance_omitted_metrics"]


def test_the_upstreams_dash_for_no_borrowings_is_not_read_as_zero():
    """
    A debt-free company's borrowings cell is ``None``/NaN, and must be reported as an absent
    number rather than a reported zero.
    """
    out = _ratios(_bs_null_borrowings())

    assert out["bfinance_debt_cr"] is None, "a NaN borrowings cell is not a company with no debt"
    assert out["bfinance_debt_cr"] != pytest.approx(0.0)
    assert out["enterprise_value_cr"] is None
    assert "Borrowings" in out["bfinance_enterprise_value_omitted"]


def test_a_literal_dash_cell_is_also_absent_not_zero():
    """
    The same absence, spelled as the literal ``-`` string, which a cache round-trip or an
    ``astype(object)`` frame can carry even though the pydantic model would reject it. This is the
    exact input ``_safe_float(..., default=0.0)`` used to silently turn into a debt of zero.
    """
    out = CustomRatiosCalculator.calculate_all_custom_ratios(
        market_cap_cr=_MCAP_CR,
        current_price=250.0,
        trailing_pe=20.0,
        book_value=500.0,
        eps=5.0,
        pnl=_frame(_pnl()),
        bs=_object_frame_with_dashes(),
        cf=_frame(_cf()),
    )

    assert out["bfinance_debt_cr"] is None
    assert out["bfinance_debt_cr"] != pytest.approx(0.0)
    assert out["enterprise_value_cr"] is None
    assert "Borrowings" in out["bfinance_enterprise_value_omitted"]


def test_a_ragged_statement_raises_its_own_message_out_of_every_surface():
    """
    The measured third state. ``FinancialStatement.to_dataframe`` raises ``ParsingError`` -- a
    ``BFinanceError``, and exactly the typed error its own docstring says escapes untyped handlers
    -- on a ragged row. No surface may turn that into a number.

    All three surfaces raise identically, which is itself part of the contract: they are one
    computation, and a caller must not get a confident EV from one of them.
    """
    for surface in ("custom_ratios", "enterprise_value", "ev_to_ebitda"):
        t = _ticker(_bs_ragged())
        with pytest.raises(ParsingError) as excinfo:
            getattr(t, surface)
        message = str(excinfo.value)
        assert "Borrowings" in message, f"{surface}: the upstream message must survive: {message!r}"
        assert "Malformed statement row" in message, f"{surface}: {message!r}"


def test_a_genuine_bfinance_error_is_reported_with_its_own_message():
    """
    Not swallowed, not reworded. The caller gets the type and the text that named the offending
    row, which is the only thing that makes a ragged upstream page diagnosable.
    """
    assert issubclass(ParsingError, BFinanceError), (
        "the narrowed handlers and this file's premise both rest on ParsingError being a "
        "BFinanceError"
    )

    with pytest.raises(BFinanceError) as excinfo:
        _ticker(_bs_ragged()).custom_ratios

    message = str(excinfo.value)
    assert "Malformed statement row 'Borrowings'" in message
    assert "3 value(s) for 2 period(s)" in message, f"the counts must survive: {message!r}"
    assert "Mar 2023, Mar 2024" in message, f"the periods must survive: {message!r}"


def test_a_non_bfinance_error_from_a_statement_propagates():
    """
    Narrowing the handlers must not become a different silent swallow. ``to_dataframe`` raising a
    ``TypeError`` is a bug in this package, not bad upstream data.
    """

    class _Boom(FinancialStatement):
        def to_dataframe(self, orient: str = "columns"):
            raise TypeError("not a data problem")

    t = _ticker(_bs_with_cash())
    t._profile.balance_sheet = _Boom(headers=list(_HEADERS), rows={"Borrowings": [1.0, 2.0]})

    with pytest.raises(TypeError):
        t.custom_ratios


def test_a_bug_in_a_cell_read_propagates_rather_than_becoming_a_missing_number():
    """
    The narrowing, tested from the inside. ``_cell`` catches exactly KeyError / IndexError / TypeError
    / ValueError. An ``AttributeError`` is a bug here, and the old ``except Exception:`` would have
    turned it into a confident ``None`` that reads as "the company reported nothing".
    """

    class _Exploding(pd.DataFrame):
        @property
        def loc(self):  # noqa: D401 - a deliberate fault injection
            raise AttributeError("a bug in this package, not bad upstream data")

    # Rows are metrics and columns are periods, so Borrowings has to be an index *label* here.
    frame = _Exploding(
        [[1.0, 2.0]], index=["Borrowings"], columns=["Mar 2023", "Mar 2024"]
    )
    with pytest.raises(AttributeError):
        ratios_mod._cell(frame, "Borrowings")


def test_cell_returns_a_value_only_when_it_has_one():
    """
    The pair contract, directly: ``value is None`` if and only if ``reason is not None``. A single
    return slot cannot carry "zero" and "unknown", which is why this returns a pair.
    """
    frame = _frame(_bs_with_cash())

    value, reason = ratios_mod._cell(frame, "Cash Equivalents")
    assert value == pytest.approx(_CASH_CR)
    assert reason is None

    value, reason = ratios_mod._cell(frame, "Not A Row")
    assert value is None
    assert reason and "Not A Row" in reason

    value, reason = ratios_mod._cell(pd.DataFrame(), "Borrowings")
    assert value is None
    assert reason and "empty" in reason

    # A reported zero is a value, with no reason. The two must never be confusable.
    zero_frame = pd.DataFrame([[0.0]], index=["Borrowings"], columns=["Mar 2024"])
    value, reason = ratios_mod._cell(zero_frame, "Borrowings")
    assert value == pytest.approx(0.0)
    assert reason is None


def test_a_nan_borrowings_cell_is_absent_not_zero():
    """
    A NaN leg cell is an absent number and reads as one, never as a reported zero.

    The frame carries cash as well as borrowings, so the *debt* leg is the missing one and the
    reason says so -- otherwise the cash leg would legitimately take precedence and this would
    pass for the wrong reason.
    """
    frame = _frame(_bs_with_cash()).astype(object)
    frame.loc["Borrowings", "Mar 2024"] = float("nan")

    out = CustomRatiosCalculator.calculate_all_custom_ratios(
        market_cap_cr=_MCAP_CR,
        current_price=250.0,
        trailing_pe=20.0,
        book_value=500.0,
        eps=5.0,
        pnl=_frame(_pnl()),
        bs=frame,
        cf=_frame(_cf()),
    )

    assert out["bfinance_cash_cr"] == pytest.approx(_CASH_CR), "the cash leg is present and known"
    assert out["bfinance_debt_cr"] is None
    assert out["enterprise_value_cr"] is None
    assert "Borrowings" in out["bfinance_enterprise_value_omitted"]


# ------------------------------------------------------------------ NaN must not reappear


def test_no_absent_metric_comes_back_as_nan():
    """
    ``None`` and ``NaN`` are not interchangeable: ``NaN`` is a *number*, so it propagates silently
    through arithmetic, prints as a plausible cell, and passes an ``is not None`` check. Every
    absent metric in every shape used in this file must be ``None``.
    """
    shapes = {
        "upstream (no cash row)": _bs_like_upstream(),
        "cash present": _bs_with_cash(),
        "debt free": _bs_debt_free(),
        "no borrowings row": _bs_no_borrowings_row(),
        "null borrowings": _bs_null_borrowings(),
    }
    for label, statement in shapes.items():
        out = _ratios(statement)
        for key, value in out.items():
            if isinstance(value, float):
                assert not math.isnan(value), f"{label}: {key} came back as NaN"

    # The specific ones, stated rather than looped.
    out = _ratios(_bs_like_upstream())
    for key in ("enterprise_value_cr", "cash_and_equivalents_cr", "ev_to_ebitda"):
        assert key not in out or out[key] is None
        assert not isinstance(out.get(key), float), f"{key} is a float, so it is arithmetic-silent"
    # Provenance values are swept too, not just the ratios.
    assert out["bfinance_cash_cr"] is None
    assert not isinstance(out["bfinance_cash_cr"], float)


def test_the_return_boundary_refuses_to_publish_a_nan_even_if_one_is_computed(monkeypatch):
    """
    The guard is at the return, so it holds for any future arithmetic -- not only for the paths
    exercised above. A monkeypatched ``_cell`` that lies and returns NaN stands in for "some
    computation produced NaN", and proves the boundary is load-bearing rather than incidental.

    This is the dict-side twin of ``dtype=object`` in ``Ticker.valuation_measures``, where an
    inferred float64 column turned every absent metric into a float NaN.
    """
    real_cell = ratios_mod._cell

    def _nan_cell(df, row):
        value, reason = real_cell(df, row)
        if row in ("Cash Equivalents", "Cash and Bank Balance"):
            return float("nan"), reason
        return value, reason

    monkeypatch.setattr(ratios_mod, "_cell", _nan_cell)
    out = _ratios(_bs_with_cash())

    assert out["enterprise_value_cr"] is None, "a NaN EV must not leave this function"
    assert not isinstance(out["enterprise_value_cr"], float)
    assert out["bfinance_cash_cr"] is None
    assert any("NaN" in note for note in out["bfinance_omitted_metrics"]), (
        "the reason a value was dropped must be recorded, not just the drop"
    )


# -------------------------------------------------- the omitted-metric ledger is not optional


def test_every_omitted_metric_is_published_with_a_reason():
    """
    The four handlers that used to be ``except Exception: pass`` discarded the reason entirely, so
    a caller could not tell a missing ratio from a bug. Each omission now leaves a line naming the
    metric and the missing input.
    """
    out = _ratios(_bs_like_upstream())
    notes = out["bfinance_omitted_metrics"]

    assert isinstance(notes, list) and notes, "the ledger must be present and non-empty"
    assert "ev_to_ebitda" not in out
    assert any(n.startswith("ev_to_ebitda:") for n in notes)
    # debt_to_equity and cfo_to_pat still compute here, so they must not be claimed as omitted.
    assert out["debt_to_equity"] is not None
    assert not any(n.startswith("debt_to_equity:") for n in notes)
    assert out["cfo_to_pat"] is not None
    assert not any(n.startswith("cfo_to_pat:") for n in notes)


def test_a_ratio_denominator_that_is_not_positive_is_omitted_with_a_reason():
    """The guards that were silent are now narrated. A loss-making company has no coverage ratio."""
    statement = _pnl()
    statement.rows["Interest"] = [0.0, 0.0]
    out = CustomRatiosCalculator.calculate_all_custom_ratios(
        market_cap_cr=_MCAP_CR,
        current_price=250.0,
        trailing_pe=20.0,
        book_value=500.0,
        eps=5.0,
        pnl=_frame(statement),
        bs=_frame(_bs_with_cash()),
        cf=_frame(_cf()),
    )

    assert "interest_coverage" not in out
    assert any("interest_coverage" in n for n in out["bfinance_omitted_metrics"])
    # And the EV, which is independent of interest, still computes.
    assert out["enterprise_value_cr"] == pytest.approx(_EV_CR)


def test_an_absent_market_cap_is_an_unknown_equity_leg_not_a_zero_equity_leg():
    """``market_cap_cr=None`` used to become ``mcap = 0.0`` and silently produce ``debt - cash``."""
    out = _ratios(_bs_with_cash(), market_cap_cr=None)

    assert out["enterprise_value_cr"] is None
    assert "equity" in out["bfinance_enterprise_value_omitted"]
    # mcap + debt - cash with a fabricated zero equity leg would be -210.0. Never report that.
    assert out["enterprise_value_cr"] != pytest.approx(_DEBT_CR - _CASH_CR)


def test_a_zero_market_cap_is_treated_as_unknown_not_as_a_real_zero():
    """Explicit ``> 0``, not truthiness: a 0 market cap is a missing input, not a nil company."""
    out = _ratios(_bs_with_cash(), market_cap_cr=0.0)

    assert out["enterprise_value_cr"] is None
    assert "equity" in out["bfinance_enterprise_value_omitted"]


def test_provenance_keys_are_always_present():
    """
    A caller that has to ask "is this key missing, or did something fail?" cannot learn anything.
    The provenance keys exist on every call, whether or not anything was omitted.
    """
    for statement in (_bs_with_cash(), _bs_like_upstream(), _bs_debt_free()):
        out = _ratios(statement)
        for key in (
            "bfinance_enterprise_value_omitted",
            "bfinance_debt_cr",
            "bfinance_cash_cr",
            "bfinance_omitted_metrics",
        ):
            assert key in out, f"{key} is missing on the {list(statement.rows)[0]!r} shape"
        assert isinstance(out["bfinance_omitted_metrics"], list)
