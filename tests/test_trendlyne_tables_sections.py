"""
trendlyne section tables — signatures and structural extractors, fully offline, no ``live`` marker.

Everything here reads the recordings in ``tests/fixtures/trendlyne/`` (2026-09-27 and 2026-10-10);
no test touches the network.

The module under test makes two different kinds of claim about those bytes, and the two are tested
under two different rules:

* **A header signature claims a table's identity.** ``research_report``, ``insider_trade`` and the
  re-validated ``board_meeting`` each name cells that were read off a real page. The tests pin the
  measured header rows, pin that the sibling signature does *not* claim the same page, and — the
  part that actually keeps this honest — mutate a header cell and require the verdict to move.
  A signature test that cannot fail is a comment.
* **A structural extractor claims a table's position.** The share-holding holder tables, the two
  buy-sell-zone tables and the mutual-fund grid cannot be told apart by their headers: the first
  two are byte-identical to each other and the rest lead with a month name that rewrites itself
  every quarter. So the tests pin what the extractors key on (``No of holders`` / ``History``, the
  tab-pane id, the ``MF`` column), pin that they still work when the months rotate, and pin that
  they **refuse** rather than guess when the thing they key on is gone.

The negative controls are inline and named, not a trailing section: each one states which verdict
it must move, so a control that stopped working is a named failure rather than a comment.
"""

from __future__ import annotations

from io import StringIO
from pathlib import Path
from typing import ClassVar

import pandas as pd
import pytest
from bs4 import BeautifulSoup

from bfinance.trendlyne import tables as tbt
from bfinance.trendlyne.errors import TrendlyneTableNotFoundError

FIXTURES = Path(__file__).parent / "fixtures" / "trendlyne"


def text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def table_headers(page: str, index: int = 0) -> list[str]:
    """The header row of one outermost table, read the way the classifier reads it."""
    soup = BeautifulSoup(page, "lxml")
    return tbt._header_row(tbt._outermost_tables(soup)[index])


#: The eight signatures that shipped before this module's section work. Pinned as a literal so a
#: silent redefinition is a failure here and not something a reader has to diff to notice.
PRE_EXISTING_SIGNATURES = (
    "bonus",
    "split",
    "dividend",
    "rights",
    "board_meeting",
    "deals",
    "sector_constituents",
    "sector_industry_aggregate",
)

NEW_SIGNATURES = ("research_report", "insider_trade")


# ============================================================ classification of every fixture


class TestEveryFixtureClassifiesAsMeasured:
    """``page_signatures`` over every table-bearing fixture, with the expected verdict spelled out.

    Two of these expectations are ``None`` and that is the finding, not a gap: on the share-holding,
    buy-sell-zone and mutual-fund pages no registered signature matches, and each of those three is
    read positionally instead. Asserting the ``None`` is what stops a future signature from
    quietly starting to claim one of them.
    """

    EXPECTED: ClassVar[dict[str, dict[int, str | None]]] = {
        "corporate_actions_motherson.html": {
            0: "bonus", 1: "split", 2: "dividend", 3: "board_meeting", 4: "rights",
        },
        "bulk_deals_motherson.html": {0: "deals"},
        "board_meeting_tcs.html": {0: "board_meeting"},
        "research_reports_tcs.html": {0: "research_report"},
        "insider_trading_tcs.html": {0: "insider_trade"},
        # Rotating month headers / byte-identical headers: a signature cannot classify these, which
        # is exactly why share_holding_tables / buy_sell_zone_tables / monthly_mf_holding_table
        # exist. See the module docstring in tables.py.
        "shareholding_tcs.html": {i: None for i in range(7)},
        "shareholding_motherson.html": {i: None for i in range(3)},
        "buy_sell_zone_tcs.html": {0: None, 1: None},
        "monthly_mf_holding_tcs.html": {0: None},
    }

    @pytest.mark.parametrize("name", sorted(EXPECTED))
    def test_the_page_reports_exactly_the_signatures_it_was_measured_with(self, name: str) -> None:
        page = text(name)
        signatures = tbt.page_signatures(page)
        assert isinstance(signatures, dict), "page_signatures must answer with a dict, always"
        assert signatures == self.EXPECTED[name]
        # Every verdict is a registered name or an honest None -- never an unknown string that a
        # caller's `signature=` would then be rejected by find_tables.
        assert all(value is None or value in tbt.known_signatures() for value in signatures.values())

    @pytest.mark.parametrize("name", sorted(EXPECTED))
    def test_every_verdict_it_does_give_is_a_name_find_tables_accepts(self, name: str) -> None:
        """``None`` is an ordinary answer (layout tables, and the three positional pages). A name
        the registry has never heard of would not be: ``find_tables`` would reject it, so the
        report would be unusable for its stated purpose."""
        for index, value in tbt.page_signatures(text(name)).items():
            assert value is None or value in tbt.known_signatures(), (
                f"{name} table {index} classified as {value!r}, which find_tables() would reject"
            )

    def test_a_page_with_no_tables_at_all_reports_an_empty_dict_not_an_error(self) -> None:
        """``latest_news_tcs.html`` and ``stock_alerts_tcs.html`` are card lists: zero ``<table>``.

        The caller diagnosing a failure gets an empty dict, and :class:`TrendlyneTableNotFoundError`
        is left for the extractor that was actually asked for something.
        """
        for name in ("latest_news_tcs.html", "stock_alerts_tcs.html"):
            assert tbt.page_signatures(text(name)) == {}


