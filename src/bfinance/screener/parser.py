"""
High-precision DOM and HTML table parser for Screener.in financial statements, ratios, and filings.
"""

import logging
import re
from typing import Any, Dict, List, Optional, Tuple
import pandas as pd
from bs4 import BeautifulSoup

from bfinance.models.company import (
    AnalysisInsights,
    CompanyProfile,
    Concall,
    PeerStock,
    TopRatios,
)
from bfinance.models.statements import FinancialStatement
from bfinance.utils.formatting import parse_indian_number

logger = logging.getLogger("bfinance")

#: The eight per-industry aggregate columns ``/market/`` prints, in document order. A leaf-industry
#: row is ``S.No. | Industry | <these eight>``, so the industry name is the only text column and
#: everything after it is a number.
MARKET_AGGREGATE_COLUMNS: Tuple[str, ...] = (
    "no_of_companies",
    "total_market_cap",
    "median_market_cap",
    "median_pe",
    "sales_growth",
    "opm",
    "wtd_avg_roce",
    "median_1y_return",
)

#: ``/market/`` links every leaf industry as a four-segment code path under ``/market/``:
#: ``/market/IN04/IN0401/IN040107/IN040107001/``. The leaf is always the deepest segment, and the
#: parents are its prefixes (``IN04`` -> ``IN0401`` -> ``IN040107``).
_MARKET_LEAF_HREF_RE = re.compile(r"^/market/((?:[A-Z0-9]+/){3}[A-Z0-9]+)/?$")

#: ``div[data-page-info]`` on a leaf-industry page reads ``"218 results found: Showing page 1 of 9"``.
#: Both halves matter and neither is redundant: the first is the count the *page* believes in, the
#: second is the only statement of how many pages the full list spans.
_PAGE_INFO_RE = re.compile(
    r"(?P<results_found>[\d,]+)\s+results found"
    r"(?::\s*Showing page\s+(?P<page>\d+)\s+of\s+(?P<pages>\d+))?",
    re.IGNORECASE,
)


def _first_present(raw: Dict[str, Optional[float]], *keys: str) -> Optional[float]:
    """First of ``keys`` whose parsed value is not ``None``, in the order given.

    screener.in spells some ratios two ways on the same page family -- "Stock P/E" /
    "P/E", "Debt to equity" / "Debt / Eq", "Price to book value" / "P/B" -- so each
    canonical label is paired with its alias, canonical first.

    The membership test is ``is not None`` and deliberately NOT truthiness. A genuine
    ``0.00`` is a real, reportable number (a debt-free company prints 0.00 D/E; a company
    with a fully written-off equity prints 0.00 P/B), and ``a or b`` cannot tell one from
    a missing value. It failed in both directions, measured on real fragments:

    * primary ``0.00``, alias absent  -> ``None`` (the real zero vanished)
    * primary ``0.00``, alias ``7.77`` -> ``7.77`` (the real zero was replaced by a
      *different wrong number*, which is worse than vanishing)

    Only the second is recoverable: ``custom_ratios`` still carried the ``0.0`` while the
    typed field was already lost. Returns ``None`` only when no key holds a value, so a
    real zero, a missing value, and an alias fallback stay three distinguishable outcomes.
    """
    for key in keys:
        value = raw.get(key)
        if value is not None:
            return value
    return None


