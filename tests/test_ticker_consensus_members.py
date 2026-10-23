"""The yfinance-named consensus members, served from one recorded symbol page.

Six members -- ``recommendations``, ``recommendations_summary``, ``earnings_estimate``,
``revenue_estimate``, ``earnings_history`` and ``growth_estimates`` -- read the *same* six sections
the opt-in symbol page already parses, so they cost no request of their own after any trendlyne
call and one page fetch on a cold client. This file measures that, and pins what each frame does
with the vendor's figures.

**The estimate labels are placed from flags, not from row order.** On the recording the first
``EPS``/``QUARTER`` row is ``Q3FY26`` while the vendor marks ``Q1FY27`` ``is_current``. A
position-based implementation would answer ``1.03`` where yfinance's ``0q`` is ``0.85``, so the
value assertions below are also the control on the placement rule: they name the row the vendor
flagged, not the row that happened to come first. ``+1q``/``+1y`` are the **earliest** forward row
after the anchor; ``REVENUE`` carries a second forward quarter (``Q3FY27``) precisely so
"earliest" is distinguishable from "latest".

**Absence is ``NaN`` plus a note, never a zero.** The recorded page has no same-quarter-prior-year
row for either quarterly estimate, so ``yearAgoEps``/``yearAgoRevenue`` are ``NaN`` there and
``attrs["bfinance_estimate_note"]`` names the two cells and says nothing was back-filled from
``growth_pct``. The alternative -- solving ``avg / (1 + growth)`` -- would publish this package's
arithmetic as a figure the vendor never printed.

**The vendor's percent fields are served as fractions.** The vendor publishes ``growth_pct`` and
``surprise_pct`` as percentages (``4.68`` for 4.68%), while yfinance 1.7.0's identically-named
columns carry Yahoo's unscaled ``raw`` value, a decimal fraction (``0.0468``). The members divide
by 100, so every assertion below is in yfinance's unit. The two ``surprisePercent`` values are
still not the same number: the vendor's ``-5.83`` becomes ``-0.0583``, while recomputing it from
the two published EPS figures gives ``-0.058252...``. The frame carries the vendor's value scaled,
and the test asserts the inequality, so a re-derivation is visible.

**Flag off is a refusal, and it is the same code path for all six.** Every member reaches the
client through ``_stock_page_section`` -> ``_section_call`` -> ``_section_client``, which raises
``NotImplementedError`` naming ``trendlyne_sections=True`` before a socket exists. Each refusal
test is paired with the flag-on control in this file.
"""

from __future__ import annotations

from typing import Dict

import httpx
import pandas as pd
import pytest

from bfinance.ticker import Ticker, _estimate_table
from bfinance.trendlyne import client as tl

# The mock-transport seam, the recorder and the fixture readers are shared with the section
# modules rather than copied: two copies of a request stub drift into testing different things.
# `tests/` is not a package, so the modules are imported by bare name exactly as pytest imports
# them.
from test_trendlyne_core import text
from test_ticker_trendlyne_sections import SYMBOL, Recorder, _install_bridge

#: The whole recorded symbol page -- the same 2026-10-21 capture ``test_trendlyne_stock_page.py``
#: uses, and the only body this file serves. It carries every section the six members read.
PAGE = "stock_page_motherson_sections.html"

#: The page's path, taken from the sitemap record rather than written out, so a template change
#: fails here instead of being agreed on by accident by both the code and the test.
RECORD = tl.parse_sitemap(text("sitemap_excerpt.xml"))[SYMBOL]
PAGE_NEEDLE = RECORD.page_path

MEMBERS = (
    "recommendations",
    "recommendations_summary",
    "earnings_estimate",
    "revenue_estimate",
    "earnings_history",
    "growth_estimates",
)

