"""
`screener/parser.py`: every ``soup.select`` / ``select_one`` call replaced by ``find_all`` logic,
with **no parsed value changed**.

Why this module exists
======================

``parse_full_profile`` made **13+ whole-document ``soup.select()`` calls** — soupsieve compiles and
matches CSS over the whole tree for each one, and is measured 6–12x slower than the equivalent
``find_all`` on these documents. This module is the evidence that the replacement is value-identical,
because a performance change that silently drops, duplicates or reorders a parsed node is worse than
the slow version it replaced.

The four traps, each reproduced here by a case that fails if that specific mistake comes back
================================================================================================

1. **`.company-profile a` is NESTED INSIDE `.company-info a`.** The comma-union at
   ``_parse_header_meta`` measures **7** nodes on the live ITC page; the two halves alone measure 6
   and 7, so *summing* them returns 13 (+86%). The union also has to come back in **document
   order**: soupsieve walks the tree once, so ``"#first a, .second a"`` interleaves the halves by
   position. :class:`TestTheNestedLinkUnion` covers both.
2. **`li.select_one(".name")` is scoped to the `#top-ratios li` container.** The document carries
   the same nine ``.name`` nodes a second time; a document-wide lookup would pair every ratio with
   whichever name came first. :class:`TestTheRatioRowScope` covers it.
3. **``:not()`` with a class predicate has no ``find_all`` equivalent.** The announcements
   container is found structurally, by excluding the three marker classes — a set test, not a
   substring test, and the **first** match only. :class:`TestTheStructuralInverseHandFilter` covers
   it on a hand-authored fragment; no recording in ``tests/fixtures/screener/`` exercises this half
   on its own.
4. **``A or B`` is a short-circuit fallback, not a union.** Concalls read ``.concalls ul li`` and
   fall back to ``#concalls ul li`` only when the first matched nothing; rewriting it as ``+``
   counts every concall twice the day a block carries both the class and the id.
   :class:`TestTheConcallShortCircuit` covers it.

The verification trap
=====================

``tests/fixtures/screener/company_itc_full_columns.html`` returns **zero matches for 15 of the 16
selectors** the parser reads — it is the untrimmed recording, kept for the *market* page and the
chart tests. A refactor validated only against it compares empty to empty and proves nothing, so it
is used here strictly as a **negative control**: every site must agree with soupsieve *and* agree
on being empty. The two fixtures that actually fire are ``company_itc.html`` and
``company_tcs.html``, and each site is asserted **node-for-node** against ``soup.select(css)`` on
both, with ``len > 0`` required so an accidentally-empty rewrite cannot pass.

What a local reimplementation proves, and what it cannot
=======================================================

Several cases below hold a **local reimplementation** of a call site — ``_find_all_version``,
``_about_blocks``, ``_uls_under``, ``_hand_filtered``, ``_parser_block`` and the ``SITES`` probes —
against soupsieve. Those are real evidence about the *helpers*: ``_descendants_of_any``,
``_first_descendant_of_any`` and ``_structural_inverse`` are production code and the probes call
them directly. But the four containers are composed *in this file*, so a call site that composes
them wrongly is invisible here: ``parser.py`` is never entered. The same blindness applies to
:class:`PreChangeScreenerHTMLParser` on the two real recordings, where a wrong composition happens
to yield the same parsed values — measured, not assumed: replacing the links call site with the
naive per-container sum that the parser's own comment warns against leaves the whole oracle suite
green and reddens exactly one case.

What closes it is :class:`TestTheCompositionOnAPageBuiltToDiscriminate`: the shipped
``parse_full_profile`` and the shipped per-method entry points, over a page built so that each
composition error *must* change a parsed value. The recordings cannot do that job — on
``company_itc.html`` the naive sum still lands on the same ``website`` / ``bse_code`` /
``nse_symbol`` — so the page is hand-authored and *ordered to discriminate*, and every one of its
discriminating properties is asserted in
``test_the_page_separates_every_composition_error_from_a_correct_one``, so a future edit that
flattens the page fails rather than silently disarming the guard.

No network, no cache, no I/O beyond reading the recorded bytes.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest
from bs4 import BeautifulSoup

from bfinance.models.company import AnalysisInsights, Concall, TopRatios
from bfinance.screener.parser import (
    _descendants_of_any,
    _first_descendant_of_any,
    _structural_inverse,
    ScreenerHTMLParser,
)
from bfinance.utils.formatting import parse_indian_number

FIXTURES = Path(__file__).parent / "fixtures" / "screener"

#: The two recordings whose markup actually carries every selector below.
FIRING = ("company_itc.html", "company_tcs.html")

#: The recording that matches **nothing**. Used only as the empty-match negative control.
EMPTY_MATCH = "company_itc_full_columns.html"


def text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def soup_of(name: str) -> BeautifulSoup:
    return BeautifulSoup(text(name), "lxml")


# ====================================================================== the oracle


class PreChangeScreenerHTMLParser(ScreenerHTMLParser):
    """``screener/parser.py`` exactly as it stood before the ``select`` -> ``find_all`` rewrite.

    **This is the oracle.** Every method below is the pre-change body, byte-for-byte, and it calls
    ``select`` / ``select_one`` — nothing else. :class:`TestTheOracleIsStillAnOracle` fails if that
    ever stops being true, so a well-meaning "let me tidy the oracle too" edit cannot quietly turn
    the comparison into ``parser == parser``.

    It subclasses rather than reimplements so that only the five rewritten methods can diverge: the
    seven untouched ones (``_parse_sector_hierarchy``, ``_parse_table_section``,
    ``_parse_shareholding_sections``, ``_parse_table_element``, ``parse_market_industries``,
    ``parse_industry_constituents``, ``_parse_peers``) and the whole of ``parse_full_profile`` are
    shared with the shipped class, and cannot drift.
    """

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
                parts = raw_text.split("/")
                if len(parts) == 2:
                    raw_ratios["High"] = parse_indian_number(parts[0])
                    raw_ratios["Low"] = parse_indian_number(parts[1])
            else:
                raw_ratios[name] = parse_indian_number(raw_text)

        from bfinance.screener.parser import _first_present

        return TopRatios(
            market_cap=raw_ratios.get("Market Cap"),
            current_price=raw_ratios.get("Current Price"),
            high_52w=raw_ratios.get("High"),
            low_52w=raw_ratios.get("Low"),
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

        ann_block = next(
            (d for d in doc_sec.select("div.documents")
             if (h3 := d.find("h3")) and h3.get_text(strip=True) == "Announcements"),
            None,
        ) or doc_sec.select_one(
            "div.documents:not(.annual-reports):not(.credit-ratings):not(.concalls)"
        )
        for a in (ann_block.select("ul.list-links a") if ann_block else []):
            announcements.append({
                "title": a.get_text(strip=True),
                "url": a.get("href", "")
            })

        return concalls, annual_reports, credit_ratings, announcements


# ============================================================ the whole-profile oracle


def leaves(value: Any, prefix: str = "") -> Dict[str, Any]:
    """Every scalar leaf of a pydantic model / dict / list, keyed by its path.

    Flattened so the comparison is genuinely *field by field*: a drop, an extra, a reordering or a
    changed type anywhere in the 27-field profile shows up as a named key rather than as a single
    opaque "the objects differ".
    """
    out: Dict[str, Any] = {}
    if isinstance(value, dict):
        for key, item in value.items():
            out.update(leaves(item, f"{prefix}.{key}"))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            out.update(leaves(item, f"{prefix}[{index}]"))
    elif hasattr(value, "model_dump"):
        out.update(leaves(value.model_dump(), prefix))
    else:
        out[prefix or "<root>"] = value
    return out


def _itc_with_an_earlier_profile_block() -> str:
    """ITC with an extra ``.company-profile`` block placed **before** ``.company-info``.

    Why this exists: the union's halves are listed ``.company-links, .links, .company-info,
    .company-profile``, so a *concatenated* rewrite elects ``.company-info``'s first anchor while
    soupsieve's document order elects the earlier one. On the unmodified recording the two are
    indistinguishable — ``.company-info`` is both first in the document and first in the union — so
    without this variant the recording cannot tell a de-duplicating rewrite from a summing one.
    """
    original = text("company_itc.html")
    anchor = '<div class="company-info">'
    injected = (
        '<div class="company-profile">'
        '<a href="https://earlier.example.com/">Earlier site</a>'
        f'</div>{anchor}'
    )
    assert original.count(anchor) == 1, "the injection anchor is no longer unique"
    return original.replace(anchor, injected)


class TestTheProfileIsUnchanged:
    """The headline claim: **every** parsed field is identical, on both recordings that fire."""

    #: The three recordings the oracle runs on. The third is ITC with an *earlier*
    #: ``.company-profile`` block grafted on, because on the unmodified page the union's first half
    #: and the document's first half are the same element — see :func:`_itc_with_an_earlier_profile_block`.
    ORACLE_CASES = ("company_itc.html", "company_tcs.html", "itc-earlier-profile-block")

    def _html(self, case):
        if case == "itc-earlier-profile-block":
            return _itc_with_an_earlier_profile_block()
        return text(case)

    @pytest.mark.parametrize("case", ORACLE_CASES)
    @pytest.mark.parametrize("with_peers", [True, False], ids=["with-peers", "without-peers"])
    def test_every_field_matches_the_pre_change_parse(self, case, with_peers):
        main = self._html(case)
        peers = None
        if with_peers and case in FIRING:
            peers = text(case.replace("company_", "peers_"))
        kwargs = dict(
            symbol="XYZ",
            company_id=1,
            main_html=main,
            peers_html=peers,
            is_consolidated=True,
            page_url="https://www.screener.in/company/XYZ/consolidated/",
        )
        old = leaves(PreChangeScreenerHTMLParser().parse_full_profile(**kwargs))
        new = leaves(ScreenerHTMLParser().parse_full_profile(**kwargs))

        assert set(new) == set(old), (
            f"{case}: the field set changed; "
            f"added={sorted(set(new) - set(old))} removed={sorted(set(old) - set(new))}"
        )
        differing = {k: (old[k], new[k]) for k in old if old[k] != new[k]}
        assert not differing, (
            f"{case}: {len(differing)} parsed value(s) changed against the pre-change parse: "
            + "; ".join(f"{k}: was {a!r}, now {b!r}" for k, (a, b) in list(differing.items())[:10])
        )
        # The recordings are dense; an "identical" result over 3 fields would prove nothing.
        assert len(old) > 150, f"{case}: only {len(old)} leaves — the comparison is too thin"

    @pytest.mark.parametrize("fixture", FIRING)
    def test_the_comparison_is_not_vacuous(self, fixture):
        """Both sides must actually be carrying data, and must agree on the counts that matter.

        ``custom_ratios`` is the carrier most exposed to a node-list rewrite — a dropped ``li``
        loses a key, a duplicated one repeats a value — so it is pinned to a non-zero size here and
        compared leaf-by-leaf above. Nine ``li`` rows produce **ten** keys, because the
        ``High / Low`` row splits into two.
        """
        soup = soup_of(fixture)
        profile = ScreenerHTMLParser().parse_full_profile(
            symbol="XYZ", company_id=1, main_html=text(fixture)
        )
        assert len(soup.select("#top-ratios li")) == 9
        assert len(profile.ratios.custom_ratios) == 10, profile.ratios.custom_ratios
        assert profile.ratios.market_cap and profile.ratios.high_52w and profile.ratios.low_52w
        assert profile.about and profile.name
        assert profile.website and profile.bse_code and profile.nse_symbol, (
            f"{fixture}: the link union produced {profile.website!r} / {profile.bse_code!r} / "
            f"{profile.nse_symbol!r}"
        )
        assert profile.peers == [], "no peers payload was passed"

    def test_the_empty_match_recording_is_identical_too(self):
        """``company_itc_full_columns.html`` matches nothing — a control, not evidence.

        It is asserted here only because a rewrite must not *invent* data on a page that carries
        none. The load-bearing evidence is the two recordings above.
        """
        kwargs = dict(symbol="XYZ", company_id=1, main_html=text(EMPTY_MATCH))
        old = leaves(PreChangeScreenerHTMLParser().parse_full_profile(**kwargs))
        new = leaves(ScreenerHTMLParser().parse_full_profile(**kwargs))
        assert old == new


class TestTheOracleIsStillAnOracle:
    def test_it_still_calls_soupsieve_and_never_the_rewrite_helpers(self):
        """If this ever stops holding, every comparison above is ``parser == parser``."""
        import inspect

        source = inspect.getsource(PreChangeScreenerHTMLParser)
        assert ".select(" in source and ".select_one(" in source, (
            "the pre-change oracle no longer reads through soupsieve — it has stopped being an oracle"
        )
        for helper in ("_descendants_of_any", "_first_descendant_of_any", "_DOCUMENTS_MARKER_CLASSES"):
            assert helper not in source, (
                f"the pre-change oracle now uses {helper!r}; it is no longer the pre-change parser"
            )


# ================================== the shipped composition, on a page built to discriminate


#: A page on which **every** mistake the rewrite could make at a call site changes a parsed value.
#:
#: The recordings are dense but not discriminating: they carry each container, yet on them the
#: element that decides ``website`` / ``bse_code`` / ``nse_symbol`` is the same one whichever way the
#: four link halves are composed, so the oracle above cannot see that mistake at all. Every ordering
#: here is chosen so the union and the wrong rewrite *cannot* agree:
#:
#: * ``.company-profile`` is nested inside ``.company-info``, and the profile's anchors come **first**
#:   in document order while ``.company-links`` is listed first among the halves — so the website
#:   (the first anchor wins unless a later one's text is literally "website"), the BSE code and the
#:   NSE symbol (later wins) all land on different anchors for the union and for the sum;
#: * an ``.about`` sits **outside** any ``.company-profile`` and **before** the profile's own, so a
#:   document-wide ``.about p`` elects the wrong paragraph instead of the same one by luck;
#: * a ``.name`` / ``.value`` pair sits **before** ``#top-ratios``, so a document-wide
#:   ``find_all(class_="name")`` zipped by index — or hoisted out of the loop — pairs a row with the
#:   ticker tape rather than agreeing with the scoped read, which is all the recordings can show;
#: * the concalls block carries **both** the class and the id, so the ``or`` fallback turned into a
#:   union parses the two rows twice.
COMPOSITION_PAGE_HTML = """
<html><body>
  <h1>Composition Ltd</h1>
  <div class="about"><p>outside the profile, and printed first</p></div>
  <div class="company-info">
    <div class="company-profile">
      <div class="about"><p>inside the profile</p></div>
      <a href="https://profile.example.com/">Our site</a>
      <a href="https://www.bseindia.com/stock-share-price/COMP/500001/">BSE 500001</a>
      <a href="https://www.nseindia.com/get-quotes/equity?symbol=OLDSYM">NSE OLD</a>
    </div>
    <div class="company-links">
      <a href="https://outside.example.com/">Group site</a>
      <a href="https://www.bseindia.com/stock-share-price/COMP/500325/">BSE 500325</a>
      <a href="https://www.nseindia.com/get-quotes/equity?symbol=XYZ">NSE XYZ</a>
    </div>
  </div>
  <ul class="ticker-tape">
    <li><span class="name">Sensex</span><span class="value">80,000</span></li>
  </ul>
  <ul id="top-ratios">
    <li><span class="name">Market Cap</span><span class="value">1,23,456</span></li>
    <li><span class="name">ROCE</span><span class="value">41.2</span></li>
  </ul>
  <section id="documents">
    <div class="documents concalls" id="concalls">
      <ul>
        <li>Jan 2026 <a href="https://x.invalid/t.pdf">Transcript</a></li>
        <li>Apr 2026 <a href="https://x.invalid/t2.pdf">Transcript</a></li>
      </ul>
    </div>
    <div class="documents annual-reports">
      <h3>Annual Reports</h3><a href="https://x.invalid/ar/2024.pdf">2024</a>
    </div>
    <div class="documents">
      <h3>Announcements</h3>
      <ul class="list-links">
        <li><a href="https://x.invalid/a1.pdf">Closure of trading window</a></li>
      </ul>
    </div>
  </section>