class TestTheRegistryOnlyGrows:
    def test_the_eight_original_signatures_are_untouched_and_in_order(self) -> None:
        assert tbt.known_signatures()[: len(PRE_EXISTING_SIGNATURES)] == PRE_EXISTING_SIGNATURES

    def test_the_new_signatures_are_registered_after_them(self) -> None:
        assert tbt.known_signatures()[len(PRE_EXISTING_SIGNATURES) :] == NEW_SIGNATURES

    def test_every_signature_names_the_page_it_was_measured_on(self) -> None:
        """A signature with no provenance string is a guess wearing a name."""
        for name in NEW_SIGNATURES:
            description = tbt.SIGNATURES[name].description
            assert "2026-10-10" in description, f"{name} does not say when it was measured"
            assert "trendlyne.com" in description or "/" in description, (
                f"{name} does not name the page: {description!r}"
            )

    def test_a_signature_cannot_be_silently_redefined(self) -> None:
        with pytest.raises(ValueError, match="already registered"):
            tbt.register_signature("insider_trade", ("Deal Type",))


# ============================================================ insider_trade is not deals


class TestInsiderTradesAreNotDeals:
    """The asymmetry, both ways, with a control that shows what is doing the work.

    Bulk/block deals require a ``Deal Type`` cell; insider trades carry ``Client Category`` and
    ``Post Transaction Holding`` and no ``Deal Type``. Registration order alone cannot be the
    reason they are distinguishable — the control at the end hands the insider header a
    ``Deal Type`` cell and requires ``deals`` to take the page.
    """

    INSIDER = "insider_trading_tcs.html"
    BULK = "bulk_deals_motherson.html"

    def test_the_insider_page_classifies_as_insider_trade(self) -> None:
        page = text(self.INSIDER)
        assert tbt.page_signatures(page) == {0: "insider_trade"}
        frame = tbt.find_table(page, "insider_trade")
        assert list(frame.columns) == [
            "Client Name", "Client Category", "Action*", "Reported To/By Exchange", "Quantity",
            "Post Transaction Holding", "Traded %", "Avg. Price", "Value", "Period",
            "Regulation (Insider/SAST)", "Security Type", "Mode",
        ]
        assert frame.shape == (174, 13), "175 <tr> = 1 header + 174 data rows, none dropped"
        assert frame.attrs["bfinance_provenance"] == tbt.PROVENANCE
        assert frame.attrs["bfinance_trendlyne_table"] == "insider_trade"

    def test_the_deals_signature_does_not_claim_the_insider_page(self) -> None:
        with pytest.raises(TrendlyneTableNotFoundError):
            tbt.find_tables(text(self.INSIDER), "deals")
        # ... and the reason, stated rather than assumed: the cell deals requires is not there.
        headers = table_headers(text(self.INSIDER))
        assert "Deal Type" not in headers
        assert "Security Type" in headers, "a near-miss cell is what makes this worth pinning"

    def test_the_bulk_page_still_classifies_as_deals(self) -> None:
        page = text(self.BULK)
        assert tbt.page_signatures(page) == {0: "deals"}
        frame = tbt.find_table(page, "deals")
        assert list(frame.columns) == [
            "Client Name", "Deal Type", "Action", "Date", "Avg. Price", "Quantity",
            "Intraday", "Exchange",
        ]
        assert len(frame) == 39, "the fixture keeps the first 40 of 82 live rows"

    def test_the_insider_signature_does_not_claim_the_bulk_page(self) -> None:
        with pytest.raises(TrendlyneTableNotFoundError):
            tbt.find_tables(text(self.BULK), "insider_trade")
        present = {tbt.normalize_header(h) for h in table_headers(text(self.BULK))}
        unsatisfied = [
            group
            for group in tbt.SIGNATURES["insider_trade"].required
            if not any(alias in present for alias in group)
        ]
        assert len(unsatisfied) == 2, (
            "a bulk-deal header must fail exactly the Client Category and Post Transaction "
            f"Holding groups, not all three by accident: {unsatisfied}"
        )

    def test_control_a_deal_type_cell_makes_the_insider_page_a_deals_page(self) -> None:
        """Negative control. If ``deals`` did not match here, the two signatures would be
        distinguishable for some reason other than the header, and the tests above would be
        passing for the wrong reason."""
        headers = table_headers(text(self.INSIDER))
        assert tbt.classify_table(headers + ["Deal Type"]) == "deals"

    def test_control_a_client_category_cell_alone_does_not_move_the_bulk_page(self) -> None:
        headers = table_headers(text(self.BULK))
        assert tbt.classify_table(headers) == "deals"
        # Adding both insider groups still cannot reach insider_trade, which needs all three.
        assert tbt.classify_table(headers + ["Client Category", "Post Transaction Holding"]) == "deals"

    def test_control_removing_client_category_makes_the_insider_page_unclassified(self) -> None:
        headers = [
            h for h in table_headers(text(self.INSIDER)) if h != "Client Category"
        ]
        assert tbt.classify_table(headers) is None


# ============================================================ board_meeting, on its own route