#: Each member's columns, for the failure path: a failed fetch must still say what shape it would
#: have had, or an empty frame cannot be told from a member that serves a different table.
COLUMNS: Dict[str, list] = {
    "recommendations": ["period", "strongBuy", "buy", "hold", "sell", "strongSell"],
    "recommendations_summary": ["period", "strongBuy", "buy", "hold", "sell", "strongSell"],
    "earnings_estimate": [
        "numberOfAnalysts", "avg", "low", "high", "yearAgoEps", "growth",
    ],
    "revenue_estimate": [
        "numberOfAnalysts", "avg", "low", "high", "yearAgoRevenue", "growth",
    ],
    "earnings_history": ["epsEstimate", "epsActual", "epsDifference", "surprisePercent"],
    "growth_estimates": ["stock"],
}


@pytest.fixture
def recorder() -> Recorder:
    """The one page this file serves, keyed on its own path.

    Everything else answers 404 through :class:`Recorder`, so a member that asked a route nobody
    registered fails loudly instead of "succeeding" on a body it was not meant to parse.
    """
    return Recorder(
        overrides={PAGE_NEEDLE: lambda request: httpx.Response(200, text=text(PAGE))}
    )


@pytest.fixture
def seam(monkeypatch: pytest.MonkeyPatch, recorder: Recorder) -> Recorder:
    _install_bridge(monkeypatch, recorder)
    return recorder


@pytest.fixture
def on(seam: Recorder) -> Ticker:
    return Ticker(SYMBOL, trendlyne_sections=True, cache_ttl_hours=0)


@pytest.fixture
def off(seam: Recorder) -> Ticker:
    """The same wiring with the flag off, so "no request" is a statement about the flag."""
    return Ticker(SYMBOL, cache_ttl_hours=0)


@pytest.fixture(scope="module")
def consensus() -> pd.DataFrame:
    """The parsed consensus section, for the pure placement tests.

    Parsed once for the module: it is a 294 KB ``data-consensusjson`` sweep, and the tests that use
    it assert placement, not parsing -- ``test_trendlyne_stock_page.py`` owns the parse itself.
    """
    return tl.parse_stock_page_consensus(text(PAGE))


# ================================================================ recommendations


def test_recommendations_are_the_vendors_dated_snapshots(on):
    frame = on.recommendations

    assert list(frame.columns) == COLUMNS["recommendations"]
    assert len(frame) == 6
    # The vendor publishes six dated snapshots; yfinance's ``0m``/``-1m`` labels would discard
    # the date and add a month index this source never stated.
    assert frame["period"].tolist() == [
        "2026-05-31", "2026-06-29", "2026-07-31",
        "2026-08-31", "2026-09-30", "2026-10-21",
    ]
    assert frame.iloc[0].tolist() == ["2026-05-31", 12.0, 6.0, 1.0, 0.0, 2.0]
    # The one row whose counts differ from its neighbours: a third analyst moved to ``hold``.
    assert frame.iloc[2].tolist() == ["2026-07-31", 12.0, 6.0, 2.0, 0.0, 2.0]
    # A count column is numeric, not the vendor's ``{"value": n, "class": ...}`` wrapper.
    assert frame[["strongBuy", "buy", "hold", "sell", "strongSell"]].dtypes.tolist() == [
        "float64", "float64", "float64", "float64", "float64",
    ]


def test_recommendation_counts_are_not_recomputed_from_the_analysts_total(on):
    """Each row's five counts sum to the vendor's own ``analysts`` field on this page.

    That is the vendor's arithmetic, measured on the recording, and it is why no total is derived
    here. The assertion is on the section frame the member deliberately drops: if the two ever
    disagree, the member has not changed anything -- the vendor has -- and the comment in
    ``ticker.py`` saying they agree would be stale.
    """
    section = tl.parse_stock_page_recommendations(text(PAGE))
    counts = section[["strong_buy", "buy", "hold", "sell", "strong_sell"]].sum(axis=1)

    assert counts.tolist() == section["analysts"].tolist()
    assert counts.tolist() == [21.0, 21.0, 22.0, 22.0, 22.0, 22.0]


def test_recommendations_summary_is_the_last_four_rows_and_no_second_fetch(on, recorder):
    frame = on.recommendations
    summary = on.recommendations_summary

    assert len(summary) == 4
    assert list(summary.columns) == list(frame.columns)
    assert summary["period"].tolist() == frame["period"].tolist()[-4:]
    assert summary.attrs["bfinance_trendlyne_rows"] == len(summary) == 4
    # Both members read the same page, once.
    assert len(recorder.calls) == 1, recorder.calls


