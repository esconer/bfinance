"""
screener.in offline tests — driven by the recordings in ``tests/fixtures/screener/``.

Every fixture in that directory is a real recording of a screener.in response taken on
2026-09-27; ``META.json`` records the source URL, HTTP status, byte count, SHA-256 prefix, capture
time and the exact trim rule for each one, and ``NOTICE`` explains what they are. See
:mod:`tests.test_trendlyne_core` for the same convention applied to trendlyne.com.

**Why this module exists.** All 18 pre-existing screener client/parser tests were marked ``live``,
and the required CI job runs ``-m "not live"`` — so the offline suite proved *nothing* about the
package's backbone fundamentals source. screener.in is a commercial site that can change its markup
at any time, there is no second source for profiles, statements, ratios, peers or concalls, and a
silent markup break reached users as an empty profile with no failing test. These tests make that
surface checkable on every commit.

**No test here touches the network.** The HTTP layer is stubbed at
``ScreenerClient._send_with_retry`` (the single seam every request goes through) and the stub serves
the recorded response *bytes*, so a broken parser still fails: every assertion below is on a parsed
number, not on a stub's return value.

The selector inventory
======================

``ScreenerHTMLParser`` reads exactly these, and the fixtures must preserve every one of them. If
screener.in renames any of them, the matching test here fails and the fixtures must be re-recorded.

======================================  ==================================================  =============
#   selector / attribute                what it drives                                     method
1    ``h1`` (first)                       company name                                        _parse_header_meta
2    ``.company-profile .about p``        about text (``.about p`` is the fallback)           _parse_header_meta
3    ``.company-links a .links a          website / BSE code / NSE symbol, by href host       _parse_header_meta
     .company-info a .company-profile a``
4    ``data-company-id="N"`` regex        the company id — the only compliant id resolver     client.extract_company_id
5    ``section#peers a[href*=/market/]``  sector, industry group, industry, sub-industry      _parse_sector_hierarchy
6    ``section#peers a`` text ~           index membership tags                               _parse_sector_hierarchy
     bse|nifty|sensex``
7    ``#top-ratios li`` > ``.name``       the 9 headline ratios; "High / Low" splits on "/"   _parse_top_ratios
     + ``.value``
8    ``.pros ul li`` / ``.cons ul li``    pros and cons                                       _parse_analysis
9    ``.ranges-table`` > ``th`` + ``td``  the four CAGR tables                                _parse_cagrs
10   ``section#{quarters,profit-loss,     the five statement tables, ``table.data-table``     _parse_table_section
     balance-sheet,cash-flow,ratios}``
11   ``section#shareholding               two tables: [0] quarterly, [1] yearly               _parse_shareholding_sections
     table.data-table``
12   ``section#documents .concalls        concalls: date regex + per-link role matchers       _parse_documents
     ul li``
13   ``section#documents                  annual reports                                      _parse_documents
     .annual-reports a``
14   ``section#documents                  credit ratings                                      _parse_documents
     .credit-ratings a``
15   ``section#documents                  announcements — the container is the *unmarked*     _parse_documents
     div.documents > h3``                 ``div.documents``: no id, no class, so it is
     + structural inverse``               found by its ``<h3>Announcements</h3>`` heading
16   ``table.data-table`` in the peers    the 11 cell columns, and symbol from the href.      _parse_peers
     payload + ``a[href]``                **Shape only — never values**, see TestPeers
17   ``datasets[].metric`` /              the chart time series columns                       charts.py
     ``datasets[].values[]``
18   ``table.striped.data-table`` on      188 leaf industries: a 4-level code path and 8      parse_market_
     ``/market/`` + ``a[href^=/market/]`` aggregates each. 13 repeated header rows must       industries
                                          be skipped, not emitted
19   ``div[data-page-info]`` on a leaf    "N results found: Showing page X of Y"              parse_industry_
     industry page``                                                                          constituents

The page's own nav anchors are ``#top #chart #analysis #peers #quarters #profit-loss
#balance-sheet #cash-flow #ratios #shareholding #documents``; the parser reads none of ``#top``,
``#chart`` or ``#insights``, and the ratio card is a ``ul#top-ratios`` rather than a section, so
the fixtures keep ``ul#top-ratios`` and every ``section#*`` the parser reads.

Rows 1-17 are checked against the two **company** recordings below. Rows 18-19 read the
``/market/`` overview and the leaf-industry pages, which are separate recordings with their own
module: :mod:`tests.test_screener_market_offline`.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import httpx
import pandas as pd
import pytest
from bs4 import BeautifulSoup

from bfinance.models.statements import INDIAN_TO_YFINANCE_LABELS
from bfinance.screener.charts import ScreenerChartEngine
from bfinance.screener.client import ScreenerClient, extract_company_id
from bfinance.screener.parser import ScreenerHTMLParser
from bfinance.utils.formatting import parse_indian_number

FIXTURES = Path(__file__).parent / "fixtures" / "screener"

#: symbol -> (company id, ticker used on screener.in, the two values that must differ per symbol)
SYMBOLS = {"itc": 1552, "tcs": 3365}

#: The 11 cells of a screener.in company table, in the order ``_parse_peers`` reads them. ``symbol``
#: is deliberately absent: it is the 12th field and the only one with no column, being the
#: ``/company/SYM/`` segment of the name cell's href. See :class:`TestPeers` for why this list is
#: about *shape* only.
PEER_FIELDS_FROM_CELLS = ("rank", "name", "cmp", "pe", "market_cap_cr", "dividend_yield",
                          "net_profit_qtr", "qtr_profit_var", "sales_qtr", "qtr_sales_var", "roce")


def text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def payload(name: str) -> dict:
    return json.loads(text(name))


def company_html(sym: str) -> str:
    return text(f"company_{sym}.html")


def parse(sym: str, *, with_peers: bool = True):
    """Parse a recorded company page exactly the way ``get_company_profile`` does."""
    peers = text(f"peers_{sym}.html") if with_peers else None
    return ScreenerHTMLParser().parse_full_profile(
        symbol=sym.upper(),
        company_id=SYMBOLS[sym],
        main_html=company_html(sym),
        peers_html=peers,
        is_consolidated=True,
        page_url=f"https://www.screener.in/company/{sym.upper()}/consolidated/",
    )


def _peer_cells() -> list[list[str]]:
    """The ``<td>`` texts of every data row of the recorded peers payload, header excluded.

    Read straight from the fixture with BeautifulSoup and *not* through ``_parse_peers``, so a test
    can state the parser's contract without restating a single expected value. Rows narrower than
    four cells are filtered out here, exactly as the parser filters them, so the two cannot drift
    into disagreeing about which rows exist.
    """
    table = BeautifulSoup(text("peers_itc.html"), "html.parser").find("table", class_="data-table")
    return [
        [td.get_text(strip=True) for td in tr.find_all("td")]
        for tr in table.find_all("tr")[1:]
        if len(tr.find_all("td")) >= 4
    ]


def parse_industry(slug: str) -> list:
    """Parse a recorded leaf-industry page into ``PeerStock`` rows.

    The same 11-column table shape as the peers payload, read from a page where the membership is
    actually correct — which is what makes the two comparable in :class:`TestPeers`.
    """
    peers, page_info = ScreenerHTMLParser().parse_industry_constituents(text(f"industry_{slug}.html"))
    assert page_info, f"industry_{slug}.html carries no div[data-page-info]; the fixture lost it"
    return peers


class Recordings:
    """Serves the recorded fixtures in place of ``httpx``, and records every request.

    The seam is ``ScreenerClient._send_with_retry``: it is the only place the client reaches the
    network, so replacing it removes all I/O while leaving the client's own request-building,
    id-resolution, caching and parse logic running for real. Responses are built from the recorded
    bytes, so a broken parser changes the assertions rather than being papered over.

    Routes are matched on the request **path**, exactly, so a stub can never accidentally serve the
    company page to a request for the chart API (their paths share the ``/api/company/{id}/``
    prefix). An unrouted request is a test failure, not an empty response.
    """

    def __init__(self) -> None:
        self.routes: dict[str, tuple[int, str]] = {}
        self.calls: list[str] = []

    def add(self, path: str, status: int, body: str) -> "Recordings":
        self.routes[path] = (status, body)
        return self

    async def __call__(self, url, params=None, headers=None, follow_redirects=True):
        url = str(url)
        self.calls.append(url)
        path = httpx.URL(url).path
        if path in self.routes:
            status, body = self.routes[path]
            return httpx.Response(status, text=body, request=httpx.Request("GET", url, params=params))
        raise AssertionError(
            f"no recording for {path!r} (requested {url!r}); recorded: {sorted(self.routes)}"
        )


def company_route(sym: str, body: str | None = None) -> tuple[str, int, str]:
    return (f"/company/{sym.upper()}/", 200, body if body is not None else company_html(sym))


def consolidated_route(sym: str, body: str | None = None) -> tuple[str, int, str]:
    return (f"/company/{sym.upper()}/consolidated/", 200,
            body if body is not None else company_html(sym))


def peers_route(sym: str) -> tuple[str, int, str]:
    return (f"/api/company/{SYMBOLS[sym]}/peers/", 200, text(f"peers_{sym}.html"))


def chart_route(sym: str) -> tuple[str, int, str]:
    return (f"/api/company/{SYMBOLS[sym]}/chart/", 200, text(f"chart_{sym}.json"))


def routes(*specs: tuple[str, int, str]) -> Recordings:
    recorder = Recordings()
    for path, status, body in specs:
        recorder.add(path, status, body)
    return recorder


def offline_client(recorder: Recordings) -> ScreenerClient:
    """A ScreenerClient whose only transport is ``recorder``.

    ``cache=None`` is the explicit "no cache at all" argument, so a test can never read or write
    the user's real ``~/.bfinance`` cache — which is also what the ``live`` marker exists to
    protect. Pacing is 0 because the real 150 ms slot would only add wall-clock to a stub.
    """
    client = ScreenerClient(cache=None, min_request_interval_ms=0.0, max_retries=1)
    client._send_with_retry = recorder  # instance attribute: shadows the bound method
    return client


# ============================================================ the selector inventory


class TestSelectorInventory:
    """Every selector the parser reads is still present in both recordings.

    This is the fixture's contract. screener.in renames one of these, this fails, and the fix is
    to re-record — not to edit the fixture.
    """

    #: (inventory row, a CSS selector or a regex that must find something)
    REQUIRED = [
        ("1  h1 company name", "h1"),
        ("2  about paragraph", ".about p"),
        ("3  company links", ".company-info a"),
        ("5/6 sector crumbs + index tags", "section#peers a[href*='/market/']"),
        ("5/6 index membership tags", "section#peers a.tag"),
        ("7  top ratios", "#top-ratios li .name"),
        ("7  top ratio values", "#top-ratios li .value"),
        ("8  pros", ".pros ul li"),
        ("8  cons", ".cons ul li"),
        ("9  cagr tables", ".ranges-table th"),
        ("10 quarters", "section#quarters table.data-table thead th"),
        ("10 profit-loss", "section#profit-loss table.data-table thead th"),
        ("10 balance-sheet", "section#balance-sheet table.data-table thead th"),
        ("10 cash-flow", "section#cash-flow table.data-table thead th"),
        ("10 ratios", "section#ratios table.data-table thead th"),
        ("11 shareholding (2 tables)", "section#shareholding table.data-table"),
        ("12 concalls", "section#documents .concalls ul li"),
        ("13 annual reports", "section#documents .annual-reports a"),
        ("14 credit ratings", "section#documents .credit-ratings a"),
    ]

    @pytest.mark.parametrize("sym", sorted(SYMBOLS))
    @pytest.mark.parametrize("label,selector", REQUIRED, ids=[r[0] for r in REQUIRED])
    def test_the_recording_still_carries_it(self, sym, label, selector):

        soup = BeautifulSoup(company_html(sym), "lxml")
        assert soup.select(selector), f"{sym}: {label} ({selector}) is gone from the page"

    @pytest.mark.parametrize("sym", sorted(SYMBOLS))
    def test_the_company_id_attribute_is_present(self, sym):
        assert f'data-company-id="{SYMBOLS[sym]}"' in company_html(sym)

    @pytest.mark.parametrize("sym", sorted(SYMBOLS))
    def test_the_about_paragraph_is_the_company_profile_one(self, sym):
        """``_parse_header_meta`` tries ``.company-profile .about p`` first and only falls back to
        ``.about p``. On this markup both resolve to the same node; pin that it is inside
        ``.company-profile`` so a markup change that adds a second, unrelated ``.about`` block
        cannot silently take over."""

        soup = BeautifulSoup(company_html(sym), "lxml")
        node = soup.select_one(".company-profile .about p")
        assert node is not None
        assert node.find_parent(class_="company-profile") is not None
        assert node.get_text(strip=True) == parse(sym).about

    @pytest.mark.parametrize("sym", sorted(SYMBOLS))
    def test_shareholding_has_both_tables(self, sym):

        soup = BeautifulSoup(company_html(sym), "lxml")
        assert len(soup.select("section#shareholding table.data-table")) == 2

    def test_the_two_recordings_are_the_same_template(self):
        """Genericity: byte-identical templates, so one parser must serve both."""

        itc, tcs = (BeautifulSoup(company_html(s), "lxml") for s in ("itc", "tcs"))
        section_ids = lambda s: [x.get("id") for x in s.find_all("section")]  # noqa: E731
        assert section_ids(itc) == section_ids(tcs)
        # the same nine headline ratio labels, in the same order
        labels = lambda s: [x.get_text(strip=True) for x in s.select("#top-ratios li .name")]  # noqa: E731
        assert labels(itc) == labels(tcs)
        # the same four CAGR tables, in the same order
        titles = lambda s: [t.find("th").get_text(strip=True) for t in s.select(".ranges-table")]  # noqa: E731
        assert titles(itc) == titles(tcs)
        # ... while the data is genuinely different, which is what makes this a real second symbol
        assert itc.find("h1").get_text(strip=True) != tcs.find("h1").get_text(strip=True)
        assert extract_company_id(str(itc)) != extract_company_id(str(tcs))


# ============================================================ data-company-id


class TestCompanyIdResolution:
    """``data-company-id`` is the primary id resolver: the search route is robots-disallowed."""

    @pytest.mark.parametrize("sym", sorted(SYMBOLS))
    def test_it_extracts_the_real_id(self, sym):
        assert extract_company_id(company_html(sym)) == SYMBOLS[sym]

    def test_the_ids_differ_between_the_two_symbols(self):
        assert SYMBOLS["itc"] == 1552 and SYMBOLS["tcs"] == 3365

    @pytest.mark.parametrize("sym", sorted(SYMBOLS))
    async def test_resolve_company_id_reads_the_page_not_the_search_route(self, sym):
        """One GET to the company page. The robots-disallowed ``/api/company/search/?q=`` is not
        called, and the id comes back exactly rather than as a best-match guess."""
        recorder = routes(company_route(sym))
        client = offline_client(recorder)
        assert await client.resolve_company_id(sym.upper()) == SYMBOLS[sym]
        assert recorder.calls == [f"https://www.screener.in/company/{sym.upper()}/"]
        assert not any("search" in call for call in recorder.calls)

    def test_a_page_without_the_attribute_is_visible_as_a_missing_id(self):
        assert extract_company_id("<html><body>no id here</body></html>") is None


# ============================================================ #top-ratios


class TestTopRatios:
    """The nine headline ratios, for both symbols, against the numbers on the page."""

    #: symbol -> the nine values as screener.in printed them
    EXPECTED = {
        "itc": dict(market_cap=337071.0, current_price=269.0, high_52w=426.0, low_52w=256.0,
                    stock_pe=17.1, book_value=57.9, dividend_yield=5.39, roce=38.9, roe=29.3,
                    face_value=1.0),
        "tcs": dict(market_cap=753286.0, current_price=2082.0, high_52w=3350.0, low_52w=1976.0,
                    stock_pe=14.0, book_value=296.0, dividend_yield=3.07, roce=63.0, roe=51.8,
                    face_value=1.0),
    }

    @pytest.mark.parametrize("sym", sorted(SYMBOLS))
    def test_all_nine_ratios(self, sym):
        r = parse(sym).ratios
        for field, value in self.EXPECTED[sym].items():
            got = getattr(r, field)
            assert got == pytest.approx(value), f"{sym}.{field}: {got!r} != {value!r}"

    @pytest.mark.parametrize("sym", sorted(SYMBOLS))
    def test_high_low_is_split_into_two_fields(self, sym):
        r = parse(sym).ratios
        assert r.high_52w > r.low_52w, "the 'High / Low' cell was not split on '/'"

    @pytest.mark.parametrize("sym", sorted(SYMBOLS))
    def test_raw_labels_survive_in_custom_ratios(self, sym):
        custom = parse(sym).ratios.custom_ratios
        assert set(custom) == {"Market Cap", "Current Price", "High", "Low", "Stock P/E",
                               "Book Value", "Dividend Yield", "ROCE", "ROE", "Face Value"}
        assert custom["Market Cap"] == pytest.approx(self.EXPECTED[sym]["market_cap"])

    def test_a_renamed_or_missing_ratio_label_does_not_silently_become_none(self):
        """A missing ratio must be missing for a *recorded* reason, not because the card vanished.

        Both recorded companies are debt-free-ish and print no 'Debt to equity', so
        ``debt_to_equity`` is legitimately None while the other nine are populated. If a markup
        change emptied the whole card, the same None would appear and nothing would notice - so
        this test also pins the card's size.
        """
        for sym in SYMBOLS:
            card = BeautifulSoup(company_html(sym), "lxml").select("#top-ratios li")
            assert len(card) == 9, f"{sym}: #top-ratios has {len(card)} items, expected 9"
            profile = parse(sym)
            assert profile.ratios.debt_to_equity is None  # genuinely absent on the page
            assert profile.ratios.current_price is not None  # ... but the card is there


# ============================================================ sector hierarchy


class TestSectorHierarchy:
    EXPECTED = {
        "itc": ("Fast Moving Consumer Goods", "Fast Moving Consumer Goods", "Diversified FMCG",
                "Diversified FMCG"),
        "tcs": ("Information Technology", "Information Technology", "IT - Software",
                "Computers - Software & Consulting"),
    }

    @staticmethod
    def _crumbs(sym: str) -> list[str]:
        section = BeautifulSoup(company_html(sym), "lxml").find("section", id="peers")
        return [a.get_text(strip=True) for a in section.find_all("a")
                if a.get("href") and "/market/" in a["href"]]

    @pytest.mark.parametrize("sym", sorted(SYMBOLS))
    def test_the_four_crumb_levels(self, sym):
        profile = parse(sym)
        assert (profile.sector, profile.industry_group, profile.industry, profile.sub_industry) \
            == self.EXPECTED[sym]

    @pytest.mark.parametrize("sym", sorted(SYMBOLS))
    def test_the_levels_are_the_page_breadcrumbs_in_order(self, sym):
        """The four fields must be the four ``/market/`` crumbs, in document order, and nothing
        else - so a crumb filter that picks up the benchmark tags, or reverses the list, fails."""
        crumbs = self._crumbs(sym)
        assert len(crumbs) == 4, crumbs
        profile = parse(sym)
        assert [profile.sector, profile.industry_group, profile.industry, profile.sub_industry] \
            == crumbs

    @pytest.mark.parametrize("sym", sorted(SYMBOLS))
    def test_a_benchmark_tag_is_never_mistaken_for_a_crumb(self, sym):
        profile = parse(sym)
        for level in (profile.sector, profile.industry_group, profile.industry, profile.sub_industry):
            assert level not in profile.indices, "an index tag leaked into the sector hierarchy"
            assert "Nifty" not in level and "Sensex" not in level

    @pytest.mark.parametrize("sym", sorted(SYMBOLS))
    def test_index_membership(self, sym):
        assert parse(sym).indices == ["BSE Sensex", "Nifty 50"]

    def test_the_two_sectors_really_differ(self):
        assert self.EXPECTED["itc"][0] != self.EXPECTED["tcs"][0]


# ============================================================ pros / cons / CAGRs


class TestAnalysisAndCagrs:
    def test_pros_and_cons(self):
        itc = parse("itc")
        assert itc.analysis.pros[0] == "Company is almost debt free."
        assert itc.analysis.cons == [
            "The company has delivered a poor sales growth of 9.87% over past five years."
        ]
        tcs = parse("tcs")
        assert tcs.analysis.cons[0] == "Stock is trading at 7.02 times its book value"
        assert len(itc.analysis.pros) == 4 and len(tcs.analysis.pros) == 3

    @pytest.mark.parametrize("sym", sorted(SYMBOLS))
    def test_the_four_cagr_tables(self, sym):
        cagrs = parse(sym).cagrs
        assert list(cagrs) == ["Compounded Sales Growth", "Compounded Profit Growth",
                               "Stock Price CAGR", "Return on Equity"]
        for title, table in cagrs.items():
            assert "10 Years:" in table, f"{sym}.{title}"
            assert "5 Years:" in table, f"{sym}.{title}"
            assert "3 Years:" in table, f"{sym}.{title}"
            # the two growth tables also carry a trailing twelve-month column; the price and
            # return tables do not - they report a 1-year window instead
            if "Growth" in title:
                assert set(table) == {"10 Years:", "5 Years:", "3 Years:", "TTM:"}
            else:
                assert set(table) == {"10 Years:", "5 Years:", "3 Years:",
                                      "1 Year:" if title == "Stock Price CAGR" else "Last Year:"}  # each table keeps its own period labels

    def test_cagr_values_are_percent_strings_from_the_page(self):
        itc = parse("itc").cagrs
        assert itc["Compounded Sales Growth"]["10 Years:"] == "7%"
        assert itc["Compounded Sales Growth"]["TTM:"] == "-3%"
        assert itc["Stock Price CAGR"]["3 Years:"] == "-13%"
        assert parse("tcs").cagrs["Return on Equity"]["10 Years:"] == "43%"

    def test_the_title_is_the_table_header_not_the_first_cell(self):
        """A misread header would key the dict on '' or on a period label."""
        itc = parse("itc").cagrs
        assert "Compounded Sales Growth" in itc
        assert "" not in itc


# ============================================================ statements


class TestStatements:
    def test_profit_loss_headers_include_ttm(self):
        assert parse("itc").profit_loss.headers == ["Mar 2026", "TTM"]

    @pytest.mark.parametrize("sym", sorted(SYMBOLS))
    def test_sales_and_net_profit(self, sym):
        expected = {"itc": ([78868.0, 76488.0], [21018.0, 20184.0]),
                    "tcs": ([267021.0, 275859.0], [49454.0, 50055.0])}[sym]
        pnl = parse(sym).profit_loss
        assert pnl.rows["Sales"] == pytest.approx(expected[0])
        assert pnl.rows["Net Profit"] == pytest.approx(expected[1])

    def test_the_plus_suffix_is_stripped_from_row_labels(self):
        """The page prints 'Sales+' and 'Net Profit+'; the accessor contract wants 'Sales'."""
        itc = parse("itc").profit_loss
        assert "Sales" in itc.rows and "Net Profit" in itc.rows
        assert not any(k.endswith("+") for k in itc.rows)

    def test_balance_sheet_balances(self):
        bs = parse("itc").balance_sheet
        assert bs.headers == ["Mar 2025", "Mar 2026"]
        assert bs.rows["Total Assets"] == bs.rows["Total Liabilities"]
        assert bs.rows["Total Assets"] == pytest.approx([88003.0, 93637.0])
        assert bs.rows["Borrowings"] == pytest.approx([285.0, 2399.0])

    def test_cash_flow_signs_survive(self):
        cf = parse("tcs").cash_flow
        assert cf.rows["Cash from Operating Activity"] == pytest.approx([48908.0, 52094.0])
        assert cf.rows["Cash from Investing Activity"] == pytest.approx([-2144.0, -11886.0])

    def test_quarters(self):
        q = parse("itc").quarters
        assert q.headers == ["Mar 2026", "Jun 2026"]
        assert q.rows["Sales"] == pytest.approx([17825.0, 19114.0])
        assert q.rows["EPS in Rs"] == pytest.approx([4.3, 3.51])

    def test_ratios_history(self):
        r = parse("itc").ratios_history
        assert r.headers == ["Mar 2025", "Mar 2026"]
        assert r.rows["ROCE %"] == pytest.approx([37.0, 39.0])
        assert r.rows["Debtor Days"] == pytest.approx([23.0, 18.0])

    @pytest.mark.parametrize("sym", sorted(SYMBOLS))
    def test_every_row_has_one_value_per_period(self, sym):
        """``to_dataframe`` refuses a ragged row; the parser must never produce one."""
        profile = parse(sym)
        for field in ("quarters", "profit_loss", "balance_sheet", "cash_flow", "ratios_history",
                      "shareholding", "shareholding_yearly"):
            statement = getattr(profile, field)
            assert statement.headers, f"{sym}.{field} is empty"
            for label, values in statement.rows.items():
                assert len(values) == len(statement.headers), f"{sym}.{field}[{label}]"

    def test_a_label_only_row_parses_to_all_none_rather_than_being_dropped(self):
        """The 'Raw PDF' row is a real row with no values; it must survive as None, not vanish."""
        q = parse("itc").quarters
        assert q.rows["Raw PDF"] == [None, None]

    def test_an_unreported_period_is_none_not_zero(self):
        """screener prints nothing for a TTM 'Tax %'; a zero would be a fabricated number."""
        pnl = parse("itc").profit_loss
        assert pnl.rows["Tax %"] == pytest.approx([25.0, None])
        assert pnl.rows["Dividend Payout %"] == pytest.approx([88.0, None])

    def test_tcs_ratios_history_reports_missing_days_as_none(self):
        r = parse("tcs").ratios_history
        assert r.rows["Inventory Days"] == [None, None]
        assert r.rows["Debtor Days"] == pytest.approx([84.0, 93.0])


class TestShareholding:
    """The two tables are trimmed to *different* column windows on purpose (see META.json), so a
    parser that reads both from the same table cannot pass these."""

    def test_the_two_tables_have_different_period_windows(self):
        profile = parse("tcs")
        assert profile.shareholding.headers == ["Mar 2026", "Jun 2026"]
        assert profile.shareholding_yearly.headers == ["Mar 2024", "Mar 2025", "Mar 2026", "Jun 2026"]

    def test_quarterly_values(self):
        sh = parse("tcs").shareholding
        assert sh.rows["Promoters"] == pytest.approx([71.77, 71.77])
        assert sh.rows["FIIs"] == pytest.approx([9.66, 9.07])
        assert sh.rows["No. of Shareholders"] == pytest.approx([2450090.0, 2605182.0])

    def test_yearly_values_are_not_the_quarterly_ones(self):
        sh, yearly = parse("tcs").shareholding, parse("tcs").shareholding_yearly
        assert yearly.rows["FIIs"] == pytest.approx([12.7, 12.04, 9.66, 9.07])
        assert yearly.rows["FIIs"] != sh.rows["FIIs"], "both tables returned the same data"

    def test_promoter_row_is_absent_for_a_company_with_no_promoter_row(self):
        """ITC prints no 'Promoters' row at all. Absent must mean absent, not 0.0."""
        itc = parse("itc").shareholding
        assert "Promoters" not in itc.rows
        assert itc.rows["DIIs"] == pytest.approx([49.15, 49.13])
        assert itc.rows["Government"] == pytest.approx([0.04, 0.04])
        assert parse("itc").shareholding_yearly.rows["DIIs"] == pytest.approx(
            [43.76, 45.19, 49.15, 49.13])


class TestYfinanceLabelMapping:
    """Indian row labels must reach the generic yfinance names, scaled to absolute rupees."""

    def test_profit_loss_labels_are_remapped(self):
        yf = parse("itc").profit_loss.to_yfinance()
        assert "Total Revenue" in yf.index and "Net Income" in yf.index
        assert "Operating Income" in yf.index and "Pretax Income" in yf.index
        assert "Sales" not in yf.index, "the Indian label leaked into the yfinance shape"
        assert "Net Income Including Noncontrolling Interests" in yf.index  # the alias row

    def test_values_are_absolute_rupees_not_crores(self):
        yf = parse("itc").profit_loss.to_yfinance()
        assert yf.loc["Total Revenue"].iloc[0] == pytest.approx(78868.0 * 1e7)

    def test_balance_sheet_labels_are_remapped(self):
        yf = parse("itc").balance_sheet.to_yfinance()
        assert {"Common Stock", "Retained Earnings", "Total Debt", "Net PPE",
                "Capital Work In Progress", "Total Assets"} <= set(yf.index)
        assert yf.loc["Total Debt"].iloc[-1] == pytest.approx(2399.0 * 1e7)

    def test_the_ttm_column_is_dropped_because_it_is_not_a_date(self):
        """screener's 'TTM' is not parseable as '01 TTM'; to_yfinance must drop it, not guess."""
        native = parse("itc").profit_loss
        assert "TTM" in native.headers
        yf = native.to_yfinance()
        assert list(yf.columns) == [pd.Timestamp("2026-03-31")]

    def test_unmapped_labels_are_kept_as_they_are(self):
        yf = parse("itc").profit_loss.to_yfinance()
        assert "OPM %" in yf.index, "an unmapped row must survive rather than be dropped"
        assert yf.index.is_unique

    def test_the_mapping_table_covers_every_label_the_recorded_page_emits(self):
        """A recorded screener row with no yfinance equivalent is fine; a *missing* mapping for a
        label that is in the table would mean the table itself is out of date."""
        page_labels = set()
        for sym in SYMBOLS:
            profile = parse(sym)
            for field in ("profit_loss", "balance_sheet", "cash_flow"):
                page_labels |= set(getattr(profile, field).rows)
        assert page_labels - set(INDIAN_TO_YFINANCE_LABELS), "no unmapped labels at all is suspicious"