class TestBoardMeetingIsValidatedOnItsOwnRoute:
    """``docs/UPSTREAM_SOURCES.md`` records that this signature's only validation was a
    corporate-actions *purpose* table and that the fix — rename it, or measure it on
    ``/equity/board-meeting/`` — was undecided.

    Measured 2026-10-10 on ``board_meeting_tcs.html``: the route's own header row is
    ``['Ex-date', 'Purpose', 'Notes']``, the same three cells, over 95 forward-dated rows. So the
    signature **does** serve the route it was named for and needs no rename. The collision is real
    and is not resolvable by any signature — the two pages carry identical headers — which is why
    every frame publishes ``bfinance_trendlyne_table_index`` and the caller is told which page it
    holds.
    """

    def test_the_board_meeting_route_classifies_as_board_meeting(self) -> None:
        page = text("board_meeting_tcs.html")
        assert tbt.page_signatures(page) == {0: "board_meeting"}
        frame = tbt.find_table(page, "board_meeting")
        assert list(frame.columns) == ["Ex-date", "Purpose", "Notes"]
        assert frame.shape == (95, 3), "96 <tr> = 1 header + 95 data rows"
        assert frame.attrs["bfinance_trendlyne_table"] == "board_meeting"

    def test_the_rows_are_forward_dated_meetings_not_history(self) -> None:
        frame = tbt.find_table(text("board_meeting_tcs.html"), "board_meeting")
        assert frame["Ex-date"].iloc[0] == "08 Oct 2026", "the fixture's first row, verbatim"
        assert frame["Purpose"].iloc[0] == "Quarterly Results & Interim Dividend"

    def test_the_collision_with_the_corporate_actions_purpose_table_is_still_there(self) -> None:
        """The honest half of the finding: this signature is a superset of two pages' headers."""
        assert tbt.page_signatures(text("corporate_actions_motherson.html"))[3] == "board_meeting"
        assert tbt.find_table(text("corporate_actions_motherson.html"), "board_meeting").shape == (10, 3)

    def test_control_renaming_notes_unclassifies_the_board_meeting_page(self) -> None:
        page = text("board_meeting_tcs.html").replace("Notes", "Comment")
        assert tbt.page_signatures(page) == {0: None}
        with pytest.raises(TrendlyneTableNotFoundError):
            tbt.find_tables(page, "board_meeting")

    def test_control_the_board_meeting_page_is_not_classified_as_anything_else(self) -> None:
        assert tbt.classify_table(["Ex-date", "Purpose", "Notes"]) == "board_meeting"
        assert tbt.classify_table(["Ex-date", "Purpose", "Notes"]) != "deals"


# ============================================================ research_report


class TestResearchReportSignature:
    def test_the_research_reports_page_classifies_as_research_report(self) -> None:
        page = text("research_reports_tcs.html")
        assert tbt.page_signatures(page) == {0: "research_report"}
        frame = tbt.find_table(page, "research_report")
        assert frame.shape == (21, 12), "22 <tr> = 1 header + 21 data rows, none dropped"
        assert frame.attrs["bfinance_trendlyne_table"] == "research_report"

    def test_the_measured_header_row_is_pinned(self) -> None:
        assert table_headers(text("research_reports_tcs.html")) == [
            "Summary", "Date", "Stock", "Author", "LTP", "Target",
            "Price at reco (Change since reco%)", "Upside(%)", "Type", "Report", "", "Discuss",
        ]

    def test_the_parentheses_in_up_survive_normalization(self) -> None:
        """``normalize_header`` lowercases, collapses whitespace and trims a trailing colon. It does
        **not** strip punctuation, so ``"Upside(%)"`` normalizes to ``"upside(%)"`` and the
        signature may require the measured cell verbatim — which is what it does."""
        assert tbt.normalize_header("Upside(%)") == "upside(%)"
        assert tbt.normalize_header("Upside (%)") == "upside (%)"
        assert tbt.normalize_header("Upside(%)") != tbt.normalize_header("Upside (%)")

    def test_it_is_not_a_blanket_match_on_the_word_upside(self) -> None:
        assert tbt.classify_table(["Summary", "Target", "Upside %"]) is None
        assert tbt.classify_table(["Summary", "Upside(%)"]) is None, "every group is required"

    def test_it_does_not_claim_the_byte_identical_buy_sell_zone_tables(self) -> None:
        """``META.json`` warns about exactly this: a signature loose enough to serve the research
        table must not also serve the two PE-band grids, which have their own trap."""
        assert tbt.page_signatures(text("buy_sell_zone_tcs.html")) == {0: None, 1: None}

    def test_control_dropping_upside_unclassifies_the_page(self) -> None:
        page = text("research_reports_tcs.html").replace("Upside(%)", "Return")
        assert tbt.page_signatures(page) == {0: None}

    def test_control_dropping_target_unclassifies_the_page(self) -> None:
        page = text("research_reports_tcs.html").replace(
            "<th class='rightAlgn numeric'>Target </th>",
            "<th class='rightAlgn numeric'>Price Target </th>",
        )
        assert page != text("research_reports_tcs.html"), "the fixture was not mutated"
        assert tbt.page_signatures(page) == {0: None}


# ============================================================ share-holding: structural


