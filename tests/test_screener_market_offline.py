"""
``/market/`` offline tests — the Industries Overview and leaf-industry constituents.

Driven by three recordings in ``tests/fixtures/screener/``: ``market.html`` (the whole-market
overview, 177 KB on the wire), and ``industry_in040107001.html`` / ``industry_in060101001.html``
(a *complete* leaf and a *truncated* one). ``META.json`` records each one's URL, status, byte
count, SHA-256 prefix, capture time and exact trim rule; see
:mod:`tests.test_screener_offline` for that convention and for the company-page surface.

**What these tests are for.** Two new public methods were added to ``ScreenerClient`` on the
strength of three live requests, and both of them are easy to get quietly wrong:

* ``/market/`` **repeats its header row 13 times** inside one 188-row table, with no ``<thead>``
  and no attribute to tell a header from a row. A parser that slices ``rows[1:]`` emits twelve
  fake industries called "Industry" with eight empty aggregates. That is the first thing pinned
  here.
* The constituent route is **permanently partial** — the page's own pager uses ``?page=`` and
  ``?limit=``, which ``robots.txt`` disallows, and the bulk export answers 403. A frame that
  looked complete while holding 25 of 218 companies is the failure mode, so partiality is
  asserted as visible rather than tolerated.

**No test here touches the network.** ``ScreenerClient._send_with_retry`` — the single seam every
request goes through — is replaced by a stub that serves the recorded bytes, so a broken parser
still fails these assertions.
"""

from __future__ import annotations

import json
import re

import pandas as pd
import pytest
from bs4 import BeautifulSoup

from bfinance.screener.client import ScreenerClient
from bfinance.screener.parser import MARKET_AGGREGATE_COLUMNS, ScreenerHTMLParser
from bfinance.utils.formatting import parse_indian_number

# The transport stub, the route builder and the fixture reader are shared with the company-page
# module rather than copied: `_send_with_retry` is the single seam every request goes through, and
# two copies of it could drift into testing different things. `tests/` is not a package (no
# `__init__.py`), and pytest prepends *its own* directory to `sys.path` — so the module is imported
# by bare name here, exactly as pytest imports it, rather than as `tests.test_screener_offline`,
# which resolves only when something else happens to put the repo root on the path.
from test_screener_offline import offline_client, routes, text

#: the two recorded leaves, by the ``{slug}: (code, url)`` the fixtures were taken from
LEAVES = {
    "in040107001": "IN040107001",   # complete: 5 results, page 1 of 1
    "in060101001": "IN060101001",   # truncated: 218 results, page 1 of 9 -> 25 rows
}

#: ``/market/``'s own per-leaf "No. of Companies" for those two leaves, same day, same recording.
#: Pharmaceuticals is the interesting one: the overview says 159 and the leaf page says 218.
MARKET_COUNTS = {"in040107001": 5.0, "in060101001": 159.0}


def market_html() -> str:
    return text("market.html")


def leaf_html(slug: str) -> str:
    return text(f"industry_{slug}.html")


#: ``div[data-page-info]`` as bs4 re-serialises it (an attribute with no value gains ``=""``), so
#: the pattern cannot hard-code the opening tag. Used to build the "pagination state removed"
#: variants below without re-recording the page.
_PAGE_INFO_BLOCK = re.compile(r"<div[^>]*data-page-info[^>]*>.*?</div>", re.DOTALL)


def without_page_info(slug: str) -> str:
    stripped = _PAGE_INFO_BLOCK.sub("", leaf_html(slug))
    assert "data-page-info" not in stripped, f"industry_{slug}.html lost its pagination block"
    return stripped


def market() -> pd.DataFrame:
    return ScreenerHTMLParser().parse_market_industries(market_html())


def leaf_path(slug: str) -> str:
    return f"/market/{ScreenerClient._leaf_url(LEAVES[slug]).split('/market/', 1)[1]}"


def market_route() -> tuple[str, int, str]:
    return ("/market/", 200, market_html())


def leaf_routes(*slugs: str) -> list[tuple[str, int, str]]:
    return [(leaf_path(s), 200, leaf_html(s)) for s in slugs]


# ============================================================ the recorded response shapes


