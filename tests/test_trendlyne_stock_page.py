"""
trendlyne symbol-page sections — fully offline, driven by one 2026-10-21 recording.

``stock_page_motherson_sections.html`` is the **whole** 294,410-byte MOTHERSON symbol page,
stored verbatim; ``META.json`` records its URL, digest and why its notes block is longer than most.
It exists because six sections of data sit in the document every OHLC call already downloads, so
``stock_page_snapshot`` is supposed to cost **zero** requests after any OHLC or token call and one
page fetch on a cold client. That claim is what most of this file measures.

Why a whole page and not six excerpts
-------------------------------------
A per-section fixture would test the parsers against a page that no client will ever fetch, and the
one thing this feature can get wrong is the *fetch seam*: the sections are parsed inside
``ohlc_token_async`` and cached beside the token, so the failure that matters is a token fetch that
somehow does not leave a snapshot behind. Six excerpts cannot show that. The cost is a 294 KB
fixture whose ``find_tables`` sweep is vacuously true — the page carries **no** ``<table>`` at all,
which is itself why the sections are read from ``data-*`` attributes rather than from tables.

What is asserted, and why each one is worth a test
--------------------------------------------------
- **Zero marginal requests**, in both directions: a snapshot after a token costs nothing and a
  token after a snapshot costs nothing. The second direction is the one a cache implemented on the
  wrong side of the fetch would fail.
- **The measured trap**: all four performance rows print ``ltp`` 157.3 while the same page's
  ``data-currentprice`` is 157.27. A parser that "helpfully" replaced the print with the quote
  would destroy the only fact this block states.
- **The published-only-when-published fields**: Graham's ``pass_pct`` is missing while its rank
  sentence still says "60.0% pass", and the recommendation counts arrive boxed as
  ``{"value": 12.0, "class": "d_buy"}`` where their siblings are plain numbers.
- **One section breaking costs only that section**, and the failure travels in ``attrs`` rather
  than being swallowed, because an empty frame that does not say why is read as "the vendor
  published nothing".
- **A bug in this module is not relabelled as a vendor change**, which is the one behaviour the
  narrow ``except`` exists for and the one a widened ``except`` would silently take away.
"""

from __future__ import annotations

from typing import Any, Tuple

import httpx
import pandas as pd
import pytest

from bfinance.trendlyne import client as tl
from bfinance.trendlyne.errors import TrendlyneSchemaError

# The mock-transport seam and the shared recorder are imported rather than copied: two copies of a
# request stub can drift into testing different things. `tests/` is not a package, so the modules
# are imported by bare name exactly as pytest imports them.
from test_trendlyne_core import text
from test_trendlyne_sections import RECORD, SYMBOL, Recorder, client_for

#: The whole-page recording. Read once: every test that builds a recorder needs the same bytes.
PAGE = "stock_page_motherson_sections.html"
HTML = text(PAGE)

#: The stock page's own path, taken from the sitemap record rather than written out, so a template
#: change fails here instead of being agreed on by accident by both the code and the test.
PAGE_NEEDLE = RECORD.page_path

#: The token the *trimmed* 2026-09-27 fixture carries. Its only job here is to prove that a page
#: with none of the six sections still yields a token -- i.e. that the section parse cannot break
#: the price path it was added to.
OLD_FIXTURE_TOKEN = "TQZITAIRHO5FCC7SYAKGO6M7QA======"


def page_client(html: str = HTML, **kwargs: Any) -> Tuple[tl.TrendlyneClient, Recorder]:
    """A mock-backed, index-warm client whose stock page is ``html``.

    The index is warm because a cold one adds a sitemap request that has nothing to do with the
    sections; :class:`TestTheSixSectionsAreOnePage` measures the page fetch on its own.
    """
    recorder = Recorder(overrides={PAGE_NEEDLE: lambda request: httpx.Response(200, text=html)})
    return client_for(recorder, **kwargs), recorder


@pytest.fixture
def client() -> tl.TrendlyneClient:
    return page_client()[0]