class TestShareHoldingTablesAreStructural:
    """The holder tables lead with ``Holding Jun 2026`` — a month name that rewrites itself every
    quarter — and the three of them repeat one header row byte for byte. So the extractor keys on
    the two cells that do not rotate (``No of holders``, ``History``) and reads the category off
    the section wrapper, which is the only place the three differ.
    """

    TCS = "shareholding_tcs.html"
    MOTHERSON = "shareholding_motherson.html"

    def test_it_returns_one_summary_grid_and_three_holder_tables(self) -> None:
        result = tbt.share_holding_tables(text(self.TCS))
        # 13 <tr> in the <tbody>, less the one empty DII placeholder row -- see
        # test_the_empty_placeholder_row_is_dropped_rather_than_labelled.
        assert result.summary.shape == (12, 13), "every row the page renders, hidden ones included"
        assert next(iter(result.summary.columns)) == "Row Type"
        assert [len(frame) for frame in result.holders] == [6, 10, 13]
        assert len(result.categories) == 3

    def test_the_categories_are_the_real_vendor_labels_in_page_order(self) -> None:
        result = tbt.share_holding_tables(text(self.TCS))
        assert result.categories == (
            "Promoter Holding",
            "Institutional Holding",
            "Public (Non-Institutional) Holding",
        )
        for category, frame in zip(result.categories, result.holders):
            assert frame.attrs["bfinance_trendlyne_holder_category"] == category

    def test_categories_line_up_with_holder_tables_one_for_one(self) -> None:
        result = tbt.share_holding_tables(text(self.TCS))
        assert len(result.categories) == len(result.holders)

    def test_the_frames_carry_the_documented_attrs(self) -> None:
        result = tbt.share_holding_tables(text(self.TCS))
        assert result.summary.attrs["bfinance_provenance"] == tbt.PROVENANCE
        assert result.summary.attrs["bfinance_trendlyne_table"] == "shareholding_summary"
        for frame in result.holders:
            assert frame.attrs["bfinance_provenance"] == tbt.PROVENANCE
            assert frame.attrs["bfinance_trendlyne_table"] == "shareholding_holder"
            assert frame.attrs["bfinance_trendlyne_table_index"] in (2, 4, 6)

    def test_the_summary_grid_is_read_with_one_header_row_and_the_holders_with_two(self) -> None:
        """Trap 1 in the module docstring, on real markup: the summary's second row is *data*
        (Promoter 71.8%), while the holder tables carry a genuine ``%`` / ``shares`` sub-header."""
        result = tbt.share_holding_tables(text(self.TCS))
        assert result.summary.index[0] == "Promoter", "the sub-header row would have eaten this"
        assert result.holders[0].columns[0] == "Name | Name"
        assert "Holding Jun 2026 | %" in list(result.holders[0].columns)

    def test_the_category_summary_grids_are_not_mistaken_for_holder_tables(self) -> None:
        """Three tables on the page carry ``History`` and no ``No of holders``. Requiring both is
        what keeps them out; requiring either alone would return seven tables."""
        page = text(self.TCS)
        assert page.count("No of holders") == 3, "one per holder table"
        assert page.count("History") == 6, "three holder tables + three category summary grids"
        assert len(tbt.share_holding_tables(page).holders) == 3

    def test_a_category_is_never_invented_when_the_markup_does_not_name_one(self) -> None:
        """The 2026-09-27 MOTHERSON fixture was trimmed to the ``<table>`` blocks, so the wrappers
        that carry the names are gone. ``None`` is the honest answer; the section id is not used as
        a fallback because a slug is not a published category.

        The summary grid is the one thing on that page that still reads in full, wrappers or not:
        the wrapper only ever named the section, never the grid's rows.
        """
        result = tbt.share_holding_tables(text(self.MOTHERSON))
        assert result.categories == (None,)
        assert len(result.holders) == 1
        assert result.holders[0].attrs["bfinance_trendlyne_holder_category"] is None
        assert result.summary.shape == (12, 13)
        assert next(iter(result.summary.columns)) == "Row Type"
        assert list(result.summary.index) == [
            "Promoter", "Holding", "Pledged", "Locked", "FII", "DII",
            "Mutual Funds", "Banks", "Insurance", "Others", "Public", "Others",
        ]

    def test_rotating_the_month_columns_does_not_change_the_result(self) -> None:
        """The whole reason this extractor is structural: ``Jun 2026`` becomes ``Sep 2026`` and
        ``Mar 2026`` becomes ``Dec 2026`` on the next quarter, and nothing about which table is
        which may move when it does."""
        page = text(self.TCS)
        rotated = page.replace("Jun 2026", "Sep 2026").replace("Mar 2026", "Dec 2026")
        rotated = rotated.replace("Dec 2025", "Sep 2025")
        assert rotated != page, "the fixture was not mutated, so the test would be vacuous"
        assert "Holding Jun 2026" not in rotated

        before, after = tbt.share_holding_tables(page), tbt.share_holding_tables(rotated)
        assert after.categories == before.categories
        assert [len(f) for f in after.holders] == [len(f) for f in before.holders]
        assert after.summary.shape == before.summary.shape
        assert next(iter(after.summary.columns)) == next(iter(before.summary.columns)) == "Row Type"
        assert [f.attrs["bfinance_trendlyne_table_index"] for f in after.holders] == [
            f.attrs["bfinance_trendlyne_table_index"] for f in before.holders
        ]
        # The column labels themselves did follow the rotation — which is why they are labels and
        # not a selector.
        assert "Holding Sep 2026 | %" in list(after.holders[0].columns)

    def test_control_renaming_the_stable_token_breaks_the_extraction(self) -> None:
        """The counterpart to the rotation test: if the months moving cannot break it and the two
        stable tokens moving cannot either, then the months test proves nothing."""
        page = text(self.TCS).replace("No of holders", "Holders")
        with pytest.raises(TrendlyneTableNotFoundError, match="no of holders"):
            tbt.share_holding_tables(page)

    def test_control_removing_the_wrappers_removes_the_categories(self) -> None:
        """Proves the category is read from the section's tab label and nothing else.

        ``data-toggle="tab"`` is what marks an anchor as the label of a pane. Turning it off
        leaves ``id="promoter-holding"`` sitting right there, so this is also the control on the
        documented refusal to fall back to that id: the answer must become ``None``, not a slug.
        """
        page = text(self.TCS)
        stripped = page.replace('data-toggle="tab"', 'data-nothing="tab"')
        assert stripped != page
        result = tbt.share_holding_tables(stripped)
        assert len(result.holders) == 3, "the tables are still found"
        assert result.categories == (None, None, None)

    def test_control_removing_the_summary_id_raises_rather_than_guessing(self) -> None:
        page = text(self.TCS).replace("shareSummaryTable", "someOtherId")
        with pytest.raises(TrendlyneTableNotFoundError, match="shareSummaryTable"):
            tbt.share_holding_tables(page)


