"""
Sector and industry support for :mod:`bfinance.trendlyne` — classification, taxonomy, constituents.

Three capabilities, and all three come off pages this package is already reading or can reach with
one request. None of them needs a new token, a new credential, or a new data source.

1. **Classification at zero marginal cost.** Every stock page this package fetches for the OHLC
   token already carries the symbol's sector and industry as schema.org microdata. See
   :func:`parse_classification`. **No new request.**
2. **The whole taxonomy, from one page.** Any sector or industry page carries
   ``<select id="JS_diviGroupSelector">`` with the complete sector and industry vocabulary. See
   :func:`parse_taxonomy`. **One request, then the vocabulary is in memory.**
3. **Constituents with exact weights.** See :func:`parse_constituents`.

The route shape is the first thing to get right, because it is unlike the stock route
---------------------------------------------------------------
A stock page is ``/equity/{stock_id}/{SYMBOL}/{slug}/`` — id first, then the symbol. A group page
puts the **id first and the slug second, with no symbol segment at all**::

    /equity/sector/20/automobiles-auto-components/
    /equity/industry/9/auto-parts-equipment/

Feeding a group URL to the stock regex does not fail loudly; it matches ``stock_id="sector"`` and
carries on. That is why the two route patterns below are separate constants rather than one with
an optional branch, and why :func:`parse_taxonomy` validates every option against the route its
own kind requires. **No id in this module is hardcoded and none is guessed** — every id, slug and
display name is read out of the page.

Where the sector and industry names come from
---------------------------------------------
There is **no ``data-*`` attribute and no embedded JSON** carrying the classification. The field
name *is* the breadcrumb label, and the value is the URL::

    <a href="https://trendlyne.com/equity/sector/20/automobiles-auto-components/"
       property="item" typeof="WebPage">
      <span property="name">SECTOR : AUTOMOBILES &amp; AUTO COMPONENTS</span></a>
    <a href="https://trendlyne.com/equity/industry/9/auto-parts-equipment/"
       property="item" typeof="WebPage">
      <span property="name">INDUSTRY : AUTO PARTS &amp; EQUIPMENT</span></a>

So the label prefix (``SECTOR :`` / ``INDUSTRY :``) is the only field name, and it is what
:func:`parse_classification` keys on. Measured 2026-09-27: 9 of 9 sampled symbols carried both
labels, across 8 distinct sectors, and the stock page carries exactly one of each — so first-match
is not a coin flip here either. A labelled breadcrumb whose href disagrees with its own label is
raised, not guessed at: that is a markup change, and silently trusting the label would attach a
sector id to the wrong slug.

Unescaping order, which is not a detail
---------------------------------------
Constituent hrefs carry an XML-escaped ampersand: the raw markup holds
``/equity/807/M&amp;M/mahindra-mahindra-ltd/`` and the live symbol is ``M&M``. Percent-decoding
first leaves the literal ``M&amp;M`` in the index, which then matches nothing and drops a
constituent **silently** — no error, just a sector that is quietly missing one of its two largest
names. So :func:`_decoded` unescapes before it percent-decodes, which is the same order
:func:`bfinance.trendlyne.client.parse_sitemap` uses and for the same reason. Measured on the
recorded Autos page: 2 of the 210 hrefs are escaped-ampersand cases (``ARE&M``, ``M&M``) and both
are recovered; the symbol pattern is also tolerant of ``-`` so the two hyphenated names in the same
class (``BAJAJ-AUTO``, ``MMRUBBR-B``) survive. The count is recorded in
``attrs["bfinance_trendlyne_escaped_ampersand_rows"]`` so the fix is visible on the frame.

The row-cap guard
-----------------
One sector can legitimately hold hundreds of constituents — 210 for Autos — and the repo has a
live precedent for a *silent* cap: the insider-trading page returns **exactly 500** rows. A page
truncated server-side looks identical to a complete one, and a constituent list that lost its tail
would report a market-cap total and a set of weights that are each internally consistent and
jointly wrong. So every constituent frame records its row count and raises a flag when the count is
a suspiciously round number. The flag is a flag, not a raise: a genuine 500-name sector is
possible, and refusing to return a frame because a number is round would be trading one wrong
answer for a different one. What must never happen is a truncated frame wearing a full
provenance note, which is what the flag and the count are for.

The market-cap unit is **not asserted**
--------------------------------------
The column header says ``Market Cap`` and the sector total is 3,660,716.05 for Autos. **No unit
label appears anywhere on the 2.3 MB page** — the whole document was searched for
Cr/L/lakh/lac/crore/million/billion near the header and there is none. So the unit is inferred,
and this module does not assert one. What was measured:

* ``data-order`` is the exact sortable value; the rendered text is the same number with thousands
  separators and one decimal. The 210 values sum to 3,660,716.05.
* A company's value is **byte-identical on the sector page and on its industry's own page** —
  147 of 147 shared names agree exactly on both the market cap and the last-traded price — so it is
  a per-company figure, not a page artefact.
* The ranking is a plausible market-cap ranking (Maruti, M&M, Bajaj Auto, Eicher, TVS, Motherson,
  Hyundai, Tata Motors CV, Bosch, Hero at the top).
* **The unit could not be confirmed.** The decisive test is to divide the market cap by the row's
  own last-traded price, which must give that company's share count. It does not: across the 210
  rows the implied values span 0.13 to 1,055, and **37 of 210 fall below 1 crore**, which no listed
  company can be. MRF alone gives 0.42 against a share count in the region of 1.3 crore. The price
  column is genuine — this package's own recorded capture has MOTHERSON closing at 166.22 on
  2026-09-25 against this page's 165.71 — so the two columns are simply not on a common scale.

Hence: ``attrs["bfinance_trendlyne_market_cap_unit"] == "unverified"`` on every frame from here.
The **weight** is the contract instead, and it is exact and unit-free: market cap divided by the
sector's own total. The four largest Autos names come out at MARUTI 10.3621%, M&M 10.3098%,
BAJAJ-AUTO 8.6131% and MOTHERSON 4.7777%, and the weights sum to 100.

Nothing is fabricated
---------------------
A page with no taxonomy select raises. A constituent row with no market cap raises. A group route
whose id will not parse raises. A breadcrumb whose label and href disagree raises. A count that
looks capped is flagged. Nothing is filled, interpolated or dropped to make a frame come out.
"""

