"""
Server-rendered table extraction for trendlyne.com, classified by **header signature**.

trendlyne renders the tables this package reads into the initial HTML — ``requests``/``httpx``
plus ``bs4`` is enough, no headless browser, no JS execution. Verified 2026-09-27 for the
corporate-actions, share-holding, bulk-deal and sector/industry pages.

Classify by header row, never by the heading above the table
------------------------------------------------------------
This is the one rule in the module that exists because the obvious approach produced garbage.

The corporate-actions page carries a single ``h2`` reading *"Check all corporate actions such as
stock splits, bonuses, dividends, mergers & acquisitions, rights issues & ..."* and one ``h3`` per
real section. Two attempts that located a table by its nearby heading picked the ``h2`` — it
matches all three keywords at once and shadows the real ``h3`` headings — and returned nonsense:
a "bonus ratio" of ``140.000`` and *95 stock splits* for TCS.

Selecting on the header row's own cells has no such failure mode, because the headers are
distinct per table type. Measured on the recorded corporate-actions page (MOTHERSON, 5 tables)::

    ['Ex-Date', 'Bonus Ratio', 'Record Date']                                  -> bonus
    ['Ex-Date', 'Old FV', 'New FV', 'Record Date']                            -> split
    ['Ex-Date', 'Dividend Amount', 'Dividend Type', 'Record Date', ...]       -> dividend
    ['Ex-Date', 'Purpose', 'Notes']                                            -> board_meeting
    ['Ex-Date', 'Ratio', 'Face Value', 'Premium', 'Record Date']              -> rights

Note the last one: a ``Ratio`` column is the *rights* table, not the bonus table. Matching on the
bare word ``"Ratio"`` would misclassify it, which is why the bonus signature requires the exact
cell ``"Bonus Ratio"`` and the rights signature requires ``"Face Value"`` **and** ``"Premium"``.

The fourth row is the ``board_meeting`` signature, which was registered against this page and
therefore also matches the corporate-actions *purpose* table. That collision is real and was
re-measured on 2026-10-10 rather than argued about: on ``/equity/board-meeting/TCS/1372/…`` the
header row is the same three cells (``Ex-date`` | ``Purpose`` | ``Notes``) over 95 forward-dated
rows, so the signature does serve its own route — it simply cannot tell the two pages apart.
:func:`find_tables` reports which outermost table it matched and every frame carries its
``bfinance_trendlyne_table_index``, so a caller holding a page knows which one it got.

Four more traps this module has to absorb
-----------------------------------------
1. **Header depth varies per table on the same page.** The share-holding page has a summary
   grid whose second row is *data* (the ``Promoter`` / ``48.6%`` line), while the pattern tables
   beneath it have a genuine second header row of ``%`` / ``shares`` sub-columns. ``header`` is
   therefore a per-call argument, not a constant: pass ``header=0`` for the first and
   ``header=[0, 1]`` for the others. :func:`find_tables` classifies on the **first** row either
   way, so classification is unaffected.
2. **Layout tables exist.** Navigation and layout tables have no ``<th>`` and often a single
   row. They are not errors — they simply match no signature, so
   :func:`find_tables` without a ``signature`` may return them. Always pass a signature.
3. **Nested tables would be double-counted.** Only outermost ``<table>`` elements are considered
   (an element with a ``table`` ancestor is skipped), so a table used as a cell layout inside
   another table cannot appear twice in a result list.
4. **Some of the vendor's own rows are hidden.** ``pandas.read_html`` defaults to
   ``displayed_only=True`` and honours the CSS ``display`` property, so a row the page renders but
   collapses returns as though it were not there. On the share-holding summary grid that was **8 of
   13 rows** — Promoter's ``Holding`` / ``Pledged`` / ``Locked`` and DII's ``Mutual Funds`` /
   ``Banks`` / ``Insurance`` / ``Others``, each in a ``<tr class="... subgroup"
   style="display:none">``. ``Pledged`` is a genuinely distinct holding figure, so that is data
   loss and not cosmetics. ``displayed_only=False`` is therefore passed for **that one extractor**
   (:func:`share_holding_tables`) and not as a module-wide default: every other page here uses
   ``display:none`` for layout scaffolding that no caller wants.

Never fabricates
----------------
A table that is absent raises :class:`~bfinance.trendlyne.errors.TrendlyneTableNotFoundError`
with the signatures that *were* present. A value that will not parse is left as the string it
was, unless *every* non-null value in the column parses as a number — partial coercion would
silently turn a value into ``NaN``, which is indistinguishable from "the vendor did not report
it". ``read_html`` is called with ``keep_default_na=False`` and ``thousands=None`` for the same
reason: its own type inference would perform that rewrite — ``"N/A"`` to ``NaN``, ``"1,234"`` to
``1234`` — before any rule here could see it.

A frame, or the element?
-----------------------
:func:`find_table` is the answer when a cell's **text** is the value. When the value only exists as
an **attribute**, ``read_html`` has already thrown it away: on a sector page the sortable market cap
is ``<td data-parameter="mcap_q" data-order="14606.745312000">14,606.7</td>``, and the 3-month
sparkline is a ``data-sparkline`` JSON blob on a cell whose text is empty. Promoting either to a
visible column would invent markup trendlyne never rendered, so :func:`find_table_elements` hands
back the ``bs4`` ``Tag`` and the caller reads the attributes itself. It classifies through the same
registry and the same header-row rule as :func:`find_tables`, so the two can never disagree about
which table on a page is which.

When a header signature is the wrong instrument
-----------------------------------------------
Three of the pages recorded on 2026-10-10 cannot be classified by their headers, and none of the
three is fixed by a cleverer signature:

* **Month-named headers rot.** The share-holding holder tables lead with ``Holding Jun 2026`` and
  the mutual-fund grid with ``Sep-2026``; both rewrite themselves every quarter, so a signature
  keyed on either expires with the data. :func:`share_holding_tables` and
  :func:`monthly_mf_holding_table` select on structure instead — a stable token (``No of
  holders`` / ``History``, and the fund column's own ``MF`` label) or the enclosing wrapper — and
  the quarter stays what it is, a *column label*.
* **Two tables, one header, byte for byte.** The buy-sell-zone page publishes the same PE-band
  grid twice, once standalone and once consolidated, with header rows that differ in nothing at
  all; only ``div.tab-pane#standalone`` vs ``div.tab-pane#consolidated`` tells them apart, and their
  numbers differ (current PE 14.9 vs 15.1 on the recorded page). :func:`buy_sell_zone_tables` is
  keyed on the wrapper.
* **The category is not in the table.** The three share-holding holder tables repeat one header row
  verbatim. The section they belong to is a sibling ``div`` with a tab label, so the category is
  read from that label — and when the markup carries no label the category is ``None``, never
  guessed.

These three are the only extractors in the module that look at position instead of at headers, and
they say so in their names' docstrings. They go through the same :func:`_read_table_frame` as the
signature sweep, so ``read_html``'s flags and the all-or-nothing numeric rule cannot drift apart
between the two paths.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from io import StringIO
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union

import pandas as pd
from bs4 import BeautifulSoup, Tag

from .errors import (
    TrendlyneSchemaError,
    TrendlyneTableNotFoundError,
    TrendlyneTokenError,
)


#: Public surface of this module. Without it, ``from bfinance.trendlyne.sectors import *``
#: binds the module's own imports as well as its API -- ``bs4``, ``json``, ``re``, ``pandas``
#: and ``logging`` all land in the caller's namespace. The package __init__ curates a subset
#: of these for ``bfinance.trendlyne`` itself; this list describes the module.
__all__ = [
    "OHLC_TOKEN_RE",
    "PROVENANCE",
    "SIGNATURES",
    "BuySellZoneTable",
    "ShareHoldingTables",
    "TableSignature",
    "buy_sell_zone_tables",
    "classify_table",
    "extract_ohlc_token",
    "find_ohlc_tokens",
    "find_table",
    "find_table_elements",
    "find_tables",
    "known_signatures",
    "monthly_mf_holding_table",
    "normalize_header",
    "page_signatures",
    "register_signature",
    "share_holding_tables",
]
logger = logging.getLogger("bfinance")

#: Provenance tag recorded on every frame this package returns, matching the convention already
#: used by ``bfinance.nse.marketlens`` (``nse_marketlens``) and ``bfinance.nse.bhavcopy``
#: (``nse_bhavcopy``). It is defined here, in the lowest module of the package, so that
#: :mod:`bfinance.trendlyne.client` and every sibling module share one definition of it.
PROVENANCE = "trendlyne"

#: The per-symbol OHLC token is only ever exposed inside a ``data-eodurl`` attribute. Verified
#: 2026-09-27: the same URL appears 6 times on the MOTHERSON page and all 6 capture groups are
#: byte-identical, so the first match is not a coin flip. The token is base64-ish and carries
#: trailing ``=`` padding; it is used in the URL **verbatim** (percent-encoding it was not
#: required, though that path was not re-tested under a correct header set).
OHLC_TOKEN_RE = re.compile(r"/mapp/v1/stock/web/ohlc/\d+/([A-Za-z0-9=]+)/")

_WHITESPACE_RE = re.compile(r"\s+")


def normalize_header(text: Optional[str]) -> str:
    """Fold a header cell to a comparable form: lowercased, whitespace-collapsed, unpunctuated
    at the edges.

    ``"  Bonus   Ratio "`` -> ``"bonus ratio"``, ``"Ex-Date:"`` -> ``"ex-date"``. Applied to both
    sides of every signature comparison so a stray space or a capitalisation change upstream
    does not break classification, while a genuinely renamed column still does.
    """
    if text is None:
        return ""
    return _WHITESPACE_RE.sub(" ", str(text)).strip().strip(":").strip().lower()


@dataclass(frozen=True)
class TableSignature:
    """A header-row fingerprint, and the name a caller matches on.

    Attributes:
        name: What callers pass as ``signature=``.
        required: Groups of aliases. **Every** group must be satisfied, and a group is satisfied
            when *any* of its aliases appears in the header row. Requiring all groups is what
            keeps ``["Ratio", "Face Value", "Premium"]`` out of the bonus bucket.
        description: Human summary, surfaced in the "not found" error message.
    """

    name: str
    required: Tuple[Tuple[str, ...], ...]
    description: str = ""


def _sig(name: str, *groups: Tuple[str, ...], description: str = "") -> TableSignature:
    return TableSignature(
        name=name,
        required=tuple(tuple(normalize_header(a) for a in group) for group in groups),
        description=description,
    )


#: Registry of known signatures, in match-priority order. Only signatures whose header rows have
#: been **observed on a live page** belong here — an unverified guess is worse than a miss,
#: because a miss raises and a wrong match returns confident nonsense. Recorded 2026-09-27 from
#: the fixtures in ``tests/fixtures/trendlyne/``; extend with :func:`register_signature` after
#: checking a real page.
SIGNATURES: Dict[str, TableSignature] = {
    "bonus": _sig(
        "bonus",
        ("Bonus Ratio", "Bonus Ratio (Record Date)"),
        description="stock bonus history (verified header: 'Bonus Ratio')",
    ),
    "split": _sig(
        "split",
        ("Old FV", "Old FV (Rs)", "Old Face Value"),
        ("New FV", "New FV (Rs)", "New Face Value"),
        description="face-value split history (verified headers: 'Old FV' + 'New FV')",
    ),
    "dividend": _sig(
        "dividend",
        ("Dividend Amount", "Dividend Amt", "Dividend Per Share"),
        description="dividend history (verified header: 'Dividend Amount')",
    ),
    "rights": _sig(
        "rights",
        ("Face Value",),
        ("Premium",),
        description="rights issue history (verified headers: 'Face Value' + 'Premium')",
    ),
    "board_meeting": _sig(
        "board_meeting",
        ("Purpose",),
        ("Notes", "Remark"),
        description="board-meeting outcomes (verified headers: 'Purpose' + 'Notes')",
    ),
    "deals": _sig(
        "deals",
        ("Client Name", "Client"),
        ("Deal Type", "Type"),
        ("Quantity", "Qty"),
        description="bulk / block deals (verified headers: 'Client Name' + 'Deal Type' + 'Quantity')",
    ),
    # The two tables of a sector page. Registered separately and on purpose: they sit on the same
    # page, and the known failure mode in this package is selecting a table by its heading or by
    # one loose word. Both were verified 2026-09-27 on
    # /equity/sector/20/automobiles-auto-components/ (2,275,767 bytes, 2 outermost tables).
    #
    # The discriminator is the group each signature requires and the other lacks. The constituent
    # table requires "Market Cap"; the aggregate block does not have it. The aggregate requires
    # "No. of Companies" and "Industry Score"; the constituent table does not have them. Neither
    # signature uses the bare word "Name" -- the aggregate's first header cell is literally
    # "Name", and requiring it would be exactly the loose match this module's docstring warns
    # about.
    "sector_constituents": _sig(
        "sector_constituents",
        ("Stock Name", "Company Name"),
        ("Market Cap", "Market Capitalisation", "Market Capitalization"),
        ("LTP Last Traded Price", "LTP", "Last Traded Price"),
        description="sector/industry constituent list (verified headers: 'Stock Name' + "
                    "'Market Cap' + 'LTP Last Traded Price')",
    ),
    "sector_industry_aggregate": _sig(
        "sector_industry_aggregate",
        ("No. of Companies", "No of Companies", "No. of Companies."),
        ("Industry Score",),
        ("Advances/Declines Ratio", "A/D Ratio", "Advances / Declines Ratio"),
        description="per-industry aggregate block on a sector page (verified headers: "
                    "'No. of Companies' + 'Industry Score' + 'Advances/Declines Ratio')",
    ),
}


def register_signature(
    signature: Union[str, TableSignature],
    *required: Iterable[str],
    description: str = "",
    overwrite: bool = False,
) -> TableSignature:
    """Register a new header signature so sibling modules can classify their own pages.

    Args:
        signature: A :class:`TableSignature`, or a name to register it under.
        *required: One string per required group; pass a tuple to register aliases for a group.
        description: Recorded on the signature and shown in "not found" errors.
        overwrite: Permit replacing an existing name. Off by default so a typo cannot silently
            redefine a verified signature.

    Returns:
        The registered :class:`TableSignature`.
    """
    if isinstance(signature, TableSignature):
        entry = signature
    else:
        groups: List[Tuple[str, ...]] = []
        for group in required:
            groups.append(tuple(group) if isinstance(group, (tuple, list)) else (group,))
        if not groups:
            raise ValueError("a signature needs at least one required header group")
        entry = _sig(signature, *groups, description=description)
    if entry.name in SIGNATURES and not overwrite:
        raise ValueError(
            f"signature {entry.name!r} is already registered; pass overwrite=True to replace it"
        )
    SIGNATURES[entry.name] = entry
    return entry


# Two signatures added 2026-10-10 from the recordings in tests/fixtures/trendlyne/. They go in
# through the public helper rather than by being spliced into the literal above, because that is
# the one door that refuses to clobber a name someone already verified.
#
# research_report: the header row of the broker-research table is
#   ['Summary', 'Date', 'Stock', 'Author', 'LTP', 'Target',
#    'Price at reco (Change since reco%)', 'Upside(%)', 'Type', 'Report', '', 'Discuss']
# -- 12 cells, of which the first, eleventh and twelfth are not data columns. ``normalize_header``
# does NOT strip parentheses (it lowercases, collapses whitespace and trims a trailing colon), so
# the required token is the measured cell verbatim, ``"Upside(%)"``. The second spelling is there
# because ``_header_row`` joins a cell's text with " ", and a vendor that renders the cell as
# ``Upside <span>(%)</span>`` would produce ``"Upside (%)"`` -- the same token, one space away.
register_signature(
    "research_report",
    ("Summary",),
    ("Target",),
    ("Upside(%)", "Upside (%)"),
    description="broker research reports for a stock (measured 2026-10-10 on the 21-row TCS page "
                "/research-reports/stock/1372/TCS/; verified headers: 'Summary' + 'Target' + "
                "'Upside(%)')",
)

# insider_trade: SAST/insider trades carry
#   ['Client Name', 'Client Category', 'Action*', 'Reported To/By Exchange', 'Quantity',
#    'Post Transaction Holding', 'Traded %', 'Avg. Price', 'Value', 'Period',
#    'Regulation (Insider/SAST)', 'Security Type', 'Mode']
# There is no ``Deal Type`` cell, which is the whole reason this is not the ``deals`` signature:
# bulk/block deals require ``Deal Type`` and insider trades must not satisfy it. The asymmetry
# holds in both directions -- a bulk-deal header has no ``Client Category`` and no
# ``Post Transaction Holding`` -- so neither page can be read as the other.
register_signature(
    "insider_trade",
    ("Client Name",),
    ("Client Category",),
    ("Post Transaction Holding",),
    description="insider / SAST trades (measured 2026-10-10 on the 174-row TCS page "
                "/equity/insider-trading-sast/all/TCS/1372/; verified headers: 'Client Name' + "
                "'Client Category' + 'Post Transaction Holding')",
)


def known_signatures() -> Tuple[str, ...]:
    """Names accepted by ``signature=``, in match-priority order."""
    return tuple(SIGNATURES)


def _matches(headers: Sequence[str], signature: TableSignature) -> bool:
    present = {normalize_header(h) for h in headers}
    return all(any(alias in present for alias in group) for group in signature.required)


def classify_table(headers: Sequence[str]) -> Optional[str]:
    """Return the name of the signature this header row matches, or ``None``.

    Args:
        headers: The cells of the header row, in order. Pass the *display* text; it is
            normalised here.

    Returns:
        A key of :data:`SIGNATURES`, or ``None`` when the row matches none of them. ``None`` is
        an ordinary outcome — layout tables produce it — and is not an error.
    """
    for entry in SIGNATURES.values():
        if _matches(headers, entry):
            return entry.name
    return None


def _outermost_tables(soup: BeautifulSoup) -> List[Tag]:
    """Top-level ``<table>`` elements in document order, skipping nested ones (trap 3)."""
    return [t for t in soup.find_all("table") if t.find_parent("table") is None]


def _header_row(table: Tag) -> List[str]:
    """Cells of the table's first row, preferring ``<th>``.

    trendlyne's data tables use ``<th>`` for the header and ``<td>`` for data, but the sub-header
    rows of the wide pattern tables are ``<td>``-only, so the ``<td>`` fallback is required.
    """
    row = table.find("tr")
    if row is None:
        return []
    cells = row.find_all("th") or row.find_all("td")
    return [c.get_text(" ", strip=True) for c in cells]


def _flatten_columns(columns: Iterable[Any]) -> List[str]:
    """Flatten ``read_html``'s column labels to plain strings.

    A two-row header yields tuples (and ``None`` for a level the cell does not span); those
    become ``"Holding Jun 2026 | %"``. Blank levels are dropped rather than rendered as ``" | "``.
    """
    flat: List[str] = []
    for column in columns:
        if isinstance(column, tuple):
            parts = [str(level) for level in column if level is not None and str(level).strip()]
            flat.append(" | ".join(_WHITESPACE_RE.sub(" ", p).strip() for p in parts))
        else:
            flat.append(_WHITESPACE_RE.sub(" ", str(column)).strip())
    return flat


def _coerce_numeric(frame: pd.DataFrame) -> pd.DataFrame:
    """Convert a column to numeric **only if every** non-null value parses.

    Partial coercion is the failure this avoids: ``pd.to_numeric(errors="coerce")`` on a column
    holding one ``"1,234"`` turns that single cell into ``NaN``, which is then indistinguishable
    from "the vendor did not report it". All-or-nothing keeps a parsed column fully numeric and
    an unparsed column fully verbatim.

    The dtype test cannot be ``== object``: pandas 3 infers ``str`` dtype for text columns, so a
    text column would be skipped and never coerced. Anything that is already a number, a bool or
    a datetime is left alone instead.
    """
    for column in frame.columns:
        series = frame[column]
        if pd.api.types.is_numeric_dtype(series) or pd.api.types.is_bool_dtype(series):
            continue
        if pd.api.types.is_datetime64_any_dtype(series):
            continue
        values = series.tolist()
        if not any(isinstance(value, str) and value.strip() for value in values):
            # Already numbers (or nulls): nothing to decide.
            if all(value is None or (isinstance(value, float) and pd.isna(value))
                   for value in values):
                continue
        converted = pd.to_numeric(series, errors="coerce")
        if bool(converted.notna().all()):
            frame[column] = converted
    return frame


def _read_table_frame(
    table: Tag,
    *,
    header: Union[int, List[int], None] = 0,
    coerce_numeric: bool = True,
    flatten_columns: bool = True,
    index: Optional[int] = None,
    matched: Optional[str] = None,
    displayed_only: bool = True,
) -> pd.DataFrame:
    """One ``<table>`` element to a frame, through the rules this module never varies.

    Every reader here -- the signature sweep in :func:`find_tables` and the three structural
    extractors below -- calls this, so the ``read_html`` flags and the all-or-nothing numeric rule
    cannot drift apart between the header-driven path and the position-driven one. A rule kept in
    two places is a rule that will be true in one of them.

    Args:
        table: The ``<table>`` element to read.
        header: ``read_html`` header rows -- ``0``, or ``[0, 1]`` for the two-row grids (trap 1).
        coerce_numeric: Apply the all-or-nothing rule from :func:`_coerce_numeric`.
        flatten_columns: Join multi-level header labels with ``" | "``.
        index: Position among the page's outermost tables, named in the error message.
        matched: The signature that selected this table, named in the error message.
        displayed_only: Whether to honour the CSS ``display`` property and skip the rows the page
            hides (trap 4). ``True`` -- ``read_html``'s own default -- everywhere except the
            share-holding summary grid, whose collapsed sub-rows are real holdings.

    Raises:
        TrendlyneSchemaError: The element did not parse as exactly one table.
    """
    fragment = str(table)
    where = f"table at index {index}" if index is not None else "table"
    try:
        # These three flags are what keep pandas from rewriting the vendor's data before any
        # rule here can see it. Measured on a two-row probe table:
        #   defaults              -> "N/A" becomes NaN, "1,234" becomes 1234
        #   keep_default_na=False -> "N/A" survives as "N/A"
        #   thousands=None        -> "1,234" survives as "1,234"
        # NaN in a cell that trendlyne actually rendered is indistinguishable from "the vendor
        # did not report it", and a silently stripped thousands separator looks like a
        # parsing bug months later. Numeric typing is then this module's decision, made
        # per column and all-or-nothing by :func:`_coerce_numeric`.
        parsed = pd.read_html(
            StringIO(fragment),
            flavor="lxml",
            header=header,
            keep_default_na=False,
            na_values=[],
            thousands=None,
            displayed_only=displayed_only,
        )
    except ValueError as exc:  # read_html raises ValueError on a non-table fragment
        raise TrendlyneSchemaError(
            f"{where} (signature={matched!r}) could not be parsed as a table; upstream markup "
            f"may have changed. First 160 chars: {fragment[:160]!r}"
        ) from exc
    if len(parsed) != 1:
        raise TrendlyneSchemaError(
            f"{where} (signature={matched!r}) parsed into {len(parsed)} frames, expected 1; "
            f"upstream markup may have changed."
        )

    frame = parsed[0]
    if flatten_columns:
        frame.columns = _flatten_columns(frame.columns)
    frame = frame.dropna(axis=0, how="all").reset_index(drop=True)
    if coerce_numeric:
        frame = _coerce_numeric(frame)
    return frame


def _stamp(
    frame: pd.DataFrame, table: Tag, index: int, matched: Optional[str]
) -> pd.DataFrame:
    """Write the four provenance attrs every frame in this module carries.

    ``bfinance_trendlyne_table`` is the signature that selected the table, or the structural name a
    positional extractor gave it; ``bfinance_trendlyne_table_index`` is its position among the
    page's outermost tables, so a caller holding a frame can cross-reference the live page even
    when the selector was position and not headers.
    """
    frame.attrs["bfinance_provenance"] = PROVENANCE
    frame.attrs["bfinance_trendlyne_table"] = matched
    frame.attrs["bfinance_trendlyne_table_index"] = index
    frame.attrs["bfinance_trendlyne_table_header"] = _header_row(table)
    return frame


def _table_index(soup: BeautifulSoup, table: Tag) -> int:
    """Where ``table`` sits among the page's outermost tables, or ``-1`` if it is not one."""
    for index, candidate in enumerate(_outermost_tables(soup)):
        if candidate is table:
            return index
    return -1