class TestTheSummaryGridCarriesItsHiddenRows:
    """``pandas.read_html`` honours the CSS ``display`` property by default, so the 8 of 13 rows
    the page renders inside ``style="display:none"`` accordions were being dropped — ``Pledged``
    among them, which is a distinct holding figure and not a restatement of the promoter's own.
    That is data loss, and the two ``test_control_*`` tests at the end are what say so out loud.
    """

    TCS = "shareholding_tcs.html"

    #: Every row the ``<tbody>`` renders, with its level, in page order.
    ROWS: ClassVar[list[tuple[str, str]]] = [
        ("Promoter", "category"),
        ("Holding", "subgroup"),
        ("Pledged", "subgroup"),
        ("Locked", "subgroup"),
        ("FII", "category"),
        ("DII", "category"),
        ("Mutual Funds", "subgroup"),
        ("Banks", "subgroup"),
        ("Insurance", "subgroup"),
        ("Others", "subgroup"),
        ("Public", "category"),
        ("Others", "category"),
    ]

    def summary(self, name: str = TCS) -> pd.DataFrame:
        return tbt.share_holding_tables(text(name)).summary

    def summary_from(self, page: str) -> pd.DataFrame:
        return tbt.share_holding_tables(page).summary

    def pandas_default_read(self, page: str) -> pd.DataFrame:
        """What ``read_html`` returns with its own defaults — the reader that used to lose 8 rows.

        The fragment is wrapped in ``StringIO`` because a bare string starting with ``<table`` is
        read as a *filename* by pandas and raises ``FileNotFoundError``.
        """
        soup = BeautifulSoup(page, "lxml")
        table = tbt._outermost_tables(soup)[0]
        assert table.get("id") == "shareSummaryTable"
        return pd.read_html(StringIO(str(table)), flavor="lxml", header=0)[0]

    def test_the_grid_carries_every_row_including_the_collapsed_ones(self) -> None:
        frame = self.summary()
        assert list(zip(frame.index, frame["Row Type"])) == self.ROWS

    def test_the_figures_of_the_hidden_rows_are_the_real_ones(self) -> None:
        """The rows are not decoration: each sub-row holds figures of its own. ``Pledged`` on the
        recorded page reads 0.28% where ``Holding`` reads 71.8%, and on MOTHERSON 2.34% where
        ``Holding`` reads 48.6% — a row that was dropped rather than blanked."""
        tcs, motherson = self.summary(), self.summary("shareholding_motherson.html")
        assert tcs.loc["Pledged", "Sep 2024"] == "0.28%"
        assert motherson.loc["Pledged", "Jun 2026"] == "2.34%"
        assert motherson.loc["Holding", "Jun 2026"] == "48.6%"

    def test_the_row_labels_are_the_index_and_are_the_vendors_own_text(self) -> None:
        frame = self.summary()
        assert frame.index.name == "Summary", "the vendor's own header cell over the label column"
        assert frame.loc["Mutual Funds", "Jun 2026"] == "5.68%"
        assert frame.loc["Insurance", "Jun 2026"] == "6.71%"

    def test_the_top_level_and_subgroup_distinction_is_observable(self) -> None:
        frame = self.summary()
        categories = frame.index[frame["Row Type"] == "category"].tolist()
        subgroups = frame.index[frame["Row Type"] == "subgroup"].tolist()
        assert categories == ["Promoter", "FII", "DII", "Public", "Others"]
        assert subgroups == ["Holding", "Pledged", "Locked", "Mutual Funds", "Banks",
                             "Insurance", "Others"]

    def test_the_index_is_not_unique_and_no_label_was_invented_to_make_it_so(self) -> None:
        """``"Others"`` is a DII sub-row *and* a top-level category on this page. Joining them
        into ``"DII / Others"`` would put a string in the frame that trendlyne never wrote, so the
        duplicate stands and ``"Row Type"`` is what separates the two rows."""
        frame = self.summary()
        assert frame.index.duplicated().sum() == 1
        assert list(frame.loc["Others", "Row Type"]) == ["subgroup", "category"]

    def test_the_empty_placeholder_row_is_dropped_rather_than_labelled(self) -> None:
        """Both recorded pages carry one ``<tr>`` with **no cells at all** where the DII accordion
        opens. It has no name and no figure, and naming it after the category above it would put a
        label in the index the vendor never wrote — so it is dropped, on both the markup side and
        the frame side, and the four real DII sub-rows around it are unaffected."""
        page = text(self.TCS)
        soup = BeautifulSoup(page, "lxml")
        body = soup.find("table", id="shareSummaryTable").find("tbody")
        blanks = [tr for tr in body.find_all("tr") if not tr.find_all("td")]
        assert len(blanks) == 1, "the fixture's one empty row"
        assert "subgroup" in blanks[0]["class"], "and it really is a sub-row, not a stray <tr>"
        assert "" not in self.summary().index

    def test_the_eight_missing_rows_are_exactly_the_collapsed_ones(self) -> None:
        """The defect, pinned as a difference rather than as prose: pandas' own default read of
        this same markup returns the five top-level rows and stops, and the eight it drops are
        the eight ``<tr>`` the page hides. One of those eight is the empty placeholder, so the
        extracted grid is 5 + 8 - 1 = 12 rows rather than 13."""
        page = text(self.TCS)
        default = self.pandas_default_read(page)
        assert len(default) == 5, "read_html's default honours the CSS display property"
        assert list(default.iloc[:, 0]) == ["Promoter", "FII", "DII", "Public", "Others"]

        frame = self.summary()
        assert len(frame) == len(default) + 8 - 1
        assert list(frame.index[frame["Row Type"] == "category"]) == list(default.iloc[:, 0])

    def test_control_removing_display_none_changes_the_row_count(self) -> None:
        """Negative control. Strip the CSS in memory and the default reader's row count must MOVE:
        5 rows to 13. If it did not, the CSS would not be what hides those rows, and every claim
        above about "the collapsed rows" would be passing for the wrong reason.

        The extractor returns 12 either way — 13 rows less the empty placeholder — which is the
        other half of the control: it is reading the hidden rows, and not reacting to them.
        """
        page = text(self.TCS)
        assert page.count('style="display:none;"') == 8, "eight collapsed rows on this page"
        shown = page.replace(' style="display:none;"', "").replace('style="display:none;"', "")
        assert shown != page and "display:none" not in shown

        collapsed, expanded = self.pandas_default_read(page), self.pandas_default_read(shown)
        assert len(collapsed) == 5 and len(expanded) == 13
        assert len(expanded) - len(collapsed) == 8

        assert list(self.summary_from(shown).index) == list(self.summary().index)

    def test_control_a_figure_inside_a_hidden_row_is_read_not_assumed(self) -> None:
        """The strongest form of the control: edit a cell that lives in a collapsed row and require
        the published frame to move. A summary that reached those rows by accident, or filled them
        in, would not follow the edit."""
        page = text(self.TCS).replace(">Pledged<", ">Pledged (restated)<")
        assert page != text(self.TCS), "the hidden row was not mutated, so the test is vacuous"
        frame = self.summary_from(page)
        assert "Pledged (restated)" in frame.index
        assert "Pledged" not in frame.index
        row = frame.loc["Pledged (restated)"]
        assert row["Row Type"] == "subgroup"
        assert row["Sep 2024"] == "0.28%", "its figures moved with it"