# ============================================================ documents


class TestDocuments:
    def test_concalls(self):
        concalls = parse("itc").concalls
        assert len(concalls) == 1
        call = concalls[0]
        assert call.date == "Jul 2026"
        assert call.transcript_url.startswith("https://www.bseindia.com/stockinfo/AnnPdfOpen.aspx")

    def test_annual_reports(self):
        reports = parse("itc").annual_reports
        assert reports[0]["url"].endswith(".pdf")
        assert reports[0]["url"].startswith("https://www.bseindia.com/xml-data/corpfiling/")

    def test_credit_ratings(self):
        ratings = parse("itc").credit_ratings
        assert ratings[0]["url"].startswith("https://www.crisil.com/")
        assert parse("tcs").credit_ratings[0]["url"].startswith("https://www.icra.in/")


class TestKnownParserGaps:
    """Two defects the recordings make visible, and one that has been **fixed**.

    The two remaining are *documented*, not fixed: each is asserted as it behaves today so that a
    change is a deliberate act rather than an accident — and so a reviewer can see the defect
    instead of discovering it in production. The third was a defect in this suite's own subject and
    is now pinned as working behaviour.
    """

    def test_announcements_parse_from_the_unmarked_container(self):
        """``CompanyProfile.announcements`` used to be **always empty**, and it was not missing data.

        ``_parse_documents`` selected ``.announcements a, #announcements a``. The live
        announcements list is the one ``div.documents`` block that carries **no distinguishing class
        and no id at all** — its three siblings are ``documents annual-reports``,
        ``documents credit-ratings`` and ``documents concalls`` — so the selector matched nothing on
        every company, and a field that looked implemented never was.

        The fix anchors on the container's ``<h3>Announcements</h3>`` heading, with the structural
        inverse (the unmarked ``div.documents``) as a second selector, so a rename of *either* one
        is caught rather than silently reverting to empty. This test pins the **result**; the two
        tests after it pin the two selectors independently, which is what makes a silent regression
        to "always empty" impossible to merge.
        """
        announcements = parse("itc").announcements
        assert len(announcements) == 5, "announcements are empty again; the selector regressed"
        first = announcements[0]
        assert first["title"].startswith("Closure of Trading Window")
        assert first["url"].startswith("https://www.bseindia.com/stockinfo/AnnPdfOpen.aspx?Pname=")
        # every entry is a real filing, and every URL is a document
        assert all(a["title"] for a in announcements)
        assert all(a["url"].startswith("http") for a in announcements)
        assert len({a["url"] for a in announcements}) == 5, "duplicate announcement URLs"
        # the container's other anchor is the "All" link to BSE's announcement index - navigation,
        # not an announcement, and it must not appear here
        assert not any(a["url"].endswith("/corp-announcements/") for a in announcements)
        assert not any(a["title"] == "All" for a in announcements)

    def test_announcements_carry_the_publication_date_the_page_publishes(self):
        """Each entry nests a ``<time datetime="2026-09-25T19:07:17+05:30">``; the title
        concatenates it without a separator, so the date is in the string.

        Pinned as a *behaviour* (which is what the parser does), not as a proposal to add a ``date``
        key — the model declares ``List[Dict[str, str]]``, and splitting that date out is a
        separate, deliberate change.
        """
        titles = [a["title"] for a in parse("itc").announcements]
        assert any("25 Sep" in t for t in titles), "the nested <time> text vanished from the title"
        assert any(t.startswith("Announcement under Regulation 30 (LODR)-") for t in titles)

    def test_tcs_has_no_announcements_container_and_returns_none_of_them(self):
        """The TCS recording dropped the announcements block (see META.json's trim rule), so it
        yields ``[]`` — which is how an *absent* container must look, and is what distinguishes it
        from the ITC recording's five real entries. If this ever returns entries, the trim changed.
        """
        assert parse("tcs").announcements == []
        soup = BeautifulSoup(company_html("tcs"), "lxml")
        assert not soup.select("section#documents div.documents:not([class*=annual])"
                               ":not([class*=credit]):not([class*=concall])")

    def test_the_announcements_selector_survives_renaming_either_half_of_it(self):
        """Both selectors, each exercised on its own, so a rename of one is not a silent empty.

        The heading selector is tried first, the structural inverse second. Killing the heading in
        the recorded markup must leave the second one working; killing the marker classes must leave
        the first one working. Losing *both* would restore the old bug, and
        :meth:`test_announcements_parse_from_the_unmarked_container` is what catches that.
        """
        parser = ScreenerHTMLParser()
        original = company_html("itc")
        reworded = original.replace(
            '<h3 class="margin-bottom-8">Announcements</h3>',
            '<h3 class="margin-bottom-8">Latest News</h3>',
        )
        assert reworded != original
        assert len(parser._parse_documents(BeautifulSoup(reworded, "lxml"))[3]) == 5, (
            "the structural fallback selector no longer finds the container"
        )

        unclassed = original.replace("documents annual-reports", "documents yrly-reports")
        assert unclassed != original
        assert len(parser._parse_documents(BeautifulSoup(unclassed, "lxml"))[3]) == 5, (
            "the heading selector no longer finds the container"
        )

    def test_the_concall_ppt_link_is_filed_as_the_transcript(self):
        """The 'Transcript' control is a ``div.button-chip``, not an anchor, so the only ``<a>`` in
        the row is the PPT - and the ``link.lower().endswith('.pdf')`` fallback claims it.
        ``presentation_url`` therefore stays None while ``transcript_url`` holds the deck."""
        call = parse("itc").concalls[0]
        assert call.transcript_url.endswith(".pdf")
        assert call.presentation_url is None
        assert call.audio_url is None
        assert "PPT" in call.title, "the title is the concatenation of the chip labels"

    def test_nested_anchor_text_is_concatenated_without_a_separator(self):
        """``get_text(strip=True)`` uses no separator, so 'Rating update' + '24 Apr' + 'from crisil'
        runs together. Present on the untrimmed recording too - it is a parser behaviour, not a
        fixture artefact."""
        assert parse("itc").credit_ratings[0]["title"] == "Rating update24 Apr from crisil"