# One parse per section for the whole module: each ``parse_stock_page_*`` builds a soup over the
# same 294 KB, and running that six times per test class would put ~2 s of parsing in the way of
# assertions that are about shapes and values, not about speed.
@pytest.fixture(scope="module")
def metrics() -> pd.DataFrame:
    return tl.parse_stock_page_metrics(HTML)


@pytest.fixture(scope="module")
def performance() -> pd.DataFrame:
    return tl.parse_stock_page_performance(HTML)


@pytest.fixture(scope="module")
def consensus() -> pd.DataFrame:
    return tl.parse_stock_page_consensus(HTML)


@pytest.fixture(scope="module")
def recommendations() -> pd.DataFrame:
    return tl.parse_stock_page_recommendations(HTML)


@pytest.fixture(scope="module")
def valuation() -> pd.DataFrame:
    return tl.parse_stock_page_valuation(HTML)


@pytest.fixture(scope="module")
def checklists() -> pd.DataFrame:
    return tl.parse_stock_page_checklists(HTML)


# ============================================================ the request contract


class TestTheSixSectionsAreOnePage:
    """The whole point: these six blocks cost no request of their own."""

    def test_all_six_keys_in_the_declared_order(self, client: tl.TrendlyneClient):
        frames = client.stock_page_snapshot(SYMBOL)
        assert tuple(frames) == tl.STOCK_PAGE_SECTIONS
        assert tuple(frames) == (
            "key_metrics",
            "price_performance",
            "consensus",
            "recommendations",
            "valuation",
            "checklists",
        )

    def test_a_snapshot_after_the_token_costs_no_request(self):
        client, recorder = page_client()
        client.ohlc_token(SYMBOL)
        spent_before = len(recorder.calls)
        frames = client.stock_page_snapshot(SYMBOL)
        assert len(recorder.calls) == spent_before, (
            f"a snapshot after a token fetch made {recorder.paths[spent_before:]} of its own; the "
            f"sections are in the page the token came out of"
        )
        assert frames["key_metrics"].attrs["bfinance_trendlyne_requests"] == 0

    def test_the_token_after_a_snapshot_costs_no_request(self):
        """The reverse direction, which a snapshot cached outside the fetch could not honour."""
        client, recorder = page_client()
        client.stock_page_snapshot(SYMBOL)
        spent_before = len(recorder.calls)
        assert client.ohlc_token(SYMBOL)
        assert len(recorder.calls) == spent_before

    def test_a_cold_snapshot_costs_exactly_the_page_the_token_needs(self):
        client, recorder = page_client()
        frames = client.stock_page_snapshot(SYMBOL)
        assert recorder.paths == [RECORD.page_path], (
            f"a cold snapshot made {recorder.paths}; it may only spend the one page whose token "
            f"the price path needs anyway"
        )
        assert frames["consensus"].attrs["bfinance_trendlyne_requests"] == 1

    def test_force_refresh_spends_the_page_again_and_says_so(self):
        client, recorder = page_client()
        client.stock_page_snapshot(SYMBOL)
        frames = client.stock_page_snapshot(SYMBOL, force_refresh=True)
        assert recorder.paths == [RECORD.page_path, RECORD.page_path]
        assert frames["valuation"].attrs["bfinance_trendlyne_requests"] == 1

    def test_clear_caches_forgets_the_sections_too(self):
        """A cleared token cache that kept its sections would serve frames for an unfetched page."""
        client, recorder = page_client()
        client.stock_page_snapshot(SYMBOL)
        client.clear_caches()
        client.stock_page_snapshot(SYMBOL)
        assert recorder.paths == [RECORD.page_path, tl.SITEMAP_PATH, RECORD.page_path]

    def test_the_returned_frames_are_copies_not_the_cache(self, client: tl.TrendlyneClient):
        first = client.stock_page_snapshot(SYMBOL)
        first["key_metrics"].loc[0, "value"] = -1.0
        second = client.stock_page_snapshot(SYMBOL)
        assert second["key_metrics"].loc[0, "value"] == 165989.7

    def test_every_frame_declares_itself_a_snapshot_of_a_named_section(
        self, client: tl.TrendlyneClient
    ):
        frames = client.stock_page_snapshot(SYMBOL)
        for key, frame in frames.items():
            assert frame.attrs[tl.SNAPSHOT_ONLY_ATTR] is True, key
            assert frame.attrs["bfinance_trendlyne_page_section"] == key
            assert frame.attrs["bfinance_trendlyne_section_status"] == "parsed", key
            assert frame.attrs["bfinance_trendlyne_sections_failed"] == {}, key
            assert frame.attrs["bfinance_symbol"] == SYMBOL, key
            assert frame.attrs[tl.SOURCE_ATTR] == RECORD.page_url, key