def find_tables(
    html: str,
    signature: Optional[str] = None,
    *,
    header: Union[int, List[int], None] = 0,
    limit: Optional[int] = None,
    coerce_numeric: bool = True,
    symbol: Optional[str] = None,
    flatten_columns: bool = True,
) -> List[pd.DataFrame]:
    """Extract every table whose header row matches ``signature``.

    Args:
        html: A trendlyne page (or a verbatim excerpt of one, as in the test fixtures).
        signature: A key of :data:`SIGNATURES`, or ``None`` to return every outermost table.
            Passing ``None`` is a debugging aid: it also returns layout tables.
        header: ``read_html`` header rows. ``0`` (default) for single-row headers;
            ``[0, 1]`` for the two-row pattern tables (trap 1). Classification always reads the
            first row, so this never affects which tables are selected.
        limit: Stop after this many matches. ``None`` means no limit.
        coerce_numeric: Apply the all-or-nothing numeric rule from :func:`_coerce_numeric`.
        symbol: When given, recorded as ``attrs["bfinance_symbol"]``.
        flatten_columns: Join multi-level header labels with ``" | "``.

    Returns:
        A list of frames in document order, each carrying
        ``attrs["bfinance_provenance"] = "trendlyne"``, ``attrs["bfinance_trendlyne_table"]``
        (the matched signature name or ``None``) and ``attrs["bfinance_trendlyne_table_index"]``
        (its position among the outermost tables of the page, so a caller can cross-reference
        the live page).

    Raises:
        ValueError: ``signature`` is not a registered name.
        TrendlyneSchemaError: A matched table could not be converted to a frame — a layout
            artefact upstream that ``read_html`` rejects, or a fragment that is not a table.
    """
    if signature is not None and signature not in SIGNATURES:
        raise ValueError(
            f"unknown signature={signature!r}. Registered: {', '.join(known_signatures())}. "
            f"Register a new one with tables.register_signature() after checking the live page."
        )

    soup = BeautifulSoup(html, "lxml")
    frames: List[pd.DataFrame] = []
    for index, table in enumerate(_outermost_tables(soup)):
        headers = _header_row(table)
        matched = classify_table(headers)
        if signature is not None and matched != signature:
            continue

        frame = _read_table_frame(
            table,
            header=header,
            coerce_numeric=coerce_numeric,
            flatten_columns=flatten_columns,
            index=index,
            matched=matched,
        )
        _stamp(frame, table, index, matched)
        if symbol is not None:
            frame.attrs["bfinance_symbol"] = symbol
        frames.append(frame)
        if limit is not None and len(frames) >= limit:
            break

    if not frames and signature is not None:
        raise TrendlyneTableNotFoundError(
            f"no table matched signature={signature!r}. Signature(s) present on this page: "
            f"{_page_signature_report(soup)}. "
            f"Classification is by header row, not by heading text, so a heading change alone "
            f"cannot cause this — it means the headers changed."
        )
    return frames