# ============================================================ peers


class TestPeers:
    """``_parse_peers`` is tested for its **contract only** — never for the *values*.

    ⚠ **Why no test here asserts that the parsed rows are ITC's peers: because they are not.**
    ``/api/company/{id}/peers/`` returns a mis-scoped set. Measured 5/5 and byte-stable across
    repeat fetches (``META.json`` records identical bytes for two consecutive requests), so this is
    a real upstream defect, not a transient or a stale cache:

    ==================  =============================  ==============================================
    company             its actual industry            what the API returns
    ==================  =============================  ==============================================
    ITC 1552            Fast Moving Consumer Goods     HBL Engineering, Inox India, KRN, Esab, Ador
                                                        — all *Other Industrial Products*
    HDFCBANK 1298       Private Sector Bank            GNG Electronics, Rashi Peripheral, Moschip,
                                                        D-Link India
    SUNPHARMA 3245      Pharmaceuticals                JK Paper, West Coast Paper, N R Agarwal
    TATASTEEL 3373      Iron & Steel                   Redington, MMTC, MSTC
    TCS 3365            IT Software                    **HTTP 404**, reproducibly
    ==================  =============================  ==============================================

    Two of the three tests below carry that evidence, so the decision is re-derivable from this
    file rather than from a changelog. ``test_the_payload_is_disjoint_from_the_company_s_own_industry``
    proves the mis-scoping offline, using two *other* fixtures;
    ``test_the_payload_contradicts_itself`` records the internal inconsistency.

    **If you are here to "restore" assertions like ``peers[0].name == "HBL Engineering"``, do not.**
    They used to be in :meth:`test_every_peer_row_is_parsed` and they certified a wrong answer as
    correct: they pinned HBL Engineering as ITC's top-ranked peer while the same suite also pinned
    ITC's industry as *Diversified FMCG*. The parser has one contract — *shape* — and that is what
    is asserted. ``peers_itc.html`` is a verbatim recording and is still the right input for
    proving shape; it is not evidence of anything about peers.
    """

    def test_the_recording_is_a_real_peers_payload(self):
        soup = BeautifulSoup(text("peers_itc.html"), "html.parser")
        rows = soup.find("table", class_="data-table").find_all("tr")
        assert len(rows) == 10, "header + 8 peers + a median row"

    def test_every_data_row_becomes_exactly_one_peer(self):
        """Row count, from the recording rather than from memory.

        9 = 8 company rows + the trailing ``Median: N Co.`` row. The point is one-to-one, not the
        number 9: a row that is dropped, or a header that is emitted, moves this count.
        """
        assert len(parse("itc").peers) == len(_peer_cells()) == 9

    def test_the_eleven_columns_map_onto_the_fields_in_order(self):
        """field <- cell position, checked against the recording instead of a memo.

        This is the contract in full: the 11 cells map to 11 fields in order, the 12th field
        (``symbol``) is not a cell at all but the ``/company/SYM/`` href, and every numeric cell
        goes through the Indian-format parser. Nothing here says *which* companies those are — the
        endpoint is mis-scoped upstream, so a value assertion would be a wrong assertion.

        Reading the expected value out of the fixture on every run is what makes this a shape test
        rather than a transcription: change a number upstream and this test follows it, whereas the
        old test failed and invited someone to "fix" the expectation to the new wrong answer.
        """
        cells, peers = _peer_cells(), parse("itc").peers
        assert len(cells) == len(peers)
        for row, peer in zip(cells, peers):
            assert len(row) == 11, f"recording row is no longer 11 columns: {row}"
            for field, cell in zip(PEER_FIELDS_FROM_CELLS, row):
                if field == "name":
                    assert peer.name == cell
                elif field == "rank":
                    continue  # the median row has no S.No.; see the fallback test
                else:
                    assert getattr(peer, field) == pytest.approx(
                        parse_indian_number(cell)
                    ), f"{field} is not column {PEER_FIELDS_FROM_CELLS.index(field)}"

    def test_the_symbol_comes_from_the_href_and_not_from_a_cell(self):
        """``symbol`` is the one field with no column: it is the ``/company/SYM/`` segment.

        Checked per row against the recording's own href, including the case where the segment is
        a six-digit BSE scrip code rather than a ticker — a real row in this payload has one, and a
        regex that only accepted letters would silently drop that row's identity.
        """
        table = BeautifulSoup(text("peers_itc.html"), "html.parser").find("table", class_="data-table")
        linked = [re.search(r"/company/([A-Z0-9\-]+)/", tr.find("a")["href"]).group(1)
                  for tr in table.find_all("tr")[1:] if tr.find("a") and tr.find("a").get("href")]
        parsed = [p.symbol for p in parse("itc").peers if p.symbol is not None]
        assert parsed == linked
        assert any(s.isdigit() for s in linked), "the recording's BSE-scrip-code row is gone; re-check"
        assert all(re.fullmatch(r"[A-Z0-9\-]+", s) for s in linked)

    def test_no_peer_is_the_header_row(self):
        """The header is a single ``<tr>`` of ``<th>``, so it carries no ``<td>`` and the parser
        skips it twice over (by slicing and by the width check). Assert the outcome anyway: a
        future markup change that gives the header ``<td>`` cells must not start emitting a peer
        called 'Company'."""
        peers = parse("itc").peers
        assert all(p.name != "Company" for p in peers)
        assert all(p.name != "S.No." for p in peers)
        header = BeautifulSoup(text("peers_itc.html"), "html.parser").find("tr")
        assert header.find("td") is None, "the recording's header row now has td cells; re-check"

    def test_the_first_data_row_is_not_skipped(self):
        """``rows[1:]`` must drop the header, not the first company.

        Compared positionally: the first emitted peer must be the recording's first *data* row. The
        old version asserted ``peers[0].name == "HBL Engineering"``, which additionally claimed
        that HBL Engineering is ITC's top peer. It is not — see the class docstring.
        """
        peers = parse("itc").peers
        assert peers[0].name == _peer_cells()[0][1]
        assert peers[0].symbol is not None
        assert peers[-2].name == _peer_cells()[-2][1], "the row before the median row was skipped"

    def test_the_trailing_median_row_keeps_its_values_and_loses_its_symbol(self):
        """The last row has an empty S.No. cell, so rank falls back to position and there is no
        ``/company/SYM/`` href to read a symbol from."""
        peers = parse("itc").peers
        median = peers[-1]
        assert median.name.startswith("Median:")
        assert median.rank == 9, "rank must fall back to position when the S.No. cell is empty"
        assert median.symbol is None
        # its numbers still parse - the row is a company-less summary, not a skipped row
        assert median.market_cap_cr == pytest.approx(parse_indian_number(_peer_cells()[-1][4]))

    def test_a_row_with_no_usable_numbers_keeps_none_and_does_not_become_zero(self):
        """An empty cell is ``None``. A zero would be a fabricated number that sorts as a real one.

        Located from the recording, not by company name — this payload has a row with an empty P/E
        and an empty Qtr Sales Var%. Asserted in **both** directions, because a parser that
        returned ``None`` for everything would otherwise pass the first half of this test:

        * a blank cell -> the field is ``None``;
        * a cell that parses -> the field is not ``None``.
        """
        cells, peers = _peer_cells(), parse("itc").peers
        assert any(parse_indian_number(c[3]) is None for c in cells), (
            "the recording has no row with an empty P/E any more; re-check"
        )
        blanks = 0
        for row, peer in zip(cells, peers):
            for field, cell in zip(PEER_FIELDS_FROM_CELLS, row):
                if field in ("name", "rank"):
                    continue  # not numeric cells; covered by the fallback and mapping tests
                if not cell:
                    blanks += 1
                    assert getattr(peer, field) is None, (
                        f"blank {field} became {getattr(peer, field)!r} instead of None"
                    )
                else:
                    assert getattr(peer, field) is not None, f"{field} was dropped for a real cell"
        assert blanks == 2, "the recording's blank-cell count changed; re-check"

    def test_the_payload_is_disjoint_from_the_company_s_own_industry(self):
        """The evidence for refusing to assert values — and it needs no network.

        Three recordings, three independent screener.in routes, all agreeing that the two sets do
        not overlap even once:

        * ``company_itc.html``'s breadcrumb puts ITC in *Diversified FMCG* (pinned by
          :class:`TestSectorHierarchy`);
        * ``market.html`` says that leaf, ``IN040107001``, holds **5** companies;
        * ``industry_in040107001.html`` lists those 5 — Hind. Unilever, ITC, Hindustan Foods,
          Godavari Biorefineries, Davangere Sugar;
        * ``peers_itc.html`` returns 8 companies, **none of which is any of them**.

        A "peer set" with zero members in common with the peer's own industry is not a peer set. If
        this test ever fails, screener.in fixed the endpoint and value assertions can come back —
        deliberately, in a change that says so.
        """
        itc_industry = parse("itc").sub_industry
        assert itc_industry == "Diversified FMCG"
        market = ScreenerHTMLParser().parse_market_industries(text("market.html"))
        assert market.loc["IN040107001", "industry"] == itc_industry
        assert market.loc["IN040107001", "no_of_companies"] == 5.0

        real = {p.name for p in parse_industry("in040107001")}
        claimed = {p.name for p in parse("itc").peers if not p.name.startswith("Median:")}
        assert len(real) == 5 and len(claimed) == 8
        assert not (real & claimed), (
            f"the peers payload now overlaps ITC's real industry on {real & claimed} — screener.in "
            f"may have fixed the endpoint; value assertions could be revisited deliberately"
        )

    def test_the_payload_contradicts_itself(self):
        """8 company rows shipped; the payload's own median row says ``Median: 33 Co.``.

        Recorded because it is the cheapest signal that the endpoint is not deriving these rows
        from a consistent query — and because a future reader who sees 8 rows here should not
        assume 8 is what screener.in believes the peer count to be.
        """
        cells = _peer_cells()
        companies = [c for c in cells if not c[1].startswith("Median:")]
        median = next(c for c in cells if c[1].startswith("Median:"))
        claimed = int(re.match(r"Median:\s*(\d+)", median[1]).group(1))
        assert len(companies) == 8
        assert claimed == 33
        assert claimed != len(companies)

    def test_the_tcs_peers_endpoint_404s_and_the_profile_survives_it(self):
        """screener.in answers 404 for /api/company/3365/peers/ (checked three ways, see META.json).
        The client must treat that as 'no peers', not as a failure - and must not lose the profile."""
        assert "<title>" in text("peers_tcs.html")
        profile = parse("tcs")
        assert profile.peers == []
        assert profile.ratios.current_price == pytest.approx(2082.0)
        assert profile.company_id == 3365