# ============================================================ buy-sell-zone: positional


class TestBuySellZoneTablesArePositional:
    """The two tables have byte-identical header rows and different numbers, so nothing about
    their headers can say which is which. The discriminator is ``div.tab-pane#standalone`` vs
    ``div.tab-pane#consolidated``, and the frames must follow the id rather than the document
    order — which is ``consolidated`` first on this page.
    """

    FIXTURE = "buy_sell_zone_tcs.html"

    def test_it_returns_exactly_the_two_documented_keys(self) -> None:
        result = tbt.buy_sell_zone_tables(text(self.FIXTURE))
        assert list(result) == ["standalone", "consolidated"]

    def test_the_two_tables_are_not_swapped(self) -> None:
        result = tbt.buy_sell_zone_tables(text(self.FIXTURE))
        assert result["standalone"].attrs["bfinance_trendlyne_current_pe"] == 14.9
        assert result["consolidated"].attrs["bfinance_trendlyne_current_pe"] == 15.1
        assert result["standalone"].attrs["bfinance_trendlyne_average_pe"] == 23.6
        assert result["consolidated"].attrs["bfinance_trendlyne_average_pe"] == 23.2
        assert result["standalone"].attrs["bfinance_trendlyne_median_pe"] == 23.8
        assert result["consolidated"].attrs["bfinance_trendlyne_median_pe"] == 23.1
        assert result["standalone"].attrs["bfinance_trendlyne_strong_buy_zone_pct"] == 1.0
        assert result["consolidated"].attrs["bfinance_trendlyne_strong_buy_zone_pct"] == 1.8

    def test_the_frame_and_its_own_tile_agree(self) -> None:
        """Two independent renderings of the current PE on the same page — the ``Current PE is``
        marker inside the band's first row and the summary tile. A swapped mapping would make
        them disagree, which is why this is checked against the frame and not only the attrs."""
        result = tbt.buy_sell_zone_tables(text(self.FIXTURE))
        for basis, expected in (("standalone", "14.9"), ("consolidated", "15.1")):
            entry = result[basis]
            assert entry.basis == basis
            assert expected in str(entry.frame.iloc[0, 0])
            assert entry.attrs["bfinance_trendlyne_current_pe"] == float(expected)

    def test_the_two_frames_are_distinct_objects(self) -> None:
        result = tbt.buy_sell_zone_tables(text(self.FIXTURE))
        standalone, consolidated = result["standalone"], result["consolidated"]
        assert standalone.frame is not consolidated.frame
        assert standalone.attrs is not consolidated.attrs
        assert standalone.frame is standalone.frame
        assert standalone.attrs is standalone.frame.attrs, (
            "BuySellZoneTable.attrs must BE the frame's attrs, not a copy that can drift"
        )
        assert standalone.frame.shape == consolidated.frame.shape == (10, 5)
        assert standalone.frame.iloc[0, 0] != consolidated.frame.iloc[0, 0]

    def test_the_total_row_is_kept_because_it_is_data(self) -> None:
        result = tbt.buy_sell_zone_tables(text(self.FIXTURE))
        for entry in result.values():
            assert entry.frame.iloc[-1, 0] == "Total"
            # The one column the all-or-nothing rule does type: every one of its 10 values is a
            # bare integer, so it is published as numbers rather than as text.
            assert entry.frame.iloc[-1, 1] == 495
            assert pd.api.types.is_numeric_dtype(entry.frame["Days traded in range"])
            assert not pd.api.types.is_numeric_dtype(entry.frame["PE range"])

    def test_the_stats_that_the_page_does_not_publish_are_none(self) -> None:
        """``% Days traded below current PE`` is the *description* under the Strong Buy Zone tile,
        not a tile of its own. Copying 1.8 into both attrs would be a claim about the vendor's
        semantics; ``None`` says the page carries no such stat."""
        result = tbt.buy_sell_zone_tables(text(self.FIXTURE))
        for entry in result.values():
            assert entry.attrs["bfinance_trendlyne_days_below_current_pe_pct"] is None
            assert entry.attrs["bfinance_trendlyne_buy_sell_tiles"]["strong buy zone"] in (
                "1.0%", "1.8%",
            )
            assert "% days traded below current pe" not in entry.attrs["bfinance_trendlyne_buy_sell_tiles"]

    def test_the_raw_tile_text_is_published_so_nothing_is_lost_to_the_unit_conversion(self) -> None:
        tiles = tbt.buy_sell_zone_tables(text(self.FIXTURE))["consolidated"].attrs
        raw = tiles["bfinance_trendlyne_buy_sell_tiles"]
        assert raw["current pe"] == "15.1"
        assert raw["strong buy zone"] == "1.8%"
        assert raw["forward eps"] == "154.4", "a tile this extractor does not name is still readable"

    def test_the_basis_attr_matches_the_key_it_is_stored_under(self) -> None:
        for basis, entry in tbt.buy_sell_zone_tables(text(self.FIXTURE)).items():
            assert entry.attrs["bfinance_trendlyne_buy_sell_basis"] == basis
            assert entry.attrs["bfinance_provenance"] == tbt.PROVENANCE
            assert entry.attrs["bfinance_trendlyne_table"] == "buy_sell_zone"

    def test_control_exchanging_the_two_pane_ids_exchanges_the_answers(self) -> None:
        """Negative control for the mapping: the answer must follow the id, not the document
        position. On the unmodified page ``consolidated`` is the *first* table, so a
        position-based implementation would return 15.1 for ``standalone``."""
        page = text(self.FIXTURE)
        assert page.count('id="standalone"') == page.count('id="consolidated"') == 1
        swapped = (
            page.replace('id="standalone"', 'id="__swap__"')
            .replace('id="consolidated"', 'id="standalone"')
            .replace('id="__swap__"', 'id="consolidated"')
        )
        result = tbt.buy_sell_zone_tables(swapped)
        assert result["standalone"].attrs["bfinance_trendlyne_current_pe"] == 15.1
        assert result["consolidated"].attrs["bfinance_trendlyne_current_pe"] == 14.9

    def test_control_renaming_a_pane_id_raises_instead_of_falling_back(self) -> None:
        page = text(self.FIXTURE).replace('id="standalone"', 'id="standaloneX"')
        with pytest.raises(TrendlyneTableNotFoundError, match="standalone"):
            tbt.buy_sell_zone_tables(page)

    def test_a_tile_that_renders_no_number_leaves_the_attr_none_rather_than_zero(self) -> None:
        """The vendor blanking a statistic is the one case where ``0.0`` would be a lie: ``-1``
        and ``0`` are different facts everywhere else in this package, and here a missing figure
        has to stay missing."""
        page = text(self.FIXTURE)
        blanked = page.replace("fw600\">\n        \n        \n            15.1\n",
                               "fw600\">\n        \n        \n            --\n")
        assert blanked != page, "the tile value was not mutated, so the test would be vacuous"
        result = tbt.buy_sell_zone_tables(blanked)
        assert result["consolidated"].attrs["bfinance_trendlyne_current_pe"] is None
        assert result["standalone"].attrs["bfinance_trendlyne_current_pe"] == 14.9, (
            "only the mutated pane changes"
        )

    def test_control_a_pane_with_no_table_raises_instead_of_borrowing_the_sibling(self) -> None:
        """One pane empty, the other carrying the grid. Returning the sibling's frame under
        ``"standalone"`` would be the worst failure this extractor could have, so it is built
        here explicitly rather than hoped against."""
        page = (
            '<div class="tab-content">'
            '<div class="tab-pane" id="standalone"></div>'
            '<div class="tab-pane" id="consolidated">'
            "<table><tr><th>PE range</th><th>Days traded in range</th></tr>"
            "<tr><td>14-16</td><td>42</td></tr></table>"
            "</div></div>"
        )
        with pytest.raises(TrendlyneTableNotFoundError, match="carries no <table>"):
            tbt.buy_sell_zone_tables(page)