# ============================================================ the sections themselves


class TestKeyMetrics:
    def test_the_fifteen_records_of_the_grid(self, metrics: pd.DataFrame):
        assert list(metrics.columns) == [
            "metric", "value", "unit", "param", "industry_rank", "insight", "color", "chartable",
        ]
        assert len(metrics) == 15
        assert list(metrics["param"]) == [
            "MCAP_Q", "PE_TTM", "PEG_TTM", "PBV_A", "INSTIHOLD", "REV4Q_Q", "SR_TTM_GROWTH",
            "NP_Q_GROWTH", "NP_TTM_GROWTH", "OPMPCT_Q", "OPMPCT_TTM", "PITROSKI_F",
            "relp_nifty50_qtrChangeP", "relp_sector_qtrChangeP", "ROE_A",
        ]

    def test_the_values_a_yfinance_consumer_reaches_for_first(self, metrics: pd.DataFrame):
        by_param = metrics.set_index("param")
        assert by_param.loc["MCAP_Q", "value"] == 165989.7
        assert by_param.loc["PE_TTM", "value"] == 37.9
        assert by_param.loc["PITROSKI_F", "value"] == 5.0
        assert by_param.loc["ROE_A", "value"] == 9.4

    def test_the_unit_is_the_vendors_and_the_ratios_carry_none(self, metrics: pd.DataFrame):
        by_param = metrics.set_index("param")
        assert by_param.loc["MCAP_Q", "unit"] == "Cr."
        assert by_param.loc["PE_TTM", "unit"] == ""

    def test_the_units_sentence_and_the_accounting_basis_travel_in_attrs(
        self, metrics: pd.DataFrame
    ):
        assert metrics.attrs["bfinance_trendlyne_units"] == (
            "All financials are in INR Cr and price data in INR"
        )
        assert metrics.attrs["bfinance_trendlyne_consolidated"] is True
        assert metrics.attrs["bfinance_trendlyne_bonus_adjusted"] is False

    def test_the_two_relative_performance_rows_are_not_given_a_comparison(
        self, metrics: pd.DataFrame
    ):
        by_param = metrics.set_index("param")
        for param in ("relp_nifty50_qtrChangeP", "relp_sector_qtrChangeP"):
            assert pd.isna(by_param.loc[param, "industry_rank"]), param
            assert pd.isna(by_param.loc[param, "insight"]), param

    def test_a_missing_grid_is_a_schema_change_not_an_empty_section(self):
        html = HTML.replace('id="stock_key_metrics"', 'id="stock_key_metrics_disabled"')
        with pytest.raises(TrendlyneSchemaError, match="stock_key_metrics"):
            tl.parse_stock_page_metrics(html)


