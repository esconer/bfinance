"""The ``info`` dict's unit contract: mixed on purpose, and now discoverable without screens.py.

THE PROBLEM THIS PINS
---------------------
``QuoteEngine.build_info_dict`` publishes four return/yield keys whose units are not uniform::

    returnOnEquity           ratio  (fraction)     roe / 100
    returnOnAssets           ratio  (fraction)     Net Profit / Total Assets
    returnOnCapitalEmployed  pct    (RAW PERCENT)  roce, UNCONVERTED   <- the odd one
    dividendYield            pct    (RAW PERCENT)  already a percent

A first pass at this work asserted the opposite -- that ROCE was "the bug" and
``roe / 100.0`` at quotes.py:244 was wrong. **That claim was backwards and is disproved here:**

* ``roa`` is not a ``TopRatios`` field at all. It is computed at quotes.py as
  ``_np / _ta`` (a true fraction) in a different function, so it was never the odd sibling.
* ``roce`` and ``roe`` are BOTH raw percents in ``TopRatios``, and both model descriptions
  say so: ``'Return on Capital Employed (ROCE %)'`` and ``'Return on Equity (ROE %)'``.
  Fixture check, ``tests/fixtures/screener/`` (read only, never written here):
  ITC ``roce=38.9 roe=29.3``; TCS ``roce=63.0 roe=51.8``.
* So ``roe / 100.0`` is a correct percent-to-fraction conversion, and it is exactly what
  makes ``screens.py``'s ``ROE_% = returnOnEquity * 100`` round-trip.

THE DECISION (option (a): normalise nothing, document loudly)
------------------------------------------------------------
``returnOnCapitalEmployed`` stays a raw percent. Rationale and the rejected alternative are
recorded in full at ``bfinance.market.quotes.INFO_VALUE_UNITS``; the short form is that
normalising it is a 0.2.0 breaking change dressed as a consistency cleanup: it would turn
four screen gates (``>= 15.0``, ``>= 20.0``, ``>= 12.0``) into never-true comparisons
without any error. The requirement that "a caller can learn the unit without reading
screens.py" is met by ``INFO_VALUE_UNITS``, asserted below to cover every key.

NO TAUTOLOGY
------------
``INFO_VALUE_UNITS`` is a table *in the source*, so comparing it to itself would prove
nothing. It is therefore not what is asserted. What is asserted is behaviour, against
literals written in this file:

* each numeric key is fed a known input and its output compared to a hard-coded number
  (``roce=20.0`` -> ``20.0`` AND explicitly ``!= 0.20``), and
* the screen gates, which are the live consumer, are shown to agree.

Normalising ROCE makes the ``!= 0.20`` half fail, the coffee_can gate fail, and the
``ROCE_%`` column test fail. The table itself being wrong-but-consistent is caught by the
hard-coded expected-unit dict in ``test_declared_units_match_the_hand_documented_contract``.
"""
import importlib

import pytest

from bfinance.market.quotes import INFO_VALUE_UNITS, QuoteEngine
from bfinance.models.company import CompanyProfile, FinancialStatement, TopRatios
from bfinance.screens import screens


def info_from(ratios, **profile_kwargs):
    """Build the real info dict from a real TopRatios. No network, no fixtures written."""
    profile = CompanyProfile(
        symbol="TEST", company_id=0, name="Test Ltd", ratios=ratios, **profile_kwargs
    )
    return QuoteEngine.build_info_dict(profile)


# ======================================================= the decision: ROCE stays a percent

def test_roce_is_published_as_a_raw_percent_not_a_fraction():
    """roce=20.0 must surface as 20.0, and must NOT surface as 0.20.

    The `!= 0.20` half is the assertion that fails if anyone normalises the key.
    """
    info = info_from(TopRatios(roce=20.0))
    assert info["returnOnCapitalEmployed"] == pytest.approx(20.0)
    assert info["returnOnCapitalEmployed"] != pytest.approx(0.20)