from __future__ import annotations

import html as _html
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Tuple
from urllib.parse import unquote

import pandas as pd
from bs4 import BeautifulSoup

from .errors import TrendlyneSchemaError
from .tables import (
    PROVENANCE,
    SIGNATURES,
    TableSignature,
    find_table_elements,
)

logger = logging.getLogger("bfinance")

#: The complete group vocabulary lives in this one ``<select>`` on every sector and industry page.
#: Its ``id`` is the only handle: there is no ``class`` and no ``name`` to key on, and the options
#: are the only place the ids are published, so an id can be harvested but never derived.
TAXONOMY_SELECT_ID = "JS_diviGroupSelector"

#: ``/equity/sector/{id}/{slug}/`` — **id first, no symbol segment**. Deliberately not a branch of
#: the stock route: see this module's docstring.
SECTOR_ROUTE = "/equity/sector/{group_id}/{group_slug}/"
#: ``/equity/industry/{group_id}/{group_slug}/``
INDUSTRY_ROUTE = "/equity/industry/{group_id}/{group_slug}/"

_GROUP_PATH = r"/equity/{kind}/(\d+)/([^/?#]+)/?"
_SECTOR_PATH_RE = re.compile(_GROUP_PATH.format(kind="sector"))
_INDUSTRY_PATH_RE = re.compile(_GROUP_PATH.format(kind="industry"))

#: The breadcrumb's only field name. There is no ``data-*`` and no embedded JSON for the
#: classification, so this label is the discriminator.
_LABEL_RE = re.compile(r"^\s*(SECTOR|INDUSTRY)\s*:\s*(.+?)\s*$", re.IGNORECASE)

#: The constituent link. Tolerant of ``-`` and ``&`` in the symbol segment because trendlyne
#: carries both: ``BAJAJ-AUTO`` and ``MMRUBBR-B`` for the hyphens, ``M&amp;M`` and ``ARE&amp;M``
#: for the ampersands. A pattern of ``[A-Z0-9]+`` would silently drop all four.
_STOCKROW_RE = re.compile(r"/equity/(\d+)/([^/]+)/([^/?#]+)/?$")

#: A constituent count that is a multiple of this is flagged. 500 is not a guess: the
#: insider-trading page returns **exactly 500** rows and may be capped server-side, so a round
#: count is this site's known truncation signature.
SUSPICIOUS_ROW_MULTIPLE = 100

