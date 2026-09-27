"""
NSE corporate-actions feed tests — fully offline, driven by fixtures recorded 2026-09-27.

Fixtures under ``tests/fixtures/corporate_actions/`` are verbatim NSE payloads from
``https://www.nseindia.com/api/corporates-corporateActions?index=equities``. Nothing here touches
the network: the client is exercised through ``httpx.MockTransport`` and the parsers are pure.

The fixtures exist because the feed's *text* is the contract. ``subject`` is free prose written by
~2,800 listed companies and their filing agents, and it carries at least nine distinct spellings
of the same three ideas (a rupee dividend, a bonus ratio, a face-value sub-division), plus
non-equity bonuses, REIT/SML "per unit" distributions, and two rows whose ratio is arithmetic
noise. A parser that handles only the tidy spelling silently produces a confidently wrong
``Adj Close``, so every observed shape has a case here.
"""

import ast
import json
from pathlib import Path

import httpx
import pandas as pd
import pytest

from bfinance.nse import corporate_actions as ca
from bfinance.utils.exceptions import (
    NotAnNseSymbolError,
    ParsingError,
    UpstreamServiceError,
)

FIXTURES = Path(__file__).parent / "fixtures" / "corporate_actions"


def load(name: str):
    with open(FIXTURES / name, encoding="utf-8") as handle:
        return json.load(handle)


@pytest.fixture
def reliance_events():
    return ca.parse_corporate_actions(
        load("nse_reliance_2020_2026.json"), symbol="RELIANCE", url="fixture"
    )


@pytest.fixture
def curated_events():
    return ca.parse_corporate_actions(
        load("nse_marketwide_2015_2026_curated.json"), url="fixture"
    )


# --------------------------------------------------------------------- endpoint shape


def test_route_is_camel_corporate_actions():
    """The kebab-case spellings return 404 (route gone); only camelCase resolves.

    This is the single fact that made the feed findable, and the easiest thing to regress by
    "tidying" the constant. ``/api/corporates-corporate-actions?index=equities`` -> 404 with the
    same 272-byte body as a deliberately nonsense route, while ``/api/marketStatus`` on the same
    cookie-primed session -> 200. So the 404 means gone, not blocked.
    """
    assert ca.API_PATH == "/api/corporates-corporateActions"
    assert "corporateActions" in ca.corporate_actions_url("RELIANCE")
    assert "corporate-actions" not in ca.corporate_actions_url("RELIANCE")


def test_url_always_carries_a_date_range():
    """Omitting the range caps a per-symbol query at 20 rows (measured on ITC).

    That truncation is silent and produces a confidently wrong Adj Close, so the client always
    sends a range. The builder leaves that decision visible to a caller who wants to see it.
    """
    url = ca.corporate_actions_url("ITC", "1990-01-01", "2026-09-27")
    assert "from_date=01-01-1990" in url
    assert "to_date=27-09-2026" in url
    assert "index=equities" in url
    assert "symbol=ITC" in url
    # A .NS suffix is stripped; the endpoint does not understand it.
    assert "symbol=RELIANCE" in ca.corporate_actions_url("RELIANCE.NS")


def test_url_can_ask_for_the_whole_market():
    """symbol omitted -> the market-wide form, which returned 43,789 rows in one request."""
    assert "symbol=" not in ca.corporate_actions_url(None, "2020-01-01", "2026-09-27")


@pytest.mark.parametrize("given,expected", [
    ("equities", "equities"), ("EQUITIES", "equities"), (" sme ", "sme"), ("mf", "mf"),
])
def test_validate_index(given, expected):
    assert ca.validate_index(given) == expected


@pytest.mark.parametrize("given", ["debt", "nonsense", "", None, 7])
def test_debt_and_junk_indices_are_rejected_locally(given):
    """`index=debt` answers {"data": [], "msg": "no data found"} — a dict, not the row array.

    Accepting it would push a shape mismatch into the parser instead of failing as an argument
    error, so it is rejected before a request is issued.
    """
    with pytest.raises(ValueError):
        ca.validate_index(given)


def test_bse_symbol_is_rejected_before_any_request():
    with pytest.raises(NotAnNseSymbolError):
        ca.corporate_actions_url("RELIANCE.BO")


# --------------------------------------------------------------------- response shapes


def test_parse_schemas_a_list_payload(reliance_events):
    """The array-of-flat-objects schema, every documented field present."""
    assert list(reliance_events.columns) == list(ca.COLUMNS)
    assert str(reliance_events.index.name) == "Date"
    row = reliance_events.loc[pd.Timestamp("2024-08-19")]
    assert row["Symbol"] == "RELIANCE"
    assert row["Action"] == ca.DIVIDEND
    assert row["Value"] == pytest.approx(10.0)
    assert row["Basis"] == "per_share"
    assert row["RecordDate"] == pd.Timestamp("2024-08-19")
    assert row["FaceValue"] == pytest.approx(10.0)
    assert row["Series"] == "EQ"
    assert row["ISIN"] == "INE002A01018"


def test_parse_records_provenance(reliance_events):
    assert reliance_events.attrs["bfinance_provenance"] == ca.PROVENANCE
    assert reliance_events.attrs["bfinance_symbol"] == "RELIANCE"
    assert reliance_events.attrs["bfinance_source_row_count"] == 10
    assert reliance_events.attrs["bfinance_undated_action_rows"] == 0