class TestRecordedShapes:
    """The response shape this code was written against, re-measured from the fixtures.

    Every assertion here is about the *recording*, not about the parser, so a change in screener.in
    that these tests catch arrives as "the page changed" rather than as a mysterious parser bug.
    """

    def test_the_overview_is_one_table_with_no_thead(self):
        soup = BeautifulSoup(market_html(), "lxml")
        tables = soup.find_all("table")
        assert len(tables) == 1
        assert tables[0].find("thead") is None
        assert "data-table" in tables[0]["class"] and "striped" in tables[0]["class"]

    def test_the_overview_repeats_its_header_and_the_rows_are_188(self):
        """The defect this parser had to survive: 201 ``<tr>`` for 188 industries.

        The header is a ``<tr>`` of ten ``<th>`` and it is re-emitted every 16 rows. A parser that
        treated "first row only" as the header would be right for one row and wrong for 12.
        """
        table = BeautifulSoup(market_html(), "lxml").find("table", class_="data-table")
        rows = table.find_all("tr")
        headers = [i for i, tr in enumerate(rows) if tr.find("th") and not tr.find("td")]
        assert len(rows) == 201
        assert headers == [0, 16, 32, 48, 64, 80, 96, 112, 128, 144, 160, 176, 192]
        assert len(rows) - len(headers) == 188

    def test_no_tr_carries_an_attribute_that_identifies_a_header(self):
        """Every structural signal a parser could use is absent or useless here.

        There is no ``<thead>`` to find, no attribute on any ``<tr>``, and the one ``<tbody>``
        contains the header rows *and* the data rows together — so it separates nothing. That is
        why the parser requires both a leaf link and a cell count rather than a position.
        """
        table = BeautifulSoup(market_html(), "lxml").find("table", class_="data-table")
        assert table.find("thead") is None
        assert all(not tr.attrs for tr in table.find_all("tr"))
        bodies = table.find_all("tbody")
        assert len(bodies) == 1, "the wire page has exactly one <tbody>"
        assert any(tr.find("th") for tr in bodies[0].find_all("tr"))
        assert any(tr.find("td") for tr in bodies[0].find_all("tr")), (
            "headers and data share one <tbody>, which is the point"
        )

    def test_every_industry_row_is_ten_cells_linking_to_a_four_level_leaf(self):
        table = BeautifulSoup(market_html(), "lxml").find("table", class_="data-table")
        widths = {len(tr.find_all("td")) for tr in table.find_all("tr") if tr.find("td")}
        assert widths == {10}
        hrefs = [a["href"] for a in table.select("a[href^='/market/']")]
        assert len(hrefs) == 188 == len(set(hrefs)), "188 distinct leaves, no duplicates"
        for href in hrefs:
            segments = href.strip("/").split("/")[1:]
            assert len(segments) == 4, href
            assert all(re.fullmatch(r"[A-Z0-9]+", s) for s in segments), href

    def test_the_aggregate_columns_are_named_as_documented(self):
        """Ten ``<th>``: S.No., Industry, then the eight the parser names — in this order.

        Asserted verbatim. The parser reads cells by position, so a *re-order* upstream would
        silently permute the eight aggregate columns; the two tests below catch that per-row, and
        this one names what it was written against so a change is a visible diff.
        """
        table = BeautifulSoup(market_html(), "lxml").find("table", class_="data-table")
        labels = [th.get_text(strip=True) for th in table.find("tr").find_all("th")]
        assert labels == [
            "S.No.", "Industry", "No. of Companies", "Total Market Cap.", "Median Market Cap.",
            "Median P/E", "Sales Growth", "OPM", "Wtd. Avg ROCE", "Median 1Y Return",
        ]
        assert len(MARKET_AGGREGATE_COLUMNS) == 8 == len(labels) - 2

    def test_the_leaf_pages_carry_the_pagination_state_in_data_page_info(self):
        for slug, expected in (("in040107001", "5 results found: Showing page 1 of 1"),
                               ("in060101001", "218 results found: Showing page 1 of 9")):
            node = BeautifulSoup(leaf_html(slug), "lxml").find(attrs={"data-page-info": True})
            assert node is not None, slug
            assert " ".join(node.get_text(" ", strip=True).split()) == expected

    def test_the_long_leaf_repeats_its_header_mid_table(self):
        """Row 16 of the Pharmaceuticals page is a second header — the trap ``_parse_peers``
        survives by accident (such a row has no ``<td>``, so the width guard skips it). Pinned so
        that accident is a known, tested fact rather than a surprise."""
        table = BeautifulSoup(leaf_html("in060101001"), "lxml").find("table", class_="data-table")
        rows = table.find_all("tr")
        headers = [i for i, tr in enumerate(rows) if tr.find("th") and not tr.find("td")]
        assert headers == [0, 16]
        assert len(rows) - len(headers) == 25

    def test_both_leaf_pages_are_eleven_columns_with_a_company_href(self):
        for slug, n in (("in040107001", 5), ("in060101001", 25)):
            table = BeautifulSoup(leaf_html(slug), "lxml").find("table", class_="data-table")
            widths = {len(tr.find_all("td")) for tr in table.find_all("tr") if tr.find("td")}
            assert widths == {11}, slug
            assert len(table.select("a[href^='/company/']")) == n