# ============================================================ the chart time series


class TestChartTimeseries:
    """``get_chart_timeseries`` with the transport replaced. No sockets, no live call."""

    @pytest.mark.parametrize("sym", sorted(SYMBOLS))
    async def test_it_returns_the_recorded_columns_and_rows(self, sym):
        recorder = routes(company_route(sym), chart_route(sym))
        df = await offline_client(recorder).get_chart_timeseries(sym.upper(), metric="price", days=365)

        assert isinstance(df, pd.DataFrame)
        assert list(df.columns) == ["Price", "DMA50", "DMA200", "Volume"]
        assert df.index.name == "Date"
        assert isinstance(df.index, pd.DatetimeIndex)
        assert df.index.is_monotonic_increasing, "the two recorded windows must sort chronologically"
        assert len(df) == 8
        assert df["Price"].iloc[0] == pytest.approx(407.25 if sym == "itc" else 2896.1)
        assert df["Price"].iloc[-1] == pytest.approx({"itc": 269.0, "tcs": 2082.0}[sym])
        assert df["Volume"].iloc[0] == pytest.approx(20060143.0 if sym == "itc" else 3297523.0)

    @pytest.mark.parametrize("sym", sorted(SYMBOLS))
    async def test_the_last_close_is_the_price_the_profile_reports(self, sym):
        """Two different endpoints, one number: the chart's final close and #top-ratios' 'Current
        Price'. A stale or mismatched recording cannot satisfy both."""
        recorder = routes(company_route(sym), chart_route(sym))
        df = await offline_client(recorder).get_chart_timeseries(sym.upper(), days=365)
        assert df["Price"].iloc[-1] == pytest.approx(parse(sym).ratios.current_price)

    @pytest.mark.parametrize("sym", sorted(SYMBOLS))
    async def test_it_requests_the_company_page_then_the_chart_api(self, sym):
        recorder = routes(company_route(sym), chart_route(sym))
        await offline_client(recorder).get_chart_timeseries(sym.upper(), days=365)
        assert len(recorder.calls) == 2
        assert recorder.calls[0] == f"https://www.screener.in/company/{sym.upper()}/"
        assert recorder.calls[1].startswith(f"https://www.screener.in/api/company/{SYMBOLS[sym]}/chart/")

    async def test_a_non_200_chart_is_an_upstream_error_not_an_empty_frame(self):
        from bfinance.utils.exceptions import UpstreamServiceError

        recorder = routes(company_route("itc"), (f"/api/company/{SYMBOLS['itc']}/chart/", 500, "{}"))
        with pytest.raises(UpstreamServiceError):
            await offline_client(recorder).get_chart_timeseries("ITC", days=365)

    def test_the_metric_catalog_maps_every_shorthand(self):
        for shorthand in ("price", "pe", "margins", "ev_ebitda", "pb", "mcap_sales"):
            assert ScreenerChartEngine.get_query_param(shorthand) in ScreenerChartEngine.METRIC_QUERIES.values()

    def test_an_empty_datasets_payload_is_an_empty_frame_not_a_crash(self):
        assert ScreenerChartEngine.parse_chart_json_to_dataframe({}).empty

    def test_a_value_pair_shorter_than_two_entries_is_dropped_rather_than_crashing(self):
        df = ScreenerChartEngine.parse_chart_json_to_dataframe(
            {"datasets": [{"metric": "Price", "values": [["2026-01-01", 10.0], ["2026-01-02"]]}]}
        )
        assert list(df["Price"]) == [10.0]

    def test_no_datasets_is_a_zero_by_zero_frame(self):
        """Not merely ``.empty``: a 1-column empty frame would also be empty, and a caller doing
        ``df[cols]`` would get a KeyError instead of an empty result."""
        df = ScreenerChartEngine.parse_chart_json_to_dataframe({"datasets": []})
        assert df.empty
        assert df.shape == (0, 0)

    def test_an_unsorted_payload_still_yields_a_chronological_index(self):
        """The recorded windows are already ascending, so sorting is a no-op on them. Feed a
        descending payload, which is the only way to see whether the sort happens at all."""
        df = ScreenerChartEngine.parse_chart_json_to_dataframe(
            {"datasets": [{"metric": "Price", "values": [
                ["2026-03-02", 3.0], ["2026-01-02", 1.0], ["2026-02-02", 2.0],
            ]}]}
        )
        assert df.index.is_monotonic_increasing
        assert list(df["Price"]) == [1.0, 2.0, 3.0]