class ScreenerHTMLParser:
    """
    DOM Parser extracting 100% of structured tables, corporate media, and qualitative notes from Screener.in HTML.
    """

    def parse_full_profile(
        self,
        symbol: str,
        company_id: int,
        main_html: str,
        peers_html: Optional[str] = None,
        is_consolidated: bool = True,
        page_url: str = "",
    ) -> CompanyProfile:
        soup = BeautifulSoup(main_html, "lxml")

        # 1. Header Metadata & Links
        company_name, about_text, website, bse_code, nse_symbol = self._parse_header_meta(
            soup, fallback_symbol=symbol
        )

        # 2. Sector Hierarchy & Index Memberships
        sector, ind_grp, industry, sub_ind, indices = self._parse_sector_hierarchy(soup)

        # 3. Top Ratios Card
        top_ratios = self._parse_top_ratios(soup)

        # 4. Pros & Cons
        analysis = self._parse_analysis(soup)

        # 5. CAGRs
        cagrs = self._parse_cagrs(soup)

        # 6. Financial Statements Tables
        quarters = self._parse_table_section(soup, "quarters")
        profit_loss = self._parse_table_section(soup, "profit-loss")
        balance_sheet = self._parse_table_section(soup, "balance-sheet")
        cash_flow = self._parse_table_section(soup, "cash-flow")
        ratios_history = self._parse_table_section(soup, "ratios")
        shareholding_q, shareholding_y = self._parse_shareholding_sections(soup)

        # 7. Corporate Documents & Concalls
        concalls, annual_reports, credit_ratings, announcements = self._parse_documents(soup)

        # 8. Peers (from peers_html if provided, else fallback to soup)
        peers = self._parse_peers(peers_html) if peers_html else []

        return CompanyProfile(
            symbol=symbol.upper(),
            company_id=company_id,
            name=company_name,
            about=about_text,
            website=website,
            bse_code=bse_code,
            nse_symbol=nse_symbol,
            is_consolidated=is_consolidated,
            url=page_url,
            sector=sector,
            industry_group=ind_grp,
            industry=industry,
            sub_industry=sub_ind,
            indices=indices,
            ratios=top_ratios,
            analysis=analysis,
            cagrs=cagrs,
            quarters=quarters,
            profit_loss=profit_loss,
            balance_sheet=balance_sheet,
            cash_flow=cash_flow,
            ratios_history=ratios_history,
            shareholding=shareholding_q,
            shareholding_yearly=shareholding_y,
            peers=peers,
            concalls=concalls,
            annual_reports=annual_reports,
            credit_ratings=credit_ratings,
            announcements=announcements,
        )

    def _parse_header_meta(
        self, soup: BeautifulSoup, fallback_symbol: str
    ) -> Tuple[str, str, Optional[str], Optional[str], Optional[str]]:
        # Name
        h1 = soup.find("h1")
        company_name = h1.get_text(strip=True) if h1 else fallback_symbol

        # About text
        about_div = soup.select_one(".company-profile .about p") or soup.select_one(".about p")
        about_text = about_div.get_text(strip=True) if about_div else ""

        # Links (Website, BSE, NSE)
        website = None
        bse_code = None
        nse_symbol = None

        links = soup.select(".company-links a, .links a, .company-info a, .company-profile a")
        for a in links:
            href = a.get("href", "").strip()
            text = a.get_text(strip=True)
            if not href or href.startswith("#"):
                continue

            if "bseindia.com" in href:
                match = re.search(r"/(\d{6})/?", href)
                if match:
                    bse_code = match.group(1)
                elif "BSE" in text and re.search(r"\d{6}", text):
                    bse_code = re.search(r"\d{6}", text).group(0)
            elif "nseindia.com" in href:
                match = re.search(r"symbol=([A-Z0-9\-]+)", href)
                if match:
                    nse_symbol = match.group(1)
            elif (
                href.startswith("http")
                and "screener.in" not in href
                and "bseindia" not in href
                and "nseindia" not in href
                and not href.lower().endswith(".pdf")
            ):
                if text.lower() == "website" or not website:
                    website = href

        return company_name, about_text, website, bse_code, nse_symbol

    def _parse_sector_hierarchy(
        self, soup: BeautifulSoup
    ) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str], List[str]]:
        sector = None
        ind_grp = None
        industry = None
        sub_ind = None
        indices: List[str] = []

        peers_sec = soup.find("section", id="peers")
        if peers_sec:
            # Breadcrumbs from /market/ links
            crumbs = [a.get_text(strip=True) for a in peers_sec.find_all("a") if a.get("href") and "/market/" in a.get("href")]
            if len(crumbs) >= 1:
                sector = crumbs[0]
            if len(crumbs) >= 2:
                ind_grp = crumbs[1]
            if len(crumbs) >= 3:
                industry = crumbs[2]
            if len(crumbs) >= 4:
                sub_ind = crumbs[3]

            # Indices part of
            for a in peers_sec.find_all("a"):
                txt = a.get_text(strip=True)
                if any(k in txt.lower() for k in ["bse", "nifty", "sensex"]):
                    if txt not in indices:
                        indices.append(txt)

        return sector, ind_grp, industry, sub_ind, indices

    def _parse_top_ratios(self, soup: BeautifulSoup) -> TopRatios:
        raw_ratios: Dict[str, Optional[float]] = {}

        for li in soup.select("#top-ratios li"):
            name_el = li.select_one(".name")
            val_el = li.select_one(".value")
            if not name_el or not val_el:
                continue

            name = name_el.get_text(strip=True)
            raw_text = val_el.get_text(strip=True)

            if "High / Low" in name or "High/Low" in name:
                # Split into high_52w / low_52w. The combined string used to also be handed to a
                # `high_low_52w=` kwarg that TopRatios does not declare, so pydantic dropped it -
                # it was parsed, formatted, then discarded.
                parts = raw_text.split("/")
                if len(parts) == 2:
                    raw_ratios["High"] = parse_indian_number(parts[0])
                    raw_ratios["Low"] = parse_indian_number(parts[1])
            else:
                raw_ratios[name] = parse_indian_number(raw_text)

        return TopRatios(
            market_cap=raw_ratios.get("Market Cap"),
            current_price=raw_ratios.get("Current Price"),
            high_52w=raw_ratios.get("High"),
            low_52w=raw_ratios.get("Low"),
            # `high_low_52w` used to be passed here. TopRatios does not declare it, so pydantic
            # discarded it silently (model_extra is None) - the value was parsed, formatted and
            # then thrown away. The individual high_52w / low_52w above are the real carriers.
            # The three aliased labels go through _first_present, not `or`: a genuine 0.00
            # must survive, must not be replaced by the alias, and must stay distinguishable
            # from an absent value. See _first_present for the measured failure modes.
            stock_pe=_first_present(raw_ratios, "Stock P/E", "P/E"),
            book_value=raw_ratios.get("Book Value"),
            dividend_yield=raw_ratios.get("Dividend Yield"),
            roce=raw_ratios.get("ROCE"),
            roe=raw_ratios.get("ROE"),
            face_value=raw_ratios.get("Face Value"),
            debt_to_equity=_first_present(raw_ratios, "Debt to equity", "Debt / Eq"),
            peg_ratio=raw_ratios.get("PEG Ratio"),
            price_to_book=_first_present(raw_ratios, "Price to book value", "P/B"),
            eps_ttm=raw_ratios.get("EPS"),
            promoter_holding=raw_ratios.get("Promoter holding"),
            promoter_pledged=raw_ratios.get("Pledged percentage"),
            free_cash_flow_3y=raw_ratios.get("Free Cash Flow 3Y"),
            # Unfiltered pass-through of every label screener.in printed, so a value the
            # typed fields above dropped is still recoverable by its raw label.
            custom_ratios=raw_ratios,
        )

    def _parse_analysis(self, soup: BeautifulSoup) -> AnalysisInsights:
        pros = [li.get_text(strip=True) for li in soup.select(".pros ul li")]
        cons = [li.get_text(strip=True) for li in soup.select(".cons ul li")]
        return AnalysisInsights(pros=pros, cons=cons)

    def _parse_cagrs(self, soup: BeautifulSoup) -> Dict[str, Dict[str, str]]:
        cagrs: Dict[str, Dict[str, str]] = {}
        for table in soup.select(".ranges-table"):
            th = table.find("th")
            if not th:
                continue
            title = th.get_text(strip=True)
            cagrs[title] = {}
            for tr in table.find_all("tr"):
                tds = tr.find_all("td")
                if len(tds) >= 2:
                    k = tds[0].get_text(strip=True)
                    v = tds[1].get_text(strip=True)
                    cagrs[title][k] = v
        return cagrs

    def _parse_table_element(self, table) -> FinancialStatement:
        if not table:
            return FinancialStatement()

        thead = table.find("thead")
        headers = []
        if thead:
            headers = [th.get_text(strip=True) for th in thead.find_all("th") if th.get_text(strip=True)]
        else:
            first_tr = table.find("tr")
            if first_tr:
                headers = [th.get_text(strip=True) for th in first_tr.find_all("th") if th.get_text(strip=True)]

        rows: Dict[str, List[Optional[float]]] = {}
        tbody = table.find("tbody") or table
        for tr in tbody.find_all("tr"):
            cells = tr.find_all("td")
            if not cells:
                continue
            row_name = cells[0].get_text(strip=True).replace("+", "").strip()
            if row_name in headers:
                continue
            values = [parse_indian_number(c.get_text(strip=True)) for c in cells[1:]]
            if row_name:
                rows[row_name] = values

        return FinancialStatement(headers=headers, rows=rows)

    def _parse_table_section(self, soup: BeautifulSoup, section_id: str) -> FinancialStatement:
        sec = soup.find("section", id=section_id)
        if not sec:
            return FinancialStatement()
        table = sec.find("table", class_="data-table")
        return self._parse_table_element(table)

    def _parse_shareholding_sections(
        self, soup: BeautifulSoup
    ) -> Tuple[FinancialStatement, FinancialStatement]:
        sh_sec = soup.find("section", id="shareholding")
        if not sh_sec:
            return FinancialStatement(), FinancialStatement()

        tables = sh_sec.find_all("table", class_="data-table")
        quarterly = self._parse_table_element(tables[0]) if len(tables) >= 1 else FinancialStatement()
        yearly = self._parse_table_element(tables[1]) if len(tables) >= 2 else FinancialStatement()
        return quarterly, yearly

    def _parse_documents(
        self, soup: BeautifulSoup
    ) -> Tuple[List[Concall], List[Dict[str, str]], List[Dict[str, str]], List[Dict[str, str]]]:
        concalls: List[Concall] = []
        annual_reports: List[Dict[str, str]] = []
        credit_ratings: List[Dict[str, str]] = []
        announcements: List[Dict[str, str]] = []

        doc_sec = soup.find("section", id="documents")
        if not doc_sec:
            return concalls, annual_reports, credit_ratings, announcements

        # Concalls
        concall_items = doc_sec.select(".concalls ul li") or doc_sec.select("#concalls ul li")
        for li in concall_items:
            title_text = li.get_text(" ", strip=True)
            date_match = re.search(r"(\w+\s+\d{4}|\d{1,2}\s+\w+\s+\d{4})", title_text)
            date_str = date_match.group(1) if date_match else ""

            transcript_url = None
            audio_url = None
            ppt_url = None

            for a in li.find_all("a"):
                link = a.get("href", "")
                link_text = a.get_text(strip=True).lower()
                if "audio" in link_text or "mp3" in link.lower() or "recording" in link_text or "rec" in link_text:
                    audio_url = link
                elif "transcript" in link_text or link.lower().endswith(".pdf"):
                    transcript_url = link
                elif "ppt" in link_text or "presentation" in link_text or "notes" in link_text:
                    ppt_url = link
                elif not transcript_url:
                    transcript_url = link

            concalls.append(
                Concall(
                    date=date_str,
                    title=title_text,
                    transcript_url=transcript_url,
                    audio_url=audio_url,
                    presentation_url=ppt_url,
                )
            )

        # Annual reports
        for a in doc_sec.select(".annual-reports a, #annual-reports a"):
            annual_reports.append({
                "year": a.get_text(strip=True),
                "url": a.get("href", "")
            })

        # Credit ratings
        for a in doc_sec.select(".credit-ratings a, #credit-ratings a"):
            credit_ratings.append({
                "title": a.get_text(strip=True),
                "url": a.get("href", "")
            })

        # Announcements.
        #
        # This used to select `.announcements a, #announcements a`, which matches **nothing**: the
        # live announcements list is the one `div.documents` block that carries no distinguishing
        # class and **no id at all** (its three siblings are `documents annual-reports`,
        # `documents credit-ratings` and `documents concalls`). `CompanyProfile.announcements` was
        # therefore always empty — a field that looked implemented and never was.
        #
        # Two selectors, so that a rename of *either* one is caught rather than silently reverting
        # to empty: the heading text (semantic, survives a class shuffle) and the structural inverse
        # (the unmarked `div.documents`, survives a reworded heading). The first match only, because
        # a future fifth `div.documents` with no marker class would otherwise double-count.
        ann_block = next(
            (d for d in doc_sec.select("div.documents")
             if (h3 := d.find("h3")) and h3.get_text(strip=True) == "Announcements"),
            None,
        ) or doc_sec.select_one(
            "div.documents:not(.annual-reports):not(.credit-ratings):not(.concalls)"
        )
        # `ul.list-links` is the list itself; the container's other anchor is the "All" link to
        # BSE's announcement page, which is navigation and not an announcement.
        for a in (ann_block.select("ul.list-links a") if ann_block else []):
            announcements.append({
                "title": a.get_text(strip=True),
                "url": a.get("href", "")
            })

        return concalls, annual_reports, credit_ratings, announcements

    def _parse_peers(self, peers_html: str) -> List[PeerStock]:
        """Parse the 11-column screener.in company table.

        ⚠ **The input must be a table of *constituents of a known industry*, not a trustable peer
        set.** ``/api/company/{id}/peers/`` returns a **mis-scoped** set and its output must not be
        used for sector-adjacent inference. Measured 5/5, byte-stable across repeat fetches, so it
        is not a transient or cached-wrong:

        ==========  ====================================  ===========================================
        company     its actual industry                   what the API returns
        ==========  ====================================  ===========================================
        ITC 1552    Fast Moving Consumer Goods            HBL Engineering, Inox India, KRN, Esab,
                                                         Ador — all *Other Industrial Products*
        HDFCBANK    Private Sector Bank                   GNG Electronics, Rashi Peripheral,
        1298                                                    Moschip, D-Link India
        SUNPHARMA   Pharmaceuticals                       JK Paper, West Coast Paper, N R Agarwal
        3245
        TATASTEEL   Iron & Steel                          Redington, MMTC, MSTC
        3373
        TCS 3365    IT Software                           **HTTP 404**, reproducibly
        ==========  ====================================  ===========================================

        The payload is also internally inconsistent: it ships **8 rows** while its own
        ``Median: N Co.`` row says 16–159.

        This method therefore has exactly one contract — *shape*. It reads the 11 columns
        ``S.No. | Company | CMPRs. | P/E | Mar Cap Rs.Cr. | Div Yld% | NP Qtr Rs.Cr. | Qtr Profit
        Var% | Sales Qtr Rs.Cr. | Qtr Sales Var% | ROCE%``, skips the header row, falls back to
        position when the S.No. cell is empty (the ``Median: N Co.`` row), and leaves a missing
        number as ``None`` rather than 0. It does not and cannot know whether the rows it was given
        are the right companies.

        For a *correct* industry membership, use :meth:`ScreenerClient.get_industry_constituents`,
        which reads a leaf industry's own page — and which is itself partial by construction, because
        screener.in's robots.txt disallows the ``?page=``/``?limit=`` that a full list would need.
        """
        peers: List[PeerStock] = []
        if not peers_html:
            return peers

        soup = BeautifulSoup(peers_html, "html.parser")
        table = soup.find("table", class_="data-table")
        if not table:
            return peers

        rows = table.find_all("tr")
        if not rows or len(rows) < 2:
            return peers

        # `rows[1:]` drops the first header row. It also drops any *repeated* header further down:
        # screener.in re-emits the header every 15 rows on the long industry pages, and such a row
        # has no `<td>` at all, so the width guard below skips it. Asserted by the offline suite.
        for tr in rows[1:]:
            cells = [td.get_text(strip=True) for td in tr.find_all("td")]
            if len(cells) < 4:
                continue

            rank_val = parse_indian_number(cells[0])
            name = cells[1]
            
            symbol = None
            link = tr.find("a")
            if link and link.get("href"):
                href_match = re.search(r"/company/([A-Z0-9\-]+)/", link.get("href"))
                if href_match:
                    symbol = href_match.group(1)

            peers.append(
                PeerStock(
                    # `is not None`, not truthiness: the `len(peers)+1` fallback is documented
                    # above as the *empty-S.No. cell* case (the "Median: N Co." row), and
                    # parse_indian_number already maps "-" to None. screener.in's S.No. column
                    # is 1-based, so a real 0 does not occur and this is a latent-consistency
                    # fix rather than a behaviour change: ranks 1..N are identical either way.
                    rank=int(rank_val) if rank_val is not None else len(peers) + 1,
                    name=name,
                    symbol=symbol,
                    cmp=parse_indian_number(cells[2]) if len(cells) > 2 else None,
                    pe=parse_indian_number(cells[3]) if len(cells) > 3 else None,
                    market_cap_cr=parse_indian_number(cells[4]) if len(cells) > 4 else None,
                    dividend_yield=parse_indian_number(cells[5]) if len(cells) > 5 else None,
                    net_profit_qtr=parse_indian_number(cells[6]) if len(cells) > 6 else None,
                    qtr_profit_var=parse_indian_number(cells[7]) if len(cells) > 7 else None,
                    sales_qtr=parse_indian_number(cells[8]) if len(cells) > 8 else None,
                    qtr_sales_var=parse_indian_number(cells[9]) if len(cells) > 9 else None,
                    roce=parse_indian_number(cells[10]) if len(cells) > 10 else None,
                )
            )

        return peers

    # ============================================================ /market/

    def parse_market_industries(self, market_html: str) -> pd.DataFrame:
        """Parse the ``/market/`` "Industries Overview" page into one row per leaf industry.

        Measured response shape (2026-09-27, ``text/html; charset=utf-8``, 177,020 bytes, HTTP
        200, one request, **no query string** — so no ``robots.txt`` rule, all of which require a
        ``?``, can match it):
        * **one** ``table.striped.data-table`` and nothing else carrying data;
        * **201** ``<tr>``: **13 header rows** and **188 industry rows**. The header is a ``<tr>`` of
          ten ``<th>`` — ``S.No. | Industry | No. of Companies | Total Market Cap. | Median Market
          Cap. | Median P/E | Sales Growth | OPM | Wtd. Avg ROCE | Median 1Y Return`` — and it is
          **re-emitted every 16 rows**, not once. There is **no ``<thead>``**, and the single
          ``<tbody>`` holds the header rows *and* the data rows together, so it separates nothing;
          no ``<tr>`` carries an attribute either. A parser that assumes "the first row is the
          header" therefore emits twelve rows called "Industry", and one that looks for a
          ``<thead>`` finds nothing at all. This one requires each row to carry a ``/market/`` leaf
          link *and* ten ``<td>``, which is why both conditions are tested;
        * each industry row is ten ``<td>``: a display ordinal, a name, and the eight aggregates in
          :data:`MARKET_AGGREGATE_COLUMNS`. Percent cells are ``<td>30<span class="sub">%</span>``
          — the sign is inside the cell and must survive, so the ``<span>`` is part of what this
          reads. An unreported aggregate is an **empty cell**, which becomes ``None`` and not 0.0;
        * the name cell is ``<a href="/market/IN04/IN0401/IN040107/IN040107001/">`` — the full
          four-level code path, which is the only parent information on this page (it carries no
          parent *names*; a company page's breadcrumb does).

        ⚠ **Three inconsistent company-count universes exist on this site for the same node**, and
        the filters behind each are undetermined. Measured on 2026-09-27, market-wide:

        =======================================  =======  ==================================
        count                                   source    what it counts
        =======================================  =======  ==================================
        **3,319**                               summed    this page's own per-leaf
                                                   from      "No. of Companies" column
                                                   ``/market/``
        **5,468**                               summed    the leaf pages' own
                                                   from      ``N results found`` line
                                                   leaf
                                                   pages
        **7,068**                               summed    the browse dropdown's child count
                                                   from
                                                   the
                                                   dropdown
        =======================================  =======  ==================================

        The same disagreement is visible *within a single node and a single day*: for
        Pharmaceuticals (``IN060101001``) this page says **159** while the leaf page's own
        ``results found`` says **218**, and it paginates to 9 pages of 25 = 225, which reconciles
        with neither. **This page's own count is the most conservative of the three** and is the
        one recorded, in ``attrs["bfinance_market_companies_total_leaf_table"]``. The other two are
        named in the ``attrs`` key rather than silently folded in, and the basis is in the key name
        so a consumer can tell which universe any number belongs to.

        ⚠ ``/market/`` is an **unadvertised entry point**: it is not linked from any company page and
        not from the top navigation. It is reachable only by typing the URL or by following a
        company page's ``/market/`` breadcrumb — and that breadcrumb points at a *leaf* page, not
        here. A reader who cannot find the link in the UI is not missing something.

        Returns:
            A frame of 188 rows indexed by the leaf code (``index.name == "industry_code"``) with
            ``industry``, ``code_path`` and the eight aggregates. ``attrs`` carries
            ``bfinance_market_row_count``, ``bfinance_market_skipped_rows`` and
            ``bfinance_market_companies_total_leaf_table``; the client adds
            ``bfinance_source_url`` and ``bfinance_retrieved_at``.
        """
        columns = ["industry", "code_path", *MARKET_AGGREGATE_COLUMNS]
        if not market_html:
            # Same columns and same index name as a populated frame, so a caller can select on
            # `df["median_pe"]` without first checking whether the fetch worked. Built from a
            # record list rather than a column dict, so the two paths cannot drift apart.
            frame = pd.DataFrame([], columns=columns).set_index(
                pd.Index([], name="industry_code", dtype="object")
            )
            frame.attrs.update(
                bfinance_market_row_count=0,
                bfinance_market_skipped_rows=0,
                bfinance_market_companies_total_leaf_table=None,
            )
            return frame

        soup = BeautifulSoup(market_html, "lxml")
        table = soup.find("table", class_="data-table")
        records: List[Dict[str, Any]] = []
        skipped = 0

        if table is None:
            # Not a partial read and not an empty industry list: a page shape change. Say so, and
            # report zero rows so the caller can tell this from a genuinely empty overview.
            logger.warning(
                "No table.data-table in the /market/ payload - Screener.in markup may have changed."
            )
        else:
            for tr in table.find_all("tr"):
                cells = tr.find_all("td")
                link = tr.find("a", href=_MARKET_LEAF_HREF_RE)
                # Both conditions, deliberately. The link says "this row is an industry"; the cell
                # count says "and it is a full one". Either alone would let the 13 repeated header
                # rows through as rows called "Industry" with eight empty aggregates.
                if link is None or len(cells) != len(MARKET_AGGREGATE_COLUMNS) + 2:
                    skipped += 1
                    continue
                code_path = _MARKET_LEAF_HREF_RE.match(link["href"]).group(1)
                record: Dict[str, Any] = {
                    "industry": link.get_text(strip=True),
                    "code_path": code_path,
                }
                record.update(
                    (name, parse_indian_number(td.get_text(strip=True)))
                    for name, td in zip(MARKET_AGGREGATE_COLUMNS, cells[2:])
                )
                records.append(record)

        frame = pd.DataFrame(records, columns=columns).set_index(
            pd.Index(
                [r["code_path"].rsplit("/", 1)[-1] for r in records],
                name="industry_code",
                dtype="object",
            )
        )
        companies = frame["no_of_companies"]
        frame.attrs.update(
            bfinance_market_row_count=int(len(frame)),
            bfinance_market_skipped_rows=int(skipped),
            bfinance_market_companies_total_leaf_table=(
                int(companies.sum()) if len(frame) and not companies.isna().any() else None
            ),
        )
        return frame

    def parse_industry_constituents(self, html: str) -> Tuple[List[PeerStock], Dict[str, Any]]:
        """Parse one leaf industry's constituent page.

        Measured response shape (2026-09-27): ``table.data-table`` with **no** ``<thead>`` — a header
        ``<tr>`` of eleven ``<th>`` at the top and, on any list long enough to need it, **the same
        header re-emitted every 15 rows** — then data rows of exactly eleven ``<td>`` in the order
        :meth:`_parse_peers` expects. Every row also carries
        ``tr[data-row-company-id="3245"]``, the numeric id, and the name cell links to
        ``/company/SUNPHARMA/consolidated/``, which is where the symbol comes from. There is no
        ``Median: N Co.`` footer row here (that belongs to the peers payload), so every row is a
        real company.

        The pagination state lives in ``div[data-page-info]``, which reads
        ``"218 results found: Showing page 1 of 9"``. Both halves are read:
        ``results_found`` is how many companies the page *claims* exist, and ``pages`` is how many
        pages it says the full list spans.

        ⚠ **A complete list is not robots-obtainable from this source.** screener.in's
        ``robots.txt`` disallows ``/*?page=`` and ``/*?limit=``, which are exactly the parameters
        the page's own pager and its "10 / 25 / 50" page-size control emit, and the bulk route
        ``POST /api/export/screen/?url_name=browse_industry&mes_code=IN04`` answers **403**
        unauthenticated. One compliant request therefore returns at most the first **25** of a list
        that can hold 159. :meth:`ScreenerClient.get_industry_constituents` turns that into explicit
        ``attrs`` rather than a frame that merely looks complete.

        Returns:
            ``(peers, page_info)`` where ``page_info`` is
            ``{"results_found": int | None, "page": int | None, "pages": int | None}``. All three
            are ``None`` when the page carries no ``div[data-page-info]`` — a *missing* pagination
            statement, which is not the same claim as "this page is the whole list", and the client
            resolves it to "unknown" rather than to "complete".
        """
        page_info: Dict[str, Any] = {"results_found": None, "page": None, "pages": None}
        if not html:
            return [], page_info

        node = BeautifulSoup(html, "lxml").find(attrs={"data-page-info": True})
        if node is not None:
            match = _PAGE_INFO_RE.search(node.get_text(" ", strip=True))
            if match:
                page_info["results_found"] = int(match.group("results_found").replace(",", ""))
                if match.group("pages"):
                    page_info["page"] = int(match.group("page"))
                    page_info["pages"] = int(match.group("pages"))
        return self._parse_peers(html), page_info
