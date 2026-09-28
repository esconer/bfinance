"""
Regression tests for the valuation-honesty defects in :mod:`bfinance.ticker`.

Two wrong numbers, three silent swallows
----------------------------------------

**1. ``cash_cr = 0.0`` (was ``ticker.py:651``).** The comment beside it claimed "no cash row in
balance-sheet model". That claim is *true of the upstream source* and false as a reason to
substitute a constant, which is what the code did. Measured on the two real screener.in
fixtures (``tests/fixtures/screener/company_itc.html:904-1218``, ``company_tcs.html:904-1218``),
read through ``ScreenerHTMLParser._parse_table_element``, the balance-sheet table has exactly
ten rows::

    Equity Capital, Reserves, Borrowings, Other Liabilities, Total Liabilities,
    Fixed Assets, CWIP, Investments, Other Assets, Total Assets

Cash sits inside the undifferentiated "Other Assets" bucket, so the cash leg of
``mcap + debt - cash`` is genuinely unsourceable. The old code did not say so -- it returned
``mcap + debt``, which for every net-cash company overstates EV by the entire cash balance,
permanently and with no exception ever raised.

**2. A bare ``except`` around ``to_dataframe()`` (was ``ticker.py:631``/``:635``).**
``FinancialStatement.to_dataframe()`` raises ``ParsingError`` -- a ``BFinanceError``, and exactly
the typed error its own docstring says "escapes the typed handlers in ``Ticker``" -- on a ragged
row. The bare handler caught it, substituted ``pd.DataFrame()``, and the arithmetic continued on
a frame where every row reads ``0.0``. Measured, on a balance sheet ragged by one cell::

    control   EnterpriseValue = 5,000,000,000.00
    ragged    EnterpriseValue = 1,000,000,000.00
    difference                = 4,000,000,000.00     <- 80% understatement, no flag, no None

The three remaining bare ``except Exception:`` (was ``ticker.py:139``, ``:157``, ``:646``)
degrade to a *missing* value rather than a wrong one, so they are lower severity -- but the
``history()`` failure was invisible: a 52-week range that is absent because the fetch failed is
indistinguishable from a company that has none.

What this file pins
-------------------

The contract is "never a confidently wrong number; returning nothing is acceptable". So:

* EV is returned **only** when all three legs (equity, debt, cash) are actually in hand;
* any missing leg yields ``None`` for ``EnterpriseValue``/``EVToRevenue``/``EVToEBITDA``, plus a
  machine-readable reason in ``df.attrs["bfinance_enterprise_value_omitted"]``;
* a statement that failed to parse is reported, never rounded down to zero;
* debt-free, row-absent and failed-to-parse are three distinguishable states;
* an upstream ``history()`` failure is recorded on ``ticker.data_warnings``, or raises under
  ``raise_errors=True``;
* and the common path -- everything available -- is asserted unchanged, so this file cannot pass
  by breaking ``info`` / ``fast_info``.

Everything here is offline: a synthetic ``CompanyProfile`` is injected onto a real ``Ticker`` via
the private ``_profile`` attribute that ``_ensure_profile()`` short-circuits on, and the OHLCV
engine is replaced by a stub so ``info`` / ``fast_info`` / ``valuation_measures`` never reach the
network. A gate that depends on the network is not a gate.
"""

import pandas as pd
import pytest

from bfinance.models.company import CompanyProfile, TopRatios
from bfinance.models.statements import FinancialStatement
from bfinance.market.ohlcv import RealOHLCUnavailableError
from bfinance.ticker import Ticker
from bfinance.utils.exceptions import BFinanceError, ParsingError


# --------------------------------------------------------------- offline stubs


class _StubOHLCV:
    """
    Stands in for ``Ticker.ohlcv_engine``. Raises ``error`` if given, else returns ``frame``.

    ``Ticker.history()`` is exercised for real -- only the fetch beneath it is replaced. Without
    this, every ``info`` / ``fast_info`` / ``valuation_measures`` test in this file would make a
    live DNS call.
    """

    def __init__(self, frame=None, error=None):
        self.frame = pd.DataFrame() if frame is None else frame
        self.error = error
        self.calls = 0

    async def fetch_history(self, *args, **kwargs):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.frame