def find_table_elements(html: str, signature: str) -> List[Tag]:
    """The outermost ``<table>`` **elements** whose header row matches ``signature``, in document
    order.

    For a caller that needs the markup rather than a frame. The reason it exists: ``read_html``
    keeps a cell's *text* and drops its *attributes*, so a value that only exists as
    ``data-parameter="mcap_q"``, ``data-order="14606.74"`` or ``data-sparkline='{"v": [...]}'`` is
    unreachable from :func:`find_tables`. Rewriting those cells as visible text would fabricate a
    column trendlyne never rendered, so the parser keeps the element and reads the attributes
    itself.

    Classification is identical to :func:`find_tables` — same registry, same first-header-row
    rule, same outermost-only sweep — so the two can never disagree about which table is which.

    Args:
        html: A trendlyne page, or a verbatim excerpt of one.
        signature: A key of :data:`SIGNATURES`.

    Returns:
        The matching ``bs4`` ``Tag`` objects. Empty only if nothing matched, which raises.

    Raises:
        ValueError: ``signature`` is not a registered name.
        TrendlyneTableNotFoundError: Nothing on the page matched.
    """
    if signature not in SIGNATURES:
        raise ValueError(
            f"unknown signature={signature!r}. Registered: {', '.join(known_signatures())}. "
            f"Register a new one with tables.register_signature() after checking the live page."
        )
    soup = BeautifulSoup(html, "lxml")
    found = [
        table
        for table in _outermost_tables(soup)
        if classify_table(_header_row(table)) == signature
    ]
    if not found:
        raise TrendlyneTableNotFoundError(
            f"no table matched signature={signature!r}. Signature(s) present on this page: "
            f"{_page_signature_report(soup)}. "
            f"Classification is by header row, not by heading text, so a heading change alone "
            f"cannot cause this — it means the headers changed."
        )
    return found