# ============================================================ the client, end to end


class TestProfileOverStubbedTransport:
    """``get_company_profile`` against the recordings: request sequence, id, peers, parse."""

    async def test_it_parses_the_whole_profile_from_two_recorded_gets(self):
        recorder = routes(consolidated_route("itc"), peers_route("itc"))
        profile = await offline_client(recorder).get_company_profile("ITC")

        assert recorder.calls == [
            "https://www.screener.in/company/ITC/consolidated/",
            "https://www.screener.in/api/company/1552/peers/",
        ]
        assert profile.symbol == "ITC" and profile.company_id == 1552
        assert profile.name == "ITC Ltd"
        assert profile.is_consolidated is True
        assert profile.url.endswith("/company/ITC/consolidated/")
        assert profile.bse_code == "500875" and profile.nse_symbol == "ITC"
        assert profile.website == "https://www.itcportal.com"
        assert profile.about.startswith("Established in 1910, ITC is the largest cigarette")
        assert profile.ratios.market_cap == pytest.approx(337071.0)
        assert profile.sector == "Fast Moving Consumer Goods"
        assert len(profile.peers) == 9
        assert profile.profit_loss.rows["Sales"] == pytest.approx([78868.0, 76488.0])

    async def test_a_missing_standalone_page_is_only_fetched_when_ratios_are_absent(self):
        """ITC's recorded page has ratios, so the standalone fallback must not fire - a test that
        does not assert this would pass even if the client fetched three pages every time."""
        recorder = routes(consolidated_route("itc"), peers_route("itc"))
        await offline_client(recorder).get_company_profile("ITC")
        assert not any(call.endswith("/company/ITC/") for call in recorder.calls[1:])

    async def test_the_standalone_fallback_fires_when_the_card_is_gone(self):
        """Break the card in the recording and the client must try the standalone page - the
        behaviour ``get_company_profile`` documents for debt-free BSE/IRFC-style entities."""
        broken = company_html("itc").replace('id="top-ratios"', 'id="top-ratios-GONE"')
        assert extract_company_id(broken) == 1552  # the id survives, only the card is gone
        recorder = routes(
            consolidated_route("itc", body=broken),
            peers_route("itc"),
            company_route("itc"),
        )
        profile = await offline_client(recorder).get_company_profile("ITC")
        # peers are fetched before the standalone fallback, so the standalone page is the last call
        assert recorder.calls[-1] == "https://www.screener.in/company/ITC/"
        assert recorder.calls[:2] == [
            "https://www.screener.in/company/ITC/consolidated/",
            "https://www.screener.in/api/company/1552/peers/",
        ]
        assert profile.is_consolidated is False
        assert profile.ratios.market_cap == pytest.approx(337071.0)

    async def test_a_peers_404_leaves_a_complete_profile(self):
        recorder = routes(
            consolidated_route("tcs"),
            (f"/api/company/{SYMBOLS['tcs']}/peers/", 404, text("peers_tcs.html")),
        )
        profile = await offline_client(recorder).get_company_profile("TCS")
        assert profile.company_id == 3365
        assert profile.peers == []
        assert profile.name == "Tata Consultancy Services Ltd"
        assert profile.ratios.current_price == pytest.approx(2082.0)
        assert profile.balance_sheet.rows["Total Assets"] == pytest.approx([158649.0, 181167.0])

    async def test_a_404_on_the_consolidated_page_falls_back_then_gives_up(self):
        from bfinance.utils.exceptions import TickerNotFoundError

        recorder = routes(
            ("/company/NOSUCH/consolidated/", 404, ""),
            ("/company/NOSUCH/", 404, ""),
        )
        with pytest.raises(TickerNotFoundError):
            await offline_client(recorder).get_company_profile("NOSUCH")

    async def test_a_non_404_error_is_an_upstream_error(self):
        from bfinance.utils.exceptions import UpstreamServiceError

        recorder = routes(("/company/ITC/consolidated/", 500, ""))
        with pytest.raises(UpstreamServiceError):
            await offline_client(recorder).get_company_profile("ITC")

    async def test_a_page_with_no_company_id_still_yields_a_profile_and_says_so(self):
        """The id is missing -> peers are skipped and company_id is 0. Previously this was
        indistinguishable from 'the upstream was blocked'."""
        stripped = company_html("itc").replace('data-company-id="1552"', 'data-warehouse-id="1552"')
        assert extract_company_id(stripped) is None
        recorder = routes(consolidated_route("itc", body=stripped), peers_route("itc"))
        profile = await offline_client(recorder).get_company_profile("ITC")
        assert profile.company_id == 0
        assert profile.peers == []
        assert profile.ratios.market_cap == pytest.approx(337071.0)
        assert len(recorder.calls) == 1, "no company id means no peers request"


