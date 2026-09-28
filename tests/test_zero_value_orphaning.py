"""A real 0.00 must never be turned into None, and never into a different wrong number.

Every test here builds a ``#top-ratios`` (or peer-table) fragment **in memory** and runs the
real parser over it, so nothing depends on the network or on the shared
``tests/fixtures/screener/`` files (which another agent owns and which this file only reads).

THE DEFECT (three `or` chains in ``ScreenerHTMLParser._parse_top_ratios``)
-------------------------------------------------------------------------
``raw.get("Debt to equity") or raw.get("Debt / Eq")`` cannot tell a genuine ``0.00`` from a
missing value, so it failed in two directions. Measured before the fix, on real fragments:

    primary 0.00, alias absent   -> None    the real zero vanished
    primary 0.00, alias 7.77     -> 7.77    a *different wrong number* replaced it
    primary absent               -> None
    primary and absent           -> byte-identical to the 0.00 case, i.e. indistinguishable

The second is the serious one: ``custom_ratios`` still held the ``0.0`` while the typed
field already reported someone else's number, so a consumer reading ``debt_to_equity``
could not detect the substitution.

A genuine 0.00 is not hypothetical here. screener.in prints ``0.00`` for a debt-free
company's D/E, and ``Debt to equity`` is exactly the field
``screens.debt_free_compounders`` gates on.
"""
import importlib

import pandas as pd
import pytest
from bs4 import BeautifulSoup

from bfinance.market.quotes import QuoteEngine
from bfinance.models.company import CompanyProfile, TopRatios
from bfinance.screener.parser import ScreenerHTMLParser


PARSER = ScreenerHTMLParser()

#: (TopRatios field, canonical screener.in label, alias screener.in label)
ALIASED_CHAINS = [
    ("debt_to_equity", "Debt to equity", "Debt / Eq"),
    ("stock_pe", "Stock P/E", "P/E"),
    ("price_to_book", "Price to book value", "P/B"),
]


# --------------------------------------------------------------------------- helpers

def top_ratios(items):
    """Run the REAL _parse_top_ratios over an in-memory #top-ratios fragment.

    ``items`` is an ordered label -> raw text mapping, exactly as screener.in emits it
    (label in ``.name``, value in ``.value``). A label absent from ``items`` is a label
    the page genuinely did not carry.
    """
    lis = "".join(
        f'<li class="flex flex-space-between" data-source="default">'
        f'<span class="name">{label}</span>'
        f'<span class="nowrap value">{value}</span></li>'
        for label, value in items.items()
    )
    soup = BeautifulSoup(f'<ul id="top-ratios">{lis}</ul>', "lxml")
    return PARSER._parse_top_ratios(soup)


def info_from(ratios):
    """Build the real info dict from a real TopRatios."""
    profile = CompanyProfile(
        symbol="TEST", company_id=0, name="Test Ltd", ratios=ratios
    )
    return QuoteEngine.build_info_dict(profile)


# ------------------------------------------- the three chains, all four cases each

@pytest.mark.parametrize("field,canonical,alias", ALIASED_CHAINS)
def test_genuine_zero_survives(field, canonical, alias):
    """A genuine 0.00 with no alias key present must arrive as 0.0, not None.

    This is the exact row that was `None` before the fix: `0.0 or None` -> None.
    """
    assert getattr(top_ratios({canonical: "0.00"}), field) == 0.0


@pytest.mark.parametrize("field,canonical,alias", ALIASED_CHAINS)
def test_genuinely_absent_is_still_none(field, canonical, alias):
    """Neither the canonical label nor the alias is present -> None (unchanged)."""
    assert getattr(top_ratios({"Book Value": "10.00"}), field) is None


@pytest.mark.parametrize("field,canonical,alias", ALIASED_CHAINS)
def test_alias_fallback_still_works(field, canonical, alias):
    """The alias exists because screener.in really does use both spellings. It must survive."""
    assert getattr(top_ratios({alias: "7.77"}), field) == pytest.approx(7.77)


@pytest.mark.parametrize("field,canonical,alias", ALIASED_CHAINS)
def test_genuine_zero_is_not_overridden_by_a_present_alias(field, canonical, alias):
    """Case D, the worst one: canonical 0.00 AND alias 7.77 -> 0.00, not 7.77.

    Before the fix this returned the alias, so a correct reading was replaced by a wrong
    number rather than merely lost.
    """
    parsed = top_ratios({canonical: "0.00", alias: "7.77"})
    assert getattr(parsed, field) == 0.0
    # Belt and braces: the wrong number must not be anywhere in the typed field.
    assert getattr(parsed, field) != pytest.approx(7.77)