def test_empty_array_payload_is_an_empty_feed_not_an_error():
    """An unresolvable symbol answers `[]` with HTTP 200 — the honest 'nothing on record'."""
    payload = load("nse_empty_feed.json")
    assert payload == []
    frame = ca.parse_corporate_actions(payload, symbol="ZZZ", url="fixture")
    assert frame.empty
    assert frame.attrs["bfinance_source_row_count"] == 0
    actions = ca.actions_frame(frame, symbol="ZZZ", url="fixture")
    assert actions.empty
    assert list(actions.columns) == list(ca.ACTION_COLUMNS)


def test_dict_shaped_payload_is_handled():
    """The `index=debt` shape: a dict with an empty `data`. Parsed as an empty feed, not a crash."""
    frame = ca.parse_corporate_actions({"data": [], "msg": "no data found"}, url="fixture")
    assert frame.empty
    assert list(frame.columns) == list(ca.COLUMNS)


def test_unexpected_payload_type_raises_rather_than_guessing():
    with pytest.raises(ParsingError):
        ca.parse_corporate_actions("a string, not json", url="fixture")
    with pytest.raises(ParsingError):
        ca.parse_corporate_actions(42, url="fixture")


def test_empty_actions_frame_keeps_the_shape_and_says_so():
    frame = ca.empty_actions_frame("ZZZ", url="fixture")
    assert frame.empty
    assert list(frame.columns) == ["Dividends", "Stock Splits"]
    assert str(frame.index.tz) == "Asia/Kolkata"
    assert frame.attrs["bfinance_empty_feed"] is True
    assert frame.attrs["bfinance_provenance"] == ca.PROVENANCE


# --------------------------------------------------------------------- dividend grammar


def test_dividend_amounts_are_the_unadjusted_rupees(reliance_events):
    """Rs 10 on 2024-08-19 — not yfinance's 5.0.

    yfinance divides every past dividend by all *subsequent* splits; NSE does not. 10 / 2.0 is
    exactly yfinance's 5.0, the 2.0 being the 1:1 bonus of 2024-10-28. The unadjusted figure is
    what `1 - D / prev_close` needs, since that factor is defined in pre-action rupees.
    """
    by_date = reliance_events.set_index(reliance_events.index)
    assert by_date.loc[pd.Timestamp("2024-08-19"), "Value"] == pytest.approx(10.0)
    assert by_date.loc[pd.Timestamp("2025-08-14"), "Value"] == pytest.approx(5.5)
    assert by_date.loc[pd.Timestamp("2026-06-05"), "Value"] == pytest.approx(6.0)


@pytest.mark.parametrize("subject,amount", [
    ("Dividend - Rs 6 Per Share", 6.0),
    ("Interim Dividend - Rs 10 Per Share", 10.0),
    ("Special Dividend - Rs 5 Per Share", 5.0),
    ("Annual General Meeting/Final Dividend - Rs 6/- Per Share", 6.0),
    # A real upstream value carries a double space. A single-space regex drops these silently.
    ("Annual General Meeting/Dividend - Rs  15 Per Share", 15.0),
    ("Dividend - Rs 6.50  Per Share", 6.5),
    ("Dividend - Re 1.50 Per Share", 1.5),
    ("Dividend - Rs 1,250.50 Per Share", 1250.5),
    ("Interim Dividend - Rs 4.50 Per Share (Purpose Revised)", 4.5),
    # Four legacy spellings live on RELIANCE's own record. They were all being dropped, i.e. a real
    # dividend silently becoming a zero in `Dividends` and a zero in the back-adjustment.
    ("Int Div-Rs.11/- Per Sh   Purpose Revised", 11.0),
    ("Int Div-Rs.13/- Per Share", 13.0),
    ("Dividend Rs. 9.50 Per Equity Share", 9.5),
    ("Annual General Meeting/ Dividend Rs. 9 Per Equity Share", 9.0),
    # Three more found by auditing the full 43,789-row pull, which took the converted count from
    # 25,177 to 25,573: a typo for "Per", a run-together "Meetingdividend", and stray punctuation.
    ("Dividend-Rs.5 Pr Share", 5.0),
    ("Annual General Meetingdividend - Rs 9 Per Share", 9.0),
    ("Dividend Rs- 5 Per Share", 5.0),
    ("Annual General Meeting / Dividend Of Rs.5/- Per Equity Share", 5.0),
    ("Annual General Meeting/Dividend Rs:- 5/- Per Share", 5.0),
    ("Agm/Div.Rs.5/- Per Share", 5.0),
    ("Dividend-Rs.5/ Per Share", 5.0),
])
def test_dividend_spellings(subject, amount):
    parsed, basis = ca.parse_dividend_amount(subject, 10.0)
    assert parsed == pytest.approx(amount)
    assert basis == "per_share"


@pytest.mark.parametrize("subject", [
    # A nil dividend is a real declaration of *zero*. There is no factor, which is the correct
    # reading — but it must not be mistaken for an unparsed row, and it must never be given a
    # number. ~110 such rows in the 43,789-row pull.
    "Dividend - Nil", "Agm/Dividend-Nil", "Div - Nil", "Dividend", "Div", "Agm/Dividend",
    "Interim Dividend", "Agm / Dividend", "Agm/Div", "Div - Nil (Purpose Revised)",
    "Annual General Meeting/Dividend - Rs Per Share",   # amount missing from the filing
    "Annual General Meeting/Dividend - 5 Per Share",    # no currency token at all
])
def test_nothing_is_inferred_where_the_filing_declares_nothing(subject):
    parsed, basis = ca.parse_dividend_amount(subject, 10.0)
    assert parsed is None, f"{subject!r} must not yield a guessed amount"
    assert basis == ""