# ============================================================ robots / policy


class TestRobotsCompliance:
    """screener.in's robots.txt, and the requests this package actually makes.

    Published rules, verbatim::

        Disallow: /user/*
        Disallow: /*?q=
        Disallow: /*?sort=
        Disallow: /*?limit=
        Disallow: /*?page=
        Disallow: /company/source/quarter/*

    ``*`` is the only wildcard, and ``?`` is a literal. So ``/*?q=`` is "any path, then ``/?q=``":
    it forbids a query string whose *first* parameter is ``q``, and permits the same parameter in
    any later position. That distinction is the whole reason the chart API is recorded with
    ``companyId=`` first.
    """

    PREFIX_RULES = ("/user/", "/company/source/quarter/")
    #: a leading query parameter, by name
    LEADING_QUERY_RULES = ("q", "sort", "limit", "page")

    @classmethod
    def _violates(cls, url: str) -> bool:
        parsed = httpx.URL(url)
        path = parsed.path + (("?" + parsed.query.decode()) if parsed.query else "")
        if any(path.startswith(prefix) for prefix in cls.PREFIX_RULES):
            return True
        return any(f"/?{name}=" in path for name in cls.LEADING_QUERY_RULES)

    def test_the_matcher_itself_agrees_with_the_published_rules(self):
        """A compliance test with a broken matcher is worse than none, so pin both directions."""
        assert self._violates("https://www.screener.in/api/company/1552/chart/?q=Price&days=365")
        assert self._violates("https://www.screener.in/company/?q=ITC")
        assert self._violates("https://www.screener.in/user/1")
        assert self._violates("https://www.screener.in/company/source/quarter/1/")
        assert not self._violates(
            "https://www.screener.in/api/company/1552/chart/?companyId=1552&q=Price&days=365"
        )
        assert not self._violates("https://www.screener.in/company/ITC/consolidated/")

    def test_no_recorded_url_was_disallowed(self):
        meta = payload("META.json")
        for name, entry in meta["files"].items():
            assert not self._violates(entry["url"]), f"{name} was recorded from a disallowed URL"

    def test_the_search_route_is_never_called_by_the_paths_we_exercise(self):
        import bfinance.screener.client as client_module

        source = Path(client_module.__file__).read_text(encoding="utf-8")
        assert "await self.search(" not in source, "a live code path calls the robots-disallowed search API"

    @pytest.mark.parametrize("sym", sorted(SYMBOLS))
    async def test_the_chart_request_is_not_robots_disallowed(self, sym):
        """The chart query carries ``q=``, so it must not be the first parameter.

        screener.in answers ``?companyId=…&q=…&days=…`` 200 (that is the URL both recordings were
        taken from, 246 points x 4 datasets) while ``?q=…&days=…`` matches ``Disallow: /*?q=``.
        """
        recorder = routes(company_route(sym), chart_route(sym))
        await offline_client(recorder).get_chart_timeseries(sym.upper(), days=365)
        chart_url = recorder.calls[-1]
        assert not self._violates(chart_url), f"chart request violates robots.txt: {chart_url}"