</body></html>
"""

LINK_UNION_CSS = ".company-links a, .links a, .company-info a, .company-profile a"


def _composition_profile(parser_class):
    """The shipped whole-profile entry point, run through ``parser_class``.

    ``parse_full_profile`` is **not** overridden by the oracle, so both classes run the same
    top-level body and differ only in the five rewritten methods — which is the composition under
    test.
    """
    return parser_class().parse_full_profile(
        symbol="COMP",
        company_id=7,
        main_html=COMPOSITION_PAGE_HTML,
        page_url="https://www.screener.in/company/COMP/",
    )


class TestTheCompositionOnAPageBuiltToDiscriminate:
    """The shipped call sites, on a page where a wrong composition has to change a value.

    Everything else in this module either probes a helper directly (the ``SITES`` table) or
    composes the containers in this file (``_find_all_version``, ``_about_blocks``, ``_uls_under``,
    ``_hand_filtered``, ``_parser_block``). Neither can see ``parser.py`` compose them wrongly —
    measured: the naive per-container sum at the links call site leaves the whole oracle suite here
    green. These cases run the shipped methods, on a page that cannot agree by accident.
    """

    def _soup(self):
        return BeautifulSoup(COMPOSITION_PAGE_HTML, "lxml")

    def test_the_page_separates_every_composition_error_from_a_correct_one(self):
        """If this fails the page has been flattened, and every case below stopped discriminating."""
        soup = self._soup()

        # 1. the link union: 12 half-order anchors against 6 in document order, with a *different*
        #    first and last anchor, so summing and half-ordering both change what gets elected.
        assert len(soup.select(LINK_UNION_CSS)) == 6
        halves = sum(
            len(soup.select(half))
            for half in (".company-links a", ".links a", ".company-info a", ".company-profile a")
        )
        assert halves == 12, "the nested-union trap is no longer present in the page"
        assert soup.select(LINK_UNION_CSS)[0] is soup.select(".company-profile a")[0]
        assert soup.select(LINK_UNION_CSS)[-1] is soup.select(".company-links a")[-1]
        assert soup.select(".company-links a")[0] is not soup.select(LINK_UNION_CSS)[0]

        # 2. the ratio rows: three `.name` nodes in the document, two of them in the card, and the
        #    document-wide first one belongs to the ticker tape, not to the first row.
        assert len(soup.select("#top-ratios li .name")) == 2
        document_names = soup.find_all(class_="name")
        assert len(document_names) == 3, "a document-wide lookup would agree with the scope again"
        assert document_names[0].get_text(strip=True) == "Sensex"
        assert document_names[0] is not soup.select("#top-ratios li .name")[0]

        # 3. the about chain: the document-wide first `.about p` is not the profile-scoped one.
        assert soup.select_one(".about p").get_text(strip=True) == "outside the profile, and printed first"
        assert soup.select_one(".company-profile .about p").get_text(strip=True) == "inside the profile"

        # 4. the concalls `or`: both halves match, so a union would parse the two rows twice.
        assert len(soup.select("section#documents .concalls ul li")) == 2
        assert len(soup.select("section#documents #concalls ul li")) == 2

    def test_the_nested_link_containers_are_read_once(self):
        """The whole shipped entry point, not ``_parse_header_meta`` in isolation.

        All three link fields are asserted because all three are decided by the composition: the
        union takes the profile's website (earlier in the document wins, since neither anchor's text
        is the literal "website") and the *later* BSE / NSE anchors. The per-container sum reverses
        both — it lists ``.company-links`` first and the nested ``.company-profile`` last — and
        elects ``outside.example.com``, the stale ``500001`` and ``OLDSYM``.
        """
        profile = _composition_profile(ScreenerHTMLParser)
        assert profile.website == "https://profile.example.com/"
        assert profile.bse_code == "500325"
        assert profile.nse_symbol == "XYZ"

    def test_the_ratio_rows_read_their_own_name_and_value(self):
        """The ``#top-ratios`` scope: a document-wide ``find_all(class_="name")`` reads the tape."""
        profile = _composition_profile(ScreenerHTMLParser)
        assert profile.ratios.custom_ratios == {"Market Cap": 123456.0, "ROCE": 41.2}, (
            profile.ratios.custom_ratios
        )
        assert profile.ratios.market_cap == 123456.0
        assert profile.ratios.roce == 41.2

    def test_the_about_paragraph_comes_from_inside_the_profile(self):
        """``.company-profile .about p`` is a two-level chain; a document-wide one elects the other."""
        assert _composition_profile(ScreenerHTMLParser).about == "inside the profile"

    def test_every_field_matches_the_pre_change_parse(self):
        """The oracle once more — this time on a page on which the wrong composition cannot hide.

        ``TestTheProfileIsUnchanged`` makes the same comparison over the recordings and is blind to
        exactly this class of defect; the flattening and the message are identical, only the page
        differs. The scalar link / ratio / about values are asserted in the four cases above; the
        list-valued fields that only this page exercises are named here, so a regression points at
        the field rather than at "the objects differ".
        """
        old = leaves(_composition_profile(PreChangeScreenerHTMLParser))
        new = leaves(_composition_profile(ScreenerHTMLParser))

        assert set(new) == set(old), (
            f"the field set changed; added={sorted(set(new) - set(old))} "
            f"removed={sorted(set(old) - set(new))}"
        )
        differing = {k: (old[k], new[k]) for k in old if old[k] != new[k]}
        assert not differing, (
            f"{len(differing)} parsed value(s) changed against the pre-change parse: "
            + "; ".join(f"{k}: was {a!r}, now {b!r}" for k, (a, b) in list(differing.items())[:10])
        )

        profile = _composition_profile(ScreenerHTMLParser)
        assert [c.title for c in profile.concalls] == ["Jan 2026 Transcript", "Apr 2026 Transcript"]
        assert [a["title"] for a in profile.announcements] == ["Closure of trading window"]
        assert [r["year"] for r in profile.annual_reports] == ["2024"]
        assert len(old) > 40, f"only {len(old)} leaves — the comparison is too thin"