@pytest.mark.parametrize("roce", [0.0, 1.0, 12.5, 20.0, 63.0, 100.0, 250.0])
def test_roce_passes_through_unscaled(roce):
    """Every percent in, the same number out. No /100 anywhere on this key."""
    assert info_from(TopRatios(roce=roce))["returnOnCapitalEmployed"] == pytest.approx(roce)


def test_roce_is_the_only_unconverted_percent_among_its_siblings():
    """A genuine 20% ROCE with a genuine 20% ROE must come back as 20.0 and 0.20.

    This is the mixed-unit contract stated as a single observation: same input magnitude,
    two different output magnitudes, on purpose. It also proves the oddity is ROCE's and
    not the /100.0's -- if /100.0 were the bug, the two would agree.
    """
    info = info_from(TopRatios(roce=20.0, roe=20.0))
    assert info["returnOnCapitalEmployed"] == pytest.approx(20.0)
    assert info["returnOnEquity"] == pytest.approx(0.20)
    assert info["returnOnCapitalEmployed"] == pytest.approx(info["returnOnEquity"] * 100)


def test_return_on_assets_is_a_true_fraction_computed_from_statements():
    """`roa` is not a TopRatios field; it is Net Profit / Total Assets at quotes.py:155.

    This is the refutation of the "ROA is the fraction that makes ROCE look wrong" claim.
    """
    profile = CompanyProfile(
        symbol="TEST", company_id=0, name="Test Ltd", ratios=TopRatios(),
        profit_loss=FinancialStatement(headers=["Mar 2025"], rows={"Net Profit": [10.0]}),
        balance_sheet=FinancialStatement(headers=["Mar 2025"], rows={"Total Assets": [200.0]}),
    )
    info = QuoteEngine.build_info_dict(profile)
    assert info["returnOnAssets"] == pytest.approx(0.05)
    assert not hasattr(TopRatios(), "roa")  # it is not a ratios field at all


# ================================ the live consumer: screens.py gates agree with the choice

def _fake(info):
    from types import SimpleNamespace

    return SimpleNamespace(info=info, symbol="FAKE")


#: screen -> the percent threshold its ROCE gate uses, as documented in screens.py.
ROCE_GATES = [
    ("coffee_can", 15.0),
    ("debt_free_compounders", 20.0),
    ("magic_formula", 20.0),
    ("high_dividend_yield", 12.0),
]


@pytest.mark.parametrize("screen_name,threshold", ROCE_GATES)
def test_each_roce_gate_threshold_is_a_percent_not_a_fraction(screen_name, threshold):
    """Just above the gate must PASS, just below must FAIL -- for a percent input.

    If ROCE were normalised to a fraction, 0.5% would become 0.005 and every one of these
    would return False, so this is the regression that would ship silently.
    """
    screen = getattr(screens, screen_name)
    base = {
        "returnOnCapitalEmployed": threshold + 1.0,
        "returnOnEquity": 0.30,
        "marketCapInCr": 50000.0,
        "debtToEquity": 0.1,
        "trailingPE": 15.0,
        "dividendYield": 5.0,
        "pegRatio": 1.0,
        "shortName": "Fake Ltd",
        "currentPrice": 100.0,
        "bookValue": 50.0,
        "cagrs": {
            "Compounded Sales Growth": {"10 Years": "12%"},
            "Compounded Profit Growth": {"10 Years": "10%"},
        },
    }
    assert screen.filter_fn(_fake(dict(base))) is True, f"{screen_name} rejected a passing ROCE"

    below = dict(base)
    below["returnOnCapitalEmployed"] = threshold - 1.0
    assert screen.filter_fn(_fake(below)) is False, f"{screen_name} accepted a failing ROCE"