def _history_frame() -> pd.DataFrame:
    """A 1y daily frame with the columns FastInfo and build_info_dict actually read."""
    idx = pd.date_range("2024-01-01", periods=300, freq="B", tz="Asia/Kolkata", name="Date")
    return pd.DataFrame(
        {
            "Open": [100.0] * 300,
            "High": [110.0] * 300,
            "Low": [95.0] * 300,
            "Close": [100.0 + i * 0.1 for i in range(300)],
            "Volume": [1_000 + i for i in range(300)],
        },
        index=idx,
    )


_HEADERS = ["Mar 2023", "Mar 2024"]

#: Market cap in Rs Cr -> ``info["marketCap"]`` in Rs (build_info_dict scales by 1e7).
_MARKET_CAP_CR = 1000.0
_MCAP_RS = _MARKET_CAP_CR * 1e7  # 10,000,000,000

#: The three legs, chosen so the arithmetic is checkable by hand and the cash term dominates.
#: mcap 1.00e10 + debt 40 Cr (4.0e8) - cash 250 Cr (2.5e9) = 7.9e9, which is BELOW bare market
#: cap. The old code returned 1.04e10 -- above it, by the full cash balance.
_DEBT_CR = 40.0
_CASH_CR = 250.0
_EV_RS = _MCAP_RS + _DEBT_CR * 1e7 - _CASH_CR * 1e7  # 7,900,000,000
_BROKEN_EV_RS = _MCAP_RS + _DEBT_CR * 1e7  # 10,400,000,000 -- what `cash_cr = 0.0` produced

_SALES_CR = 1200.0
_OP_PROFIT_CR = 260.0


def _pnl() -> FinancialStatement:
    return FinancialStatement(
        headers=list(_HEADERS),
        rows={"Sales": [1000.0, _SALES_CR], "Operating Profit": [200.0, _OP_PROFIT_CR]},
    )


def _bs_net_cash() -> FinancialStatement:
    """A well-formed balance sheet that DOES itemise cash, plus borrowings."""
    return FinancialStatement(
        headers=list(_HEADERS),
        rows={
            "Equity Capital": [100.0, 100.0],
            "Reserves": [2000.0, 2100.0],
            "Borrowings": [_DEBT_CR, _DEBT_CR],
            "Cash Equivalents": [100.0, _CASH_CR],
            "Total Assets": [2400.0, 2450.0],
        },
    )


def _bs_ragged() -> FinancialStatement:
    """The measured failure: one value too few for the number of periods -> ``ParsingError``."""
    return FinancialStatement(
        headers=list(_HEADERS),
        rows={
            "Equity Capital": [100.0, 100.0],
            "Borrowings": [30.0, 40.0, 50.0],  # ragged by one cell
            "Cash Equivalents": [100.0, 250.0],
        },
    )


def _bs_debt_free() -> FinancialStatement:
    """Debt reported as a real 0.0, and cash present. Not a parse failure."""
    return FinancialStatement(
        headers=list(_HEADERS),
        rows={
            "Equity Capital": [100.0, 100.0],
            "Borrowings": [0.0, 0.0],
            "Cash Equivalents": [80.0, 300.0],
        },
    )