# ================================================================ estimates


def test_earnings_estimate_is_the_yfinance_grid_with_the_vendors_values(on):
    frame = on.earnings_estimate

    assert list(frame.columns) == COLUMNS["earnings_estimate"]
    assert frame.index.tolist() == ["0q", "+1q", "0y", "+1y"]
    assert frame.index.name == "period"
    assert frame["numberOfAnalysts"].tolist() == [2.0, 2.0, 8.0, 16.0]
    assert frame["avg"].tolist() == pytest.approx([0.85, 1.15, 3.85, 5.41])
    assert frame["low"].tolist() == pytest.approx([0.80, 1.10, 3.56, 4.98])
    assert frame["high"].tolist() == pytest.approx([0.90, 1.20, 4.03, 6.01])
    # The vendor's growth_pct is a percent (4.68 means 4.68%); the member divides by 100 to
    # match yfinance's fraction convention (0.0468), so 4.68 is not the expected value here.
    assert frame["growth"].tolist() == pytest.approx([-0.6706, 0.1478, 0.0468, 0.3235])


def test_the_quarter_labels_are_the_flagged_rows_not_the_first_two_rows(consensus):
    """The control on the placement rule, using the vendor's own row order.

    The first two ``EPS``/``QUARTER`` rows are ``Q3FY26`` (avg 1.03) and ``Q4FY26`` (1.25).
    ``0q`` is the ``is_current`` row ``Q1FY27`` (0.85) and ``+1q`` the earliest forward row
    ``Q2FY27`` (1.15), so a position-based implementation answers with two different numbers and
    this test names both of them.
    """
    table, _ = _estimate_table(consensus, "EPS", "yearAgoEps")
    quarters = consensus[
        (consensus["statement"] == "EPS") & (consensus["period_type"] == "QUARTER")
    ]
    first_two = quarters.head(2)

    assert first_two["period"].tolist() == ["Q3FY26", "Q4FY26"]
    assert first_two["average"].tolist() == [1.03, 1.25]
    assert table.loc["0q", "avg"] == pytest.approx(0.85)
    assert table.loc["+1q", "avg"] == pytest.approx(1.15)
    assert table.loc["0q", "avg"] != pytest.approx(first_two.iloc[0]["average"])
    assert table.loc["+1q", "avg"] != pytest.approx(first_two.iloc[1]["average"])


def test_the_annual_labels_stop_at_the_earliest_forward_year(consensus):
    """``+1y`` is ``FY27`` (5.41), not the later ``FY28`` (7.09) the vendor also publishes."""
    table, _ = _estimate_table(consensus, "EPS", "yearAgoEps")
    annual = consensus[
        (consensus["statement"] == "EPS") & (consensus["period_type"] == "ANNUAL")
    ]

    assert annual[annual["is_past"] == False]["period"].tolist() == ["FY27", "FY28"]  # noqa: E712
    assert table.loc["0y", "avg"] == pytest.approx(3.85)
    assert table.loc["+1y", "avg"] == pytest.approx(5.41)
    assert table.loc["+1y", "avg"] != pytest.approx(7.09)


def test_a_block_without_exactly_one_current_row_withholds_its_labels(consensus):
    """A guessed order is worse than a missing label, so the block is withheld and named.

    The control for the flag-based rule: clear the single ``is_current`` marker on the quarterly
    ``EPS`` block and the two quarterly labels disappear, leaving the annual ones untouched and a
    note saying how many rows were flagged instead of one.
    """
    frame = consensus.copy()
    quarterly = (frame["statement"] == "EPS") & (frame["period_type"] == "QUARTER")
    frame.loc[quarterly, "is_current"] = False

    table, note = _estimate_table(frame, "EPS", "yearAgoEps")

    assert table.index.tolist() == ["0y", "+1y"]
    assert note is not None
    assert "0 quarter row(s) is_current" in note
    assert "0q/+1q cannot be placed" in note
    assert "Index labels withheld" in note