@pytest.mark.parametrize("field,canonical,alias", ALIASED_CHAINS)
def test_canonical_label_wins_when_both_are_non_zero(field, canonical, alias):
    """Precedence is unchanged: the canonical label beats the alias when both are real."""
    parsed = top_ratios({canonical: "3.33", alias: "9.99"})
    assert getattr(parsed, field) == pytest.approx(3.33)


# ------------------------------------------------- zero vs absent are distinguishable

@pytest.mark.parametrize("field,canonical,alias", ALIASED_CHAINS)
def test_zero_and_absent_are_three_distinguishable_outcomes(field, canonical, alias):
    """The entire point of the fix: 0.0 and None must be different observations.

    `A == C` was True before the fix (both were None), so a consumer could not tell "this
    company reports zero" from "this page never reported the field". A caller must still be
    able to make that distinction after the alias, if any, has had its chance to apply.
    """
    zero = getattr(top_ratios({canonical: "0.00"}), field)
    absent = getattr(top_ratios({"Book Value": "10.00"}), field)
    aliased = getattr(top_ratios({alias: "7.77"}), field)

    assert zero is not None, "a genuine 0.00 must not be reported as missing"
    assert zero == 0.0
    assert absent is None
    # Identity, not just equality: None is a singleton, so `is not None` is the real test a
    # consumer performs and equality alone would let a 0.0 slip through a sloppy check.
    assert zero is not absent
    assert zero != absent
    assert aliased is not None and aliased not in (zero, absent)


@pytest.mark.parametrize("field,canonical,alias", ALIASED_CHAINS)
def test_raw_custom_ratios_kept_the_zero_all_along(field, canonical, alias):
    """Documents *why* the substitution was silent: custom_ratios always had the 0.0.

    A caller who reads ``custom_ratios`` by raw label already had the right number while
    the typed field was wrong, which is why the bug was never caught by eyeballing output.
    """
    parsed = top_ratios({canonical: "0.00", alias: "7.77"})
    assert parsed.custom_ratios[canonical] == 0.0
    assert parsed.custom_ratios[alias] == pytest.approx(7.77)


# --------------------------------- already zero-safe: pin so a future edit cannot break it

@pytest.mark.parametrize(
    "label,field",
    [
        ("Dividend Yield", "dividend_yield"),
        ("ROE", "roe"),
        ("PEG Ratio", "peg_ratio"),
        ("Promoter holding", "promoter_holding"),
        ("Pledged percentage", "promoter_pledged"),
    ],
)
def test_single_label_fields_already_zero_safe(label, field):
    """These five use a bare `.get()` with no `or`, so a genuine 0.00 already survives.

    Verified during this work (measured on real fragments, all five returned 0.0) and pinned
    here so that adding a future `or` fallback to one of them turns this test red instead of
    silently re-introducing the defect.
    """
    assert getattr(top_ratios({label: "0.00"}), field) == 0.0


def test_roce_is_zero_safe_too():
    """ROCE uses a bare `.get()` as well; pinned for the same reason."""
    assert top_ratios({"ROCE": "0.00"}).roce == 0.0


@pytest.mark.parametrize(
    "label,field",
    [
        ("Market Cap", "market_cap"),
        ("Current Price", "current_price"),
        ("Book Value", "book_value"),
        ("Face Value", "face_value"),
        ("EPS", "eps_ttm"),
        ("Free Cash Flow 3Y", "free_cash_flow_3y"),
    ],
)
def test_other_bare_get_fields_zero_safe(label, field):
    """Remaining bare-`.get()` fields, for the same no-silent-regression reason."""
    assert getattr(top_ratios({label: "0.00"}), field) == 0.0


# ------------------------------------------------------------- quotes.py:244 `if r.roe`

def test_genuine_zero_roe_is_zero_not_none():
    """`if r.roe` mapped a genuine ROE of 0.00 to None. Measured before the fix.

    0.0 -> None (wrong), 18.25 -> 0.1825 (right), None -> None (right).
    """
    assert info_from(TopRatios(roe=0.0))["returnOnEquity"] == 0.0


def test_absent_roe_is_still_none():
    """A company whose page carried no ROE must not gain a fabricated 0.0."""
    assert info_from(TopRatios(roe=None))["returnOnEquity"] is None


def test_roe_percent_to_fraction_conversion_is_unchanged():
    """The /100.0 is correct: roe is a RAW PERCENT in TopRatios, info holds a fraction.

    This is what makes screens.py's `ROE_% = returnOnEquity * 100` round-trip.
    """
    assert info_from(TopRatios(roe=18.25))["returnOnEquity"] == pytest.approx(0.1825)
    assert info_from(TopRatios(roe=100.0))["returnOnEquity"] == pytest.approx(1.0)
    assert info_from(TopRatios(roe=0.5))["returnOnEquity"] == pytest.approx(0.005)


# ----------------------------------------------------------- quotes.py:239 priceToBook