def _page_signature_report(soup: BeautifulSoup) -> str:
    """One line naming every outermost table's signature, for a "not found" message."""
    found = []
    for table in _outermost_tables(soup):
        matched = classify_table(_header_row(table))
        found.append(matched or f"<no signature: {_header_row(table)[:4]}>")
    return ", ".join(found) if found else "<no tables at all>"


def find_table(
    html: str,
    signature: Optional[str] = None,
    *,
    index: int = 0,
    header: Union[int, List[int], None] = 0,
    coerce_numeric: bool = True,
    symbol: Optional[str] = None,
    flatten_columns: bool = True,
) -> pd.DataFrame:
    """The ``index``-th table matching ``signature`` (the first one by default).

    A convenience wrapper over :func:`find_tables` for the common case of one table per page.
    When more than one table matches, a warning naming the count is logged, because picking the
    first of several is a decision the caller should know was made; pass ``index`` to choose.

    Raises:
        TrendlyneTableNotFoundError: Nothing matched, or ``index`` is past the end.
    """
    frames = find_tables(
        html,
        signature,
        header=header,
        coerce_numeric=coerce_numeric,
        symbol=symbol,
        flatten_columns=flatten_columns,
    )
    if len(frames) > 1:
        logger.warning(
            "%d tables matched signature=%r; returning index %d of them. Pass index= to choose.",
            len(frames), signature, index,
        )
    if index >= len(frames):
        raise TrendlyneTableNotFoundError(
            f"index={index} but only {len(frames)} table(s) matched signature={signature!r}."
        )
    return frames[index]