def test_revenue_estimate_is_the_revenue_statement_not_the_eps_one(on):
    frame = on.revenue_estimate

    assert list(frame.columns) == COLUMNS["revenue_estimate"]
    assert frame.index.tolist() == ["0q", "+1q", "0y", "+1y"]
    assert frame["numberOfAnalysts"].tolist() == [17.0, 15.0, 21.0, 22.0]
    assert frame["avg"].tolist() == pytest.approx([34403.91, 37562.97, 124518.20, 152003.37])
    assert frame["low"].tolist() == pytest.approx([32610.00, 35500.00, 121703.45, 140037.70])
    assert frame["high"].tolist() == pytest.approx([36243.30, 41326.00, 126133.70, 163028.50])
    # Percent -> fraction, the same rule as the EPS grid: the vendor's 0.27 means 0.27%, and
    # yfinance's ``growth`` column is the fraction 0.0027.
    assert frame["growth"].tolist() == pytest.approx([0.0027, 0.0617, 0.0872, 0.1704])
    # Rupees, not rupees per share: the EPS grid's 0q average is 0.85.
    assert frame.loc["0q", "avg"] != pytest.approx(0.85)


def test_the_revenue_forward_label_is_the_earliest_of_two_forward_quarters(consensus):
    """``REVENUE`` publishes ``Q2FY27`` and ``Q3FY27`` after ``Q1FY27``; ``+1q`` is the first."""
    table, _ = _estimate_table(consensus, "REVENUE", "yearAgoRevenue")
    quarterly = consensus[
        (consensus["statement"] == "REVENUE")
        & (consensus["period_type"] == "QUARTER")
        & (~consensus["is_past"])
    ]

    assert quarterly["period"].tolist() == ["Q2FY27", "Q3FY27"]
    assert table.loc["+1q", "avg"] == pytest.approx(37562.97)
    assert table.loc["+1q", "avg"] != pytest.approx(38075.29)


def test_the_prior_year_cells_are_published_actuals(on):
    """``yearAgo*`` is the row one year earlier, read from its ``actual`` -- not ``avg``."""
    eps = on.earnings_estimate
    revenue = on.revenue_estimate

    assert eps.loc["0y", "yearAgoEps"] == pytest.approx(3.67)  # FY25 actual
    assert eps.loc["+1y", "yearAgoEps"] == pytest.approx(3.66)  # FY26 actual
    assert revenue.loc["0y", "yearAgoRevenue"] == pytest.approx(113662.57)  # FY25 actual
    assert revenue.loc["+1y", "yearAgoRevenue"] == pytest.approx(126103.67)  # FY26 actual
    # FY26's *average* estimate was 124518.20; the prior-year cell is its print, not its forecast.
    assert revenue.loc["+1y", "yearAgoRevenue"] != pytest.approx(124518.20)


def test_absent_prior_year_cells_are_nan_and_named_not_zero(on):
    """The recorded page has no same-quarter-prior-year row, so the cells are absent.

    ``0.0`` would be a published number; ``NaN`` plus a note is the only honest pair. The note is
    asserted to name both cells, the period they belong to and the value that was *not* derived.
    """
    for member, column, label in (
        ("earnings_estimate", "yearAgoEps", "yearAgoEps"),
        ("revenue_estimate", "yearAgoRevenue", "yearAgoRevenue"),
    ):
        frame = getattr(on, member)
        note = frame.attrs["bfinance_estimate_note"]

        assert pd.isna(frame.loc["0q", column])
        assert pd.isna(frame.loc["+1q", column])
        assert frame.loc["0q", column] != 0.0
        assert f"{label} for 0q (Q1FY27, period end 2026-06-30)" in note
        assert f"{label} for +1q (Q2FY27, period end 2026-09-30)" in note
        assert "back-filled from growth_pct" in note
        assert "Index labels withheld" not in note


# ================================================================ earnings history