# ==================================================== per-site equivalence with soupsieve


def _about_blocks(scope, *, inside_profile: bool):
    """The two `.about` element sets the about chain resolves, **composed here, not by the parser**.

    A local reimplementation: the cases that use it compare the production helper against soupsieve
    but compose the chain in this file, so they cannot see ``parser.py`` compose it wrongly.
    :meth:`TestTheCompositionOnAPageBuiltToDiscriminate.test_the_about_paragraph_comes_from_inside_the_profile`
    runs the shipped path on a page where dropping the profile scope elects a different paragraph.
    """
    if inside_profile:
        return [
            about
            for profile in scope.find_all(class_="company-profile")
            for about in profile.find_all(class_="about")
        ]
    return scope.find_all(class_="about")


def _uls_under(blocks):
    """A local reimplementation of the ``[ul for block in blocks for ul in block.find_all("ul")]``
    container resolution the parser does inline — same caveat as :func:`_about_blocks`."""
    return [ul for block in blocks for ul in block.find_all("ul")]


#: The three marker classes the structural inverse excludes. Read from the parser so the
#: comparison cannot silently drift from the shipped exclusion list.
_MARKER_CLASSES = ("annual-reports", "credit-ratings", "concalls")


def _documents(soup):
    return soup.find("section", id="documents")