def page_signatures(html: str) -> Dict[int, Optional[str]]:
    """Map each outermost table's position to the signature it matches (``None`` if none).

    The first thing to call when a sibling module gets :class:`TrendlyneTableNotFoundError`: it
    shows what the page actually contains, and needs no request and no guessing.
    """
    soup = BeautifulSoup(html, "lxml")
    return {i: classify_table(_header_row(t)) for i, t in enumerate(_outermost_tables(soup))}


# ------------------------------------------------- structural (position-driven) section readers
#
# Everything below selects a table by where it sits or by a token that does not rotate, because the
# header row cannot carry the distinction. See the module docstring for the three measurements that
# force this. Each of these reads through _read_table_frame, so the read_html flags and the
# all-or-nothing numeric rule are the same objects the signature sweep uses.

#: The summary grid on the share-holding page is the one table the vendor gives an id, and the id is
#: a label rather than a quarter -- unlike every other header on that page, it does not rotate.
_SHARE_SUMMARY_TABLE_ID = "shareSummaryTable"

#: The two marks the summary grid puts on a collapsed sub-row. Both are read and either one is
#: enough, because the recorded DII block carries one ``<tr class="... subgroup">`` with **no cells
#: at all** -- a rule keyed on the ``rt-tr-group`` cell alone would miss nothing today and would be
#: one markup change away from missing a row.
_SUMMARY_SUBGROUP_CLASS = "subgroup"
_SUMMARY_GROUP_CELL_CLASS = "rt-tr-group"

#: Name of the column that says which rows of the summary grid are top-level holder categories and
#: which are their collapsed sub-rows, and the two values it takes. A column rather than an attr, so
#: the distinction survives a ``loc`` / ``query`` a caller makes -- and because the index cannot
#: carry it honestly: ``Others`` is both a DII sub-row and a top-level category on these pages, so
#: ``loc["Others"]`` returns two rows and the marker is what tells them apart.
_SUMMARY_ROW_TYPE_COLUMN = "Row Type"
_SUMMARY_ROW_CATEGORY = "category"
_SUMMARY_ROW_SUBGROUP = "subgroup"

#: Index name of the summary frame: the vendor's own header cell over the label column, so
#: ``reset_index()`` reproduces the column the page rendered rather than one this module named.
_SUMMARY_INDEX_NAME = "Summary"

#: The two header cells that identify a per-category holder table and nothing else. Measured on the
#: TCS page, they appear in all three holder tables and in none of the seven tables around them:
#: the three per-category *summary* grids carry ``History`` but no ``No of holders``, which is
#: exactly what keeps them out.
_HOLDER_HEADER_TOKENS = ("no of holders", "history")

#: First-cell label of the mutual-fund grid's fund column. The month columns around it (``Sep-2026``,
#: ``Aug-2026``, ``Jul-2026``) rewrite themselves every month and are never a selector.
_MF_FIRST_COLUMN_LABEL = "MF"

#: The two panes of the buy-sell-zone page, and the only thing that tells their byte-identical
#: tables apart. Order is the order they are returned in.
_BUY_SELL_BASES = ("standalone", "consolidated")

#: Rendered statistic -> the ``attrs`` key it is published under, with the label spellings measured
#: on the page. An entry is ``None`` when that pane carries no such tile: a stat the page does not
#: publish is absent, not zero. The aliases are written already normalised because the tile keys
#: they are matched against come out of :func:`normalize_header` -- same convention as ``_sig``.
_BUY_SELL_TILE_STATS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("bfinance_trendlyne_strong_buy_zone_pct", ("strong buy zone",)),
    (
        "bfinance_trendlyne_days_below_current_pe_pct",
        ("% days traded below current pe", "days traded below current pe", "days below current pe"),
    ),
    ("bfinance_trendlyne_current_pe", ("current pe",)),
    ("bfinance_trendlyne_average_pe", ("average pe",)),
    ("bfinance_trendlyne_median_pe", ("median pe",)),
)


def _is_holder_table(headers: Sequence[str]) -> bool:
    """Whether a header row belongs to a per-category holder table rather than a summary grid."""
    present = {normalize_header(h) for h in headers}
    return all(token in present for token in _HOLDER_HEADER_TOKENS)


def _collapsed(text: Optional[str]) -> str:
    """Whitespace-collapsed text of a node, which is the form every label here is compared in."""
    return _WHITESPACE_RE.sub(" ", str(text or "")).strip()


def _cell_text(value: Any) -> str:
    """:func:`_collapsed` for a value ``read_html`` produced, where a null cell is an empty one.

    ``_collapsed`` renders whatever it is handed, and ``str(float("nan"))`` is ``"nan"`` -- a label
    nobody wrote. That matters here because ``_read_table_frame``'s ``keep_default_na=False`` means a
    blank cell arrives as ``""`` rather than as ``NaN``, so an all-blank row is not null and its
    ``dropna(how="all")`` never fires on it.
    """
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return _collapsed(value)


def _section_category(table: Tag, soup: BeautifulSoup) -> Optional[str]:
    """The section a holder table sits in, read off the markup, or ``None`` when it has no name.

    The three holder tables of a share-holding page carry a **byte-identical** header row, so
    nothing inside a table can say which category it is. What says so is the wrapper the table is
    inside: on the recorded TCS page each is a ``div.tab-pane`` whose own label is an anchor in the
    tab strip (``<a href="#promoter-holding" data-toggle="tab">Promoter Holding</a>``), and the
    measured names are ``Promoter Holding`` / ``Institutional Holding`` /
    ``Public (Non-Institutional) Holding``.

    Two sources are read, in order: that tab label, and a heading inside the wrapper -- but only
    inside a wrapper the vendor gave an ``id`` to, because a heading floating in a page-level
    container would name every section on the page the same thing. If neither exists the answer is
    ``None``. The 2026-09-27 MOTHERSON fixture is exactly that case -- it was trimmed to the
    ``<table>`` blocks, so the wrappers are gone and nothing in the file names the category -- and
    the id is deliberately **not** used as a fallback: ``promoter-holding`` is a slug the site
    generated, and returning it would be a fabricated category wearing markup's clothes.
    """
    for parent in table.parents:
        if not isinstance(parent, Tag) or parent.name in ("body", "html", "[document]"):
            continue
        section_id = parent.get("id")
        if not section_id:
            continue
        link = soup.find("a", href=f"#{section_id}")
        if link is not None and link.get("data-toggle") == "tab":
            label = _collapsed(link.get_text(" ", strip=True))
            if label:
                return label
        heading = parent.find(["h1", "h2", "h3", "h4", "h5", "h6"])
        if heading is not None:
            label = _collapsed(heading.get_text(" ", strip=True))
            if label:
                return label
    return None