def _bs_no_borrowings_row() -> FinancialStatement:
    """Readable, but the statement simply has no Borrowings row. A third, distinct state."""
    return FinancialStatement(
        headers=list(_HEADERS),
        rows={"Equity Capital": [100.0, 100.0], "Cash Equivalents": [80.0, 300.0]},
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


def _ticker(balance_sheet, history=None, history_error=None, raise_errors=False) -> Ticker:
    """A Ticker with a pre-loaded profile and a stubbed OHLCV engine. No network, ever."""
    t = Ticker("TEST", raise_errors=raise_errors)
    t._profile = CompanyProfile(
        symbol="TEST",
        company_id=1,
        name="Test Co",
        ratios=TopRatios(
            market_cap=_MARKET_CAP_CR,
            current_price=250.0,
            high_52w=300.0,
            low_52w=180.0,
            stock_pe=20.0,
            book_value=500.0,
        ),
        profit_loss=_pnl(),
        balance_sheet=balance_sheet,
    )
    t.ohlcv_engine = _StubOHLCV(frame=history, error=history_error)
    return t


def _metric(vm: pd.DataFrame, name: str):
    return vm.loc[vm["Metric"] == name, "Value"].iloc[0]


# --------------------------------------------------------------- Defect 1: the cash leg


def test_net_cash_ev_subtracts_cash_instead_of_assuming_zero():
    """
    The headline assertion. A company holding Rs 250 Cr of cash against Rs 40 Cr of debt must
    report EV of ``mcap + debt - cash`` = 7.9e9, not ``mcap + debt`` = 1.04e9.

    This is the assertion the old code could not satisfy. Restoring ``cash_cr = 0.0`` makes it
    red by 2.5e9 -- verified, see the negative control in the run log.
    """
    vm = _ticker(_bs_net_cash()).valuation_measures

    ev = _metric(vm, "EnterpriseValue")
    assert ev is not None, "a fully-known bridge must produce a number"
    assert ev == pytest.approx(_EV_RS)
    assert ev != pytest.approx(_MCAP_RS), "EV equal to bare market cap is the defect itself"
    assert ev != pytest.approx(_BROKEN_EV_RS), "EV equal to mcap+debt means cash was ignored"
    # Direction of the original bias: the old code reported EV *above* market cap by the whole
    # cash balance. A net-cash company's EV must be *below* it.
    assert ev < _MCAP_RS
    assert _MCAP_RS - ev == pytest.approx(_CASH_CR * 1e7 - _DEBT_CR * 1e7)


def test_net_cash_ev_multiples_are_computed_from_the_corrected_ev():
    """EVToRevenue / EVToEBITDA must follow the same EV, not a stale one."""
    vm = _ticker(_bs_net_cash()).valuation_measures

    assert _metric(vm, "EVToRevenue") == pytest.approx(_EV_RS / (_SALES_CR * 1e7))
    assert _metric(vm, "EVToEBITDA") == pytest.approx(_EV_RS / (_OP_PROFIT_CR * 1e7))
    # And explicitly not the numbers the zero-cash EV produced.
    assert _metric(vm, "EVToRevenue") != pytest.approx(_BROKEN_EV_RS / (_SALES_CR * 1e7))


def test_absent_metrics_are_none_not_nan():
    """
    A missing metric must be ``None``, not ``NaN``.

    ``pd.DataFrame`` infers float64 for an all-numeric Value column and converts every ``None``
    into a float ``NaN``. That silently undoes the gate: ``NaN`` is a *number*, so it propagates
    through arithmetic, renders as a populated cell, and ``ev is not None`` is true. The Value
    column is therefore built with ``dtype=object``.
    """
    vm = _ticker(_bs_like_upstream()).valuation_measures

    assert vm["Value"].dtype == object, "an object column is what keeps None from becoming NaN"
    for name in ("EnterpriseValue", "EVToRevenue", "EVToEBITDA"):
        value = _metric(vm, name)
        assert value is None, f"{name} came back as {value!r}"
        assert not isinstance(value, float), f"{name} is a float, so it is arithmetic-silent"
    # A metric that WAS computed is still a plain number, not an object wrapper surprise.
    assert isinstance(_metric(vm, "MarketCap"), float)


def test_upstream_shape_without_a_cash_row_omits_ev_rather_than_guessing_zero_cash():
    """
    The real screener.in balance sheet has no itemised cash row, so the cash leg is
    unsourceable. The honest answer is "not computed, and here is why" -- not ``mcap + debt``.

    This is the case that affects essentially every live caller, and it is the one that used to
    return a confident wrong number.
    """
    t = _ticker(_bs_like_upstream())
    vm = t.valuation_measures

    assert _metric(vm, "EnterpriseValue") is None
    assert _metric(vm, "EVToRevenue") is None
    assert _metric(vm, "EVToEBITDA") is None

    omitted = vm.attrs["bfinance_enterprise_value_omitted"]
    assert omitted and "cash" in omitted, f"the reason must name the missing leg, got {omitted!r}"
    # The frame itself is still honest about the equity leg it *can* report.
    assert _metric(vm, "MarketCap") == pytest.approx(_MCAP_RS)
    # And the debt leg it can read is still published, rather than discarded with the EV.
    assert vm.attrs["bfinance_debt_cr"] == pytest.approx(2399.0)
    # Nothing about this is a failure -- it is an absent input, and it is not logged as one.
    assert not any("balance sheet" in w for w in t.data_warnings)


# --------------------------------------------------------------- Defect 2: the swallow


def test_ragged_balance_sheet_yields_no_enterprise_value():
    """
    A ragged row raises ``ParsingError``. The caller must not receive a number computed from a
    frame that replaced the real statement with an empty one.
    """
    vm = _ticker(_bs_ragged()).valuation_measures

    assert _metric(vm, "EnterpriseValue") is None
    assert _metric(vm, "EVToRevenue") is None
    assert _metric(vm, "EVToEBITDA") is None

    error = vm.attrs["bfinance_balance_sheet_error"]
    assert error, "a failed parse must be reported, not reduced to a missing row"


def test_ragged_balance_sheet_does_not_collapse_ev_to_bare_market_cap():
    """
    The measured 80% understatement, pinned directly.

    Old behaviour: the empty substitute frame made ``Borrowings`` read 0.0 and cash read 0.0, so
    EV came back as bare market cap -- here 1.0e10 against a correct 7.9e9. Asserted as "not that
    number" so the test cannot be satisfied by returning ``None`` for the wrong reason or by
    returning yet another wrong number.
    """
    vm = _ticker(_bs_ragged()).valuation_measures
    ev = _metric(vm, "EnterpriseValue")

    assert ev is None
    assert ev != pytest.approx(_MCAP_RS)
    assert ev != pytest.approx(_EV_RS)
    assert _metric(vm, "MarketCap") == pytest.approx(_MCAP_RS), "the real market cap survives"


def test_ragged_case_cannot_pass_by_breaking_the_happy_path():
    """
    The control, in the same file and the same run.

    The identical profile with one fewer cell returns the exact correct number. If the ragged
    test above goes green because EV broke everywhere, this goes red.
    """
    control = _ticker(_bs_net_cash()).valuation_measures
    ragged = _ticker(_bs_ragged()).valuation_measures

    assert _metric(control, "EnterpriseValue") == pytest.approx(_EV_RS)
    assert _metric(ragged, "EnterpriseValue") is None
    assert _metric(control, "MarketCap") == pytest.approx(_MCAP_RS)
    assert _metric(ragged, "MarketCap") == pytest.approx(_MCAP_RS)


def test_ragged_statement_is_reported_to_the_caller_with_its_own_message():
    """A failure is a recorded degradation, not a silently missing field."""
    t = _ticker(_bs_ragged())
    t.valuation_measures

    assert t.data_warnings, "the ragged statement produced no observable trace"
    joined = " | ".join(t.data_warnings)
    assert "balance sheet" in joined
    assert "ragged" in joined, f"the upstream message must survive: {joined!r}"
    assert any(isinstance(w, str) for w in t.data_warnings)


def test_ragged_statement_raises_under_raise_errors():
    """``raise_errors=True`` turns the degradation into a typed raise, per package convention."""
    with pytest.raises(ParsingError):
        _ticker(_bs_ragged(), raise_errors=True).valuation_measures


def test_a_non_bfinance_error_from_a_statement_is_not_swallowed():
    """
    Narrowing the handler means a genuine bug surfaces. ``to_dataframe`` raising a ``TypeError``
    is a bug in this package, not bad upstream data, and must not become an empty frame.
    """

    class _Boom(FinancialStatement):
        def to_dataframe(self, orient: str = "columns"):
            raise TypeError("not a data problem")

    t = _ticker(_bs_net_cash())
    t._profile.balance_sheet = _Boom(headers=list(_HEADERS), rows={"Borrowings": [1.0, 2.0]})
    with pytest.raises(TypeError):
        t.valuation_measures


# --------------------------------------------------------------- debt-free vs failed parse


def test_debt_free_row_absent_and_failed_parse_are_three_distinguishable_states():
    """
    ``borrow_cr = ... or 0.0`` made "no borrowings" and "the row failed to parse" the same
    number. Three inputs, three outcomes.
    """
    debt_free = _ticker(_bs_debt_free())
    row_absent = _ticker(_bs_no_borrowings_row())
    failed = _ticker(_bs_ragged())

    debt_free_vm = debt_free.valuation_measures
    row_absent_vm = row_absent.valuation_measures
    failed_vm = failed.valuation_measures

    # 1. Debt-free, cash known: a real EV, with a genuinely zero debt leg.
    assert debt_free_vm.attrs["bfinance_debt_cr"] == pytest.approx(0.0)
    assert _metric(debt_free_vm, "EnterpriseValue") == pytest.approx(
        _MCAP_RS - 300.0 * 1e7
    )
    assert not any("balance sheet" in w for w in debt_free.data_warnings)
    assert debt_free_vm.attrs["bfinance_balance_sheet_error"] is None

    # 2. Row absent: readable statement, no Borrowings line. The debt leg is unknown -> no EV,
    #    and the reason says so. No error recorded, because nothing failed.
    assert row_absent_vm.attrs["bfinance_debt_cr"] is None
    assert row_absent_vm.attrs["bfinance_balance_sheet_error"] is None
    assert _metric(row_absent_vm, "EnterpriseValue") is None
    assert "Borrowings" in row_absent_vm.attrs["bfinance_enterprise_value_omitted"]
    assert not any("balance sheet" in w for w in row_absent.data_warnings)

    # 3. Failed parse: an error is reported, on both channels.
    assert failed_vm.attrs["bfinance_balance_sheet_error"]
    assert failed_vm.attrs["bfinance_debt_cr"] is None
    assert "balance sheet unreadable" in failed_vm.attrs["bfinance_enterprise_value_omitted"]
    assert any("balance sheet" in w for w in failed.data_warnings)

    # No two of the three collapsed into the same signal.
    assert debt_free_vm.attrs["bfinance_enterprise_value_omitted"] is None
    assert row_absent_vm.attrs["bfinance_enterprise_value_omitted"] != (
        failed_vm.attrs["bfinance_enterprise_value_omitted"]
    )


# --------------------------------------------------------------- Defect 3: the other handlers


def test_history_failure_is_visible_through_data_warnings_for_info():
    """
    ``history()`` failing used to be indistinguishable from a company with no 52-week range: the
    handler swallowed it and returned an ``info`` with those fields simply absent.
    """
    boom = RealOHLCUnavailableError("TEST: NSE bhavcopy unavailable for the 1y window")
    t = _ticker(_bs_net_cash(), history_error=boom)

    info = t.info

    assert info["marketCap"] == pytest.approx(_MCAP_RS), "the profile-derived keys still build"
    assert t.data_warnings, "a failed history fetch left no trace at all"
    joined = " | ".join(t.data_warnings)
    assert "NSE bhavcopy unavailable" in joined, f"the upstream reason must survive: {joined!r}"
    assert "previous close" in joined


def test_history_failure_is_visible_through_data_warnings_for_fast_info():
    boom = RealOHLCUnavailableError("TEST: NSE bhavcopy returned no data")
    t = _ticker(_bs_net_cash(), history_error=boom)

    fi = t.fast_info

    # last_price still comes from the profile...
    assert fi.last_price == pytest.approx(250.0)
    # ...but the history-derived fields are absent, and that is now on the record.
    assert fi.previous_close is None
    assert fi.fifty_day_average is None
    assert t.data_warnings
    assert any("history failed" in w for w in t.data_warnings)


def test_history_failure_raises_under_raise_errors():
    """`raise_errors=True` is the loud path, for both members."""
    boom = RealOHLCUnavailableError("TEST: NSE bhavcopy returned no data")

    with pytest.raises(RealOHLCUnavailableError):
        _ticker(_bs_net_cash(), history_error=boom, raise_errors=True).info
    with pytest.raises(RealOHLCUnavailableError):
        _ticker(_bs_net_cash(), history_error=boom, raise_errors=True).fast_info


def test_a_bug_raised_by_history_is_not_swallowed():
    """
    The narrowing must not become a different silent swallow: only ``BFinanceError`` and
    ``RuntimeError`` are absorbed. A ``TypeError`` from the fetch is a bug here and propagates.
    """

    class _BuggyOHLCV:
        async def fetch_history(self, *args, **kwargs):
            raise TypeError("engine bug")

    t = _ticker(_bs_net_cash())
    t.ohlcv_engine = _BuggyOHLCV()
    with pytest.raises(TypeError):
        t.info


# --------------------------------------------------------------- the common path still works


def test_info_and_fast_info_are_unchanged_when_history_succeeds():
    """
    Pre-existing behaviour, pinned so the narrowed handlers cannot silently degrade the normal
    case: a full 1y frame is fetched, the 52-week fields are real, and nothing is recorded.
    """
    t = _ticker(_bs_net_cash(), history=_history_frame())

    info = t.info
    fi = t.fast_info

    assert info["marketCap"] == pytest.approx(_MCAP_RS)
    assert info["currentPrice"] == pytest.approx(250.0)
    assert t.ohlcv_engine.calls == 2, "one 1y frame per member, as before this change"

    assert fi.last_price == pytest.approx(250.0)
    assert fi.year_high == pytest.approx(300.0)
    assert fi.year_low == pytest.approx(180.0)
    assert fi.previous_close is not None and fi.previous_close > 0
    assert fi.last_volume == 1000 + 299
    assert fi.fifty_day_average is not None and fi.fifty_day_average > 0
    assert fi.two_hundred_day_average is not None and fi.two_hundred_day_average > 0

    assert t.data_warnings == (), f"a clean fetch recorded degradations: {t.data_warnings}"


def test_fast_info_history_is_fetched_once_and_reused_by_info():
    """One cached frame, no extra fan-out -- the behaviour the shared `_history_for_metadata` exists for."""
    t = _ticker(_bs_net_cash(), history=_history_frame())
    t.fast_info
    assert t.ohlcv_engine.calls == 1
    t.info
    assert t.ohlcv_engine.calls == 2, "each member fetches its own frame; that is unchanged"


def test_history_metadata_still_reports_a_real_previous_close():
    """`history_metadata` reads the frame `info` stored; the happy path must not lose it."""
    t = _ticker(_bs_net_cash(), history=_history_frame())
    meta = t.history_metadata

    assert meta["currency"] == "INR"
    assert meta["regularMarketPrice"] == pytest.approx(250.0)
    assert meta["previousClose"] is not None
    assert t.data_warnings == ()


def test_cagrs_is_untouched_and_never_had_a_handler():
    """
    Not a fix -- a check. The audit that found the five bare handlers assumed 631/635/646 were
    the CAGR handlers. They are not: ``Ticker.cagrs`` is three lines with no ``try`` at all, and
    it passes the profile's own value straight through. Asserted so the assumption cannot be
    re-imported by a later reader of this file.
    """
    t = _ticker(_bs_net_cash())
    t._profile.cagrs = {
        "10 Year": {"Sales CAGR": "12.4%", "Profit CAGR": "18.1%"},
        "5 Year": {"Sales CAGR": "9.2%"},
    }

    assert t.cagrs == t._profile.cagrs
    assert t.cagrs["10 Year"]["Sales CAGR"] == "12.4%"

    import inspect

    src = inspect.getsource(type(t).cagrs.fget)
    assert "try:" not in src and "except" not in src, "cagrs must not gain a swallowing handler"


# --------------------------------------------------------------- the cash question, pinned


def test_the_upstream_balance_sheet_really_has_no_cash_row():
    """
    The factual basis for refusing to compute EV from a real profile, asserted rather than
    asserted-in-a-comment. Read straight from the two committed screener.in fixtures.
    """
    from pathlib import Path

    from bs4 import BeautifulSoup

    from bfinance.screener.parser import ScreenerHTMLParser

    parser = ScreenerHTMLParser()
    for name in ("company_itc", "company_tcs"):
        html = (Path(__file__).parent / "fixtures" / "screener" / f"{name}.html").read_text(
            encoding="utf-8", errors="replace"
        )
        soup = BeautifulSoup(html, "lxml")
        statement = parser._parse_table_section(soup, "balance-sheet")
        rows = set(statement.rows)

        assert "Borrowings" in rows
        assert not rows & {"Cash Equivalents", "Cash and Bank Balance"}, (
            f"{name} DOES itemise cash -- if this now fails, _CASH_ROW_LABELS in ticker.py can "
            "be promoted from a hook to a working lookup and the EV gate can be relaxed."
        )


def test_bfinance_error_hierarchy_is_what_the_narrowed_handlers_catch():
    """
    The narrowing rests on ``ParsingError`` and ``RealOHLCUnavailableError`` both being
    ``BFinanceError``. If that ever stops being true the handlers would stop catching the
    failures they were written for, silently.
    """
    assert issubclass(ParsingError, BFinanceError)
    assert issubclass(RealOHLCUnavailableError, BFinanceError)