def test_a_fraction_shaped_roce_is_rejected_by_the_gates():
    """The loud version: the fraction 0.20 (= 20%) must NOT satisfy a 15% ROCE gate.

    A 20% ROCE company is exactly the Coffee Can case. This assertion is what makes the
    percent decision visible rather than implicit.
    """
    info = {
        "returnOnCapitalEmployed": 0.20,
        "returnOnEquity": 0.30,
        "marketCapInCr": 50000.0,
        "cagrs": {
            "Compounded Sales Growth": {"10 Years": "12%"},
            "Compounded Profit Growth": {"10 Years": "10%"},
        },
    }
    assert screens.coffee_can.filter_fn(_fake(info)) is False


@pytest.mark.parametrize("screen_name,threshold", ROCE_GATES)
def test_gates_agree_with_the_value_the_builder_actually_publishes(screen_name, threshold):
    """End-to-end: TopRatios -> build_info_dict -> the real screen gate. No hand-fed dict.

    The two gate tests above pin the gates in isolation; this one pins the *interaction*,
    which is the only thing that actually matters for the mixed-unit contract. Normalising
    ROCE in quotes.py leaves every hand-fed test green and still makes a real 20% company
    fail every screen -- this is the assertion that catches that.
    """
    screen = getattr(screens, screen_name)
    ratios = TopRatios(
        roce=threshold + 1.0,        # a raw percent, e.g. 21.0 == 21%
        roe=30.0,
        dividend_yield=5.0,
        market_cap=50000.0,
        current_price=100.0,
        stock_pe=15.0,
        book_value=50.0,
        debt_to_equity=0.1,
        peg_ratio=1.0,
    )
    info = info_from(
        ratios,
        cagrs={
            "Compounded Sales Growth": {"10 Years": "12%"},
            "Compounded Profit Growth": {"10 Years": "10%"},
        },
    )
    # Sanity: the builder really did publish a percent here.
    assert info["returnOnCapitalEmployed"] == pytest.approx(threshold + 1.0)
    assert screen.filter_fn(_fake(info)) is True, (
        f"{screen_name} rejected a company whose real ROCE is {threshold + 1.0}%"
    )


# ================================= the ROCE_% / ROE_% columns as published

def _screen_df(monkeypatch, info):
    screens_mod = importlib.import_module("bfinance.screens")

    class _StubTicker:
        def __init__(self, symbol):
            self.symbol = symbol
            self.info = info

    monkeypatch.setattr(screens_mod, "Ticker", _StubTicker)
    return screens_mod.Screen(
        name="probe", description="probe", filter_fn=lambda t: True
    ).run(universe=["FAKE"], show_progress=False)


def test_roce_column_is_raw_percent_and_roe_column_is_percent(monkeypatch):
    """20% ROCE and 20% ROE must both publish as 20.0, from two different input shapes.

    End-to-end proof that the /100.0 and the missing /100.0 are the same number in the end.
    """
    info = info_from(TopRatios(roce=20.0, roe=20.0))
    df = _screen_df(monkeypatch, info)
    assert df.loc[0, "ROCE_%"] == pytest.approx(20.0)
    assert df.loc[0, "ROE_%"] == pytest.approx(20.0)


def test_dividend_yield_is_a_raw_percent_end_to_end(monkeypatch):
    """A 0.15% payer must publish 0.15, not 15.0.

    The same class of "already correct, do not touch" as ROCE: a *100 here once inflated
    every yield 100x, and a /100 now would deflate it.
    """
    df = _screen_df(monkeypatch, info_from(TopRatios(dividend_yield=0.15, roce=5.0)))
    assert df.loc[0, "DivYield_%"] == pytest.approx(0.15)
    assert df.loc[0, "DivYield_%"] != pytest.approx(15.0)


# ============================================ the unit table is discoverable and complete