def _require_documents(soup):
    section = _documents(soup)
    assert section is not None, "this page carries no section#documents"
    return section


def _hand_filtered(doc_sec):
    """The ``div.documents:not(...)`` structural inverse, by hand.

    A local reimplementation: it mirrors :func:`_structural_inverse` rather than calling it, so the
    cases that use it compare two test-side filters and cannot see the shipped one drift.
    :meth:`TestTheStructuralInverseHandFilter.test_the_shipped_filter_is_the_container_soupsieve_elects`
    calls the production helper on the same pages.
    """
    return [
        d for d in doc_sec.find_all("div", class_="documents")
        if set(d.get("class") or []).isdisjoint(_MARKER_CLASSES)
    ]


def _parser_block(doc_sec):
    """The container the parser elects: heading first, structural inverse second, first match only.

    Hybrid: the fallback half calls the production :func:`_structural_inverse`, but the heading
    election is recomposed here, so a call site that elected differently would not be caught.
    """
    blocks = doc_sec.find_all("div", class_="documents")
    return next(
        (d for d in blocks if (h3 := d.find("h3")) and h3.get_text(strip=True) == "Announcements"),
        None,
    ) or _structural_inverse(doc_sec)


def _probe_announcements(soup):
    """``:434`` — the list anchors inside whichever container the parser elected, or nothing."""
    doc_sec = _documents(soup)
    block = _parser_block(doc_sec) if doc_sec is not None else None
    if block is None:
        return [], []
    return (
        _descendants_of_any(block, "a", block.find_all("ul", class_="list-links")),
        block.select("ul.list-links a"),
    )


#: Every rewritten call site. Each entry returns ``(find_all_version, soupsieve_version)`` so the
#: test can hold them against each other **node for node** — counts alone miss reordering, which is
#: itself a parsed value (concalls, pros, cons, reports and ratings are all ordered lists).
#:
#: ``fires_on`` names the recordings on which the site must match **non-empty**. A site absent from
#: it must match empty on both sides: that is either an unused fallback half (``:367``'s
#: ``#concalls``) or a site the recording genuinely does not carry (``:434`` on the TCS recording,
#: whose trim rule dropped the announcements block — see ``META.json``).
#:
#: ⚠ Every probe composes the containers itself, as the note at the top of this file says: the
#: *helpers* are production code and are exercised directly, but the *composition* is this file's.
#: A probe that agreed with the shipped call site while the call site was wrong would still pass.
SITES = [
    pytest.param(
        ":166 .company-profile .about p",
        ("company_itc.html", "company_tcs.html"), False,
        lambda s: (
            _first_descendant_of_any(s, "p", _about_blocks(s, inside_profile=True)),
            s.select_one(".company-profile .about p"),
        ),
    ),
    pytest.param(
        ":166 .about p (the or-fallback)",
        ("company_itc.html", "company_tcs.html"), False,
        lambda s: (
            _first_descendant_of_any(s, "p", _about_blocks(s, inside_profile=False)),
            s.select_one(".about p"),
        ),
    ),
    pytest.param(
        ":174 the four-way link union",
        ("company_itc.html", "company_tcs.html"), False,
        lambda s: (
            _descendants_of_any(
                s, "a",
                s.find_all(class_="company-links") + s.find_all(class_="links")
                + s.find_all(class_="company-info") + s.find_all(class_="company-profile"),
            ),
            s.select(".company-links a, .links a, .company-info a, .company-profile a"),
        ),
    ),
    pytest.param(
        ":237 #top-ratios li",
        ("company_itc.html", "company_tcs.html"), False,
        lambda s: (
            _descendants_of_any(s, "li", s.find_all(id="top-ratios")),
            s.select("#top-ratios li"),
        ),
    ),
    pytest.param(
        ":238 li > .name (scoped to the li)",
        ("company_itc.html", "company_tcs.html"), False,
        lambda s: (
            [li.find(class_="name")
             for li in _descendants_of_any(s, "li", s.find_all(id="top-ratios"))],
            s.select("#top-ratios li .name"),
        ),
    ),
    pytest.param(
        ":239 li > .value (scoped to the li)",
        ("company_itc.html", "company_tcs.html"), False,
        lambda s: (
            [li.find(class_="value")
             for li in _descendants_of_any(s, "li", s.find_all(id="top-ratios"))],
            s.select("#top-ratios li .value"),
        ),
    ),
    pytest.param(
        ":287 .pros ul li",
        ("company_itc.html", "company_tcs.html"), False,
        lambda s: (
            _descendants_of_any(s, "li", _uls_under(s.find_all(class_="pros"))),
            s.select(".pros ul li"),
        ),
    ),
    pytest.param(
        ":288 .cons ul li",
        ("company_itc.html", "company_tcs.html"), False,
        lambda s: (
            _descendants_of_any(s, "li", _uls_under(s.find_all(class_="cons"))),
            s.select(".cons ul li"),
        ),
    ),
    pytest.param(
        ":293 .ranges-table",
        ("company_itc.html", "company_tcs.html"), False,
        lambda s: (s.find_all(class_="ranges-table"), s.select(".ranges-table")),
    ),
    pytest.param(
        ":367 .concalls ul li",
        ("company_itc.html", "company_tcs.html"), True,
        lambda s: (
            _descendants_of_any(
                _require_documents(s), "li",
                _uls_under(_require_documents(s).find_all(class_="concalls")),
            ),
            _require_documents(s).select(".concalls ul li"),
        ),
    ),
    pytest.param(
        ":367 #concalls ul li (the unused fallback half)",
        (), True,
        lambda s: (
            _descendants_of_any(
                _require_documents(s), "li",
                _uls_under(_require_documents(s).find_all(id="concalls")),
            ),
            _require_documents(s).select("#concalls ul li"),
        ),
    ),
    pytest.param(
        ":400 annual reports union",
        ("company_itc.html", "company_tcs.html"), True,
        lambda s: (
            _descendants_of_any(
                _require_documents(s), "a",
                _require_documents(s).find_all(class_="annual-reports")
                + _require_documents(s).find_all(id="annual-reports"),
            ),
            _require_documents(s).select(".annual-reports a, #annual-reports a"),
        ),
    ),
    pytest.param(
        ":407 credit ratings union",
        ("company_itc.html", "company_tcs.html"), True,
        lambda s: (
            _descendants_of_any(
                _require_documents(s), "a",
                _require_documents(s).find_all(class_="credit-ratings")
                + _require_documents(s).find_all(id="credit-ratings"),
            ),
            _require_documents(s).select(".credit-ratings a, #credit-ratings a"),
        ),
    ),
    pytest.param(
        ":425 div.documents",
        ("company_itc.html", "company_tcs.html"), True,
        lambda s: (
            _require_documents(s).find_all("div", class_="documents"),
            _require_documents(s).select("div.documents"),
        ),
    ),
    pytest.param(
        ":429 div.documents:not(...) — the hand filter",
        ("company_itc.html",), True,
        lambda s: (
            _structural_inverse(_require_documents(s)),
            _require_documents(s).select_one(
                "div.documents:not(.annual-reports):not(.credit-ratings):not(.concalls)"
            ),
        ),
    ),
    pytest.param(
        ":434 ul.list-links a",
        ("company_itc.html",), True,
        lambda s: _probe_announcements(s),
    ),
]
SITE_IDS = [p.values[0] for p in SITES]