def test_dividend_and_special_dividend_on_one_ex_date_sum():
    """A holder receives both, and the frame has one slot per date, so they add."""
    subject = "Dividend - Rs 22 Per Share/Special Dividend - Rs 5 Per Share"
    parsed, _ = ca.parse_dividend_amount(subject, 10.0)
    assert parsed == pytest.approx(27.0)


def test_two_components_sharing_one_trailing_per_share_are_both_kept():
    """ITC, ex-date 2011-06-10. A holder received Rs 4.45/share and the parse returned nothing.

    ``Dividend Final Rs 2.80 And Special Rs 1.65 Per Share`` declares two components and puts
    **one** ``Per Share`` at the end, so a pattern that needs "div" next to the amount *and*
    "Per Share" next to the amount is unsatisfiable. The row became a zero dividend and a
    silently wrong back-adjustment. Both components are kept and summed.
    """
    subject = "Dividend Final Rs 2.80 And Special Rs 1.65 Per Share"
    assert ca.dividend_amounts(subject) == ["2.80", "1.65"]
    parsed, basis = ca.parse_dividend_amount(subject, 1.0)
    assert parsed == pytest.approx(4.45)
    assert basis == "per_share"
    # The same filing with the qualifier word omitted parses identically — the amount never needed
    # the noun beside it, only the declaration of a dividend somewhere in the subject.
    assert ca.parse_dividend_amount(
        "Dividend Rs 2.80 And Special Rs 1.65 Per Share", 1.0
    )[0] == pytest.approx(4.45)


@pytest.mark.parametrize("subject,components,total", [
    # 19 real rows in the pull where a second component was dropped behind a single trailing
    # "Per Share". Every one of them now sums. These are the shapes, verbatim.
    ("Final Dividend Rs. 4 And Special Dividend Rs 8 Per Share", ["4", "8"], 12.0),
    ("Annual General Meeting/Dividend Final Rs.13 +Special Dividend Rs.20 Per Share", ["13", "20"], 33.0),
    ("Annual General Meeting/Dividend - Final Rs 25 + Special Rs 25 Per Share", ["25", "25"], 50.0),
    ("Annual General Meeting/Dividend Final Rs 16 + Special Rs 5 Per Share", ["16", "5"], 21.0),
    ("Agm/Dividend - Final Re 1 + Special Re 0.20 Per Share", ["1", "0.20"], 1.20),
    ("Annual General Meeting And Dividend - Final - Rs.23/- And Special - Rs.28/- Per Share",
     ["23", "28"], 51.0),
    ("Interim Dividend Rs 5.75/- + Special Dividend - Rs 1.25/- Per Share (Purpose Revised)",
     ["5.75", "1.25"], 7.0),
    ("Annual General Meeting / Final Dividend Rs 0.60  And Special Dividend Rs 1.40 Per Share.",
     ["0.60", "1.40"], 2.0),
])
def test_a_component_behind_a_shared_per_share_is_not_dropped(subject, components, total):
    assert ca.dividend_amounts(subject) == components
    assert ca.parse_dividend_amount(subject, 10.0)[0] == pytest.approx(total)


def test_per_unit_and_nil_declarations_stay_out():
    """The discriminator that must survive the loosening.

    ``Total Dividend ... Per Unit`` is a REIT/SML distribution and ``Dividend - Nil`` is a real
    declaration of zero. Neither is a per-share equity cash flow, so both must stay empty. Neither
    is protected by the dividend noun alone — both say "Dividend" — only by the per-share
    requirement plus the per-unit refusal.
    """
    for subject in ("Total Dividend Rs 5.00 Per Unit", "Dividend - Nil"):
        assert ca.dividend_amounts(subject) == [], subject
        assert ca.parse_dividend_amount(subject, 10.0) == (None, ""), subject
        assert ca.parse_action_subject(subject, 10.0) == (ca.OTHER_ACTION, None, ""), subject


@pytest.mark.parametrize("subject,expected", [
    # A real dividend noun, but the rupee amount is a face value, not cash. Reading Rs 10 as a
    # dividend would overstate it 10x. This is the documented RSSOFTWARE row: three amounts, one
    # cash.
    ("Interim Dividend - Rs 1.50/- Per Share / Face Value Split - From Rs 10/- Per Share "
     "To Rs 5/- Per Share", ["1.50"]),
    ("Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 2/- Per Share", []),
    # A dividend and a rights issue on one ex-date. The Rs 554 is the subscription price.
    ("Interim Dividend- Rs 3 Per Share / Rights 1:16 @ Premium Rs 554 Per Share", ["3"]),
    ("Dividend Rs.1.80 Per Share And Rights 1:3 @ Premium Rs.30/- Per Share", ["1.80"]),
    ("Rights 7:5@Premium Rs.55 Per Share/Annual General Meeting / Dividend Re.1/- Per Equity Share",
     ["1"]),
    # A bonus ratio is not cash. "Sub-Division" contains "Div" but declares no dividend.
    ("Bonus 1:1/Div-Rs.5 Per Sh", ["5"]),
    ("Bonus 1:1 And Face Value Split Rs.10/- To Rs.5/- Per Share", []),
])
def test_a_price_per_share_that_is_not_cash_is_never_read_as_a_dividend(subject, expected):
    assert ca.dividend_amounts(subject) == expected