def test_reported_zero_price_to_book_is_data_not_an_absence():
    """Same `or` shape, same fix. Before: reported 0.00 was dropped for a CMP/BV estimate."""
    info = info_from(TopRatios(price_to_book=0.0, book_value=50.0, current_price=100.0))
    assert info["priceToBook"] == 0.0


def test_price_to_book_still_falls_back_to_the_estimate_when_unreported():
    """`is not None` must not have cost us the CMP/book-value estimate path."""
    info = info_from(TopRatios(price_to_book=None, book_value=50.0, current_price=100.0))
    assert info["priceToBook"] == pytest.approx(2.0)


def test_price_to_book_fallback_still_requires_a_nonzero_book_value():
    """`r.book_value` keeps a truthiness test ON PURPOSE: it guards a division.

    Widening it to `is not None` turns a real book value of 0.00 into a ZeroDivisionError.
    """
    info = info_from(TopRatios(price_to_book=None, book_value=0.0, current_price=100.0))
    assert info["priceToBook"] is None
    assert info["bookValue"] == 0.0  # the zero itself is preserved


# ------------------------------------------------------------- screens.py:114 ROE_%

def _screen_df(monkeypatch, info):
    """Run the real Screen.run over one stubbed ticker, offline."""
    screens_mod = importlib.import_module("bfinance.screens")

    class _StubTicker:
        def __init__(self, symbol):
            self.symbol = symbol
            self.info = info

    monkeypatch.setattr(screens_mod, "Ticker", _StubTicker)
    return screens_mod.Screen(
        name="probe", description="probe", filter_fn=lambda t: True
    ).run(universe=["FAKE"], show_progress=False)


def test_genuine_zero_roe_reaches_the_screen_table_as_zero(monkeypatch):
    """screens.py multiplied by 100 and then re-tested truthiness, re-orphaning the zero.

    The quotes.py fix alone would have been undone here: `0.0 * 100` is 0.0, but
    `if 0.0` is False, so the published ROE_% column showed a blank for a company that
    genuinely earned nothing.
    """
    df = _screen_df(monkeypatch, {"returnOnEquity": 0.0, "returnOnCapitalEmployed": 12.0})
    assert df.loc[0, "ROE_%"] == 0.0
    assert df.loc[0, "ROE_%"] is not None


def test_absent_roe_stays_blank_in_the_screen_table(monkeypatch):
    """A genuinely unknown ROE must not be invented as 0.0 in the published table."""
    df = _screen_df(monkeypatch, {"returnOnEquity": None, "returnOnCapitalEmployed": 12.0})
    assert df.loc[0, "ROE_%"] is None
    assert bool(pd.isna(df.loc[0, "ROE_%"]))


# -------------------------------------------------------------- parser.py peers rank

def _peer_table(rows):
    """A screener.in-shaped peer table with the given S.No. cells."""
    return (
        '<table class="data-table"><tbody>'
        "<tr><th>S.No.</th><th>Company</th><th>CMPRs.</th><th>P/E</th></tr>"
        f"{''.join('<tr>' + ''.join(f'<td>{c}</td>' for c in r) + '</tr>' for r in rows)}"
        "</tbody></table>"
    )


def test_one_based_ranks_are_preserved():
    """screener.in's S.No. is 1-based, so the normal path is unchanged by the `is not None`."""
    peers = PARSER._parse_peers(
        _peer_table([["1", "Alpha", "100", "20"], ["2", "Beta", "200", "30"]])
    )
    assert [p.rank for p in peers] == [1, 2]
    assert [p.name for p in peers] == ["Alpha", "Beta"]


def test_empty_s_no_cell_falls_back_to_position():
    """The documented contract: an empty S.No. (the 'Median: N Co.' row) takes its position."""
    peers = PARSER._parse_peers(
        _peer_table([["1", "Alpha", "100", "20"], ["-", "Median: 2 Co.", "150", "25"]])
    )
    assert [p.rank for p in peers] == [1, 2]
    assert peers[1].name == "Median: 2 Co."


def test_a_zero_s_no_is_reported_as_zero_not_silently_renumbered():
    """Policy test, deliberately about a value screener.in does not emit (S.No. is 1-based).

    It is here to pin *which* substitution the parser is allowed to make. `_parse_peers`
    documents itself as leaving a missing number as None and substituting a position only
    for a missing cell; a truthiness test substitutes for `0` as well, which is the same
    silent-renumbering defect the ratio chains had. So the rule asserted is: report what the
    page said, substitute only for genuinely absent cells.
    """
    peers = PARSER._parse_peers(
        _peer_table([["0", "ZeroRow", "100", "20"], ["1", "Alpha", "200", "30"]])
    )
    assert [p.rank for p in peers] == [0, 1]
    assert peers[0].name == "ZeroRow"