#: What each site must find on :data:`EMPTY_DOCUMENT_HTML`. Exactly one entry is non-zero —
#: ``:425`` counts *containers*, and that page does have one — so every other site must come back
#: empty, including ``:429``: the filter has to reject the lone marker block, not elect it.
EMPTY_DOCUMENT_COUNTS = {":425 div.documents": 1}


#: A page that carries **every container the parser reads, and no content inside any of them**.
#:
#: The negative control has to be a page where the containers exist: on
#: ``company_itc_full_columns.html`` there is no ``section#documents`` at all, so a scope-widening
#: bug could not express itself there.
#:
#: ``section#documents`` deliberately keeps **one marker block** and no unmarked one. That is what
#: gives the ``:not()`` site its teeth: a hand filter that stopped excluding anything would then
#: elect that marker block and find content that does not exist.
EMPTY_DOCUMENT_HTML = """
<html><body>
  <div class="company-info company-profile"><div class="about"></div>
    <div class="company-links"></div><div class="links"></div></div>
  <ul id="top-ratios"></ul>
  <div class="pros"><ul></ul></div>
  <div class="cons"><ul></ul></div>
  <section id="documents">
    <div class="documents concalls"><ul></ul></div>
  </section>
</body></html>
"""


def _as_list(value):
    """``select_one`` yields a node or ``None``; normalise so one comparison serves both shapes."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


class TestEachSiteAgreesWithSoupsieve:
    @pytest.mark.parametrize("fixture", FIRING)
    @pytest.mark.parametrize("label,firings_on,needs_documents,probe", SITES, ids=SITE_IDS)
    def test_same_nodes_in_the_same_order(
        self, fixture, label, firings_on, needs_documents, probe):
        soup = soup_of(fixture)
        actual, expected = _as_list(probe(soup)[0]), _as_list(probe(soup)[1])

        if fixture in firings_on:
            assert len(expected) > 0, (
                f"{fixture}: {label} matched nothing on a recording that is supposed to exercise "
                "it — this case cannot tell a correct rewrite from an empty one"
            )
            assert len(actual) == len(expected), (
                f"{fixture}: {label} — {len(actual)} nodes where soupsieve found {len(expected)}"
            )
        else:
            assert expected == [], f"{fixture}: {label} was expected to match nothing"
        # Node identity, not just count: order and duplicates are the trap.
        assert actual == expected, f"{fixture}: {label} — same count, different nodes or order"

    @pytest.mark.parametrize("fixture", FIRING)
    @pytest.mark.parametrize("label,firings_on,needs_documents,probe", SITES, ids=SITE_IDS)
    def test_the_empty_recording_disagrees_with_nothing(self, fixture, label, firings_on, needs_documents, probe):
        """Negative control, part one: the recording that matches nothing stays matching nothing.

        ``company_itc_full_columns.html`` returns zero matches for 15 of the 16 selectors the
        parser reads, so every rewritten site must agree with soupsieve *and* agree that it is
        empty. An accidental scope widening — the whole risk in this change — shows up here as a
        non-empty list where the selector was empty. (``.ranges-table`` is the one site that does
        fire, and it is compared on both sides anyway.)
        """
        soup = soup_of(EMPTY_MATCH)
        if needs_documents and _documents(soup) is None:
            pytest.skip("this recording carries no section#documents at all — see the next test")
        actual, expected = _as_list(probe(soup)[0]), _as_list(probe(soup)[1])
        assert actual == expected, (
            f"{label}: the rewrite disagrees with soupsieve on the empty recording"
        )

    @pytest.mark.parametrize("label,firings_on,needs_documents,probe", SITES, ids=SITE_IDS)
    def test_a_page_with_empty_containers_matches_nothing(self, label, firings_on, needs_documents, probe):
        """Negative control, part two: every container present, every list empty.

        This is where a widened scope is caught for the ``section#documents`` sites, which the
        full-columns recording cannot exercise because it has no documents section at all. The
        page keeps one *marker* ``div.documents`` and no unmarked one, so ``:429`` is asserted to
        find **nothing**: a hand filter that stopped excluding would elect that block and return
        its (empty) list, which is one node instead of zero.
        """
        soup = BeautifulSoup(EMPTY_DOCUMENT_HTML, "lxml")
        actual, expected = _as_list(probe(soup)[0]), _as_list(probe(soup)[1])
        assert actual == expected, f"{label}: disagrees with soupsieve on the empty page"
        assert len(actual) == EMPTY_DOCUMENT_COUNTS.get(label, 0), (
            f"{label}: found {len(actual)} nodes on a page whose containers are all empty"
        )


# ============================================================ trap 1: the nested union


#: `.company-profile` nested inside `.company-info`, with an external link **outside** the profile
#: but **inside** the info block, placed after it. That ordering is what separates the union from
#: the sum: soupsieve returns document order (profile first), the sum returns
#: `.company-links a` first (outside first) and therefore elects a *different* website.
NESTED_UNION_HTML = """
<html><body>
  <div class="company-info">
    <div class="company-profile">
      <a href="https://profile.example.com/">Profile site</a>
    </div>
    <div class="company-links">
      <a href="https://outside.example.com/">Outside site</a>
      <a href="https://www.bseindia.com/stock-share-price/XYZ/500325/">BSE</a>
    </div>
  </div>
</body></html>
"""


class TestTheNestedLinkUnion:
    def _soup(self):
        return BeautifulSoup(NESTED_UNION_HTML, "lxml")

    def _find_all_version(self, soup):
        """A **local** copy of the shipped links composition — intent, not a regression guard.

        It calls the production :func:`_descendants_of_any`, so it is real evidence about the helper,
        but the four containers are composed *here*: it cannot see ``parser.py`` compose them wrongly
        and it never enters the shipped code path. The cases below that use it document the shape of
        the union and the de-duplication; the shipped composition is guarded by
        :meth:`TestTheCompositionOnAPageBuiltToDiscriminate.test_the_nested_link_containers_are_read_once`.
        """
        return _descendants_of_any(
            soup,
            "a",
            soup.find_all(class_="company-links")
            + soup.find_all(class_="links")
            + soup.find_all(class_="company-info")
            + soup.find_all(class_="company-profile"),
        )

    def test_the_selector_measures_three_and_the_sum_measures_six(self):
        soup = self._soup()
        css = ".company-links a, .links a, .company-info a, .company-profile a"
        assert len(soup.select(css)) == 3
        halves = (
            len(soup.select(".company-links a"))
            + len(soup.select(".links a"))
            + len(soup.select(".company-info a"))
            + len(soup.select(".company-profile a"))
        )
        assert halves == 6, "the fixture no longer exhibits the nested-union trap"
        assert halves > len(soup.select(css)), (
            "summing the halves is no longer an over-count here, so the trap is untested"
        )

    def test_the_rewrite_returns_the_same_nodes_in_document_order(self):
        soup = self._soup()
        css = ".company-links a, .links a, .company-info a, .company-profile a"
        assert self._find_all_version(soup) == soup.select(css)

    def test_no_node_appears_twice(self):
        nodes = self._find_all_version(self._soup())
        assert len({id(n) for n in nodes}) == len(nodes), "the union de-duplicated nothing"

    def test_the_website_comes_from_the_profile_block_not_the_later_sibling(self):
        """The parsed value that the over-count would corrupt.

        Document order elects ``profile.example.com``. The naive sum lists ``.company-links a``
        first and elects ``outside.example.com`` instead — a different `CompanyProfile.website`,
        which is the whole point of doing this carefully.
        """
        soup = self._soup()
        _, _, website, bse_code, nse_symbol = ScreenerHTMLParser()._parse_header_meta(soup, "FB")
        assert website == "https://profile.example.com/"
        assert bse_code == "500325"
        assert nse_symbol is None

        _, _, naive_website, _, _ = PreChangeScreenerHTMLParser()._parse_header_meta(soup, "FB")
        assert naive_website == website, "the two implementations must agree on this fragment"

    def test_the_real_recording_is_not_double_counted(self):
        """Lengths only, and through the **local** copy above: the production call site is untouched.

        Measured, not assumed — replacing the shipped composition with the naive per-container sum
        leaves this case green. The real recordings cannot discriminate: the naive sum still lands on
        the same ``website`` / ``bse_code`` / ``nse_symbol`` there. What this asserts is that the
        union is 7 anchors against halves of 7 and 6, which is what makes the *helper* interesting.
        """
        for fixture in FIRING:
            soup = soup_of(fixture)
            nodes = self._find_all_version(soup)
            assert len(nodes) == 7, f"{fixture}: {len(nodes)} anchors, the union is 7"
            assert len(soup.select(".company-info a")) == 7
            assert len(soup.select(".company-profile a")) == 6
            assert len({id(n) for n in nodes}) == 7, f"{fixture}: nested anchors counted twice"


# ============================================================ trap 2: the li scope


#: Two real ratio rows, plus a second `.name`/`.value` pair on a *sibling* list outside
#: `#top-ratios`. Document-wide there are three of each; scoped to the container there are two.
RATIO_SCOPE_HTML = """
<html><body>
  <ul id="top-ratios">
    <li><span class="name">Market Cap</span><span class="value">1,23,456</span></li>
    <li><span class="name">ROCE</span><span class="value">41.2</span></li>
  </ul>
  <ul class="not-top-ratios">
    <li><span class="name">Poison</span><span class="value">999</span></li>
  </ul>