class TestPricePerformance:
    def test_the_four_ranges_in_page_order(self, performance: pd.DataFrame):
        assert list(performance.columns) == [
            "period", "low", "high", "change_pct", "last_price", "label", "color",
        ]
        assert list(performance["period"]) == [
            "Day Price Range", "Week Price Range", "Month Price Range", "52 Week Price Range",
        ]

    def test_the_52_week_row_is_the_range_and_the_one_year_return(
        self, performance: pd.DataFrame
    ):
        by_period = performance.set_index("period")
        assert by_period.loc["52 Week Price Range", "low"] == 101.1
        assert by_period.loc["52 Week Price Range", "high"] == 173.3
        assert by_period.loc["52 Week Price Range", "change_pct"] == 48.69

    def test_the_last_price_is_the_rounded_print_not_the_quote(self):
        """The trap this block sets, asserted against the page's own quote in another section."""
        client, _ = page_client()
        frames = client.stock_page_snapshot(SYMBOL)
        assert set(frames["price_performance"]["last_price"]) == {157.3}
        assert frames["consensus"].attrs["bfinance_trendlyne_current_price"] == 157.27, (
            "the page's own quote is 157.27; the ranges' ltp is a rounded print of it. If these "
            "two ever agree, the parser has started substituting one for the other"
        )


class TestConsensusEstimates:
    def test_one_long_frame_over_every_statement_and_period(self, consensus: pd.DataFrame):
        assert list(consensus.columns) == [
            "statement", "period_type", "period", "period_end", "actual", "average", "high",
            "low", "median", "analysts", "surprise_pct", "growth_pct", "is_past", "is_current",
            "forecast",
        ]
        assert len(consensus) == 116
        assert set(consensus["period_type"]) == {"QUARTER", "ANNUAL"}

    def test_the_vendors_statement_names_are_kept_apart(self, consensus: pd.DataFrame):
        """``EPS`` and ``CASH_EPS`` are two series; collapsing them would silently pick one."""
        statements = set(consensus["statement"])
        assert {"EPS", "CASH_EPS"} <= statements
        assert len(statements) == 14

    def test_the_target_price_forecast_is_here_and_flagged_as_forecast(
        self, consensus: pd.DataFrame
    ):
        """The block the plan believed was behind a 404 on /equity/consensus-estimates/."""
        forecast = consensus[consensus["forecast"]]
        assert len(forecast) == 2
        assert set(forecast["statement"]) == {"TARGET_PRICE"}
        assert set(forecast["period"]) == {"FY27", "Q3FY27"}
        assert set(forecast["average"]) == {174.95}
        assert set(forecast["high"]) == {210.0}
        assert set(forecast["low"]) == {115.0}
        assert set(forecast["analysts"]) == {22.0}
        assert set(forecast["is_past"]) == {False}

    def test_the_recommendation_list_is_named_rather_than_dropped_silently(
        self, consensus: pd.DataFrame
    ):
        assert consensus.attrs["bfinance_trendlyne_unread_statements"] == ["RECOMMENDATION"]
        assert "RECOMMENDATION" not in set(consensus["statement"])

    def test_the_vendors_per_statement_insight_is_carried_once_not_per_row(
        self, consensus: pd.DataFrame
    ):
        insights = consensus.attrs["bfinance_trendlyne_insights"]
        assert insights["EPS"]["QUARTER"] == (
            "Samvardhana Motherson's EPS was higher than average estimate 2 times in past 3 "
            "quarters EPS is expected to grow by 17.3% in Q2FY27"
        )
        assert insights["TARGET_PRICE"]["ANNUAL"].endswith("upside of 11.2%")

    def test_a_past_print_and_a_forward_estimate_are_not_confused(
        self, consensus: pd.DataFrame
    ):
        """``actual`` is a print, ``average`` a forecast, and nothing is inferred from a date."""
        forecast = consensus[consensus["forecast"]]
        assert forecast["actual"].isna().all()
        printed = consensus[consensus["is_past"] & consensus["actual"].notna()]
        assert len(printed) > 0, "the recorded page carries past prints; if none parse, this is red"

    def test_a_payload_with_no_series_at_all_is_a_schema_error(self):
        html = HTML.replace("data-consensusjson=", "data-consensusjson-broken=")
        with pytest.raises(TrendlyneSchemaError, match="consensus"):
            tl.parse_stock_page_consensus(html)