def _summary_row_type(row: Tag) -> str:
    """``"category"`` or ``"subgroup"`` for one ``<tr>`` of the share-holding summary grid.

    Read off the markup and never inferred from the row above it. The vendor marks a collapsed
    sub-row with a ``subgroup`` class on the ``<tr>`` and an ``rt-tr-group`` class on its label
    cell; that mark *is* the claim, and positional inference would call whatever follows ``Public``
    a sub-row of it -- which on a page where a category expands to nothing would be an invention.
    """
    if _SUMMARY_SUBGROUP_CLASS in (row.get("class") or []):
        return _SUMMARY_ROW_SUBGROUP
    if row.find("td", class_=_SUMMARY_GROUP_CELL_CLASS) is not None:
        return _SUMMARY_ROW_SUBGROUP
    return _SUMMARY_ROW_CATEGORY


def _summary_rows(table: Tag) -> List[Tuple[str, str]]:
    """``(label, row type)`` for every data row the summary grid renders, in page order.

    A ``<tr>`` no cell of which carries any text is **dropped**, not labelled. Both recorded pages
    carry exactly one of these -- an empty ``<tr>`` with no cells at all, where the DII accordion
    opens -- and it is a template placeholder rather than a holding: no name, no figure, and the
    four real DII sub-rows around it (``Mutual Funds``, ``Banks``, ``Insurance``, ``Others``) are
    unaffected by its absence. Naming it after the category above it would put a label in the index
    that trendlyne never wrote, and this module does not write vendor labels.

    The label is the row's first cell, whitespace-collapsed: ``"Mutual Funds"`` out of a
    ``td.rt-tr-group``, ``"Promoter"`` out of the anchor inside a ``td.trigger-tab``. Nothing here
    normalises, abbreviates or infers it beyond collapsing whitespace.
    """
    body = table.find("tbody") or table
    rows: List[Tuple[str, str]] = []
    for row in body.find_all("tr", recursive=False):
        cells = row.find_all("td", recursive=False)
        if not any(_collapsed(cell.get_text(" ", strip=True)) for cell in cells):
            continue
        label = _cell_text(cells[0].get_text(" ", strip=True)) if cells else ""
        rows.append((label, _summary_row_type(row)))
    return rows


def _index_summary(
    frame: pd.DataFrame,
    index: Optional[int],
    rows: Sequence[Tuple[str, str]],
) -> pd.DataFrame:
    """The summary grid's label column into the index, plus a column marking each row's level.

    Two things happen, and the second is the one that makes the first safe.

    The all-blank row is dropped here rather than left to ``_read_table_frame``'s
    ``dropna(how="all")``, because that rule cannot fire on this table: ``keep_default_na=False``
    turns a blank cell into ``""`` and not into ``NaN``, so an all-blank row is not null and
    survives. :func:`_summary_rows` skips the same row, so both sides drop it and the two lists
    stay the same length -- which is what makes the check below a real check.

    The check is the point. The labels come from the markup and the figures come from pandas, so
    if the two ever disagree about which row is which, no frame is returned at all. A summary whose
    index reads ``Pledged`` over a row of Promoter's percentages is worse than an error, and it is
    exactly the shape a silently mis-aligned frame would have.

    Args:
        frame: The summary grid as :func:`_read_table_frame` returned it, label column first.
        index: Its position among the page's outermost tables, for the error message.
        rows: ``_summary_rows`` for the same table.

    Raises:
        TrendlyneSchemaError: The markup rows and the parsed rows do not correspond.
    """
    keep = [
        any(_cell_text(cell) for cell in row)
        for row in frame.itertuples(index=False, name=None)
    ]
    if not all(keep):
        frame = frame.loc[keep].reset_index(drop=True)
    label_column = frame.columns[0]
    labels = [label for label, _ in rows]
    if len(rows) != len(frame) or [_cell_text(v) for v in frame[label_column]] != labels:
        where = f"table at index {index}" if index is not None else "table"
        raise TrendlyneSchemaError(
            f"{where} (id={_SHARE_SUMMARY_TABLE_ID!r}) parsed into {len(frame)} row(s) but the "
            f"markup carries {len(rows)} labelled one(s), so the labels cannot be attached to the "
            f"right rows; upstream markup may have changed. Refusing to index a grid whose rows "
            f"might be mislabelled."
        )
    # The label column becomes the index and is dropped: the two are the same strings, and a
    # frame carrying both is a second copy that a caller could read one of.
    frame.index = pd.Index(labels, name=_SUMMARY_INDEX_NAME)
    frame = frame.drop(columns=[label_column])
    frame.insert(0, _SUMMARY_ROW_TYPE_COLUMN, [kind for _, kind in rows])
    return frame


@dataclass(frozen=True)
class ShareHoldingTables:
    """The share-holding page's summary grid and its per-category holder tables.

    Attributes:
        summary: The ``shareSummaryTable`` grid -- holder categories down (``Promoter``, ``FII``,
            ``DII``, ``Public``, ``Others``) and their collapsed sub-rows (``Holding``,
            ``Pledged``, ``Locked``, ``Mutual Funds``, ``Banks``, ``Insurance``, ``Others``),
            quarters across. Read with ``header=0`` because its second row is *data* (trap 1); the
            column labels are quarter names and rotate. **Every** row is here, collapsed ones
            included (trap 4): the frame is indexed by the vendor's own row label and its first
            column, ``"Row Type"``, is ``"category"`` or ``"subgroup"``. On the recorded TCS page
            that is 12 rows over 12 quarter columns.
        holders: The per-category holder tables in page order, one per section, each read with
            ``header=[0, 1]`` because the ``%`` / ``shares`` sub-header row beneath the real one is
            a header and not a holder named ``%``.
        categories: The section names, same length and same order as ``holders``. An entry is
            ``None`` for a section the markup does not name -- see :func:`_section_category`.
            Positionally aligned with ``holders`` on purpose: a parallel tuple of *frame indexes*
            would be a second way to be wrong.
    """

    summary: pd.DataFrame
    holders: Tuple[pd.DataFrame, ...]
    categories: Tuple[Optional[str], ...]