# ============================================================ parse_market_industries


class TestMarketIndustries:
    def test_it_returns_188_leaves_indexed_by_code(self):
        df = market()
        assert df.shape == (188, 10)
        assert df.index.name == "industry_code"
        assert df.index.is_unique
        assert "IN060101001" in df.index

    def test_the_columns_are_the_name_the_path_and_the_eight_aggregates(self):
        assert list(market().columns) == ["industry", "code_path", *MARKET_AGGREGATE_COLUMNS]

    def test_a_known_row_carries_the_numbers_the_page_printed(self):
        """Values for **one** row, pinned as a transcription of the page.

        One row is enough to prove the column-to-field mapping end to end (the peers tests cover
        that exhaustively for all 188); pinning all 188 x 8 numbers would be a second copy of the
        fixture that can only ever be wrong in a new way. What is checked here is that the 11
        cells of *that* row land in the right fields.
        """
        table = BeautifulSoup(market_html(), "lxml").find("table", class_="data-table")
        row = next(tr for tr in table.find_all("tr")
                   if tr.find("a", href="/market/IN02/IN0201/IN020101/IN020101002/"))
        cells = [td.get_text(strip=True) for td in row.find_all("td")]
        got = market().loc["IN020101002"]
        assert got["industry"] == cells[1] == "2/3 Wheelers"
        assert got["code_path"] == "IN02/IN0201/IN020101/IN020101002"
        for field, cell in zip(MARKET_AGGREGATE_COLUMNS, cells[2:]):
            # `parse_indian_number` rather than float(): the cells carry Indian digit grouping and a
            # "%" suffix, and using the same primitive is what makes this a test of *which cell
            # lands in which field* rather than a second, weaker number parser.
            assert got[field] == pytest.approx(parse_indian_number(cell)), f"{field} mis-mapped"

    def test_indian_grouped_market_caps_are_un_grouped_not_truncated(self):
        """'8,96,944' is 896,944 — a comma-stripping parser that ignored grouping would read 8.96
        or 896944 differently. The leaf whose total market cap is 8.96 lakh crore is the proof."""
        assert market().loc["IN020101002", "total_market_cap"] == pytest.approx(896944.0)

    def test_the_percent_sign_rides_inside_the_cell(self):
        """Percent cells are ``<td>30<span class="sub">%</span>`` — the ``%`` is a child element.

        So a parser that read ``td.contents[0]`` or ``td.string`` would get the number only, and one
        that took the first child *text node* would get whitespace. ``get_text`` is the only read
        that works, and this is the row that shows it.
        """
        table = BeautifulSoup(market_html(), "lxml").find("table", class_="data-table")
        row = next(tr for tr in table.find_all("tr")
                   if tr.find("a", href="/market/IN02/IN0201/IN020101/IN020101002/"))
        cell = row.find_all("td")[6]  # Sales Growth
        assert cell.find("span", class_="sub").get_text(strip=True) == "%"
        assert market().loc["IN020101002", "sales_growth"] == pytest.approx(30.0)

    def test_the_repeated_header_rows_are_skipped_not_emitted(self):
        """The 13 repeated headers must not become 13 industries called "Industry"."""
        df = market()
        assert "Industry" not in set(df["industry"])
        assert not df["industry"].isna().any()
        assert df.attrs["bfinance_market_skipped_rows"] == 13
        assert df.attrs["bfinance_market_row_count"] == 188

    def test_an_unreported_aggregate_is_none_and_not_zero(self):
        """screener.in prints an empty cell where it has no median. A 0.0 would be a fabricated
        valuation — a screen for ``median_pe < 10`` would then pick these industries up."""
        df = market()
        assert df["median_pe"].isna().sum() == 2
        assert df["median_1y_return"].isna().sum() == 2
        assert 0.0 not in set(df["median_pe"].dropna())
        for code in df.index[df["median_pe"].isna()]:
            assert df.loc[code, "no_of_companies"] > 0, "the row itself is real, only the median is blank"

    def test_every_leaf_index_matches_the_last_segment_of_its_own_path(self):
        """The index is derived from the path, so the two cannot drift apart."""
        for code, path in zip(market().index, market()["code_path"]):
            assert code == path.rsplit("/", 1)[-1]
            assert code.startswith(path.split("/")[0])

    def test_the_parent_codes_are_prefixes_of_the_leaf_so_a_path_is_derivable(self):
        """screener.in's numbering: 4 / 6 / 8 / 11 characters, each parent a prefix of its child.

        This is the invariant :meth:`ScreenerClient._leaf_url` relies on to expand a bare leaf
        code into a full path without a lookup request, so it is pinned here against all 188
        leaves — if screener.in ever renumbers, the 12 expansions below start 404ing and this
        fails first.
        """
        bad = []
        for code, row in market().iterrows():
            derived = "/".join((code[:4], code[:6], code[:8], code))
            if derived != row["code_path"]:
                bad.append((code, row["code_path"], derived))
        assert not bad, f"the code-prefix rule no longer holds: {bad[:5]}"
        assert set(market().index.str.len()) == {11}

    def test_the_attrs_carry_the_row_counts_and_the_conservative_company_total(self):
        """The sum of this page's own counts is 3,319 — the most conservative of the three
        universes screener.in publishes — and it is recorded under a key that names its basis."""
        df = market()
        assert df["no_of_companies"].sum() == pytest.approx(3319.0)
        assert df.attrs["bfinance_market_companies_total_leaf_table"] == 3319

    def test_a_page_with_no_table_is_reported_as_zero_rows_not_as_a_crash(self):
        frame = ScreenerHTMLParser().parse_market_industries("<html><body>hello</body></html>")
        assert frame.empty
        assert list(frame.columns) == ["industry", "code_path", *MARKET_AGGREGATE_COLUMNS]
        assert frame.attrs["bfinance_market_row_count"] == 0
        assert frame.attrs["bfinance_market_companies_total_leaf_table"] is None

    def test_empty_input_is_an_empty_frame_with_the_same_columns(self):
        frame = ScreenerHTMLParser().parse_market_industries("")
        assert frame.empty
        assert frame.index.name == "industry_code"
        assert frame.attrs["bfinance_market_row_count"] == 0