# ============================================================ mutual-fund grid


class TestMonthlyMutualFundHoldingTable:
    def test_it_reads_the_ragged_three_month_grid(self) -> None:
        frame = tbt.monthly_mf_holding_table(text("monthly_mf_holding_tcs.html"))
        assert frame.shape == (31, 10), "30 kept data rows + the Total: row"
        assert list(frame.columns) == [
            "MF | MF",
            "Sep-2026 | AUM (Cr)", "Sep-2026 | AUM %", "Sep-2026 | Shares Held",
            "Sep-2026 | Month Change", "Sep-2026 | Month Change %",
            "Aug-2026 | Shares Held", "Aug-2026 | Month Change %",
            "Jul-2026 | Shares Held", "Jul-2026 | Month Change %",
        ]

    def test_the_two_row_header_keeps_the_five_column_newest_band(self) -> None:
        """9 cells of sub-header over 10 cells of data: with ``header=0`` the band of five under
        the newest month collapses and the sub-header row becomes a holder named 'AUM (Cr)'."""
        frame = tbt.monthly_mf_holding_table(text("monthly_mf_holding_tcs.html"))
        september = [c for c in frame.columns if c.startswith("Sep-2026")]
        assert len(september) == 5

    def test_the_total_row_survived_the_fixture_trim(self) -> None:
        frame = tbt.monthly_mf_holding_table(text("monthly_mf_holding_tcs.html"))
        assert frame.iloc[-1, 0] == "Total:"
        assert frame.iloc[-1, 3] == "29,136,528"

    def test_the_ragged_values_stay_text(self) -> None:
        """``-`` and ``1,234`` in one column: the all-or-nothing rule declines to type every month
        column, which is the contract, not an oversight."""
        frame = tbt.monthly_mf_holding_table(text("monthly_mf_holding_tcs.html"))
        assert not any(pd.api.types.is_numeric_dtype(column) for column in frame.dtypes)
        assert (frame["Sep-2026 | Shares Held"] == "-").any()
        assert frame["Aug-2026 | Shares Held"].str.contains(",").any()

    def test_it_carries_the_documented_attrs(self) -> None:
        frame = tbt.monthly_mf_holding_table(text("monthly_mf_holding_tcs.html"))
        assert frame.attrs["bfinance_provenance"] == tbt.PROVENANCE
        assert frame.attrs["bfinance_trendlyne_table"] == "monthly_mf_holding"
        assert frame.attrs["bfinance_trendlyne_table_index"] == 0

    def test_control_rotating_the_month_headers_does_not_break_the_selection(self) -> None:
        page = (
            text("monthly_mf_holding_tcs.html")
            .replace("Sep-2026", "Dec-2026")
            .replace("Aug-2026", "Nov-2026")
        )
        frame = tbt.monthly_mf_holding_table(page)
        assert frame.shape == (31, 10)
        assert any(column.startswith("Dec-2026") for column in frame.columns)

    def test_control_renaming_the_mf_column_raises_rather_than_returning_a_wrong_frame(self) -> None:
        page = text("monthly_mf_holding_tcs.html").replace(">MF<", ">Fund<", 1)
        with pytest.raises(TrendlyneTableNotFoundError, match="MF"):
            tbt.monthly_mf_holding_table(page)