class TestAnalystRecommendations:
    def test_the_six_monthly_snapshots(self, recommendations: pd.DataFrame):
        assert list(recommendations.columns) == [
            "as_of", "strong_buy", "buy", "hold", "sell", "strong_sell", "no_opinion",
            "analysts", "reco_value", "reco_text", "average_reco_text", "insight",
        ]
        assert len(recommendations) == 6
        assert list(recommendations["as_of"]) == [
            "2026-05-31", "2026-06-29", "2026-07-31", "2026-08-31", "2026-09-30", "2026-10-21",
        ]

    def test_the_boxed_counts_are_published_as_counts(self, recommendations: pd.DataFrame):
        """``RECOMMENDATION`` boxes each count as ``{"value": n, "class": "buy"}``."""
        first = recommendations.iloc[0]
        assert (first["strong_buy"], first["buy"], first["hold"]) == (12.0, 6.0, 1.0)
        assert (first["sell"], first["strong_sell"], first["no_opinion"]) == (0.0, 2.0, 1.0)
        assert not isinstance(first["strong_buy"], dict), (
            "a column named strong_buy must hold a count, not the vendor's colour box"
        )

    def test_the_recommendation_value_and_its_text_are_carried_verbatim(
        self, recommendations: pd.DataFrame
    ):
        assert recommendations.iloc[0]["reco_value"] == 1.76
        assert recommendations.iloc[-1]["reco_value"] == 1.82
        assert set(recommendations["reco_text"]) == {"BUY"}
        assert set(recommendations["average_reco_text"]) == {"Outperform"}

    def test_the_insight_sentence_is_the_vendors_own(self, recommendations: pd.DataFrame):
        assert recommendations.iloc[0]["insight"] == (
            "The consensus recommendation from 21 analysts for Samvardhana Motherson is BUY"
        )


class TestPeValuation:
    def test_the_two_bases_and_their_fair_prices(self, valuation: pd.DataFrame):
        assert list(valuation.columns) == [
            "basis", "heading", "title", "fair_price", "fair_price_basis", "upside_pct",
            "upside_basis", "state",
        ]
        assert list(valuation["basis"]) == ["current", "future"]
        assert list(valuation["fair_price"]) == [184.3, 236.3]
        assert list(valuation["upside_pct"]) == [17.2, 50.3]
        assert list(valuation["state"]) == ["Undervalued", "Undervalued"]

    def test_the_derivation_is_stated_in_the_row_it_belongs_to(self, valuation: pd.DataFrame):
        assert list(valuation["fair_price_basis"]) == [
            "Based on 5Yr Average PE", "Based on 1Yr Forward EPS",
        ]

    def test_the_insight_and_the_tier_two_route_are_recorded_not_fetched(
        self, valuation: pd.DataFrame
    ):
        assert valuation.attrs["bfinance_trendlyne_insight"] == (
            "Samvardhana Motherson is undervalued at both current PE and future earnings estimates."
        )
        assert valuation.attrs["bfinance_trendlyne_buy_sell_zone_url"] == (
            "https://trendlyne.com/tools/buy-sell-zone/878/MOTHERSON/"
            "samvardhana-motherson-international-ltd/"
        )

    def test_breaking_the_second_attribute_is_a_schema_error(self):
        html = HTML.replace("data-pe-future-value=", "data-pe-future-value-broken=")
        with pytest.raises(TrendlyneSchemaError, match="data-pe-future-value"):
            tl.parse_stock_page_valuation(html)


class TestChecklists:
    def test_the_two_checklists_as_counts(self, checklists: pd.DataFrame):
        assert list(checklists.columns) == [
            "checklist", "title", "passed", "failed", "total", "pass_pct", "rank", "insight",
        ]
        assert list(checklists["checklist"]) == ["trendlyne", "graham"]
        assert list(checklists["passed"]) == [14, 6]
        assert list(checklists["failed"]) == [9, 4]
        assert list(checklists["total"]) == [23, 10]

    def test_a_percentage_published_only_as_prose_is_left_missing(
        self, checklists: pd.DataFrame
    ):
        """Graham's ``checklistP`` is absent while its rank sentence still states 60.0%."""
        by_name = checklists.set_index("checklist")
        assert pd.isna(by_name.loc["graham", "pass_pct"]), (
            "the vendor publishes Graham's percentage only inside a sentence; parsing it back out "
            "would be a regex over prose, and 6/10 is the same fact"
        )
        assert by_name.loc["graham", "rank"] == "High rank - 60.0% pass in checklist"
        assert by_name.loc["trendlyne", "pass_pct"] == pytest.approx(60.869565)