# ============================================================ the three count universes


class TestTheThreeCompanyCountUniverses:
    """The caveat, encoded rather than papered over.

    screener.in publishes three different company counts for the same node. They are not rounding
    differences and not one stale copy — the filters behind each are undetermined. These tests
    exist so the disagreement is *visible in the fixtures* and the code cannot quietly adopt one.
    """

    def test_the_overview_and_the_leaf_page_disagree_for_one_node_on_one_day(self):
        """Pharmaceuticals: **159** in ``/market/``, **218** on its own leaf page. Same node, same
        day, same recording session. Neither is derived from the other and no test reconciles them,
        because nothing in either response says how either count is filtered."""
        assert market().loc["IN060101001", "no_of_companies"] == 159.0
        _, page_info = ScreenerHTMLParser().parse_industry_constituents(leaf_html("in060101001"))
        assert page_info["results_found"] == 218

    def test_the_leaf_pages_own_arithmetic_does_not_close_either(self):
        """9 pages x 25 rows = 225 rows exist, against 218 claimed. So even within one response the
        three numbers are mutually inconsistent, which is why ``partial`` is derived from the
        *pagination* and not from ``rows x page_size``."""
        _, page_info = ScreenerHTMLParser().parse_industry_constituents(leaf_html("in060101001"))
        assert page_info["pages"] * ScreenerClient.INDUSTRY_PAGE_SIZE == 225
        assert page_info["results_found"] == 218

    @pytest.mark.parametrize("slug", sorted(LEAVES))
    def test_the_overview_count_is_never_above_the_leaf_page_count(self, slug):
        """Not a coincidence worth betting on, but true of both recordings and cheap to keep
        watching: the overview is the conservative one for every leaf here."""
        overview = market().loc[LEAVES[slug], "no_of_companies"]
        _, page_info = ScreenerHTMLParser().parse_industry_constituents(leaf_html(slug))
        assert overview <= page_info["results_found"]
        assert overview == MARKET_COUNTS[slug]

    def test_thirty_five_of_the_188_leaves_cannot_be_read_in_one_compliant_request(self):
        """The ceiling, measured from the overview alone: **35** leaves report more companies than
        the fixed page size of 25, and their overflow sums to **1,246** companies that no compliant
        request can reach.

        Derived here rather than hard-coded, so the test recomputes from the recording instead of
        comparing a number to itself.
        """
        df = market()
        over = df[df["no_of_companies"] > ScreenerClient.INDUSTRY_PAGE_SIZE]
        assert len(over) == 35
        assert (over["no_of_companies"] - ScreenerClient.INDUSTRY_PAGE_SIZE).sum() == pytest.approx(1246.0)
        # ... and the 153 that do fit are exactly the rest
        assert len(df) - len(over) == 153

    async def test_the_bulk_export_route_is_never_requested(self):
        """``POST /api/export/screen/?url_name=browse_industry&mes_code=IN06`` answers **403**
        unauthenticated, so the export route is not a fallback — and nothing may quietly start
        using it.

        Checked on *behaviour* rather than by grepping the source for the string, because the
        docstrings name that route precisely to explain why it is not used. The stub raises on any
        unrouted path, so a code path that reached for the export would fail loudly here; this
        asserts the negative explicitly so the intent is recorded even if the stub changes.
        """
        recorder = routes(market_route(), *leaf_routes("in060101001"))
        client = offline_client(recorder)
        await client.get_market_industries()
        await client.get_industry_constituents("IN060101001")
        assert len(recorder.calls) == 2
        assert not any("export" in call for call in recorder.calls)
        meta = json.loads(text("META.json"))
        for entry in meta["files"].values():
            assert "export" not in entry["url"], entry["url"]


