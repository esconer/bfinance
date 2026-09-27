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
    ['Ex-Date', 'Purpose', 'Notes']                                            -> (no signature)
    ['Ex-Date', 'Ratio', 'Face Value', 'Premium', 'Record Date']              -> rights

Note the last one: a ``Ratio`` column is the *rights* table, not the bonus table. Matching on the
bare word ``"Ratio"`` would misclassify it, which is why the bonus signature requires the exact
cell ``"Bonus Ratio"`` and the rights signature requires ``"Face Value"`` **and** ``"Premium"``.

Three more traps this module has to absorb
------------------------------------------
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

        fragment = str(table)
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
            )
        except ValueError as exc:  # read_html raises ValueError on a non-table fragment
            raise TrendlyneSchemaError(
                f"table at index {index} (signature={matched!r}) could not be parsed as a "
                f"table; upstream markup may have changed. First 160 chars: {fragment[:160]!r}"
            ) from exc
        if len(parsed) != 1:
            raise TrendlyneSchemaError(
                f"table at index {index} (signature={matched!r}) parsed into {len(parsed)} "
                f"frames, expected 1; upstream markup may have changed."
            )

        frame = parsed[0]
        if flatten_columns:
            frame.columns = _flatten_columns(frame.columns)
        frame = frame.dropna(axis=0, how="all").reset_index(drop=True)
        if coerce_numeric:
            frame = _coerce_numeric(frame)

        frame.attrs["bfinance_provenance"] = PROVENANCE
        frame.attrs["bfinance_trendlyne_table"] = matched
        frame.attrs["bfinance_trendlyne_table_index"] = index
        frame.attrs["bfinance_trendlyne_table_header"] = list(headers)
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