# ============================================================ one section breaking


class TestOneBadSectionDoesNotCostTheOtherFive:
    def test_a_renamed_checklist_element_costs_only_the_checklist(self):
        html = HTML.replace('id="trendlyne_checklist"', 'id="trendlyne_checklist_renamed"')
        client, _ = page_client(html)
        frames = client.stock_page_snapshot(SYMBOL)

        assert tuple(frames) == tl.STOCK_PAGE_SECTIONS, "a broken section must not drop its key"
        broken = frames["checklists"]
        assert broken.empty
        assert list(broken.columns) == [
            "checklist", "title", "passed", "failed", "total", "pass_pct", "rank", "insight",
        ]
        assert broken.attrs["bfinance_trendlyne_section_status"] == "unreadable"
        assert "trendlyne_checklist" in broken.attrs["bfinance_trendlyne_section_error"]
        assert len(frames["key_metrics"]) == 15
        assert len(frames["consensus"]) == 116

    def test_the_failure_is_on_every_frame_not_only_the_broken_one(self):
        html = HTML.replace('id="trendlyne_checklist"', 'id="trendlyne_checklist_renamed"')
        client, _ = page_client(html)
        frames = client.stock_page_snapshot(SYMBOL)
        for key, frame in frames.items():
            failed = frame.attrs["bfinance_trendlyne_sections_failed"]
            assert set(failed) == {"checklists"}, key
            assert "TrendlyneSchemaError" in failed["checklists"], key

    def test_an_unreadable_section_is_never_an_empty_vendor_section(self):
        """``rows == 0`` plus ``status == 'unreadable'``; the two must not be confusable."""
        html = HTML.replace("data-pe-future-value=", "data-pe-future-value-broken=")
        client, _ = page_client(html)
        frames = client.stock_page_snapshot(SYMBOL)
        unreadable = frames["valuation"]
        assert unreadable.attrs["bfinance_trendlyne_section_status"] == "unreadable"
        assert unreadable.attrs["bfinance_trendlyne_rows"] == 0
        assert frames["checklists"].attrs["bfinance_trendlyne_section_status"] == "parsed"
        assert frames["checklists"].attrs["bfinance_trendlyne_rows"] == 2

    def test_a_bug_in_this_module_is_not_relabelled_as_a_vendor_change(self, monkeypatch):
        """
        The narrow ``except`` is the guard: only ``TrendlyneError`` and ``ValueError`` mean "the
        page changed shape". A ``RuntimeError`` is this module's own defect and must escape.
        """

        def boom(*args: Any, **kwargs: Any) -> pd.DataFrame:
            raise RuntimeError("a defect, not a vendor change")

        monkeypatch.setitem(tl._STOCK_PAGE_PARSERS, "key_metrics", boom)
        client, _ = page_client()
        with pytest.raises(RuntimeError, match="a defect"):
            client.stock_page_snapshot(SYMBOL)


class TestThePageFetchIsStillTheTokenFetch:
    def test_a_page_with_none_of_the_sections_still_yields_its_token(self):
        """The 2026-09-27 trimmed page carries the token and no section at all."""
        client, recorder = page_client(text("stock_page_motherson.html"))
        assert client.ohlc_token(SYMBOL) == OLD_FIXTURE_TOKEN
        frames = client.stock_page_snapshot(SYMBOL)
        assert recorder.paths == [RECORD.page_path]
        for key, frame in frames.items():
            assert frame.attrs["bfinance_trendlyne_section_status"] == "unreadable", key
            assert len(frame.columns) > 0, key
        assert set(frames["key_metrics"].attrs["bfinance_trendlyne_sections_failed"]) == set(
            tl.STOCK_PAGE_SECTIONS
        )