#: The signatures this module reads. Referenced by name rather than redefined, so a rename in
#: :data:`bfinance.trendlyne.tables.SIGNATURES` is a failure here instead of a silent miss.
CONSTITUENTS_SIG = "sector_constituents"
AGGREGATE_SIG = "sector_industry_aggregate"


def _decoded(value: str) -> str:
    """XML entity-decode **then** percent-decode a path segment.

    The order is load-bearing. A constituent href is stored HTML-escaped
    (``/equity/807/M&amp;M/...``) and the live symbol is ``M&M``; percent-decoding first leaves
    the literal ``M&amp;M``, which matches nothing and drops the constituent with no error at all.
    This is the same helper and the same order as :func:`bfinance.trendlyne.client._decoded`,
    duplicated rather than imported because :mod:`~bfinance.trendlyne.client` imports this module
    for the fetch methods and an import edge the other way would be a cycle.
    """
    return unquote(_html.unescape(value))


# ------------------------------------------------------------------ the group


@dataclass(frozen=True)
class TrendlyneGroup:
    """One sector or industry: everything needed to address it on trendlyne.

    Attributes:
        kind: ``"sector"`` or ``"industry"``. Not free text — it selects the route, and
            :attr:`path` is only correct for one of the two.
        group_id: The integer path segment. Read out of a page, never derived.
        slug: The lowercase hyphenated name used in the URL.
        name: The display name, with the breadcrumb's ``SECTOR :`` / ``INDUSTRY :`` label and the
            select's ``Sector:`` / ``Industry:`` prefix removed. **Verbatim, in whatever case the
            page used** — trendlyne itself is inconsistent here: the stock-page breadcrumb renders
            ``AUTOMOBILES & AUTO COMPONENTS`` in caps while the taxonomy select on the sector page
            renders ``Automobiles & Auto Components`` in title case, for the same sector id 20.
            Both are preserved rather than normalised, because folding case would be inventing a
            spelling the site does not use. Key on :attr:`group_id` or :attr:`slug`, which agree
            across both sources.
    """

    kind: str
    group_id: int
    slug: str
    name: str

    def __post_init__(self) -> None:
        if self.kind not in ("sector", "industry"):
            raise ValueError(
                f"kind must be 'sector' or 'industry', got {self.kind!r}. The kind selects the "
                f"route shape and there is no third one."
            )
        if self.group_id <= 0:
            raise ValueError(
                f"group_id must be a positive integer, got {self.group_id!r} for "
                f"{self.kind} {self.name!r}"
            )

    @property
    def key(self) -> str:
        """``"sector:20"`` — the key this group is filed under in a taxonomy mapping."""
        return f"{self.kind}:{self.group_id}"

    @property
    def route(self) -> str:
        """The route template for this kind."""
        return SECTOR_ROUTE if self.kind == "sector" else INDUSTRY_ROUTE

    @property
    def path(self) -> str:
        """The absolute path of this group's page. **Id first** — see this module's docstring."""
        return self.route.format(group_id=self.group_id, group_slug=self.slug)

    @property
    def url(self) -> str:
        """Absolute URL of :attr:`path`."""
        return f"https://trendlyne.com{self.path}"


def _group_from_path(path: str, kind: str, name: str) -> TrendlyneGroup:
    """Build a :class:`TrendlyneGroup` from a path, validated against the route for ``kind``.

    Raises:
        TrendlyneSchemaError: The path does not match the route its own kind requires. A group id
            that will not parse is a markup change, and returning a group with a guessed id would
            put a wrong sector on every symbol that uses it.
    """
    pattern = _SECTOR_PATH_RE if kind == "sector" else _INDUSTRY_PATH_RE
    match = pattern.search(path or "")
    if match is None:
        other = "industry" if kind == "sector" else "sector"
        raise TrendlyneSchemaError(
            f"a {kind} option points at {path!r}, which does not match {self_route(kind)!r}. "
            f"Note the id comes first and there is no symbol segment. If this option is really "
            f"an {other}, the page has changed shape; nothing here will guess which."
        )
    return TrendlyneGroup(kind=kind, group_id=int(match.group(1)), slug=match.group(2), name=name)


def self_route(kind: str) -> str:
    """The route template for ``kind``. A function so the error message cannot drift from it."""
    return SECTOR_ROUTE if kind == "sector" else INDUSTRY_ROUTE