def test_earnings_history_is_reported_quarters_indexed_by_fiscal_period_end(on):
    frame = on.earnings_history

    assert list(frame.columns) == COLUMNS["earnings_history"]
    assert frame.index.name == "quarter"
    assert frame.index.tolist() == [
        pd.Timestamp("2025-12-31"),
        pd.Timestamp("2026-03-31"),
        pd.Timestamp("2026-06-30"),
    ]
    assert frame["epsEstimate"].tolist() == pytest.approx([1.03, 1.25, 0.85])
    assert frame["epsActual"].tolist() == pytest.approx([0.97, 1.42, 0.98])
    assert frame["epsDifference"].tolist() == pytest.approx([-0.06, 0.17, 0.13])
    assert frame.attrs["bfinance_date_basis"] == "fiscal_period_end"
    assert "index is the fiscal period end" in frame.attrs["bfinance_trendlyne_note"]


def test_a_forward_quarter_with_a_filled_actual_is_not_a_print(on):
    """``Q2FY27`` carries ``actual == average`` and ``surprise_pct == 0.0`` but is not past.

    The vendor's provisional cell is not a reported quarter. The control reads that row off the
    page separately, so the exclusion is asserted against a row that really exists rather than
    against an absence that could be an empty frame.
    """
    frame = on.earnings_history
    section = tl.parse_stock_page_consensus(text(PAGE))
    forward = section[
        (section["statement"] == "EPS") & (section["period"] == "Q2FY27")
    ].iloc[0]

    assert float(forward["actual"]) == pytest.approx(1.15)
    assert bool(forward["is_past"]) is False
    assert pd.Timestamp("2026-09-30") not in frame.index
    assert len(frame) == 3


def test_surprise_percent_is_the_vendors_field_not_a_recomputation(on):
    """The vendor publishes -5.83 percent; the frame carries yfinance's fraction, -0.0583.

    ``-5.8252...%`` = ``-0.058252...`` is what recomputing it produces. The division by 100 is a
    unit conversion only: the number is still the vendor's, not this package's rounding.
    """
    frame = on.earnings_history
    published = float(frame.loc["2025-12-31", "surprisePercent"])
    recomputed = float(frame.loc["2025-12-31", "epsDifference"]) / float(
        frame.loc["2025-12-31", "epsEstimate"]
    )

    assert published == pytest.approx(-0.0583)
    assert published != recomputed
    assert recomputed == pytest.approx(-0.058252, abs=1e-6)


# ================================================================ growth


def test_growth_estimates_are_the_eps_growth_column_in_the_vendors_order(on):
    frame = on.growth_estimates

    assert list(frame.columns) == ["stock"]
    assert frame.index.name == "period"
    assert frame.index.tolist() == [
        "Q3FY26", "Q4FY26", "Q1FY27", "Q2FY27",
        "FY24", "FY25", "FY26", "FY27", "FY28",
    ]
    # Percent -> fraction: the vendor's 22.40 is 0.2240, matching yfinance's unit.
    assert frame["stock"].tolist() == pytest.approx(
        [0.00, 0.2240, -0.6706, 0.1478, 0.00, 0.25, 0.0468, 0.3235, 0.237]
    )


def test_growth_estimates_do_not_fold_revenue_growth_behind_the_same_labels(on):
    """``REVENUE`` shares these period labels and publishes its own ``growth_pct``.

    Folding the two statements together would put a revenue rate and an EPS rate behind one label
    with nothing to tell them apart, so the revenue figures are absent from this frame by rule.
    The control reads them off the section to prove they exist and were not simply zero.
    """
    frame = on.growth_estimates
    section = tl.parse_stock_page_consensus(text(PAGE))
    revenue = section[section["statement"] == "REVENUE"].set_index("period")["growth_pct"]

    assert revenue.loc["Q1FY27"] == pytest.approx(0.27)
    assert revenue.loc["FY26"] == pytest.approx(8.72)
    # The section still carries percentages; the member's column is fractions, so the revenue
    # rows are absent in either unit.
    assert 0.27 not in frame["stock"].tolist()
    assert 8.72 not in frame["stock"].tolist()
    # And the EPS figures are present, as fractions, so this is a filter and not an empty frame.
    assert frame.loc["Q1FY27", "stock"] == pytest.approx(-0.6706)
    assert frame.loc["FY26", "stock"] == pytest.approx(0.0468)