@pytest.mark.parametrize("subject", [
    # A number that cannot be read cleanly, so no number is taken from it. `Rs 62..50` and
    # `Rs 0 .70` both yield a plausible-looking wrong value; `Re.0.60 Paise` has a unit word that
    # contradicts the numeral; `Div Fin-Rs18+Spl-18 Pr Sh` fuses a dividend and a split ratio under
    # one "Pr Sh", so the phrase qualifying it is ambiguous. All four are real upstream values.
    "Annual General Meeting/Dividend - Rs 62..50/- Per Share",
    "Annual General Meeting/Dividend - Re 0..30 Per Share",
    "Dividend - Rs 0 .70 Per Share",
    "Dividend - Re.0.60 Paise Per Share",
    "Div Fin-Rs18+Spl-18 Pr Sh",
])
def test_an_amount_that_cannot_be_read_cleanly_is_left_unconverted(subject):
    assert ca.dividend_amounts(subject) == [], subject
    assert ca.parse_dividend_amount(subject, 10.0) == (None, ""), subject


def test_legacy_percentage_dividend_uses_the_published_face_value():
    """`Dividend-10%` on a Rs 10 face value is Rs 1.00/share.

    Arithmetic on two published fields, not a guess about a missing fact, so it is used — and
    labelled `pct_of_face_value` so a consumer can exclude it.
    """
    parsed, basis = ca.parse_dividend_amount("Agm/Dividend-10%", 10.0)
    assert parsed == pytest.approx(1.0)
    assert basis == "pct_of_face_value"
    # With no face value there is nothing to compute from, so nothing is invented.
    assert ca.parse_dividend_amount("Agm/Dividend-10%", None) == (None, "")


def test_per_unit_distributions_are_not_equity_dividends():
    """359 rows in the 2015-2026 pull are REIT/SML distributions 'per unit'."""
    action, value, basis = ca.parse_action_subject(
        "Distribution - Rs 5.50 Per Unit Consists Of Interest- Rs 1 Per Unit", 10.0
    )
    assert (action, value, basis) == (ca.OTHER_ACTION, None, "")
    assert ca.split_ratio("Distribution - Rs 5.50 Per Unit")[0] is None


def test_subject_with_no_dividend_declares_none():
    for subject in ("Annual General Meeting", "Agm", "E-Voting", "-", "", None):
        action, value, basis = ca.parse_action_subject(subject, 10.0)
        assert (action, value, basis) == (ca.OTHER_ACTION, None, "")


# --------------------------------------------------------------------- split grammar


@pytest.mark.parametrize("subject,ratio", [
    ("Bonus 1:1", 2.0), ("Bonus 3:1", 4.0), ("Bonus 1:2", 1.5), ("Bonus 4:1", 5.0),
    # A dash after "Bonus" is an observed spelling; a word is not.
    ("Bonus- 1:2", 1.5), ("Annual General Meeting / Dividend - Rs 3/- Per Share / Bonus - 1:2", 1.5),
    ("Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 2/- Per Share", 5.0),
    ("Face Value Split (Sub-Division) - From Rs 10/- Per Share To Re 1/- Per Share", 10.0),
    ("Face Value Split (Sub-Division) - From Rs 2/- Per Share To Rs 1/- Per Share", 2.0),
    ("Face Value Split (Sub-Division) - From Rs 5/- Per Share To Re 1/- Per Share", 5.0),
    # Older and typo'd spellings, all present in the 43,789-row pull.
    ("Fv Split Rs.10/- To Rs.5/", 2.0),
    ("Fv Splt Frm Rs 10 To Rs 2", 5.0),
    ("Face Valus Split (Sub-Division) - From Rs 10/- Per To Rs 2/- Per Share", 5.0),
    ("Face Value Split From Rs.10/- To Rs.5/-", 2.0),
])
def test_split_ratios(subject, ratio):
    parsed, _ = ca.split_ratio(subject)
    assert parsed == pytest.approx(ratio)


@pytest.mark.parametrize("subject", [
    # Preference shares and debentures: the equity share count is untouched, so there is no price
    # factor. Reading `Bonus Ncrps 1:10` as a 1.1x equity split would be a fabrication.
    "Scheme Of Arrangement - Bonus Ncrps 1:10",
    "Bonus Ncrps 1:116",
    "Scheme Of Arrangement - Bonus Debentures 1:1",
    "Scheme Of Arangement- Bonus - 1 Debenture For 1 Equity Share Held",
    # A denominator this large is not a share bonus. Parsed, it yields 1.0008 — a "split" that
    # looks real and is noise, and it would rescale every prior bar by a factor indistinguishable
    # from 1. Rejecting leaves the event visible as an unconverted row.
    "Bonus 1 : 1250",
    " Bonus 25:202",
    # A missing face value cannot be divided.
    "Face Value Split (Sub-Division) - From Rs  Per Share To Rs 2/- Per Share",
    # No split declared at all.
    "Dividend - Rs 6 Per Share", "Buy Back", "", None,
])
def test_non_splits_produce_no_ratio(subject):
    assert ca.split_ratio(subject)[0] is None