# ------------------------------------------------------------------ 1. classification


def parse_classification(html: str) -> Dict[str, TrendlyneGroup]:
    """The sector and industry of the symbol whose page this is, read from the breadcrumb.

    **Free.** This parses the page the client already fetches for the per-symbol OHLC token, so
    adding it costs no request. Verified 2026-09-27: MOTHERSON's page carries exactly one
    ``SECTOR :`` and one ``INDUSTRY :`` label, and 9 of 9 sampled symbols carried both across 8
    distinct sectors.

    There is no ``data-*`` attribute and no embedded JSON for this, so the breadcrumb label is the
    field name and the href is the value. See this module's docstring.

    Args:
        html: A trendlyne **stock** page.

    Returns:
        ``{"sector": TrendlyneGroup, "industry": TrendlyneGroup}``. A page that carries neither
        label returns ``{}`` — total by design, because the caller may be parsing a page whose
        classification is a bonus rather than the product. :meth:`TrendlyneClient.classification`
        is the strict door: it raises rather than returning an empty mapping.

    Raises:
        TrendlyneSchemaError: A labelled breadcrumb's href does not match the route its own label
            implies. The label and the URL disagree, so attaching one to the other would be a
            guess; the two facts on the page contradict each other and that is reported.
    """
    soup = BeautifulSoup(html or "", "lxml")
    out: Dict[str, TrendlyneGroup] = {}
    for anchor in soup.select('a[property="item"][typeof="WebPage"]'):
        span = anchor.find("span", attrs={"property": "name"})
        if span is None:
            continue
        label = _LABEL_RE.match(span.get_text(" ", strip=True))
        if label is None:
            # MARKETS, and the symbol's own breadcrumb. Not a classification.
            continue
        kind = label.group(1).lower()
        name = label.group(2).strip()
        href = str(anchor.get("href") or "")
        path = href[len("https://trendlyne.com"):] if href.startswith("https://trendlyne.com") \
            else href
        out[kind] = _group_from_path(path, kind, name)
    return out


# ------------------------------------------------------------------ 2. taxonomy


def parse_taxonomy(html: str) -> Dict[str, TrendlyneGroup]:
    """The complete sector and industry vocabulary, harvested from one page.

    Any sector or industry page carries ``<select id="JS_diviGroupSelector">`` with **164 options
    on 2026-09-27: 31 sectors (ids 1-31, contiguous) and 133 industries (ids 2-284,
    non-contiguous)**, each ``value`` a full URL and each option's text a clean display name. The
    ids are published **only** here, which is the point: nothing in this module hardcodes an id or
    derives one, so a re-numbering upstream is a different number in a different mapping rather
    than a wrong sector attached to a symbol.

    The option text is ``"Sector: Automobiles & Auto Components"``, so the kind prefix is stripped
    and the remainder is the display name.

    Args:
        html: A trendlyne sector or industry page.

    Returns:
        ``{"sector:20": TrendlyneGroup, "industry:9": TrendlyneGroup, ...}`` — every option,
        keyed by :attr:`TrendlyneGroup.key`.

    Raises:
        TrendlyneSchemaError: The page carried no taxonomy select, or none of its options parsed.
            A silently empty vocabulary would make every group id look invalid, so the change is
            reported.
    """
    soup = BeautifulSoup(html or "", "lxml")
    select = soup.find("select", id=TAXONOMY_SELECT_ID)
    if select is None:
        raise TrendlyneSchemaError(
            f"no <select id={TAXONOMY_SELECT_ID!r}> on this page, so there is no sector/industry "
            f"vocabulary to read. That select is the only place trendlyne publishes the group "
            f"ids, so this page is not a group page, or the markup changed."
        )

    out: Dict[str, TrendlyneGroup] = {}
    unparsed: List[str] = []
    for option in select.find_all("option"):
        value = str(option.get("value") or "").strip()
        text = option.get_text(" ", strip=True)
        if not value:
            continue
        path = value[len("https://trendlyne.com"):] if value.startswith("https://trendlyne.com") \
            else value
        kind = None
        for candidate in ("sector", "industry"):
            if _route_pattern(candidate).search(path):
                kind = candidate
                break
        if kind is None:
            unparsed.append(f"{text!r} -> {value!r}")
            continue
        # "Sector: Automobiles & Auto Components" -> "Automobiles & Auto Components"
        name = text.split(":", 1)[1].strip() if ":" in text else text
        group = _group_from_path(path, kind, name)
        out[group.key] = group

    if not out:
        raise TrendlyneSchemaError(
            f"the taxonomy select held {len(select.find_all('option'))} options and none parsed. "
            f"First few: {unparsed[:3]}. The group route shape is {SECTOR_ROUTE!r} / "
            f"{INDUSTRY_ROUTE!r} -- id first, no symbol segment."
        )
    if unparsed:
        logger.warning(
            "%d taxonomy option(s) did not match a group route and were skipped: %s",
            len(unparsed), unparsed[:3],
        )
    return out