</body></html>
"""


class TestTheRatioRowScope:
    def _soup(self):
        return BeautifulSoup(RATIO_SCOPE_HTML, "lxml")

    def test_the_document_carries_more_names_than_the_card_does(self):
        soup = self._soup()
        assert len(soup.select("#top-ratios li .name")) == 2
        assert len(soup.find_all(class_="name")) == 3, (
            "a document-wide lookup now happens to agree; the scope trap is untested"
        )

    def test_the_rows_are_scoped_to_top_ratios(self):
        soup = self._soup()
        rows = _descendants_of_any(soup, "li", soup.find_all(id="top-ratios"))
        assert rows == soup.select("#top-ratios li")

    def test_every_ratio_reads_its_own_name_and_value(self):
        ratios = ScreenerHTMLParser()._parse_top_ratios(self._soup())
        assert ratios.custom_ratios == {"Market Cap": 123456.0, "ROCE": 41.2}, ratios.custom_ratios
        assert ratios.market_cap == 123456.0
        assert ratios.roce == 41.2
        assert ratios.custom_ratios == PreChangeScreenerHTMLParser()._parse_top_ratios(self._soup()).custom_ratios

    def test_the_recording_reads_its_nine_rows_into_ten_keys(self):
        for fixture in FIRING:
            soup = soup_of(fixture)
            ratios = ScreenerHTMLParser()._parse_top_ratios(soup)
            assert len(soup.select("#top-ratios li")) == 9
            assert len(ratios.custom_ratios) == 10, f"{fixture}: {sorted(ratios.custom_ratios)}"
            assert ratios == PreChangeScreenerHTMLParser()._parse_top_ratios(soup), fixture


# ============================================== trap 3: the :not() structural inverse


#: The announcements block carries **no marker class and no id**, which is why the structural
#: inverse exists at all. Its heading is deliberately *not* the literal "Announcements", so the
#: heading selector does **not** fire and only the hand-filtered `:not()` chain can find it — the
#: case no recording covers on its own.
_ANN_BLOCK = """
    <div class="documents">
      <h3>Corp Announcements</h3>
      <a href="https://www.bseindia.com/corp-announcements/">All</a>
      <ul class="list-links">
        <li><a href="https://x.invalid/a1.pdf">Closure of trading window</a></li>
        <li><a href="https://x.invalid/a2.pdf">Board meeting intimation</a></li>
      </ul>
    </div>
"""

#: A block whose class list *contains* "concalls" as a **prefix** of a longer class.
#: `:not(.concalls)` matches it — class predicates compare whole class names, so `concalls-archive`
#: is a different class and is NOT excluded. A ``"concalls" in d["class"]`` substring test would
#: exclude it and silently disagree with soupsieve.
_ARCHIVE_BLOCK = """
    <div class="documents concalls-archive">
      <h3>Archive</h3>
      <ul class="list-links"><li><a href="https://x.invalid/arch.pdf">Archived call</a></li></ul>
    </div>
"""

_MARKER_BLOCKS = """
    <div class="documents annual-reports">
      <h3>Annual Reports</h3><a href="https://x.invalid/ar/2024.pdf">2024</a>
    </div>
    <div class="documents credit-ratings">
      <h3>Credit Ratings</h3><a href="https://crisil.invalid/r">Rating update</a>
    </div>
    <div class="documents concalls">
      <h3>Concalls</h3><ul><li>Jan 2026 <a href="https://x.invalid/t.pdf">Transcript</a></li></ul>
    </div>