def test_bonus_denominator_bound_is_guarded_by_a_constant():
    """Every real bonus denominator observed is <= 26. The bound is a documented cliff, not a guess."""
    assert ca.MAX_BONUS_DENOMINATOR == 100
    assert ca.parse_bonus_ratio("Bonus 1:25") == pytest.approx(1.04)   # real, KTIL 2015
    assert ca.parse_bonus_ratio("Bonus 1:126") is None                 # beyond the bound


def test_composite_bonus_and_subdivision_multiply():
    """Bonus 1:1 alongside a Rs 10 -> Rs 2 sub-division is 2 x 5 = 10.

    This is the one inference in the module: a holder ends with ten shares where there was one, so
    the combined factor multiplies. The `Components` string records that it was composite.
    """
    subject = "Bonus 1:1 / Face Value Split From Rs 10/- Per Share To Rs 2/- Per Share"
    ratio, parts = ca.split_ratio(subject)
    assert ratio == pytest.approx(10.0)
    assert "bonus=2.0" in parts and "subdivision=5.0" in parts
    action, value, _ = ca.parse_action_subject(subject, 2.0)
    assert action == ca.BONUS and value is None


def test_dividend_and_split_on_one_subject_yield_both():
    """RSSOFTWARE 21-Jan-2015. yfinance records this dividend as 0.375, i.e. 1.50/4 — inconsistent
    with its own 2.0 split. NSE's Rs 1.50 pre-split (0.75 post-split) is the self-consistent
    figure, and the bhavcopy agrees: 600.6 -> 290.65 on the day, of which 2.0 is the split."""
    subject = (
        "Interim Dividend - Rs 1.50/- Per Share / Face Value Split - From Rs 10/- Per Share "
        "To Rs 5/- Per Share (Purpose Revised)"
    )
    action, value, basis = ca.parse_action_subject(subject, 5.0)
    assert action == ca.DIVIDEND
    assert value == pytest.approx(1.5)
    assert basis == "per_share"
    assert ca.split_ratio(subject)[0] == pytest.approx(2.0)


# --------------------------------------------------------------------- malformed input


def test_malformed_rows_are_kept_and_counted_not_dropped():
    """An event with a missing or unparseable ex-date has no place in a time series.

    It is kept with a NaT index, counted in attrs, and excluded from the actions frame by
    construction — invisible to the arithmetic, visible to the reader.
    """
    frame = ca.parse_corporate_actions(load("nse_malformed_rows.json"), symbol="AAA", url="fixture")
    assert frame.attrs["bfinance_undated_action_rows"] == 2
    assert frame.attrs["bfinance_undated_action_subjects"] == ["Bonus 1:1", "Bonus 1:1"]
    # A dividend with no amount, and a sub-division with no source face value: parsed as
    # unconverted rather than defaulted to a number.
    assert (frame["Action"] == ca.OTHER_ACTION).sum() == 2
    actions = ca.actions_frame(frame, symbol="AAA", url="fixture")
    # The two *dated* unconvertible events keep their dates so they stay visible, carrying 0.0
    # because the yfinance shape has no column for them. The two NaT rows are excluded, which is
    # what keeps an undated event from being silently applied to whichever day pandas picked.
    assert len(actions) == 2
    assert bool((actions[["Dividends", "Stock Splits"]] == 0.0).all().all())
    assert pd.Timestamp("2024-01-01", tz="Asia/Kolkata") in actions.index


def test_absent_date_sentinel_is_a_dash_not_null():
    assert ca.ABSENT == "-"
    assert ca._ex_date("-") is None
    assert ca._ex_date(None) is None
    assert ca._ex_date("  ") is None
    assert ca._ex_date("05-Jun-2026") == pd.Timestamp("2026-06-05")


# --------------------------------------------------------------------- actions frame


def test_actions_frame_shape(reliance_events):
    actions = ca.actions_frame(reliance_events, symbol="RELIANCE", url="fixture")
    assert list(actions.columns) == ["Dividends", "Stock Splits"]
    assert str(actions.index.tz) == "Asia/Kolkata"
    assert actions.index.is_monotonic_increasing
    assert actions["Dividends"].dtype == float
    assert actions["Stock Splits"].dtype == float
    assert actions.attrs["bfinance_provenance"] == ca.PROVENANCE
    assert actions.attrs["bfinance_dividend_basis"] == "unadjusted_rupees_at_ex_date"


def test_actions_frame_maps_a_dividend_and_a_split_to_the_right_columns(reliance_events):
    actions = ca.actions_frame(reliance_events, symbol="RELIANCE", url="fixture")
    assert actions.loc[pd.Timestamp("2024-08-19", tz="Asia/Kolkata"), "Dividends"] == pytest.approx(10.0)
    assert actions.loc[pd.Timestamp("2024-08-19", tz="Asia/Kolkata"), "Stock Splits"] == 0.0
    assert actions.loc[pd.Timestamp("2024-10-28", tz="Asia/Kolkata"), "Stock Splits"] == pytest.approx(2.0)
    assert actions.loc[pd.Timestamp("2024-10-28", tz="Asia/Kolkata"), "Dividends"] == 0.0