# ============================================================ branches the recordings do not reach


class TestBranchesTheRecordingsDoNotExercise:
    """Four guard clauses the two recorded pages never trigger, so no fixture can prove them.

    Each of these is an **equivalent mutant** on the recorded markup: mutating the guard changes
    nothing observable, because screener.in does not currently emit the input that reaches it.
    Asserting the branch directly is the only way these lines are not untested - and a mutation
    harness that reports them as survivors is telling the truth about the coverage, not failing.
    """

    #: a minimal ``.company-info`` block in the shape the parser expects. The unlabelled external
    #: links sit on *both* sides of the labelled one, which is what distinguishes the real rule
    #: ("the first eligible link, then any link labelled 'Website' may replace it") from the
    #: tempting wrong one ("the last eligible link wins"). screener.in happens to put nothing
    #: eligible after its Website link, so no recording can tell the two apart.
    INFO_BLOCK = """
    <html><body><h1>Example Ltd</h1>
    <div class="company-info"><div class="company-profile">
      <div class="sub show-more-box about"><p>About the company.</p></div>
      <a href="https://example.com/about">More</a>
      <a href="https://example.com">Website</a>
      <a href="https://vendor.example.net/terms">Terms</a>
    </div></div></body></html>
    """

    def test_the_website_is_the_labelled_link_not_the_last_external_one(self):
        """``_parse_header_meta`` records the first eligible link and then lets a link whose text
        is exactly 'Website' replace it. Here the last eligible link is an unlabelled Terms page,
        so 'last one wins' would return the Terms URL."""
        soup = BeautifulSoup(self.INFO_BLOCK, "lxml")
        parser = ScreenerHTMLParser()
        _, _, website, _, _ = parser._parse_header_meta(soup, fallback_symbol="EX")
        assert website == "https://example.com"

    def test_an_unlabelled_link_is_still_kept_when_nothing_is_labelled(self):
        """The label check must not be the only way a website is found."""
        soup = BeautifulSoup(
            '<html><body><h1>X</h1><div class="company-info">'
            '<a href="https://only.example.com/x">Home</a></div></body></html>', "lxml",
        )
        _, _, website, _, _ = ScreenerHTMLParser()._parse_header_meta(soup, fallback_symbol="X")
        assert website == "https://only.example.com/x"

    def test_anchor_fragments_and_empty_hrefs_are_skipped(self):
        soup = BeautifulSoup(
            '<html><body><h1>X</h1><div class="company-info"><a href="#top">jump</a>'
            '<a href="">none</a><a href="https://example.com/x">Website</a></div></body></html>',
            "lxml",
        )
        _, _, website, _, _ = ScreenerHTMLParser()._parse_header_meta(soup, fallback_symbol="X")
        assert website == "https://example.com/x"

    def test_a_peers_row_narrower_than_four_cells_is_skipped_not_indexed(self):
        """The recorded peers payload has 11 cells in every row, so the ``len(cells) < 4`` guard is
        never reached. A truncated row must be skipped rather than raise IndexError."""
        payload_html = (
            '<html><body><table class="data-table">'
            "<tr><th>S.No.</th><th>Company</th><th>CMP</th><th>P/E</th></tr>"
            "<tr><td>1.</td><td>Wide Co</td><td>10</td><td>5</td></tr>"
            "<tr><td>2.</td><td>Narrow Co</td></tr>"
            "</table></body></html>"
        )
        peers = ScreenerHTMLParser()._parse_peers(payload_html)
        assert [p.name for p in peers] == ["Wide Co"]

    def test_a_page_with_no_peers_section_yields_a_null_sector_rather_than_a_crash(self):
        parser = ScreenerHTMLParser()
        sector, group, industry, sub, indices = parser._parse_sector_hierarchy(
            BeautifulSoup("<html><body>no peers section</body></html>", "lxml")
        )
        assert (sector, group, industry, sub, indices) == (None, None, None, None, [])

    def test_a_table_with_no_thead_falls_back_to_the_first_row(self):
        from bfinance.models.statements import FinancialStatement

        table = BeautifulSoup(
            "<table><tr><th>Metric</th><th>Mar 2026</th></tr>"
            "<tr><td>Sales</td><td>100</td></tr></table>", "lxml",
        ).find("table")
        statement = ScreenerHTMLParser()._parse_table_element(table)
        assert isinstance(statement, FinancialStatement)
        assert statement.headers == ["Metric", "Mar 2026"]
        assert statement.rows == {"Sales": [100.0]}