def _route_pattern(kind: str) -> "re.Pattern[str]":
    return _SECTOR_PATH_RE if kind == "sector" else _INDUSTRY_PATH_RE


def declared_group(html: str) -> Optional[TrendlyneGroup]:
    """The group **this page is about**, from the one option trendlyne marks ``selected``.

    A group page's taxonomy select carries all 164 options but marks exactly one ``selected`` —
    the group whose page you are on. Verified 2026-09-27: the Autos page selects
    ``sector/20/automobiles-auto-components`` and the Auto Parts page selects
    ``industry/9/auto-parts-equipment``, each while still listing the other 163.

    This is the page's own statement of its identity, so it is preferred over anything a caller
    could pass in. The caller usually knows the same thing (it built the URL), and a
    disagreement between the two is worth seeing, but the page is the authority here because it is
    the only one of the two that cannot be wrong about which page it is.

    Returns:
        The :class:`TrendlyneGroup`, or ``None`` when the page carries no taxonomy select or marks
        no option. ``None`` is a legitimate answer — a bare excerpt of a group page, or a page
        this module does not recognise — and is not an error.
    """
    soup = BeautifulSoup(html or "", "lxml")
    select = soup.find("select", id=TAXONOMY_SELECT_ID)
    if select is None:
        return None
    for option in select.find_all("option"):
        if not option.has_attr("selected"):
            continue
        value = str(option.get("value") or "").strip()
        if not value:
            continue
        path = value[len("https://trendlyne.com"):] if value.startswith("https://trendlyne.com") \
            else value
        for candidate in ("sector", "industry"):
            if _route_pattern(candidate).search(path):
                text = option.get_text(" ", strip=True)
                name = text.split(":", 1)[1].strip() if ":" in text else text
                return _group_from_path(path, candidate, name)
    return None


def taxonomy_counts(taxonomy: Mapping[str, TrendlyneGroup]) -> Dict[str, int]:
    """``{"sectors": 31, "industries": 133, "total": 164}`` — a shape check, not a constant.

    Returned rather than asserted so a caller can log what the page actually carried, and so a
    test can compare it against the live page's numbers without hardcoding them twice.
    """
    sectors = sum(1 for g in taxonomy.values() if g.kind == "sector")
    industries = sum(1 for g in taxonomy.values() if g.kind == "industry")
    return {"sectors": sectors, "industries": industries, "total": len(taxonomy)}


# ------------------------------------------------------------------ 3. constituents


@dataclass(frozen=True)
class ConstituentResult:
    """A constituent frame plus the measurements that make it checkable.

    Attributes:
        frame: The constituents. Columns: ``Symbol``, ``Company``, ``Stock ID``, ``Market Cap``,
            ``Weight %``, ``LTP``, ``Page``, ``Sparkline``.
        group: The sector or industry the list belongs to, when the page declared one.
        row_count: Data rows in the table. **Check this against ``attrs``** — a server-side cap
            is invisible in the frame otherwise.
        sector_market_cap: The sum of the rows' market caps, in the page's unstated unit. This is
            the divisor for every weight.
        escaped_ampersand_rows: How many hrefs arrived HTML-escaped and were recovered by
            unescaping before percent-decoding — counted as the rows whose parsed symbol holds a
            literal ``&``. **2 on the recorded Autos page** (``ARE&M``, ``M&M``). ``0`` would mean
            either that no row needed it or that the decode order regressed, and the recorded
            fixture makes the two distinguishable: it is not 0 there.
    """

    frame: pd.DataFrame
    group: Optional[TrendlyneGroup]
    row_count: int
    sector_market_cap: float
    escaped_ampersand_rows: int

    @property
    def suspiciously_capped(self) -> bool:
        """``True`` when the row count is a multiple of :data:`SUSPICIOUS_ROW_MULTIPLE`.

        The live precedent is the insider-trading page's **exactly 500**. A flag, not a raise: a
        genuine 500-name sector is possible, and refusing to return a frame because a number is
        round would trade one wrong answer for another. The count and this flag together are what
        stop a truncated list from wearing a full provenance note.
        """
        return self.row_count > 0 and self.row_count % SUSPICIOUS_ROW_MULTIPLE == 0