"""


def _not_filter_html(archive_first: bool) -> str:
    """The same page, with the ``concalls-archive`` block placed before or after the real one.

    ``select_one`` returns whichever of the two qualifying containers comes **first**, so both
    orderings have to land on the same node soupsieve picks.
    """
    blocks = (_MARKER_BLOCKS + _ARCHIVE_BLOCK + _ANN_BLOCK) if archive_first else (
        _MARKER_BLOCKS + _ANN_BLOCK + _ARCHIVE_BLOCK
    )
    return f'<html><body>\n  <section id="documents">{blocks}\n  </section>\n</body></html>'


NOT_FILTER_HTML = _not_filter_html(archive_first=False)

#: The marker blocks plus an unmarked ``documents concalls-archive`` block, and **no** block whose
#: heading reads "Announcements". Two things follow, and both are the point:
#:
#: * the heading selector cannot fire, so the only route to a container is the ``:not()`` chain;
#: * ``concalls-archive`` is the **only** block that chain can elect — the three markers are
#:   excluded, and this one survives on whole-class semantics. A substring test elects *nothing*, so
#:   the announcements silently become empty rather than wrong, which is the failure mode
#:   :meth:`TestTheStructuralInverseHandFilter.test_a_substring_test_would_be_a_different_filter`
#:   describes but cannot see on the shipped path.
ARCHIVE_ONLY_HTML = (
    '<html><body>\n  <section id="documents">'
    f"{_MARKER_BLOCKS}{_ARCHIVE_BLOCK}"
    "\n  </section>\n</body></html>"
)


class TestTheStructuralInverseHandFilter:
    NOT_CSS = "div.documents:not(.annual-reports):not(.credit-ratings):not(.concalls)"

    def _soup(self, html=NOT_FILTER_HTML):
        return BeautifulSoup(html, "lxml")

    def _hand_filtered(self, soup):
        return _hand_filtered(_require_documents(soup))

    def test_the_selector_excludes_exactly_the_three_marker_blocks(self):
        soup = self._soup()
        matched = soup.select(self.NOT_CSS)
        blocks = soup.find_all("div", class_="documents")
        assert len(blocks) == 5, f"{len(blocks)} div.documents in the fragment"
        # three marker blocks + the unmarked announcements block + the `concalls-archive` block,
        # which `:not(.concalls)` does NOT exclude (class predicates compare whole class names —
        # see the substring test below)
        assert [m.get("class") for m in matched] == [["documents"], ["documents", "concalls-archive"]]
        assert self._hand_filtered(soup) == matched

    def test_a_substring_test_would_be_a_different_filter(self):
        """The exclusion must be a **set** test, not ``in`` on the joined class string.

        ``concalls-archive`` is excluded by ``:not(.concalls)``? No — it is *not* excluded, because
        class predicates compare whole class names. A ``"concalls" in d["class"]`` test would
        exclude it and silently disagree with soupsieve.
        """
        soup = self._soup(_not_filter_html(archive_first=True))
        block = soup.select("div.documents.concalls-archive")[0]
        assert soup.select(self.NOT_CSS)[0] is block, (
            "soupsieve does match a block merely containing a longer class name"
        )
        assert self._hand_filtered(soup)[0] is block, (
            "the hand filter and soupsieve disagree about concalls-archive"
        )
        assert block in self._hand_filtered(soup), (
            "the hand filter excluded concalls-archive; it is doing a substring test, not a set test"
        )
        # ... and a substring test really would have dropped it, so this case has teeth:
        substring_filter = [
            d for d in soup.find_all("div", class_="documents")
            if not any(marker in " ".join(d.get("class") or []) for marker in
                       ("annual-reports", "credit-ratings", "concalls"))
        ]
        assert block not in substring_filter, (
            "the substring filter happens to agree here — the fragment no longer separates the two"
        )

    def test_only_the_first_match_is_taken(self):
        for archive_first in (False, True):
            soup = self._soup(_not_filter_html(archive_first))
            assert len(self._hand_filtered(soup)) == 2, "two blocks now survive the exclusion"
            first = soup.select_one(self.NOT_CSS)
            assert self._hand_filtered(soup)[0] is first, (
                "`next(...)` and `select_one` must pick the same container"
            )

    def test_the_announcements_are_read_from_the_unmarked_container(self):
        soup = self._soup()
        concalls, reports, ratings, announcements = ScreenerHTMLParser()._parse_documents(soup)
        assert announcements == [
            {"title": "Closure of trading window", "url": "https://x.invalid/a1.pdf"},
            {"title": "Board meeting intimation", "url": "https://x.invalid/a2.pdf"},
        ]
        assert [r["year"] for r in reports] == ["2024"], "the annual-reports block leaked in"
        assert [r["title"] for r in ratings] == ["Rating update"]
        assert [c.title for c in concalls] == ["Jan 2026 Transcript"]
        # the container's "All" navigation link is not an announcement
        assert not any(a["url"].endswith("/corp-announcements/") for a in announcements)

    def test_it_agrees_with_the_pre_change_parse(self):
        for html in (NOT_FILTER_HTML, _not_filter_html(archive_first=True)):
            old = PreChangeScreenerHTMLParser()._parse_documents(BeautifulSoup(html, "lxml"))
            new = ScreenerHTMLParser()._parse_documents(BeautifulSoup(html, "lxml"))
            assert new == old, html

    def test_the_marker_classes_are_still_what_the_selector_excludes(self):
        """Guards the exclusion list itself: drop one and the hand filter over-matches."""
        from bfinance.screener.parser import _DOCUMENTS_MARKER_CLASSES

        soup = self._soup()
        blocks = soup.find_all("div", class_="documents")
        # the marker classes are exactly the ones carried by the three marker blocks; the
        # `concalls-archive` block contributes a different class and must not be in the list
        marker_classes = {
            c
            for block in blocks
            for c in block.get("class")
            if c != "documents" and block.get("class") != ["documents"]
        }
        assert set(_DOCUMENTS_MARKER_CLASSES) == {"annual-reports", "credit-ratings", "concalls"}
        assert marker_classes - set(_DOCUMENTS_MARKER_CLASSES) == {"concalls-archive"}
        surviving = _hand_filtered(_require_documents(soup))
        assert surviving == soup.select(self.NOT_CSS)
        assert not any(
            set(b.get("class")) & set(_DOCUMENTS_MARKER_CLASSES) for b in surviving
        ), "a marker block survived the exclusion"

    def test_the_shipped_filter_is_the_container_soupsieve_elects(self):
        """The hand filter on the shipped path — production :func:`_structural_inverse`, not the copy.

        :meth:`test_a_substring_test_would_be_a_different_filter` builds the comparison out of
        ``self._hand_filtered``, a reimplementation living in this file, so it can only catch a
        divergence between two *test-side* filters. This one calls the production helper and then
        reads the announcements out of the container it elected, which is the parsed value a
        substring test destroys (it excludes the one electable block and returns ``None``).
        """
        for label, html in (
            ("archive only", ARCHIVE_ONLY_HTML),
            ("archive last", NOT_FILTER_HTML),
            ("archive first", _not_filter_html(archive_first=True)),
        ):
            soup = BeautifulSoup(html, "lxml")
            doc_sec = _require_documents(soup)
            assert _structural_inverse(doc_sec) is doc_sec.select_one(self.NOT_CSS), (
                f"{label}: the shipped hand filter and soupsieve elect different containers"
            )

        soup = BeautifulSoup(ARCHIVE_ONLY_HTML, "lxml")
        assert _structural_inverse(_require_documents(soup)) is soup.find(
            "div", class_="concalls-archive"
        )
        concalls, reports, ratings, announcements = ScreenerHTMLParser()._parse_documents(soup)
        assert announcements == [
            {"title": "Archived call", "url": "https://x.invalid/arch.pdf"}
        ], announcements
        assert [c.title for c in concalls] == ["Jan 2026 Transcript"], "the marker block leaked in"
        assert [r["year"] for r in reports] == ["2024"]
        assert [r["title"] for r in ratings] == ["Rating update"]
        assert (
            ScreenerHTMLParser()._parse_documents(BeautifulSoup(ARCHIVE_ONLY_HTML, "lxml"))
            == PreChangeScreenerHTMLParser()._parse_documents(BeautifulSoup(ARCHIVE_ONLY_HTML, "lxml"))
        ), "the shipped parse and the pre-change parse disagree on this page"


# ============================================== trap 4: the `or` short-circuit fallback


CONCALLS_BOTH_HTML = """
<html><body>
  <section id="documents">
    <div class="documents concalls" id="concalls">
      <ul>
        <li>Jan 2026 <a href="https://x.invalid/t.pdf">Transcript</a></li>
        <li>Apr 2026 <a href="https://x.invalid/t2.pdf">Transcript</a></li>
      </ul>
    </div>
  </section>