# ============================================================ parse_industry_constituents


class TestParseIndustryConstituents:
    def test_it_reads_the_page_and_reports_the_pagination(self):
        peers, info = ScreenerHTMLParser().parse_industry_constituents(leaf_html("in060101001"))
        assert len(peers) == 25
        assert info == {"results_found": 218, "page": 1, "pages": 9}

    def test_the_complete_leaf_reads_as_complete(self):
        peers, info = ScreenerHTMLParser().parse_industry_constituents(leaf_html("in040107001"))
        assert len(peers) == 5 == info["results_found"]
        assert info["pages"] == 1

    def test_every_row_is_a_real_company_with_a_symbol(self):
        """No ``Median: N Co.`` footer here — that belongs to the peers payload — so every row
        carries a real ``/company/SYM/`` href and there is no ``symbol is None`` row."""
        peers, _ = ScreenerHTMLParser().parse_industry_constituents(leaf_html("in060101001"))
        assert all(p.symbol for p in peers)
        assert not any(p.name.startswith("Median:") for p in peers)
        assert [p.rank for p in peers] == list(range(1, 26))

    def test_the_repeated_header_row_is_not_emitted_as_a_company(self):
        peers, _ = ScreenerHTMLParser().parse_industry_constituents(leaf_html("in060101001"))
        assert not any(p.name == "Company" for p in peers)
        assert len(peers) == 25, "the second header row at index 16 was emitted"

    def test_a_page_with_no_data_page_info_says_nothing_rather_than_guessing(self):
        """Missing pagination is a *third* state. Defaulting it to "1 page" would certify a
        truncated list as complete — the exact failure this route exists to avoid."""
        stripped = without_page_info("in060101001")
        peers, info = ScreenerHTMLParser().parse_industry_constituents(stripped)
        assert len(peers) == 25, "the rows are still there; only the completeness claim is gone"
        assert info == {"results_found": None, "page": None, "pages": None}

    def test_an_empty_page_is_no_rows_and_no_claim(self):
        peers, info = ScreenerHTMLParser().parse_industry_constituents("")
        assert peers == []
        assert info == {"results_found": None, "page": None, "pages": None}