def _float_or_none(value: Optional[str]) -> Optional[float]:
    """``data-order`` to float, or ``None`` when the attribute is absent or unparseable.

    Returns ``None`` rather than 0 or ``NaN`` for a missing value: a zero market cap would put the
    name in the frame with a real-looking weight, and ``NaN`` would poison the sector total.
    """
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_constituents(html: str) -> ConstituentResult:
    """Every constituent of a sector or industry, with an exact weight from market cap.

    The table is located by its **header signature** (``sector_constituents``), not by its heading
    or by a position on the page. On a sector page there are exactly two tables and the other one
    is the per-industry aggregate block, so selecting by index or by a loose word like ``"Name"``
    is the failure this module's registry exists to prevent.

    Weights are ``market cap / the sum of the list's market caps``, so they are exact and
    unit-free, and they sum to 100. The absolute market-cap magnitude is **not** asserted to be in
    any currency unit — see this module's docstring for what was measured and why the unit could
    not be confirmed.

    The frame is attributed to the group the page names about itself, read from the taxonomy
    option trendlyne marks ``selected`` (:func:`declared_group`). When no option is marked the
    frame simply carries no ``bfinance_trendlyne_group`` rather than being attributed to a guess.

    The values this reads live in **attributes**, not in cell text, so the table element is used
    directly (:func:`~bfinance.trendlyne.tables.find_table_elements`) rather than through
    ``read_html``, which would discard every one of them.

    Args:
        html: A trendlyne sector or industry page.

    Returns:
        A :class:`ConstituentResult`.

    Raises:
        TrendlyneTableNotFoundError: No table matched the constituent signature.
        TrendlyneSchemaError: A row carried no usable market cap, two rows carried the same
            symbol, or a row's two statements of its own id disagreed. All are reported rather
            than dropped: a missing market cap would silently change the sector total that every
            weight divides by.
    """
    table = find_table_elements(html, CONSTITUENTS_SIG)[0]
    result = _constituents_from_table(table)
    group = declared_group(html)
    if group is None:
        logger.info(
            "no option marked [selected] on the taxonomy select, so the constituent frame does "
            "not name its own sector. Pass the group explicitly if the frame must be attributed."
        )
        return result
    return ConstituentResult(
        frame=result.frame,
        group=group,
        row_count=result.row_count,
        sector_market_cap=result.sector_market_cap,
        escaped_ampersand_rows=result.escaped_ampersand_rows,
    )