def share_holding_tables(html: str) -> ShareHoldingTables:
    """The share-holding page's summary grid plus its per-category holder tables.

    Structural by measurement, not by preference. The holder tables lead with ``Holding Jun 2026``
    -- a live month name that rewrites itself every quarter -- so a signature keyed on it would
    expire with the data. The discriminator used here is the pair of header cells that do not
    rotate (``No of holders`` and ``History``), and the category comes from the section wrapper the
    table sits in, because the three tables repeat one header row verbatim.

    The summary grid is the one extractor here that reads with ``displayed_only=False`` (trap 4).
    The page renders its sub-category rows inside ``<tr class="... subgroup"
    style="display:none">`` accordions and ``read_html``'s default honours that CSS, which was
    **dropping 8 of the grid's 13 rows** -- including ``Pledged``, a holding figure distinct from
    the promoter's own. The flag is passed here and nowhere else: every other table in this module
    is either a data grid or layout scaffolding, and turning it on module-wide would pull hidden
    layout tables into every other page.

    Args:
        html: A trendlyne share-holding page, or a verbatim excerpt of one.

    Returns:
        A :class:`ShareHoldingTables`. ``summary`` is indexed by the vendor's own row label --
        ``"Promoter"``, ``"Pledged"``, ``"Mutual Funds"``, ... -- and its first column,
        ``"Row Type"``, reads ``"category"`` or ``"subgroup"``. The index is **not** unique and is
        not made to be: ``"Others"`` is both a DII sub-row and a top-level category on these pages,
        and inventing ``"DII / Others"`` to disambiguate would put a string in the frame that
        trendlyne never wrote. Filter on ``"Row Type"`` instead. Every frame carries
        ``attrs["bfinance_provenance"] = "trendlyne"``, ``attrs["bfinance_trendlyne_table"]`` and
        the holder tables additionally
        ``attrs["bfinance_trendlyne_holder_category"]`` -- the same value as the matching entry of
        ``categories``, published on the frame so a caller holding one frame still knows what it is.

    Raises:
        TrendlyneTableNotFoundError: The page carries no ``shareSummaryTable``, or no table with the
            ``No of holders`` / ``History`` header family. There is no empty-result answer here for
            the same reason there is none in :func:`find_tables`: an empty tuple standing in for a
            restructured page is indistinguishable from a page with no holders.
        TrendlyneSchemaError: The summary grid's markup rows and parsed rows do not correspond, so
            the labels cannot be attached to the rows they belong to.
    """
    soup = BeautifulSoup(html, "lxml")
    outermost = _outermost_tables(soup)

    summary_tag = next((t for t in outermost if t.get("id") == _SHARE_SUMMARY_TABLE_ID), None)
    if summary_tag is None:
        raise TrendlyneTableNotFoundError(
            f"no table with id={_SHARE_SUMMARY_TABLE_ID!r} on this page. It is the one table "
            f"trendlyne gives an id, and it is what names the summary grid; its absence means the "
            f"page is not a share-holding page or its markup changed. "
            f"{len(outermost)} outermost table(s) were present, with signature(s): "
            f"{_page_signature_report(soup)}."
        )

    holder_tags = [t for t in outermost if _is_holder_table(_header_row(t))]
    if not holder_tags:
        raise TrendlyneTableNotFoundError(
            f"no table on this page carries the {' + '.join(_HOLDER_HEADER_TOKENS)} header "
            f"family, so the per-category holder tables are absent. Those two cells are the "
            f"discriminator because the month-named 'Holding <Month Year>' column beside them "
            f"rotates every quarter. {_page_signature_report(soup)}."
        )

    summary_index = _table_index(soup, summary_tag)
    summary = _read_table_frame(
        summary_tag, header=0, index=summary_index, displayed_only=False
    )
    summary = _index_summary(summary, summary_index, _summary_rows(summary_tag))
    _stamp(summary, summary_tag, summary_index, "shareholding_summary")

    holders: List[pd.DataFrame] = []
    categories: List[Optional[str]] = []
    for tag in holder_tags:
        index = _table_index(soup, tag)
        frame = _read_table_frame(tag, header=[0, 1], index=index)
        category = _section_category(tag, soup)
        _stamp(frame, tag, index, "shareholding_holder")
        frame.attrs["bfinance_trendlyne_holder_category"] = category
        holders.append(frame)
        categories.append(category)

    return ShareHoldingTables(
        summary=summary, holders=tuple(holders), categories=tuple(categories)
    )


def _tab_pane(soup: BeautifulSoup, pane_id: str) -> Tag:
    """The ``div.tab-pane#<pane_id>``, or raise naming what the page actually offered."""
    element = soup.find(id=pane_id)
    if element is not None and "tab-pane" in (element.get("class") or []):
        return element
    panes = [
        pane.get("id")
        for pane in soup.find_all(class_="tab-pane")
        if pane.get("id")
    ]
    raise TrendlyneTableNotFoundError(
        f"no div.tab-pane#{pane_id} on this page. The buy-sell-zone tables have byte-identical "
        f"header rows, so the pane is the only thing that distinguishes them; tab-pane id(s) "
        f"present: {panes or '<none>'}."
    )


def _tile_value(node: Tag) -> str:
    """The rendered number inside a tile's ``div.value``, without the label sharing that container.

    The Strong Buy Zone tile renders ``1.8%`` and then names itself in a ``p.tile-msg`` *inside*
    ``div.value``, so the container's own text is ``"1.8% Strong Buy Zone"``. The label is walked
    past rather than parsed back out of the number -- taking the first token of a string that mixes
    the two would be a guess about which half is which.
    """
    parts: List[str] = []
    for text in node.find_all(string=True):
        ancestor = text.parent
        while ancestor is not None and ancestor is not node:
            classes = ancestor.get("class") if hasattr(ancestor, "get") else None
            if classes and "tile-msg" in classes:
                break
            ancestor = ancestor.parent
        else:
            parts.append(str(text))
    return _collapsed(" ".join(parts))


def _tile_stats(pane: Tag) -> Dict[str, str]:
    """Every ``div.stock-indicator-tile`` in one pane, as ``normalised label -> rendered text``.

    A tile's own label is its ``p.tile-msg`` when it has one and its ``p.tile-data`` otherwise.
    Measured 2026-10-10, the Strong Buy Zone tile reads ``1.8%`` in ``div.value``, names itself
    ``Strong Buy Zone`` in ``p.tile-msg`` and *describes* itself ``% Days traded below current PE``
    in ``p.tile-data``; the other six tiles name themselves in ``p.tile-data`` alone. The
    description is the vendor explaining one number, not a second name for it, so only the label
    keys this mapping -- which is why ``days_below_current_pe_pct`` is ``None`` on the recorded
    page rather than a second copy of the strong-buy-zone figure.

    The values are returned exactly as rendered, ``"1.8%"`` and not ``1.8``; the numeric attrs are
    derived from them in :func:`buy_sell_zone_tables` and published alongside, so nothing the
    vendor wrote is lost to a unit conversion.
    """
    stats: Dict[str, str] = {}
    for tile in pane.select("div.stock-indicator-tile"):
        label_node = tile.select_one("p.tile-msg")
        if label_node is None:
            label_node = tile.select_one("p.tile-data")
        value_node = tile.select_one("div.value")
        if label_node is None or value_node is None:
            continue
        label = normalize_header(_collapsed(label_node.get_text(" ", strip=True)))
        if label:
            stats[label] = _tile_value(value_node)
    return stats


def _stat_number(rendered: Optional[str], attr_key: str, basis: str) -> Optional[float]:
    """A tile's rendered figure as a number, or ``None`` when it is not one.

    ``"1.8%"`` is ``1.8`` *percent*, which is what ``_pct`` in the attr name says; ``"1,234"`` is
    1234. A rendered value that is neither -- ``"--"``, ``""`` -- leaves the attr ``None`` rather
    than becoming ``0.0``, per the rule that a number from trendlyne must be traceable to something
    the page actually showed.
    """
    if rendered is None:
        return None
    cleaned = rendered.replace(",", "").replace("%", "").strip()
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        logger.warning(
            "buy-sell-zone tile %r on the %s basis rendered %r, which is not a number; leaving "
            "the attr None rather than guessing at it.",
            attr_key, basis, rendered,
        )
        return None


@dataclass(frozen=True)
class BuySellZoneTable:
    """One basis of the buy-sell-zone PE-band grid, with the statistics published beside it.

    Attributes:
        frame: The PE-band table of this basis: one row per band, a ``Total`` row at the bottom,
            and a ``Current PE is <n>`` marker in the first band row.
        basis: ``"standalone"`` or ``"consolidated"`` -- the ``div.tab-pane`` id it was read from,
            and the key this value is stored under in :func:`buy_sell_zone_tables`.
        attrs: The frame's own ``attrs`` dict, **the same object**, not a copy. Published as a field
            so a caller holding the result reads the statistics without reaching through the frame;
            two dicts that could disagree would be one more thing to keep in sync.
    """

    frame: pd.DataFrame
    basis: str
    attrs: Dict[str, Any]