</body></html>
"""

CONCALLS_ID_ONLY_HTML = CONCALLS_BOTH_HTML.replace(
    'class="documents concalls" id="concalls"', 'class="documents" id="concalls"'
)

CONCALLS_CLASS_ONLY_HTML = CONCALLS_BOTH_HTML.replace(
    'class="documents concalls" id="concalls"', 'class="documents concalls"'
)


class TestTheConcallShortCircuit:
    def _soup(self, html):
        return BeautifulSoup(html, "lxml")

    def test_both_halves_match_so_a_union_would_double_count(self):
        """The fixture has to make the `or` decidable, or the test proves nothing.

        With the class *and* the id on the same block, soupsieve returns two nodes for **each**
        half. Concatenating them would give four concalls from a two-row list.
        """
        soup = self._soup(CONCALLS_BOTH_HTML)
        assert len(soup.select(".concalls ul li")) == 2
        assert len(soup.select("#concalls ul li")) == 2
        assert len(soup.select(".concalls ul li") + soup.select("#concalls ul li")) == 4

    def test_the_class_half_wins_and_the_id_half_is_never_read(self):
        concalls = ScreenerHTMLParser()._parse_documents(self._soup(CONCALLS_BOTH_HTML))[0]
        assert [c.title for c in concalls] == ["Jan 2026 Transcript", "Apr 2026 Transcript"], (
            f"{len(concalls)} concalls parsed from a two-row list"
        )

    @pytest.mark.parametrize(
        "html,expected",
        [
            (CONCALLS_CLASS_ONLY_HTML, ["Jan 2026 Transcript", "Apr 2026 Transcript"]),
            (CONCALLS_ID_ONLY_HTML, ["Jan 2026 Transcript", "Apr 2026 Transcript"]),
        ],
        ids=["class-only", "id-only"],
    )
    def test_either_half_on_its_own_still_parses_both_rows(self, html, expected):
        concalls = ScreenerHTMLParser()._parse_documents(self._soup(html))[0]
        assert [c.title for c in concalls] == expected

    @pytest.mark.parametrize(
        "html", [CONCALLS_BOTH_HTML, CONCALLS_CLASS_ONLY_HTML, CONCALLS_ID_ONLY_HTML],
        ids=["both", "class-only", "id-only"],
    )
    def test_it_agrees_with_the_pre_change_parse(self, html):
        old = PreChangeScreenerHTMLParser()._parse_documents(self._soup(html))
        new = ScreenerHTMLParser()._parse_documents(self._soup(html))
        assert new == old


# =================================================== the analysis and cagr sites, on fragments


PROS_CONS_HTML = """
<html><body>
  <div class="pros"><ul><li>pro one</li><li>pro two</li></ul></div>
  <div class="cons"><ul><li>con one</li></ul></div>
  <ul><li>a bare li that is in neither</li></ul>
  <div class="pros"><p>no list here</p></div>
  <table class="ranges-table"><th>Profit Growth</th>
    <tr><td>10 years</td><td>12%</td></tr>
    <tr><td>5 years</td><td>14%</td></tr>
  </table>
  <table class="not-a-range"><th>Ignored</th><tr><td>a</td><td>b</td></tr></table>
</body></html>
"""


class TestProsConsAndCagrs:
    def _soup(self):
        return BeautifulSoup(PROS_CONS_HTML, "lxml")

    def test_pros_and_cons_do_not_leak_a_stray_li(self):
        soup = self._soup()
        pros = _descendants_of_any(soup, "li", _uls_under(soup.find_all(class_="pros")))
        cons = _descendants_of_any(soup, "li", _uls_under(soup.find_all(class_="cons")))
        assert pros == soup.select(".pros ul li")
        assert cons == soup.select(".cons ul li")
        assert len(pros) == 2 and len(cons) == 1

    def test_a_block_without_a_list_contributes_nothing(self):
        assert "no list here" not in str(ScreenerHTMLParser()._parse_analysis(self._soup()).pros)

    def test_cagrs_read_every_ranges_table_and_no_other(self):
        soup = self._soup()
        assert [
            t.find("th").get_text(strip=True) for t in soup.find_all(class_="ranges-table")
        ] == [t.find("th").get_text(strip=True) for t in soup.select(".ranges-table")]
        cagrs = ScreenerHTMLParser()._parse_cagrs(soup)
        assert cagrs == {"Profit Growth": {"10 years": "12%", "5 years": "14%"}}, cagrs
        assert "Ignored" not in cagrs

    def test_it_agrees_with_the_pre_change_parse(self):
        soup = self._soup()
        assert ScreenerHTMLParser()._parse_analysis(soup) == PreChangeScreenerHTMLParser()._parse_analysis(soup)
        assert ScreenerHTMLParser()._parse_cagrs(soup) == PreChangeScreenerHTMLParser()._parse_cagrs(soup)


class TestTheAboutParagraphFallback:
    """``:166`` — a two-level chain, then an ``or`` that must not fire when it should not.

    ⚠ Neither page here discriminates the chain: on ``INSIDE_HTML`` the document's first ``.about``
    happens to be the profile's, so dropping the ``.company-profile`` scope still reads the right
    paragraph. Measured: a document-wide ``.about p`` at this call site leaves both cases green. The
    shipped composition is guarded on a page where the outsider comes first —
    :meth:`TestTheCompositionOnAPageBuiltToDiscriminate.test_the_about_paragraph_comes_from_inside_the_profile`.
    """

    INSIDE_HTML = """
    <html><body>
      <div class="company-profile"><div class="about"><p>inside the profile</p></div></div>
      <div class="about"><p>outside the profile</p></div>
    </body></html>
    """

    OUTSIDE_HTML = """
    <html><body>
      <div class="about"><p>outside the profile</p></div>
      <div class="company-profile"><div class="commentary"><p>not inside an .about</p></div></div>
    </body></html>
    """

    def test_the_profile_scoped_paragraph_wins_over_the_document_wide_one(self):
        soup = BeautifulSoup(self.INSIDE_HTML, "lxml")
        assert _first_descendant_of_any(
            soup, "p", _about_blocks(soup, inside_profile=True)
        ) is soup.select_one(".company-profile .about p")
        assert ScreenerHTMLParser()._parse_header_meta(soup, "FB")[1] == "inside the profile"

    def test_the_document_wide_paragraph_is_the_fallback(self):
        soup = BeautifulSoup(self.OUTSIDE_HTML, "lxml")
        assert _first_descendant_of_any(
            soup, "p", _about_blocks(soup, inside_profile=True)
        ) is None, "there is no .about inside a .company-profile here"
        assert ScreenerHTMLParser()._parse_header_meta(soup, "FB")[1] == "outside the profile"

    @pytest.mark.parametrize("html", [INSIDE_HTML, OUTSIDE_HTML])
    def test_it_agrees_with_the_pre_change_parse(self, html):
        soup = BeautifulSoup(html, "lxml")
        assert (
            ScreenerHTMLParser()._parse_header_meta(soup, "FB")
            == PreChangeScreenerHTMLParser()._parse_header_meta(soup, "FB")
        )

    def test_one_element_carrying_both_classes_is_not_a_descendant_of_itself(self):
        """``<div class="company-profile about">`` must not satisfy ``.company-profile .about p``."""
        soup = BeautifulSoup(
            '<div class="company-profile about"><p>same element</p></div>', "lxml"
        )
        assert soup.select_one(".company-profile .about p") is None
        assert _first_descendant_of_any(
            soup, "p", _about_blocks(soup, inside_profile=True)
        ) is None


class TestTheRecordingsStillCarryEverySelector:
    """A **fixture** guard: the recordings still contain the markup every case above reads.

    It asserts nothing about ``parser.py`` — it runs ``soup.select`` against the recorded bytes, so a
    selector *narrowed in the rewrite* would leave this green. What it catches is a trimmed or
    re-recorded fixture quietly emptying out the cases that depend on it, which is the failure mode
    the module docstring calls out for ``company_itc_full_columns.html``.
    """

    REQUIRED = [
        ".about p",
        ".company-info a",
        "#top-ratios li .name",
        "#top-ratios li .value",
        ".pros ul li",
        ".cons ul li",
        ".ranges-table th",
        "section#documents .concalls ul li",
        "section#documents .annual-reports a",
        "section#documents .credit-ratings a",
        "section#documents div.documents ul.list-links a",
    ]

    @pytest.mark.parametrize("fixture", FIRING)
    @pytest.mark.parametrize("selector", REQUIRED, ids=[s[:28] for s in REQUIRED])
    def test_it_still_matches(self, fixture, selector):
        assert soup_of(fixture).select(selector), f"{fixture}: {selector} is gone"