def _constituents_from_table(table: Any) -> ConstituentResult:
    """Walk the constituent table element. Split out so the trim-neutrality check can call it."""
    rows: List[Dict[str, Any]] = []
    escaped = 0
    seen: Dict[str, int] = {}
    for position, tr in enumerate(table.find_all("tr")):
        anchor = tr.find("a", class_="stockrow")
        if anchor is None:
            continue  # the header row, or a row the page renders without a link
        href = str(anchor.get("href") or "")
        match = _STOCKROW_RE.search(_decoded(href))
        if match is None:
            raise TrendlyneSchemaError(
                f"constituent row {position} has an href that is not a stock page: {href!r}. "
                f"Expected /equity/{{id}}/{{symbol}}/{{slug}}/ with the symbol tolerant of '&' "
                f"and '-'. Refusing to invent an id."
            )
        if "&" in match.group(2):
            # The symbol segment holds a literal '&'. In the raw markup that character arrived as
            # "&amp;", so this row is only readable because the unescape ran before the
            # percent-decode. Counted here, on the *parsed* value, because BeautifulSoup has
            # already resolved the entity by the time this sees the href -- testing the attribute
            # for "&amp;" would always be false and the count would always read 0, which is how a
            # regression in the decode order would go unnoticed.
            escaped += 1
        stock_id, symbol, slug = int(match.group(1)), match.group(2), match.group(3)
        # The page states the id twice. Disagreement is a markup change, not a value to pick from.
        declared = anchor.get("data-stockpk")
        if declared is not None and str(declared).strip().isdigit() and int(declared) != stock_id:
            raise TrendlyneSchemaError(
                f"constituent {symbol!r} has href id {stock_id} but data-stockpk={declared!r}. "
                f"Two ids for one row is a markup change; nothing here picks one."
            )
        mcap_cell = tr.find("td", attrs={"data-parameter": "mcap_q"})
        if mcap_cell is None:
            raise TrendlyneSchemaError(
                f"constituent {symbol!r} (row {position}) has no td[data-parameter='mcap_q']. "
                f"Every weight on this page divides by the sum of that column, so a row without "
                f"one would quietly change the total rather than fail."
            )
        market_cap = _float_or_none(mcap_cell.get("data-order"))
        if market_cap is None:
            raise TrendlyneSchemaError(
                f"constituent {symbol!r} has a market-cap cell whose data-order is "
                f"{mcap_cell.get('data-order')!r}, which is not a number."
            )
        price_cell = tr.find("td", attrs={"data-parameter": "currentPrice"})
        spark = tr.find(attrs={"data-sparkline": True})
        if symbol in seen:
            raise TrendlyneSchemaError(
                f"constituent {symbol!r} appears twice (rows {seen[symbol]} and {position}). "
                f"A duplicated name would double-count it in the sector total."
            )
        seen[symbol] = position
        rows.append(
            {
                "Symbol": symbol,
                "Company": anchor.get_text(" ", strip=True),
                "Stock ID": stock_id,
                "Market Cap": market_cap,
                "LTP": _float_or_none(price_cell.get("data-order")) if price_cell else None,
                "Page": f"/equity/{stock_id}/{symbol}/{slug}/",
                # Verbatim, uninterpreted. Shape: {"v": [weekly closes],
                # "d": ["DD-Mon-YYYY" labels, the last one a live timestamp]}. The lists are
                # parallel and the last close equals this row's LTP, but the length is NOT a
                # constant: 183 of the 210 recorded rows carry the full 14 weekly points and 27
                # carry fewer (13 down to 2), all of them recently-listed names. Nothing here
                # pads, truncates or interprets it.
                "Sparkline": spark.get("data-sparkline") if spark is not None else None,
            }
        )

    if not rows:
        raise TrendlyneSchemaError(
            "the constituent table matched its header signature but held no rows carrying an "
            "a.stockrow link. A header with no body is a truncated page, not an empty sector."
        )

    total = sum(row["Market Cap"] for row in rows)
    if total <= 0:
        raise TrendlyneSchemaError(
            f"the {len(rows)} constituents sum to a market cap of {total!r}. Weights are that sum "
            f"divided out, so a non-positive total would make every weight meaningless."
        )
    for row in rows:
        row["Weight %"] = row["Market Cap"] / total * 100.0

    frame = pd.DataFrame(
        rows, columns=["Symbol", "Company", "Stock ID", "Market Cap", "Weight %", "LTP",
                       "Page", "Sparkline"]
    )
    return ConstituentResult(
        frame=frame,
        group=None,
        row_count=len(rows),
        sector_market_cap=total,
        escaped_ampersand_rows=escaped,
    )


def parse_industry_aggregate(html: str) -> pd.DataFrame:
    """The per-industry aggregate block that sits beside a sector's constituent list.

    A **separate signature from the constituent table** (``sector_industry_aggregate``), because
    the two are on the same page and share the word "Name" as their first header cell. The
    discriminator is the group each requires and the other lacks: this one needs
    ``No. of Companies`` and ``Industry Score``, which the constituent table does not have.

    Measured on the recorded Autos page: 5 industry rows whose ``No. of Companies`` sum to
    **210**, exactly the constituent table's row count, and industry 9's own page independently
    lists **147** rows — which is that block's own figure for Auto Parts & Equipment. Two
    server-side counts agreeing is what makes the sector table trustworthy as the exact union of
    its industries, and it is asserted in the test suite.

    Args:
        html: A trendlyne **sector** page. An industry page carries no aggregate block, and this
            raises there rather than returning an empty frame.

    Returns:
        One row per industry, classified by signature.

    Raises:
        TrendlyneTableNotFoundError: The page has no aggregate block.
    """
    from .tables import find_table  # local: keeps the module import graph flat at the top

    frame = find_table(html, AGGREGATE_SIG)
    frame.attrs["bfinance_trendlyne_note"] = (
        "One row per industry inside this sector. 'No. of Companies' is the industry's own "
        "constituent count; on the recorded page these sum to the sector's constituent row count, "
        "so the sector list is the exact union of its industries."
    )
    return frame


