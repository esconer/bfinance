"""
High-precision DOM and HTML table parser for Screener.in financial statements, ratios, and filings.
"""

import re
from typing import Any, Dict, List, Optional, Tuple
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
        high_low_str = None

        for li in soup.select("#top-ratios li"):
            name_el = li.select_one(".name")
            val_el = li.select_one(".value")
            if not name_el or not val_el:
                continue

            name = name_el.get_text(strip=True)
            raw_text = val_el.get_text(strip=True)

            if "High / Low" in name or "High/Low" in name:
                high_low_str = raw_text
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
            high_low_52w=high_low_str,
            stock_pe=raw_ratios.get("Stock P/E") or raw_ratios.get("P/E"),
            book_value=raw_ratios.get("Book Value"),
            dividend_yield=raw_ratios.get("Dividend Yield"),
            roce=raw_ratios.get("ROCE"),
            roe=raw_ratios.get("ROE"),
            face_value=raw_ratios.get("Face Value"),
            debt_to_equity=raw_ratios.get("Debt to equity") or raw_ratios.get("Debt / Eq"),
            peg_ratio=raw_ratios.get("PEG Ratio"),
            price_to_book=raw_ratios.get("Price to book value") or raw_ratios.get("P/B"),
            eps_ttm=raw_ratios.get("EPS"),
            promoter_holding=raw_ratios.get("Promoter holding"),
            promoter_pledged=raw_ratios.get("Pledged percentage"),
            free_cash_flow_3y=raw_ratios.get("Free Cash Flow 3Y"),
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

        # Announcements
        for a in doc_sec.select(".announcements a, #announcements a"):
            announcements.append({
                "title": a.get_text(strip=True),
                "url": a.get("href", "")
            })

        return concalls, annual_reports, credit_ratings, announcements

    def _parse_peers(self, peers_html: str) -> List[PeerStock]:
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
                    rank=int(rank_val) if rank_val else len(peers) + 1,
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