def test_events_that_carry_no_factor_still_get_a_row(reliance_events):
    """A rights issue and a demerger are real corporate actions.

    The yfinance-shaped frame has no column for them, so they land as 0.0 — but the date is present
    and the count of unconverted events is carried in attrs, so they are not invisible.
    """
    assert reliance_events.attrs["bfinance_unconverted_action_rows"] == 2
    assert reliance_events.attrs["bfinance_action_counts"]["other"] == 2
    actions = ca.actions_frame(reliance_events, symbol="RELIANCE", url="fixture")
    assert pd.Timestamp("2020-05-13", tz="Asia/Kolkata") in actions.index
    assert actions.attrs["bfinance_unconverted_action_rows"] == 2


def test_two_split_rows_on_one_date_multiply():
    frame = pd.DataFrame(
        [
            {"Action": ca.BONUS, "Value": None, "SplitRatio": 2.0},
            {"Action": ca.SUBDIVISION, "Value": None, "SplitRatio": 5.0},
        ],
        index=pd.DatetimeIndex(pd.to_datetime(["2020-01-01", "2020-01-01"]), name="Date"),
    )
    actions = ca.actions_frame(frame)
    assert actions["Stock Splits"].iloc[0] == pytest.approx(10.0)


# --------------------------------------------------------------------- structural events


def test_structural_event_kinds_are_named_not_guessed():
    """A demerger and an AGM notice are both Action="other"; only one of them is an event.

    The counts are measured over the 43,788-row pull, and they are not equal: ``scheme`` is 142
    rows but only 26 distinct subjects, while ``composit`` is 5 rows and 3 distinct subjects. All
    five proposed keywords do occur.
    """
    assert ca.STRUCTURAL_ACTION_KINDS == ("demerger", "scheme", "amalgamation", "composite")
    for subject, expected in [
        ("Demerger", ("demerger",)),
        ("De-Merger", ("demerger",)),                       # the hyphenated spelling is real
        ("Capital Red./Demerger", ("demerger",)),
        ("Scheme Of Arrangement", ("scheme",)),
        ("Scheme Of Arrangement Cum Demerger", ("demerger", "scheme")),   # both, in that order
        ("Scheme Of Amalgamation", ("scheme", "amalgamation")),
        ("Amalgamation With Tewl . To Be Suspended.", ("amalgamation",)),
        ("Composite Scheme Of Arrangement", ("scheme", "composite")),
        ("Annual General Meeting", ()),
        ("Agm", ()),
        ("Dividend - Rs 6 Per Share", ()),
        ("Buy Back", ()),
        ("-", ()),
        (None, ()),
        ("", ()),
    ]:
        assert ca.structural_action_kinds(subject) == expected, subject


def test_composite_as_a_company_name_is_not_a_structural_event():
    """``composite`` is conditional, and the condition is measured, not stylistic.

    One of only three distinct subjects containing "composit" in the pull is a *company* —
    ``Allotment Of Shares Of Gujarat Composite Ltd & Digvijay Finlease Ltd ...`` — so matching
    "composit" unconditionally reports an allotment as a structural event. The other two both
    qualify a scheme, which is the discriminator.
    """
    assert ca.structural_action_kinds(
        "Allotment Of Shares Of Gujarat Composite Ltd & Digvijay Finlease Ltd "
        "To The Shareholders Of The Company"
    ) == ()


def test_a_demerger_is_named_dated_and_left_unconverted(reliance_events):
    """The event is reported; the factor is not invented.

    Naming the event is free. Deriving a split factor is not: it needs a curated entitlement ratio
    that this feed does not publish (the subject is the bare string ``Demerger``), and 122 of the
    123 such rows have no such ratio available at all. So ``Action`` stays ``other`` and
    ``SplitRatio`` stays ``None`` — visible, dated, and not guessed at.
    """
    row = reliance_events.loc[pd.Timestamp("2023-07-20")]
    assert row["Symbol"] == "RELIANCE"
    assert row["Action"] == ca.OTHER_ACTION
    assert pd.isna(row["Value"])
    assert pd.isna(row["SplitRatio"])
    # And it still reaches the yfinance-shaped frame as a dated zero, so it is not invisible.
    actions = ca.actions_frame(reliance_events, symbol="RELIANCE", url="fixture")
    day = pd.Timestamp("2023-07-20", tz="Asia/Kolkata")
    assert day in actions.index
    assert actions.loc[day, "Dividends"] == 0.0
    assert actions.loc[day, "Stock Splits"] == 0.0


def test_structural_actions_accessor_names_every_kind(curated_events):
    """``structural_actions`` is a reader over a parsed frame, not a second parser.

    Returns ``Symbol``, ``Kind``, ``Subject`` on a naive ``Date`` index — mirroring
    ``parse_corporate_actions``, because the feed's ex-dates are IST calendar days and it is
    ``actions_frame`` that adds the zone. The curated market-wide fixture holds 8 such rows.
    """
    structural = ca.structural_actions(curated_events)
    assert list(structural.columns) == ["Symbol", "Kind", "Subject"]
    assert str(structural.index.name) == "Date"
    assert structural.index.is_monotonic_increasing
    assert len(structural) == curated_events.attrs["bfinance_structural_action_rows"] == 8
    assert set(structural["Kind"].str.split("+").explode()) <= set(ca.STRUCTURAL_ACTION_KINDS)

    demergers = structural[structural["Kind"] == "demerger"]
    assert demergers["Symbol"].tolist() == ["ASAHISONG", "SFCL", "RELIANCE"]
    # A subject can declare more than one kind, and the composite qualification composes.
    composite = structural[structural["Kind"].str.contains("composite")]
    assert composite["Kind"].tolist() == ["scheme+amalgamation+composite"]
    # A scheme row is still unconverted: a scheme does not itself change the share count.
    scheme_row = curated_events.loc[pd.Timestamp("2015-03-20")]
    assert scheme_row["Action"] == ca.OTHER_ACTION
    assert pd.isna(scheme_row["SplitRatio"])