# ============================================================ absence is a refusal


class TestAnAbsentTableRaisesRatherThanReturningSomething:
    """Never a silent wrong frame. An empty string, a page with no tables, and a page whose table
    is something else all produce :class:`TrendlyneTableNotFoundError` from all three extractors —
    the typed failure this module has always used, and no new exception class.
    """

    EMPTY = (
        "",
        "<html><body><p>no tables here</p></body></html>",
        (
            "<table><tr><th>Ex-Date</th><th>Bonus Ratio</th><th>Record Date</th></tr>"
            "<tr><td>01 Oct 2026</td><td>1:2</td><td>10 Oct 2026</td></tr></table>"
        ),
    )

    @pytest.mark.parametrize("page", EMPTY)
    def test_share_holding_tables_refuses(self, page: str) -> None:
        with pytest.raises(TrendlyneTableNotFoundError):
            tbt.share_holding_tables(page)

    @pytest.mark.parametrize("page", EMPTY)
    def test_buy_sell_zone_tables_refuses(self, page: str) -> None:
        with pytest.raises(TrendlyneTableNotFoundError):
            tbt.buy_sell_zone_tables(page)

    @pytest.mark.parametrize("page", EMPTY)
    def test_monthly_mf_holding_table_refuses(self, page: str) -> None:
        with pytest.raises(TrendlyneTableNotFoundError):
            tbt.monthly_mf_holding_table(page)

    def test_the_refusal_is_a_trendlyne_error_and_names_what_was_missing(self) -> None:
        from bfinance.trendlyne.errors import TrendlyneError

        with pytest.raises(TrendlyneError) as excinfo:
            tbt.monthly_mf_holding_table("<html></html>")
        message = str(excinfo.value)
        assert "MF" in message, "the caller should be told what was looked for"
        assert "<no tables at all>" in message, "and what the page actually had"

    def test_the_three_extractors_do_not_share_a_collector_by_accident(self) -> None:
        """Each raises on the *other's* page. A shared 'grab the first table that looks like
        something' path would return a real frame here instead."""
        with pytest.raises(TrendlyneTableNotFoundError):
            tbt.share_holding_tables(text("buy_sell_zone_tcs.html"))
        with pytest.raises(TrendlyneTableNotFoundError):
            tbt.monthly_mf_holding_table(text("shareholding_tcs.html"))
        with pytest.raises(TrendlyneTableNotFoundError):
            tbt.buy_sell_zone_tables(text("monthly_mf_holding_tcs.html"))