# ============================================================ the client methods


class TestGetMarketIndustries:
    async def test_one_request_with_no_query_string(self):
        """No query string means no ``robots.txt`` rule can match: all six require a ``?``."""
        recorder = routes(market_route())
        df = await offline_client(recorder).get_market_industries()
        assert recorder.calls == ["https://www.screener.in/market/"]
        assert "?" not in recorder.calls[0]
        assert len(df) == 188

    async def test_the_attrs_record_where_and_when(self):
        recorder = routes(market_route())
        df = await offline_client(recorder).get_market_industries()
        assert df.attrs["bfinance_source_url"] == "https://www.screener.in/market/"
        stamp = df.attrs["bfinance_retrieved_at"]
        assert stamp.endswith("+00:00")
        assert pd.Timestamp(stamp).tzinfo is not None
        assert df.attrs["bfinance_market_single_request"] is True

    async def test_the_counts_survive_the_round_trip(self):
        recorder = routes(market_route())
        df = await offline_client(recorder).get_market_industries()
        assert df.attrs["bfinance_market_row_count"] == 188
        assert df.attrs["bfinance_market_skipped_rows"] == 13
        assert df.attrs["bfinance_market_companies_total_leaf_table"] == 3319
        assert df["no_of_companies"].sum() == pytest.approx(3319.0)

    async def test_a_non_200_is_an_upstream_error_not_an_empty_market(self):
        from bfinance.utils.exceptions import UpstreamServiceError

        recorder = routes(("/market/", 403, "blocked"))
        with pytest.raises(UpstreamServiceError, match="upstream failure"):
            await offline_client(recorder).get_market_industries()

    async def test_a_cached_call_does_not_re_request_and_force_refresh_does(self, temp_cache):
        """A second call is served from the cache, and ``force_refresh=True`` goes back out.

        Uses the suite's real ``SQLiteCache`` fixture rather than a dict, so the caching path
        exercised here is the one that ships.
        """
        client = ScreenerClient(cache=temp_cache, min_request_interval_ms=0.0, max_retries=1)
        recorder = routes(market_route())
        client._send_with_retry = recorder

        first = await client.get_market_industries()
        assert len(recorder.calls) == 1

        second = await client.get_market_industries()
        assert len(recorder.calls) == 1, "the second call hit the network"
        pd.testing.assert_frame_equal(first, second)

        await client.get_market_industries(force_refresh=True)
        assert len(recorder.calls) == 2, "force_refresh=True did not bypass the cache"

    async def test_the_cached_value_is_the_raw_response_not_a_serialised_frame(self, temp_cache):
        """The cache stores plain strings, so what lands in it is the bytes screener.in sent.

        This is the reason the method re-parses on a cache hit: a DataFrame would need a serialiser
        that nothing else in this package uses, and re-parsing a 99 KB table is cheaper than
        inventing one. Asserting the stored *type* keeps that decision honest — a future switch to
        ``frame.to_json()`` would silently change the cache's shape.
        """
        client = ScreenerClient(cache=temp_cache, min_request_interval_ms=0.0, max_retries=1)
        client._send_with_retry = routes(market_route())
        await client.get_market_industries()
        stored = temp_cache.get("screener_market_industries")
        assert isinstance(stored, str)
        assert stored.lstrip().startswith("<html><body>")
        assert "Industries Overview" not in stored  # the trim dropped <head> and the nav