def test_structural_actions_accessor_is_empty_in_empty_out(curated_events):
    assert ca.structural_actions(pd.DataFrame()).empty
    assert list(ca.structural_actions(pd.DataFrame()).columns) == ["Symbol", "Kind", "Subject"]
    assert ca.structural_actions(curated_events[curated_events["Symbol"] == "ZZZ"]).empty
    assert ca.structural_actions(None).empty


def test_frames_record_how_many_structural_events_they_refused(curated_events, reliance_events):
    assert curated_events.attrs["bfinance_structural_action_rows"] == 8
    assert reliance_events.attrs["bfinance_structural_action_rows"] == 1
    empty = ca.parse_corporate_actions(load("nse_empty_feed.json"))
    assert empty.attrs["bfinance_structural_action_rows"] == 0


# --------------------------------------------------------------------- no price blending


def test_module_ships_no_price_source_at_all():
    """The feed carries corporate actions only, so it cannot be used to blend a price.

    Checked on the parsed tree rather than by grepping the text: the docstring *must* discuss the
    bhavcopy prev-close column, so a substring scan would flag the explanation of why that column
    is useless. What must not exist is an import of a price module, or a request to a price route.
    """
    tree = ast.parse(Path(ca.__file__).read_text(encoding="utf-8"))

    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    assert not any("bhavcopy" in name or "marketlens" == name.split(".")[-1] and "price" in name
                   for name in imported), imported
    # marketlens is imported for its *symbol validator* only; that is a pure function.
    assert "bfinance.nse.marketlens" in imported

    routes = {
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    for price_route in ("/content/cm/", "price-history", "historical/cm/equity"):
        assert not any(price_route in text for text in routes), price_route

    # And the output contract carries no price columns at all.
    actions = ca.actions_frame(ca.parse_corporate_actions(load("nse_empty_feed.json")))
    assert list(actions.columns) == ["Dividends", "Stock Splits"]


def test_events_frame_has_no_price_columns(curated_events):
    price_like = {c for c in curated_events.columns if c.lower() in {"open", "high", "low", "close", "adj close", "volume", "price"}}
    assert not price_like, f"the actions feed must not carry prices: {price_like}"


def test_dividend_is_never_a_function_of_price():
    """The rupee amount is whatever NSE filed. No adjustment is applied to it here.

    yfinance's normalised figure (RELIANCE 5.0 where NSE says 10.0) would require dividing by every
    later split — an adjustment, applied in the wrong direction, on top of a factor that is already
    defined in pre-action rupees.
    """
    events = ca.parse_corporate_actions(load("nse_reliance_2020_2026.json"), symbol="RELIANCE")
    amounts = events.loc[events["Action"] == ca.DIVIDEND, "Value"].tolist()
    assert amounts == [6.5, 7.0, 8.0, 9.0, 10.0, 5.5, 6.0]


# --------------------------------------------------------------------- consistency check


def test_unexplained_price_jumps_flags_an_event_the_feed_missed():
    """The local replacement for the bhavcopy PrvsClsgPric column, which was measured useless.

    PrvsClsgPric(2024-08-19) == ClsPric(2024-08-16) == 2956.4 on RELIANCE's Rs 10 ex-date, and
    PrvsClsgPric(2025-01-10) == ClsPric(2025-01-09) == 2809.85 on SHRIRAMFIN's 5-for-1
    sub-division, and a control session gives ClsPric(t-1)/PREVCLOSE(t) == 1.0000. NSE publishes
    the previous close unadjusted for any action, so the column carries no information about
    whether one occurred and cannot verify a factor. This check can.
    """
    idx = pd.DatetimeIndex(pd.to_datetime(
        ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"]
    ))
    close = pd.Series([100.0, 100.0, 40.0, 41.0], index=idx)  # a 60% fall on 2024-01-04
    actions = pd.DataFrame(
        {"Dividends": [0.0, 0.0, 0.0, 0.0], "Stock Splits": [0.0, 0.0, 0.0, 0.0]},
        index=pd.DatetimeIndex(pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"]), tz="Asia/Kolkata"),
    )
    unexplained = ca.unexplained_price_jumps(close, actions)
    assert len(unexplained) == 1
    assert unexplained["Date"].iloc[0] == pd.Timestamp("2024-01-04")
    assert unexplained["Ratio"].iloc[0] == pytest.approx(2.5)


def test_unexplained_price_jumps_explains_a_known_split():
    idx = pd.DatetimeIndex(pd.to_datetime(["2024-01-02", "2024-01-03"]))
    close = pd.Series([100.0, 20.0], index=idx)  # a 5-for-1 on 2024-01-03
    actions = pd.DataFrame(
        {"Dividends": [0.0, 0.0], "Stock Splits": [0.0, 5.0]},
        index=pd.DatetimeIndex(pd.to_datetime(["2024-01-02", "2024-01-03"]), tz="Asia/Kolkata"),
    )
    assert ca.unexplained_price_jumps(close, actions).empty


def test_unexplained_price_jumps_ignores_ordinary_moves():
    idx = pd.DatetimeIndex(pd.to_datetime(["2024-01-02", "2024-01-03"]))
    close = pd.Series([100.0, 98.0], index=idx)
    assert ca.unexplained_price_jumps(close, pd.DataFrame()).empty


# --------------------------------------------------------------------- client plumbing


def _client(handler, **kwargs):
    return ca.CorporateActionsClient(**kwargs)


async def _run_with_mock(client, handler, symbol="RELIANCE", **kwargs):
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        return await client._get_json(http, ca.corporate_actions_url(symbol, "2020-01-01", "2026-09-27"))


@pytest.mark.asyncio
async def test_bare_request_succeeds_without_cookie_priming():
    """Measured 4/4 HTTP 200 on a fresh client with zero cookies primed.

    The jugaad-data prime-the-landing-page pattern is the right instinct for other NSE routes and
    the wrong requirement for this one, so it must not be on the happy path.
    """
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=load("nse_reliance_2020_2026.json"))

    client = _client(handler)
    payload = await _run_with_mock(client, handler)
    assert len(payload) == 10
    assert [r.url.path for r in seen] == [ca.API_PATH]
    assert client.primed_cookie is False, "priming must not run when the request already works"


@pytest.mark.asyncio
async def test_403_triggers_exactly_one_cookie_priming_then_retries():
    """A blocked response primes a session cookie once and retries — then gives up loudly.

    The retry is bounded to one. A second block raises, because a loop against a blocking edge is
    how a client gets a whole IP range blocked.
    """
    seen = []

    def handler(request):
        seen.append(request.url.path)
        if request.url.path == ca.LANDING_URL.replace(ca.BASE_URL, ""):
            return httpx.Response(200, html="<html>ok</html>")
        if request.url.path == ca.API_PATH and seen.count(ca.API_PATH) == 1:
            return httpx.Response(403, text="Access Denied")
        return httpx.Response(200, json=load("nse_reliance_2020_2026.json"))

    client = _client(handler)
    payload = await _run_with_mock(client, handler)
    assert len(payload) == 10
    assert client.primed_cookie is True
    landing = ca.LANDING_URL.replace(ca.BASE_URL, "")
    assert seen.count(landing) == 1, "primed exactly once"
    assert seen.count(ca.API_PATH) == 2, "one blocked attempt plus one retry"
    assert seen.index(landing) < seen.index(ca.API_PATH, 1), "priming happens before the retry"


@pytest.mark.asyncio
async def test_persistent_403_raises_rather_than_looping():
    calls = {"n": 0}

    def handler(request):
        if request.url.path != ca.API_PATH:
            return httpx.Response(200, html="<html>ok</html>")
        calls["n"] += 1
        return httpx.Response(403, text="Access Denied")

    client = _client(handler, max_retries=3)
    with pytest.raises(UpstreamServiceError) as exc:
        await _run_with_mock(client, handler)
    # A single blocked retry plus the final attempt: bounded, and the message says the route is
    # camelCase so the next reader does not go looking for a spelling problem.
    assert calls["n"] <= 3
    assert "corporateActions" in str(exc.value)


@pytest.mark.asyncio
async def test_429_raises_rate_limit_error():
    def handler(request):
        return httpx.Response(429, text="slow down")

    client = _client(handler, max_retries=2)
    with pytest.raises(Exception) as exc:
        await _run_with_mock(client, handler)
    assert "429" in str(exc.value)


@pytest.mark.asyncio
async def test_non_json_body_raises_a_parsing_error():
    """An HTML page served with a 200 is the usual cause and must not become an empty feed."""

    def handler(request):
        return httpx.Response(200, text="<html><body>maintenance</body></html>")

    client = _client(handler, max_retries=1)
    with pytest.raises(ParsingError):
        await _run_with_mock(client, handler)


@pytest.mark.asyncio
async def test_fetch_always_requests_the_full_history_never_a_bare_symbol():
    """``_fetch`` composes _get_json + the parsers, and it must always send a range.

    Reproduced here rather than by calling ``_fetch``, because ``_fetch`` owns its ``AsyncClient``
    and a MockTransport cannot be injected into it — the point of the test is the URL it builds.
    """
    client = _client(lambda request: httpx.Response(200, json=[]))
    assert client.min_request_interval_ms == 300.0

    url = ca.corporate_actions_url("RELIANCE", ca.FEED_FIRST_DATE, "2026-09-27")
    assert "from_date=01-01-1990" in url, "a bare symbol would be capped at 20 rows"
    assert "to_date=27-09-2026" in url

    # The composed path, offline: a payload in, the yfinance-shaped actions frame out.
    def handler(request):
        return httpx.Response(200, json=load("nse_reliance_2020_2026.json"))

    payload = await _run_with_mock(_client(handler), handler)
    actions = ca.actions_frame(
        ca.parse_corporate_actions(payload, symbol="RELIANCE", url="fixture"),
        symbol="RELIANCE",
        url="fixture",
    )
    # 6.5 + 7 + 8 + 9 + 10 + 5.5 + 6 = 52, and one bonus of 2.0.
    assert actions["Dividends"].sum() == pytest.approx(52.0)
    assert actions["Stock Splits"].sum() == pytest.approx(2.0)