# ================================================================ provenance


@pytest.mark.parametrize(
    "member,rows",
    [
        ("recommendations", 6),
        ("recommendations_summary", 4),
        ("earnings_estimate", 4),
        ("revenue_estimate", 4),
        ("earnings_history", 3),
        ("growth_estimates", 9),
    ],
)
def test_every_derived_frame_carries_the_section_provenance(on, member, rows):
    frame = getattr(on, member)
    attrs = frame.attrs

    assert attrs["bfinance_provenance"] == "trendlyne"
    assert attrs["bfinance_symbol"] == SYMBOL
    assert attrs["bfinance_trendlyne_snapshot_only"] is True
    assert attrs["bfinance_trendlyne_source"].startswith(tl.BASE_URL)
    assert attrs["bfinance_trendlyne_retrieved_at"]
    # One measured count, and it counts the rows the caller now holds.
    assert attrs["bfinance_trendlyne_rows"] == len(frame) == rows
    # The derived frame's note says what the derivation did; the section's note describes 116
    # rows and would be wrong on a four-row frame.
    assert "116 analyst estimate row(s)" not in attrs["bfinance_trendlyne_note"]


@pytest.mark.parametrize(
    "member,field",
    [
        ("earnings_estimate", "growth_pct"),
        ("revenue_estimate", "growth_pct"),
        ("earnings_history", "surprise_pct"),
        ("growth_estimates", "growth_pct"),
    ],
)
def test_the_note_records_the_percent_to_fraction_conversion(on, member, field):
    """A caller reading the frame must be able to find the unit change without the source."""
    note = getattr(on, member).attrs["bfinance_trendlyne_note"]

    assert field in note
    assert "divided by 100" in note


# ================================================================ cost


def test_six_members_share_one_page_fetch(on, recorder):
    assert recorder.calls == [], "constructing the Ticker touched the vendor"

    for member in MEMBERS:
        getattr(on, member)

    assert len(recorder.calls) == 1, recorder.calls
    assert recorder.paths == [PAGE_NEEDLE]

    # A second read of every member is served from the Ticker's own memo.
    for member in MEMBERS:
        getattr(on, member)
    assert len(recorder.calls) == 1, recorder.calls


def test_the_page_over_the_same_client_is_not_fetched_twice_by_the_members(on, recorder):
    """The cold-client claim in the docstrings: one fetch, whichever member asks first."""
    on.earnings_history
    on.recommendations
    on.growth_estimates

    assert len(recorder.calls) == 1
    assert recorder.paths == [PAGE_NEEDLE]


# ================================================================ the flag


@pytest.mark.parametrize("member", MEMBERS)
def test_the_flag_off_refuses_every_member_and_costs_nothing(off, recorder, member):
    with pytest.raises(NotImplementedError, match="trendlyne_sections=True"):
        getattr(off, member)

    assert recorder.calls == [], f"{member} reached the vendor with the flag off"
    assert off.data_warnings == (), "a refusal was recorded as a degradation"


def test_the_same_members_answer_with_the_flag_on(on):
    """The control on the refusal sweep: the six names are not simply unimplemented."""
    for member in MEMBERS:
        frame = getattr(on, member)
        assert isinstance(frame, pd.DataFrame)
        assert not frame.empty, member


# ================================================================ degradation


@pytest.mark.parametrize("member", MEMBERS)
def test_a_failed_page_returns_the_members_empty_shape_and_names_the_cause(
    on, recorder, member
):
    recorder.overrides[PAGE_NEEDLE] = httpx.Response(500, text="server error")

    frame = getattr(on, member)

    assert frame.empty
    assert list(frame.columns) == COLUMNS[member]
    assert any("TrendlyneServerError" in w for w in on.data_warnings), on.data_warnings


def test_a_served_page_records_no_warning(on):
    for member in MEMBERS:
        getattr(on, member)

    assert on.data_warnings == (), on.data_warnings