def buy_sell_zone_tables(html: str) -> Dict[str, BuySellZoneTable]:
    """The buy-sell-zone page's two PE-band grids, keyed by their ``div.tab-pane`` id.

    Positional because it has to be. Measured 2026-10-10, the standalone and consolidated tables
    carry header rows that are **byte-identical**::

        PE range | Days traded in range | % Days traded in range
                | Days traded within & below range | % Days traded within & below range

    and 10 data rows each, with the same PE-band *values* in different geometry. A header signature
    matches both or neither, so the discriminator is ``div.tab-pane#standalone`` /
    ``div.tab-pane#consolidated``. The frames are separate objects and their numbers differ (current
    PE 14.9 standalone vs 15.1 consolidated on the recorded page), which is what a swapped mapping
    would destroy.

    Each pane also carries the summary statistics, in ``div.stock-indicator-tile``: the value in
    ``div.value``, its own label in ``p.tile-msg`` or ``p.tile-data``. They are published as
    ``bfinance_trendlyne_strong_buy_zone_pct``, ``bfinance_trendlyne_days_below_current_pe_pct``,
    ``bfinance_trendlyne_current_pe``, ``bfinance_trendlyne_average_pe`` and
    ``bfinance_trendlyne_median_pe``, plus
    ``bfinance_trendlyne_buy_sell_tiles`` -- the **raw** rendered text of every tile on that pane,
    so the forward trio and any tile added later are readable without a second parse.

    An attr is ``None`` when its tile is absent from the page. On the recorded page that is the
    measured answer for ``days_below_current_pe_pct``: the phrase ``% Days traded below current PE``
    is the *description* under the Strong Buy Zone tile, not a tile of its own, and publishing
    1.8 under both names would be a claim about the vendor's semantics, not a reading of the page.

    Args:
        html: A trendlyne buy-sell-zone page, or a verbatim excerpt of one.

    Returns:
        Exactly the keys ``"standalone"`` and ``"consolidated"``, in that order.

    Raises:
        TrendlyneTableNotFoundError: Either pane is missing, or a pane carries no table. Both keys
            are always present when this returns -- a dict with one entry would be a caller having
            to distinguish "absent" from "not looked for".
    """
    soup = BeautifulSoup(html, "lxml")
    results: Dict[str, BuySellZoneTable] = {}

    for basis in _BUY_SELL_BASES:
        pane = _tab_pane(soup, basis)
        table = pane.find("table")
        if table is None:
            raise TrendlyneTableNotFoundError(
                f"div.tab-pane#{basis} carries no <table>; the buy-sell-zone grid for that basis "
                f"is absent, and the sibling pane's table must not stand in for it."
            )
        index = _table_index(soup, table)
        frame = _read_table_frame(table, header=0, index=index)
        _stamp(frame, table, index, "buy_sell_zone")
        attrs = frame.attrs
        attrs["bfinance_trendlyne_buy_sell_basis"] = basis
        stats = _tile_stats(pane)
        attrs["bfinance_trendlyne_buy_sell_tiles"] = stats
        for attr_key, aliases in _BUY_SELL_TILE_STATS:
            rendered = next((stats[alias] for alias in aliases if alias in stats), None)
            attrs[attr_key] = _stat_number(rendered, attr_key, basis)
        results[basis] = BuySellZoneTable(frame=frame, basis=basis, attrs=attrs)

    return results


def monthly_mf_holding_table(html: str) -> pd.DataFrame:
    """The monthly mutual-fund holding grid: one row per fund, three ragged month columns.

    Structural for the same reason as the rest of this section. The month's own columns
    (``Sep-2026``, ``Aug-2026``, ``Jul-2026``) change every month, and the sub-header row beneath
    them is ragged against the data row -- 9 cells above 10, because the newest month carries five
    sub-columns (``AUM (Cr)``, ``AUM %``, ``Shares Held``, ``Month Change``, ``Month Change %``) and
    the two older ones carry two each. So it is read with ``header=[0, 1]``, which is the only
    setting that keeps the band of five, and the raggedness in the *values* -- a ``-`` for a month
    in which the fund held nothing -- is why no month column may be coerced to numeric: the
    all-or-nothing rule keeps every one of them verbatim.

    Selection is the fund column's own label, ``MF``. That is a label the page publishes, not a
    quarter, so it does not rot; a page with two such tables would be ambiguous and the second is
    logged rather than silently preferred.

    Args:
        html: A trendlyne monthly-mutual-fund-share-holding page, or an excerpt of one.

    Returns:
        One frame carrying ``attrs["bfinance_provenance"] = "trendlyne"`` and
        ``attrs["bfinance_trendlyne_table"] = "monthly_mf_holding"``, with multi-level column
        labels flattened to ``"Sep-2026 | Shares Held"``.

    Raises:
        TrendlyneTableNotFoundError: No outermost table starts with an ``MF`` column. The
            ``Total:`` footer row survives the fixture trim and is kept, so the total still
            reconciles against the rows that were kept.
    """
    soup = BeautifulSoup(html, "lxml")
    matches = []
    for table in _outermost_tables(soup):
        headers = _header_row(table)
        if headers and normalize_header(headers[0]) == normalize_header(_MF_FIRST_COLUMN_LABEL):
            matches.append(table)
    if not matches:
        raise TrendlyneTableNotFoundError(
            f"no table on this page starts with a {_MF_FIRST_COLUMN_LABEL!r} column, so the "
            f"monthly mutual-fund grid is absent. The selector is that column label rather than "
            f"the month names beside it, which change every month. "
            f"{_page_signature_report(soup)}."
        )
    if len(matches) > 1:
        logger.warning(
            "%d outermost tables start with an %r column; returning the first of them. Pass the "
            "region you mean if the page ever carries two.",
            len(matches), _MF_FIRST_COLUMN_LABEL,
        )
    table = matches[0]
    index = _table_index(soup, table)
    frame = _read_table_frame(table, header=[0, 1], index=index)
    return _stamp(frame, table, index, "monthly_mf_holding")


def find_ohlc_tokens(html: str) -> List[str]:
    """Every distinct per-symbol OHLC token in a stock page, in order of appearance.

    Deduplicated because the live page repeats the same ``data-eodurl`` six times. Returns an
    empty list when the page carries no token — check the length before assuming one symbol.
    """
    seen: Dict[str, None] = {}
    for token in OHLC_TOKEN_RE.findall(html or ""):
        seen.setdefault(token, None)
    return list(seen)


def extract_ohlc_token(html: str) -> str:
    """The per-symbol OHLC token, read out of a stock page.

    This is the only way to obtain it: the OHLC route is
    ``/mapp/v1/stock/web/ohlc/{stock_id}/{token}/`` and the token exists nowhere else.

    Args:
        html: A trendlyne stock page.

    Returns:
        The token, verbatim, including any ``=`` padding. Do not re-encode it.

    Raises:
        TrendlyneTokenError: The page carries no token. That is a markup change (or the wrong
            page), never a valid "no data" answer, so it is raised rather than returned empty.
    """
    tokens = find_ohlc_tokens(html)
    if not tokens:
        raise TrendlyneTokenError(
            "no OHLC token found on this page: expected a URL matching "
            f"{OHLC_TOKEN_RE.pattern!r} (the data-eodurl attribute of the stock chart). "
            "The token is the only way to reach the OHLC route, so this is terminal until "
            "trendlyne's markup is re-inspected. Refusing to guess a token."
        )
    if len(tokens) > 1:
        logger.warning(
            "stock page carries %d distinct OHLC tokens; using the first. Tokens: %s",
            len(tokens), [f"{t[:6]}...({len(t)})" for t in tokens],
        )
    return tokens[0]