# ============================================================ the fixtures themselves


class TestFixtureProvenance:
    """The fixtures are the evidence. If they drift from ``META.json`` the tests above are fiction."""

    def test_meta_records_every_file_on_disk(self):
        meta = payload("META.json")
        on_disk = {p.name for p in FIXTURES.iterdir() if p.suffix != ".pyc"}
        assert on_disk == set(meta["files"]) | {"META.json", "NOTICE"}

    def test_every_entry_is_a_real_recording(self):
        for name, entry in payload("META.json")["files"].items():
            assert entry["url"].startswith("https://www.screener.in/"), name
            assert entry["http_status"] in (200, 404), name
            assert entry["original_bytes"] > 0, name
            assert len(entry["original_sha256_16"]) == 16, name
            assert entry["trim_rule"], name
            # Assert the sha of the *normalised* content, not `stat().st_size`. Git checks these
            # out with CRLF on Windows and LF elsewhere, so a raw size or sha assertion passes in
            # the working tree where Python wrote the file and then fails on any fresh clone —
            # the exact "green locally, red in CI" failure this suite exists to prevent.
            # `.gitattributes` pins `eol=lf` for the fixture tree; normalising here keeps the test
            # correct even where that is not honoured. The sha is the real assertion: it detects
            # any edit to a recording, which a size check alone would not.
            raw = (FIXTURES / name).read_bytes().replace(b"\r\n", b"\n")
            assert hashlib.sha256(raw).hexdigest() == entry["stored_sha256"], (
                f"{name} does not match the content META.json describes — the fixture was edited, "
                "truncated, or line-ending translated since it was recorded"
            )
            assert len(raw) == entry["stored_bytes"], name
            assert entry["stored_line_endings"] == "lf", name

    def test_the_recordings_are_much_larger_than_the_fixtures(self):
        """A trim that did not trim would make ``stored_bytes == original_bytes`` everywhere."""
        for entry in payload("META.json")["files"].values():
            assert entry["stored_bytes"] <= entry["original_bytes"]
        trimmed = [e for e in payload("META.json")["files"].values()
                   if e["stored_bytes"] < e["original_bytes"]]
        assert len(trimmed) >= 4

    def test_the_chart_fixtures_record_their_true_point_count(self):
        """A frame must not be able to claim 246 bars from an 8-row excerpt."""
        for sym in SYMBOLS:
            raw = payload(f"chart_{sym}.json")
            for dataset in raw["datasets"]:
                assert dataset["_fixture_full_point_count"] == 246
                assert len(dataset["values"]) == 8
                assert dataset["_fixture_trim_rule"]

    def test_the_notice_says_what_these_files_are(self):
        notice = text("NOTICE")
        assert "screener.in" in notice
        assert "test fixtures only" in notice
        assert "robots" in notice.lower()

    def test_meta_names_the_robots_policy_that_constrained_the_recording(self):
        assert "/*?q=" in payload("META.json")["robots"]