def constituent_attrs(
    result: ConstituentResult,
    *,
    source_url: Optional[str] = None,
) -> Dict[str, Any]:
    """The ``attrs`` for a constituent frame: provenance, the count, and the cap flag.

    Assembled here rather than through
    :func:`bfinance.trendlyne.client.build_attrs` because this module is imported **by** the
    client, and an import edge back would be a cycle. The keys and their meaning match
    ``build_attrs``'s convention: the ``bfinance_trendlyne_*`` prefix, ``bfinance_provenance``,
    and a note that says what the frame does not contain.

    The two keys that exist to stop a reader being misled are
    ``bfinance_trendlyne_constituent_rows`` and
    ``bfinance_trendlyne_rows_suspiciously_capped``: a server-side truncation is invisible in the
    frame itself, so the count and the flag travel with it.
    """
    attrs: Dict[str, Any] = {
        "bfinance_provenance": PROVENANCE,
        "bfinance_trendlyne_table": CONSTITUENTS_SIG,
        "bfinance_trendlyne_constituent_rows": result.row_count,
        "bfinance_trendlyne_rows_suspiciously_capped": result.suspiciously_capped,
        "bfinance_trendlyne_sector_market_cap": result.sector_market_cap,
        "bfinance_trendlyne_market_cap_unit": "unverified",
        "bfinance_trendlyne_weight_sum_percent": float(result.frame["Weight %"].sum()),
        "bfinance_trendlyne_escaped_ampersand_rows": result.escaped_ampersand_rows,
    }
    if result.group is not None:
        attrs["bfinance_trendlyne_group"] = result.group.key
        attrs["bfinance_trendlyne_group_name"] = result.group.name
    if source_url:
        attrs["bfinance_trendlyne_source"] = source_url
    attrs["bfinance_trendlyne_note"] = (
        f"{result.row_count} constituent rows, weighted by market cap divided by the list's own "
        f"total ({result.sector_market_cap:,.2f}), so the weights are exact and unit-free. The "
        f"market-cap UNIT is not asserted: the column header carries no unit label and dividing "
        f"it by each row's own last-traded price does not reproduce a plausible share count on "
        f"{'this' if not result.suspiciously_capped else 'a capped'} page. Use Weight %, not the "
        f"absolute value. The market-cap total here covers the rows returned, which is not "
        f"necessarily the whole sector if the page was capped."
    )
    return attrs


def load_signatures() -> Tuple[TableSignature, TableSignature]:
    """The two group-page signatures, fetched from the registry.

    Exposed so a caller (and the test suite) can assert that the names this module uses really are
    registered, rather than this module keeping a private copy that could drift.
    """
    missing = [name for name in (CONSTITUENTS_SIG, AGGREGATE_SIG) if name not in SIGNATURES]
    if missing:
        raise TrendlyneSchemaError(
            f"the group-page signatures {missing} are not registered in "
            f"bfinance.trendlyne.tables.SIGNATURES, so find_table_elements would reject them."
        )
    return SIGNATURES[CONSTITUENTS_SIG], SIGNATURES[AGGREGATE_SIG]


def sparkline_points(value: Optional[str]) -> List[float]:
    """The ``v`` list of a ``data-sparkline`` value, or ``[]`` when there is none.

    Split out so the sparkline's shape is checkable in a test without a caller having to know it
    is JSON inside an HTML attribute. The ``d`` date list is deliberately **not** returned: this
    module does not interpret the payload, and a caller that wants dates reads the raw
    ``Sparkline`` column. Returns ``[]`` for a missing or unparseable value rather than
    raising, because on this page a short sparkline is a fact about a young listing, not an
    error.
    """
    if not value:
        return []
    try:
        payload = json.loads(value)
    except (TypeError, ValueError):
        logger.warning("data-sparkline is not valid JSON; left uninterpreted.", exc_info=True)
        return []
    if not isinstance(payload, dict):
        return []
    values = payload.get("v")
    return [float(v) for v in values] if isinstance(values, list) else []