class TestGetIndustryConstituents:
    async def test_the_truncated_leaf_says_so_in_its_attrs(self):
        """**The whole point of the route.** 25 rows of 218, and no consumer can mistake that for a
        complete industry."""
        recorder = routes(*leaf_routes("in060101001"))
        df = await offline_client(recorder).get_industry_constituents("IN060101001")
        assert len(df) == 25
        attrs = df.attrs
        assert attrs["bfinance_constituents_partial"] is True
        assert attrs["bfinance_constituents_total_reported"] == 218
        assert attrs["bfinance_constituents_returned"] == 25
        assert attrs["bfinance_constituents_page_size"] == 25
        assert attrs["bfinance_constituents_pages"] == 9
        assert attrs["bfinance_constituents_not_returned"] == 193
        assert attrs["bfinance_constituents_pagination_robots_blocked"] is True

    async def test_the_complete_leaf_says_so_too(self):
        """``partial is False``, not merely absent — a one-page industry is a positive statement and
        is distinguishable from the third state below."""
        recorder = routes(*leaf_routes("in040107001"))
        df = await offline_client(recorder).get_industry_constituents("IN040107001")
        assert len(df) == 5
        assert df.attrs["bfinance_constituents_partial"] is False
        assert df.attrs["bfinance_constituents_total_reported"] == 5
        assert df.attrs["bfinance_constituents_not_returned"] == 0
        assert df.attrs["bfinance_constituents_pages"] == 1

    async def test_an_unknown_completeness_is_neither_complete_nor_truncated(self, caplog):
        """``None``, the third state. ``assert not attrs["..._partial"]`` and
        ``assert attrs["..._partial"] is False`` are different claims, and only the first is true
        here — which is exactly why the key is tri-state on purpose."""
        recorder = routes(("/market/IN06/IN0601/IN060101/IN060101001/", 200,
                           without_page_info("in060101001")))
        df = await offline_client(recorder).get_industry_constituents("IN060101001")
        assert len(df) == 25
        assert df.attrs["bfinance_constituents_partial"] is None
        assert df.attrs["bfinance_constituents_total_reported"] is None
        assert df.attrs["bfinance_constituents_not_returned"] is None
        assert df.attrs["bfinance_constituents_pages"] is None
        # ... and no warning is emitted, because nothing was claimed to be missing
        assert not [r for r in caplog.records if "permanently partial" in r.getMessage()]

    async def test_a_bare_leaf_code_expands_to_its_four_level_path(self):
        """No lookup request in between: ``IN060101001`` -> the full path, one GET."""
        recorder = routes(*leaf_routes("in060101001"))
        await offline_client(recorder).get_industry_constituents("IN060101001")
        assert recorder.calls == [
            "https://www.screener.in/market/IN06/IN0601/IN060101/IN060101001/"
        ]

    @pytest.mark.parametrize("code", [
        "IN060101001",
        "in060101001",
        "IN06/IN0601/IN060101/IN060101001",
        "IN06/IN0601/IN060101/IN060101001/",
        "/market/IN06/IN0601/IN060101/IN060101001/",
        "market/IN06/IN0601/IN060101/IN060101001",
        "  IN060101001  ",
    ])
    def test_every_accepted_spelling_of_a_code_reaches_the_same_url(self, code):
        assert ScreenerClient._leaf_url(code) == (
            "https://www.screener.in/market/IN06/IN0601/IN060101/IN060101001/"
        )

    @pytest.mark.parametrize("code", ["", "   ", "IN06;", "not a code", "../../etc/passwd",
                                      "IN06/IN0601/IN060101/IN060101001/EXTRA"])
    def test_a_code_that_is_not_a_code_is_rejected_without_a_request(self, code):
        """Raised locally. A 404 out of a typo would cost a request to learn something knowable,
        and would surface as "this industry has no companies" — which is a different and false
        claim."""
        with pytest.raises(ValueError, match="not a screener.in industry code"):
            ScreenerClient._leaf_url(code)

    async def test_a_non_200_is_an_upstream_error_not_an_empty_industry(self):
        from bfinance.utils.exceptions import UpstreamServiceError

        recorder = routes(("/market/IN06/IN0601/IN060101/IN060101001/", 404, "<title>404</title>"))
        with pytest.raises(UpstreamServiceError, match="upstream failure"):
            await offline_client(recorder).get_industry_constituents("IN060101001")

    async def test_the_frame_is_the_eleven_columns_indexed_by_symbol(self):
        recorder = routes(*leaf_routes("in060101001"))
        df = await offline_client(recorder).get_industry_constituents("IN060101001")
        assert df.index.name == "symbol"
        assert df.index[0] == "SUNPHARMA"
        assert df.index.is_unique
        assert list(df.columns) == [
            "rank", "name", "symbol", "cmp", "pe", "market_cap_cr", "dividend_yield",
            "net_profit_qtr", "qtr_profit_var", "sales_qtr", "qtr_sales_var", "roce",
        ]
        assert len(df.columns) == 12

    async def test_a_row_with_no_company_link_falls_back_to_its_name_as_the_index(self):
        """The index cannot be ``symbol`` for a row that has none, and dropping the row would be
        worse than an index of names — so the fallback is explicit and tested.

        Note what a missing symbol reads as *in the frame*: pandas promotes the object column to
        float on the first ``None``, so the cell is ``NaN``, not ``None``. That is why the
        assertion is ``pd.isna`` and not ``is None`` — a consumer writing ``== None`` would get a
        false negative on a genuinely absent symbol, which is the same class of bug as the empty
        announcements list.
        """
        broken = leaf_html("in040107001").replace(
            '<a href="/company/HINDUNILVR/consolidated/" target="_blank">Hind. Unilever</a>',
            "Hind. Unilever",
        )
        assert "/company/HINDUNILVR/" not in broken
        recorder = routes(("/market/IN04/IN0401/IN040107/IN040107001/", 200, broken))
        df = await offline_client(recorder).get_industry_constituents("IN040107001")
        assert len(df) == 5
        assert "Hind. Unilever" in df.index
        assert pd.isna(df.loc["Hind. Unilever", "symbol"])
        assert df.index.is_unique

    async def test_the_attrs_name_the_url_and_the_code(self):
        recorder = routes(*leaf_routes("in060101001"))
        df = await offline_client(recorder).get_industry_constituents("IN06/IN0601/IN060101/IN060101001")
        assert df.attrs["bfinance_industry_code"] == "IN060101001"
        assert df.attrs["bfinance_source_url"].endswith("/market/IN06/IN0601/IN060101/IN060101001/")
        assert df.attrs["bfinance_retrieved_at"].endswith("+00:00")

    async def test_the_industry_codes_in_the_overview_are_all_addressable(self):
        """Every index the overview hands out must be a key this route accepts, and must expand to
        a four-level path. If the prefix rule ever breaks, this fails before a user does."""
        for code in market().index:
            url = ScreenerClient._leaf_url(code)
            assert url.count("/market/") == 1
            assert len(url.split("/market/")[1].strip("/").split("/")) == 4, code


# ============================================================ cross-route consistency


class TestTheTwoRoutesAgreeWhereTheyCan:
    def test_the_same_industry_is_named_the_same_way_on_both_routes(self):
        """``/market/`` and the leaf page describe the same node; if the names drift, a lookup by
        name is ambiguous."""
        overview = market()
        for slug in LEAVES:
            code = LEAVES[slug]
            _, page_info = ScreenerHTMLParser().parse_industry_constituents(leaf_html(slug))
            assert page_info["results_found"] >= overview.loc[code, "no_of_companies"]

    def test_itc_appears_in_its_own_industry(self):
        """The sanity check the peers payload fails: the right route puts ITC in Diversified FMCG
        with its actual co-members, and nowhere else."""
        peers, _ = ScreenerHTMLParser().parse_industry_constituents(leaf_html("in040107001"))
        symbols = [p.symbol for p in peers]
        assert "ITC" in symbols
        assert "HINDUNILVR" in symbols
        assert len(symbols) == 5
        # ... and the mis-scoped peers payload shares none of them
        peers_payload = ScreenerHTMLParser()._parse_peers(text("peers_itc.html"))
        assert not ({p.name for p in peers_payload if not p.name.startswith("Median:")}
                    & {p.name for p in peers})