#: Hand-written from the measured behaviour above and the source comments, NOT read out of
#: INFO_VALUE_UNITS. If the table and the behaviour ever disagree, one of these two fails.
EXPECTED_UNITS = {
    "returnOnEquity": "ratio",
    "returnOnAssets": "ratio",
    "returnOnCapitalEmployed": "pct",
    "dividendYield": "pct",
    "dividendYieldPercent": "pct",
    "heldPercentInsiders": "ratio",
    "promoterHolding": "pct",
    "promoterPledged": "pct",
    "marketCap": "inr",
    "marketCapInCr": "inr_cr",
    "currentPrice": "inr",
    "bookValue": "inr",
    "faceValue": "inr",
    "trailingEps": "inr",
    "trailingPE": "multiple",
    "priceToBook": "multiple",
    "debtToEquity": "multiple",
    "pegRatio": "multiple",
    "sharesOutstanding": "count",
    "volume": "count",
    "gmtOffSetMilliseconds": "ms",
    "isConsolidated": "bool",
    "pros": "text_list",
    "cagrs": "mapping",
    "currentTradingPeriod": None,  # not an info key; listed to prove extras are harmless
}


def test_declared_units_match_the_hand_documented_contract():
    """The table agrees with the units measured above. Hard-coded on both sides."""
    for key, unit in EXPECTED_UNITS.items():
        if unit is None:
            continue
        assert INFO_VALUE_UNITS.get(key) == unit, f"{key} declared as {unit!r}"


def test_every_info_key_has_a_declared_unit():
    """No key may be undocumented, or the contract is only half discoverable.

    Asserted over the dict `build_info_dict` really produces, so a newly added key without a
    unit entry fails here. `custom_ratios` is empty in this profile, so the only extra keys
    are the statically declared ones.
    """
    info = info_from(TopRatios())
    undeclared = sorted(k for k in info if k not in INFO_VALUE_UNITS)
    assert undeclared == [], f"info keys with no declared unit: {undeclared}"


def test_table_has_no_stale_keys():
    """The reverse: no declared unit for a key the dict no longer produces."""
    info = info_from(TopRatios())
    stale = sorted(k for k in INFO_VALUE_UNITS if k not in info)
    assert stale == [], f"INFO_VALUE_UNITS documents keys that no longer exist: {stale}"


def test_only_documented_unit_vocabulary_is_used():
    """Keeps the vocabulary closed, so `pct` can never drift to mean something else."""
    allowed = {
        "inr", "inr_cr", "pct", "ratio", "multiple", "count", "ms",
        "text", "bool", "text_list", "mapping", "unknown",
    }
    bad = {k: v for k, v in INFO_VALUE_UNITS.items() if v not in allowed}
    assert bad == {}, f"unknown unit labels: {bad}"


def test_the_return_and_yield_keys_are_declared_mixed_not_uniform():
    """The headline: a caller sees the mix in one place, without reading screens.py."""
    returns = {k: INFO_VALUE_UNITS[k] for k in
               ("returnOnEquity", "returnOnAssets", "returnOnCapitalEmployed")}
    assert returns == {
        "returnOnEquity": "ratio",
        "returnOnAssets": "ratio",
        "returnOnCapitalEmployed": "pct",
    }
    # The mix is real, not an artefact of a uniform table with one typo.
    assert len(set(returns.values())) > 1


def test_the_contract_is_reachable_from_the_builder_without_screens():
    """A caller holding only the info dict can name the table off the builder's own docs.

    `build_info_dict` is the thing a caller has in hand; its docstring is what points at the
    unit table. If that pointer is deleted, a caller is back to reading screens.py.
    """
    assert "INFO_VALUE_UNITS" in (QuoteEngine.build_info_dict.__doc__ or "")


def test_screens_module_defers_to_the_table_rather_than_restating_it():
    """screens.py must point at INFO_VALUE_UNITS, so there is exactly one contract."""
    screens_mod = importlib.import_module("bfinance.screens")
    module_doc = screens_mod.__doc__ or ""
    assert "INFO_VALUE_UNITS" in module_doc
    run_doc = screens_mod.Screen.run.__doc__ or ""
    assert "INFO_VALUE_UNITS" in run_doc
