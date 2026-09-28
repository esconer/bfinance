"""
The second vendor's fundamentals pages as a statement provider — two routes, and they are not
interchangeable.

Why this module is named ``fundamentals_provider`` and not ``trendlyne_provider``
------------------------------------------------------------------------------
Two reasons, both load-bearing. The first is the repo's rule: ``bfinance.trendlyne`` is a
**leaf** — no other module under ``src/bfinance`` may import it, and
``tests/test_trendlyne_core.py::TestIsolation::test_no_other_bfinance_module_imports_trendlyne``
enforces it by scanning the AST of every module outside that package. This module reads that
vendor's pages, so it cannot reach into it; :func:`_read_frame` below therefore duplicates the
table extraction rather than calling :func:`bfinance.trendlyne.tables.find_tables`, and
``tests/test_statements_cascade.py`` pins the two against each other cell for cell so the
duplication cannot drift. (The isolation test matches the *substring* ``trendlyne`` in a module
name, so a sibling module called ``trendlyne_provider`` trips it as well — hence the route-based
name.)

The second is that ``/fundamentals/`` is the part of that site this module reads, and naming the
module after the routes keeps that true when the vendor goes down. Two requests, both measured.

``parse_financials_html`` — the SSR page, **standalone only**
------------------------------------------------------------
``/fundamentals/financials/{id}/{SYM}/{slug}/`` answers with five outermost ``<table>``s
(212,407 B cut from an 866,708 B response) and is the only HTML route that carries data.
**It renders standalone while its own XHR says ``is_consolidated: true``.** Measured on
MOTHERSON at Mar 2026: the page prints ``TotalShareHoldersFunds_A`` as **38,290.1** and
``annualDataDump['consolidated']['Mar 2026']['TotalShareHoldersFunds_A']`` is **40,979.6** for
the same row and the same period. ``is_consolidated`` is a display default, not the payload's
basis. This provider is therefore registered standalone-only
(:data:`~bfinance.statements.registry.PROVIDERS`), and a consolidated request skips it with that
reason recorded rather than being served a number that belongs to the other basis.

``parse_fundamental_results`` — the XHR payload, **both bases**
---------------------------------------------------------------
``/fundamentals/get-fundamental_results-v2/{id}/{token}/`` ships ``quarterlyDataDump`` and
``annualDataDump``, each with a ``consolidated`` and a ``standalone`` sub-object. It is the only
route in bfinance that can serve a standalone statement at all.

Units
-----
The payload says so itself: ``tableInsightCurrency`` is the literal string *"All financials are
in INR Cr and price data in INR"*, and it is recorded **verbatim** on ``attrs`` rather than
paraphrased. Rows whose label ends in ``%`` are percentages, not Rs Cr, and the note says so.

How a row is found, and how a table is told apart
-------------------------------------------------
**The machine id, never the label.** Each data row's first cell holds
``<div class="indicator-code" data-indcode="TOTAL_SR_Q" style="display:none">TOTAL_SR_Q</div>``
followed by the display text, so ``td.get_text()`` yields ``"TOTAL_SR_Q Total Rev. ⌄"``. The id
is the stable half; the label is truncated with an ellipsis and **occurs twice in one table**
(``Operating Exp.`` in both the quarterly and the annual P&L, ``Cash Earning Retention`` twice
in the ratios table). The frame carries the *visible* label in column 0 — ``read_html`` drops the
``display:none`` div, verified on a one-row probe — so the ids are read from the DOM in a second
pass and aligned to the frame by row position, behind a hard length check so a misalignment
cannot pass silently.

Even the id repeats, so :attr:`~bfinance.statements.model.StatementResult.duplicate_rows`
counts and the first occurrence wins. Losing a row is bad; losing it without a number saying so
is worse.

**Three of the five tables have byte-identical header rows** (trap 2 in
:mod:`bfinance.statements.registry`) and are discriminated by row id only. Do not signature-match
them.

Trap 1 — a tab page is not an error and not a data page
---------------------------------------------------------
``/fundamentals/balance-sheet/`` and its four siblings answer HTTP 200 with an ~85 KB shell
holding **zero** ``<table>``. They are not 404s and nothing is robots-disallowed; they differ
only in ``<title>``. This module raises
:class:`~bfinance.statements.errors.StatementNotADataPageError` naming the two routes that do
carry data, because an empty frame here reads as a verified negative and is not one.

Trap 3 — ``order.quarter.bs`` advertises 13 quarters and holds none
-------------------------------------------------------------------
``body.order.quarter`` lists 13 period labels under **every** key, ``bs`` and ``cf`` included.
All 260 balance-sheet / cash-flow / ratio row ids are ``_A``-suffixed and **zero** of them
appears in ``body.quarterlyDataDump`` (82 keys = 75 ``_Q`` + ``NP_TTM`` + ``EPS_TTM`` + 5
non-numeric). So this module takes a statement's **row set from the frequency suffix on the ids
the dump actually holds**, never from ``order``; an empty row set is
:class:`~bfinance.statements.errors.StatementEmptyError` carrying the arithmetic. ``order`` is
read for *ordering* only, intersected with the dump's own keys, so a period the vendor listed but
never populated cannot become a column.

No URL is recorded on ``attrs``
-------------------------------
The JSON route is addressed by a per-symbol token. No ``attrs`` key here carries a URL, so there
is no credential on a frame, in a log line, or in a CSV export.
"""

from __future__ import annotations

import dataclasses
import logging
import re
from io import StringIO
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import pandas as pd
from bs4 import BeautifulSoup

from bfinance.utils.formatting import parse_indian_number

from .errors import (
    StatementEmptyError,
    StatementNotADataPageError,
    StatementSchemaError,
    StatementTableError,
)
from .model import Basis, Frequency, StatementKind, StatementRequest, StatementResult
from .periods import TTM, canonical_period, order_periods
from .registry import CAPABILITIES, TABLES, identify_by_header, identify_by_row_ids

logger = logging.getLogger("bfinance")

__all__ = [
    "HTML_PAYLOAD",
    "JSON_PAYLOAD",
    "NON_PERIOD_HEADERS",
    "NON_NUMERIC_ROW_IDS",
    "UNSUFFIXED_TTM_ROW_IDS",
    "parse_financials_html",
    "parse_fundamental_results",
    "classify_html_tables",
    "html_table_headers",
    "html_row_ids",
]

HTML_PAYLOAD = "trendlyne_html"
JSON_PAYLOAD = "trendlyne_json"

#: Header cells that are table structure rather than periods, folded. Anything else in the header
#: row of one of these five tables is read as a period, and an unrecognised one is then refused
#: by :func:`~bfinance.statements.periods.canonical_period` — which is the right way to find out.
NON_PERIOD_HEADERS: frozenset = frozenset({"indicator", "graph", "cagr 3 yrs", "cagr 5 yrs"})

#: Row ids carrying a document link, a notes link or an AI blurb rather than a number — measured
#: as ``dict`` or ``str`` valued in both dumps. The drop is per-value rather than per-name, so an
#: id missing from this list would be dropped too; the list is here so a reader can see what the
#: non-numeric rows are.
NON_NUMERIC_ROW_IDS: Tuple[str, ...] = (
    "earnings_transcripts", "ai_summary", "results_pdf", "result_notes", "Annual_Reports",
)

#: The two TTM figures that carry **no** frequency suffix and are genuinely rows of the quarterly
#: table. They are what the TTM request is cross-checked against.
UNSUFFIXED_TTM_ROW_IDS: Tuple[str, ...] = ("NP_TTM", "EPS_TTM")

_HTML_UNITS_NOTE = (
    "trendlyne.com SSR page. The JSON route states, verbatim: 'All financials are in INR Cr and "
    "price data in INR' (tableInsightCurrency). This page does not repeat that note and the same "
    "convention is applied here. Rows whose label ends in '%' are percentages, not Rs Cr."
)

_WHITESPACE_RE = re.compile(r"\s+")


def _read_frame(tag) -> pd.DataFrame:
    """One table element as a frame, under the vendor's own cell-formatting contract.

    Duplicated from :func:`bfinance.trendlyne.tables.find_tables` rather than imported, and the
    duplication is deliberate: that package is a **leaf** and no other module under ``src/`` may
    import it (see the module docstring). ``tests/test_statements_cascade.py`` compares this
    output against :func:`~bfinance.trendlyne.tables.find_tables` cell for cell, so the two
    cannot drift.

    The three flags are load-bearing, and they are the same three the trendlyne extractor passes:

    ``keep_default_na=False``, ``na_values=[]``
        pandas would otherwise turn ``"N/A"`` into ``NaN`` **before any rule here could see it**.
        A ``NaN`` in a cell this vendor actually rendered is indistinguishable from "the vendor
        did not report it", and the two want opposite handling — one is a rendered dash, the
        other is an absence.
    ``thousands=None``
        pandas would strip the separator from ``"3,827.7"``, and a statement whose numbers lost
        their formatting looks like a parsing bug months later.

    Numeric typing is deliberately **not** pandas' job and **not** delegated: every value is
    read here as the string the page printed and parsed by
    :func:`bfinance.utils.formatting.parse_indian_number`, which handles ``"12.47%"`` and
    ``"-3,154"`` and maps ``"-"`` to ``None`` — none, not zero.
    """
    fragment = str(tag)
    try:
        parsed = pd.read_html(
            StringIO(fragment),
            flavor="lxml",
            header=0,
            keep_default_na=False,
            na_values=[],
            thousands=None,
        )
    except ValueError as exc:  # read_html raises ValueError on a non-table fragment
        raise StatementTableError(
            f"a table on this page could not be read as a table; upstream markup may have "
            f"changed. First 160 chars: {fragment[:160]!r}"
        ) from exc
    if len(parsed) != 1:  # pragma: no cover - one element in, one frame out
        raise StatementTableError(
            f"a table element parsed into {len(parsed)} frames, expected 1; upstream markup may "
            f"have changed."
        )
    frame = parsed[0]
    frame.columns = [_flatten(column) for column in frame.columns]
    # Kept for parity with the trendlyne extractor so the two frames are comparable cell for
    # cell. With keep_default_na=False an empty cell is '' rather than NaN, so this only drops a
    # row that is genuinely empty all the way down.
    return frame.dropna(axis=0, how="all").reset_index(drop=True)


def _flatten(column: Any) -> str:
    """Flatten a ``read_html`` column label to a plain string.

    A multi-level header yields tuples with ``None`` for a level the cell does not span; those
    become ``"A | B"`` and blank levels are dropped rather than rendered as ``" | "``. A
    single-row header yields plain strings, which pass through.
    """
    if isinstance(column, tuple):
        levels = [str(level) for level in column if level is not None and str(level).strip()]
        return " | ".join(_WHITESPACE_RE.sub(" ", level).strip() for level in levels)
    return _WHITESPACE_RE.sub(" ", str(column)).strip()


# --------------------------------------------------------------------------- #
# shared row assembly
# --------------------------------------------------------------------------- #


def _assemble(
    request: StatementRequest,
    *,
    provider: str,
    payload: str,
    basis: Basis,
    spec_name: str,
    periods: Sequence[str],
    values: Mapping[str, Sequence[Optional[float]]],
    row_labels: Mapping[str, str],
    units_note: str,
    omitted_rows: int = 0,
) -> StatementResult:
    """Fold read rows into a result, dropping what carries no number and refusing what is empty.

    Two rules, and both exist because a frame is a bad place to hide either:

    * a row with **no** number in any cell is dropped and **named** on
      ``attrs["bfinance_statements_dropped_rows"]``. On trendlyne these are the document-link and
      notes rows. A row that vanished must be findable.
    * a statement with **no** number in *any* cell is
      :class:`~bfinance.statements.errors.StatementEmptyError`, never a frame of nulls. That is
      trap 3: 13 advertised quarterly balance-sheet periods, 0 of 13 non-null, and reporting that
      as "the vendor published no balance sheet" is a false negative about the vendor.
    """
    rows: Dict[str, Tuple[Optional[float], ...]] = {}
    labels: Dict[str, str] = {}
    dropped: List[str] = []
    duplicates = 0
    for metric, cells in values.items():
        row = tuple(cells)
        if all(value is None for value in row):
            dropped.append(metric)
            continue
        if metric in rows:
            duplicates += 1
            logger.warning(
                "trendlyne row id %r appears more than once in the %s table; keeping the first "
                "occurrence and recording the count on attrs.",
                metric, request.kind.value,
            )
            continue
        rows[metric] = row
        labels[metric] = row_labels.get(metric, metric)

    if not rows:
        raise StatementEmptyError(
            f"trendlyne {request.kind.value}/{request.frequency.value} for {request.symbol}: "
            f"{len(values)} row(s) matched the statement and NONE of them carries a number in any "
            f"of the {len(periods)} period(s) {list(periods)[:4]}"
            f"{'…' if len(periods) > 4 else ''}. That is a statement that could not be read, not "
            f"a company with no numbers in it, and it is not returned as an empty frame. If this "
            f"is a balance sheet or cash flow read across quarterly columns, the cause is "
            f"measured: every one of those row ids is '_A'-suffixed and none appears in the "
            f"quarterly dump, so the quarterly period list the vendor advertises is not a period "
            f"set for that statement."
        )

    spec = TABLES[spec_name]
    return StatementResult(
        request=request,
        provider=provider,
        payload=payload,
        basis=basis,
        periods=tuple(periods),
        rows=rows,
        row_ids={metric: metric for metric in rows},
        row_labels=labels,
        identified_by=spec.identified_by,
        identify_note=spec.note,
        duplicate_rows=duplicates,
        dropped_rows=tuple(dropped),
        omitted_rows=int(omitted_rows),
        units_note=units_note,
    )


# --------------------------------------------------------------------------- #
# traps 1 and 2: the SSR page
# --------------------------------------------------------------------------- #


def html_table_headers(tag) -> List[str]:
    """Every ``<th>`` of a table's first row, in order, unfiltered.

    Unfiltered on purpose: the caller needs the *positions* of the period cells to index the
    frame, and the structure cells are what establish the offset.
    """
    header_row = tag.find("tr")
    if header_row is None:
        return []
    return [cell.get_text(" ", strip=True) for cell in header_row.find_all("th")]


def html_row_ids(tag) -> List[str]:
    """The machine id of every data row, in document order.

    ``<td role="rowheader">`` holds ``<div class="indicator-code">`` whose text is the id, sitting
    *before* the display span. The class is the selector because the ``data-indcode`` attribute
    lives on that div and not on the ``<td>`` — verified, not assumed.
    """
    ids: List[str] = []
    for cell in tag.find_all("td", attrs={"role": "rowheader"}):
        code = cell.find("div", class_="indicator-code")
        if code is not None:
            ids.append(code.get_text(strip=True))
    return ids


def _outermost_tables(soup: BeautifulSoup) -> List[Any]:
    """Top-level ``<table>`` elements, skipping nested ones.

    Same rule as :func:`bfinance.trendlyne.tables.find_tables` uses, and it has to be the same
    rule: the two lists are compared against each other, so any difference in what counts as a
    table would misalign every row id with its values.
    """
    return [t for t in soup.find_all("table") if t.find_parent("table") is None]


def classify_html_tables(html: str) -> List[Optional[str]]:
    """Classify every outermost table on a trendlyne financials page, in document order.

    Header identification first, row id second, and the order is the whole point: the two P&L
    tables *can* be told apart by their headers and the other three cannot be told apart by
    anything else. Returns ``None`` for a table that matched nothing, which on a financials page
    means a layout table and is not an error.

    The page is parsed once for the DOM (row ids, header cells, the data table) and the data table
    is then handed to :func:`_read_frame` for the values, because ``read_html`` keeps a cell's
    text and throws away its attributes — and the machine id is an attribute.
    """
    soup = BeautifulSoup(html or "", "lxml")
    tags = _outermost_tables(soup)
    if not tags:
        return []

    specs = list(TABLES.values())
    by_header = [spec for spec in specs if spec.identified_by == "header"]
    by_row_id = [spec for spec in specs if spec.identified_by == "row_id"]

    found: List[Optional[str]] = []
    for tag in tags:
        headers = html_table_headers(tag)
        name = next((spec.name for spec in by_header if identify_by_header(headers, spec)), None)
        if name is None:
            ids = html_row_ids(tag)
            name = next((spec.name for spec in by_row_id if identify_by_row_ids(ids, spec)), None)
        found.append(name)
    return found


def parse_financials_html(
    html: str,
    request: StatementRequest,
    *,
    spec_name: Optional[str] = None,
) -> StatementResult:
    """Read one statement out of the ``/fundamentals/financials/`` SSR page.

    The page renders **standalone**, and that is recorded rather than inferred from anything the
    caller passes in: the same response's XHR reports ``is_consolidated: true`` while printing
    no consolidated figure anywhere.

    Args:
        html: The SSR page, verbatim or as the ``tests/fixtures/trendlyne/`` recording of one.
        request: What to read. ``request.basis`` must be
            :attr:`~bfinance.statements.model.Basis.STANDALONE`.
        spec_name: Override the spec the capability cell registers. Used by the trap-2 test to
            ask for each of the three ambiguous tables by name.

    Returns:
        A :class:`~bfinance.statements.model.StatementResult` on the standalone basis, metric
        rows x canonical period columns.

    Raises:
        StatementNotADataPageError: The bytes carry zero ``<table>`` — a tab-page shell (trap 1).
        StatementTableError: The page has tables but not the one asked for, or the frames and the
            DOM disagree on row count.
        StatementEmptyError: The table was read and no cell carried a number.
        ValueError: ``request.basis`` is consolidated — this page cannot serve it.
    """
    if request.basis is not Basis.STANDALONE:
        raise ValueError(
            f"the trendlyne SSR page renders the standalone basis, so it cannot serve a "
            f"{request.basis} request. Its own XHR says is_consolidated=true, but the numbers it "
            f"prints are standalone (MOTHERSON, Mar 2026: the page prints 38,290.1 and the "
            f"consolidated dump says 40,979.6, same row, same period). Use the JSON provider for "
            f"{request.basis}."
        )

    if not html or not html.strip():
        raise StatementNotADataPageError(
            "the trendlyne provider was handed an empty payload. Expected the SSR route "
            "/fundamentals/financials/{id}/{SYM}/{slug}/ or the JSON route "
            "/fundamentals/get-fundamental_results-v2/{id}/{token}/."
        )

    cell = CAPABILITIES.get((request.kind, request.frequency))
    if spec_name is None:
        spec_name = cell.tables().get("trendlyne-html") if cell else None
    if spec_name is None:
        raise StatementTableError(
            f"the trendlyne SSR page is not registered for "
            f"{request.kind.value}/{request.frequency.value}."
        )
    spec = TABLES[spec_name]

    soup = BeautifulSoup(html, "lxml")
    tags = _outermost_tables(soup)
    if not tags:
        # Trap 1. The five tab pages answer 200 with exactly this shape.
        raise StatementNotADataPageError(
            f"this trendlyne page has ZERO <table> elements ({len(html)} bytes), so it is not a "
            f"data page. If it came from /fundamentals/balance-sheet/, /fundamentals/cash-flow/, "
            f"/fundamentals/quarterly-results/, /fundamentals/annual-results/ or "
            f"/fundamentals/financial-ratios/ that is expected: those five tabs answer HTTP 200 "
            f"with an ~85 KB shell that differs only in <title> and populates from one XHR. They "
            f"are decoys, not errors, and reading one yields an empty frame that reads as 'the "
            f"vendor has no data'. Only two routes carry data: "
            f"/fundamentals/financials/{{id}}/{{SYM}}/{{slug}}/ and "
            f"/fundamentals/get-fundamental_results-v2/{{id}}/{{token}}/."
        )

    classified = classify_html_tables(html)
    matching = [index for index, name in enumerate(classified) if name == spec_name]
    if not matching:
        raise StatementTableError(
            f"the trendlyne page carries {len(tags)} table(s) and none is {spec_name!r}. "
            f"Classified, in document order: {classified}. The three annual tables — balance "
            f"sheet, ratios, cash flow — share a byte-identical header row and are told apart "
            f"only by their row ids (TA_A/TL_A/CA_A/CL_A, ROE_A/ROCE_A/BVSH_A, "
            f"CFO_A/CFI_A/CFA_A/NCF_A). {spec.note}"
        )
    if len(matching) > 1:  # pragma: no cover - no measured page produces this
        raise StatementTableError(
            f"{len(matching)} tables on this page classified as {spec_name!r} "
            f"(indexes {matching}). Refusing to pick one."
        )

    index = matching[0]
    frame = _read_frame(tags[index])
    tag = tags[index]

    headers = html_table_headers(tag)
    # The frame's columns ARE the header cells, in order: read_html with header=0 names each
    # column after the <th> of the first row. Asserted rather than assumed, because every row
    # value below is addressed by header position and an unchecked offset would put every number
    # in a neighbouring column while still producing a plausible frame.
    if [str(column) for column in frame.columns] != headers:
        raise StatementTableError(
            f"for {spec_name!r} the frame columns {list(frame.columns)[:4]}… do not match the "
            f"header cells {headers[:4]}…, so no value can be addressed to a period. Refusing to "
            f"guess the offset."
        )

    period_positions = [
        position
        for position, text in enumerate(headers)
        if text.strip().lower() not in NON_PERIOD_HEADERS
    ]
    period_labels = [headers[position] for position in period_positions]
    if not period_labels:
        raise StatementTableError(
            f"{spec_name!r} has a header row with no period cells: {headers}. Refusing to guess "
            f"its periods."
        )

    if request.frequency is Frequency.TTM:
        ttm_at = next(
            (i for i, text in enumerate(period_labels) if text.strip().upper() == TTM), None
        )
        if ttm_at is None:
            raise StatementTableError(
                f"{spec_name!r} has no TTM column. Its period cells are {period_labels[:4]}…"
                f"{period_labels[-2:]}. Only the annual P&L table on this site carries one."
            )
        periods: Tuple[str, ...] = (TTM,)
        frame_columns = [str(frame.columns[period_positions[ttm_at]])]
    else:
        periods = order_periods(period_labels)
        canonical_at = {
            canonical_period(label): position for position, label in enumerate(period_labels)
        }
        frame_columns = [
            str(frame.columns[period_positions[canonical_at[period]]]) for period in periods
        ]

    row_ids = html_row_ids(tag)
    if len(row_ids) != len(frame.index):
        raise StatementTableError(
            f"{len(row_ids)} row id(s) in the DOM but {len(frame.index)} row(s) in the frame for "
            f"{spec_name!r}; they cannot be aligned, and guessing the offset would attach every "
            f"machine id to the wrong row."
        )

    label_column = str(frame.columns[0])
    values: Dict[str, List[Optional[float]]] = {}
    labels: Dict[str, str] = {}
    duplicates = 0
    dropped: List[str] = []
    seen: set = set()
    for row_id, (_, row) in zip(row_ids, frame.iterrows(), strict=True):
        if row_id in seen:
            duplicates += 1
            continue
        seen.add(row_id)
        parsed = [parse_indian_number(row[column]) for column in frame_columns]
        if all(value is None for value in parsed):
            dropped.append(row_id)
            continue
        values[row_id] = parsed
        labels[row_id] = str(row[label_column])

    result = _assemble(
        request,
        provider="trendlyne-html",
        payload=HTML_PAYLOAD,
        basis=Basis.STANDALONE,
        spec_name=spec_name,
        periods=periods,
        values=values,
        row_labels=labels,
        units_note=_HTML_UNITS_NOTE,
    )
    # The duplicate count is measured while iterating and _assemble counts its own, which is
    # always zero here; carry the DOM count across rather than dropping it.
    return _with_duplicate_count(result, duplicates, dropped)


def _with_duplicate_count(
    result: StatementResult, duplicates: int, dropped: Sequence[str]
) -> StatementResult:
    """The same result with the DOM-side duplicate and drop counts attached.

    ``StatementResult`` is frozen and the counts are measured while reading the DOM, before
    :func:`_assemble` gets to see the rows. Rebuilding is clearer than threading a mutable
    counter in by reference.
    """
    return dataclasses.replace(
        result,
        duplicate_rows=duplicates,
        dropped_rows=tuple(dict.fromkeys(tuple(result.dropped_rows) + tuple(dropped))),
    )


# --------------------------------------------------------------------------- #
# trap 3: the JSON payload
# --------------------------------------------------------------------------- #

#: (kind, frequency) -> (dump key, order root, order group).
#:
#: The ``order`` root matters and is the other half of trap 3: ``body.order.quarter.bs`` and
#: ``body.order.annual.bs`` both list 13 and 11 periods respectively, and neither is a period set
#: the balance-sheet rows have. The quarterly cells for a balance sheet simply are not in this
#: table, which is why there is no quarterly entry for ``BALANCE_SHEET``.
_JSON_LAYOUT: Dict[Tuple[StatementKind, Frequency], Tuple[str, str, str]] = {
    (StatementKind.INCOME, Frequency.QUARTERLY): ("quarterlyDataDump", "quarter", "qtr"),
    (StatementKind.INCOME, Frequency.ANNUAL): ("annualDataDump", "annual", "annual"),
    (StatementKind.INCOME, Frequency.TTM): ("annualDataDump", "annual", "annual"),
    (StatementKind.BALANCE_SHEET, Frequency.ANNUAL): ("annualDataDump", "annual", "bs"),
    (StatementKind.CASH_FLOW, Frequency.ANNUAL): ("annualDataDump", "annual", "cf"),
}

#: The frequency suffix that marks a row id as belonging to a frequency. This is the vendor's own
#: marker, and trap 3 in its entirety is that ``order`` advertises periods its ``_A`` rows do not
#: have.
_JSON_SUFFIX: Dict[Frequency, str] = {
    Frequency.QUARTERLY: "_Q",
    Frequency.ANNUAL: "_A",
    Frequency.TTM: "_A",
}


def _body(payload: Any, context: str) -> Mapping[str, Any]:
    """Unwrap trendlyne's ``{"head": …, "body": …}`` envelope, or say precisely what is wrong.

    Every JSON route on this site answers **HTTP 200** for a failed request, so a status code is
    not evidence of success and ``head.status != "0"`` is checked here. Both the whole envelope
    and an already-unwrapped ``body`` are accepted.
    """
    if not isinstance(payload, Mapping):
        raise StatementSchemaError(
            f"{context}: expected a JSON object, got {type(payload).__name__}."
        )
    if "head" not in payload:
        return payload
    head = payload["head"]
    if not isinstance(head, Mapping):
        raise StatementSchemaError(
            f"{context}: 'head' is {type(head).__name__}, expected an object."
        )
    status = str(head.get("status", "")).strip()
    if status != "0":
        raise StatementSchemaError(
            f"{context}: trendlyne reported status={status!r} "
            f"({head.get('statusDescription') or 'no description'}). This arrives as HTTP 200 with "
            f"an empty body, so a client that only checks the status code sees success and then "
            f"an empty statement."
        )
    body = payload.get("body")
    if not isinstance(body, Mapping):
        raise StatementSchemaError(
            f"{context}: 'body' is {type(body).__name__}, expected an object."
        )
    return body


def _ordered_periods(
    body: Mapping[str, Any],
    dump: Mapping[str, Any],
    dump_name: str,
    order_root: str,
    group: str,
    frequency: Frequency,
) -> List[str]:
    """Wire period labels, ordered by the vendor and **restricted to the dump's own keys**.

    ``body.order.{order_root}.{group}`` supplies the order; the *set* comes from the dump. A
    period the vendor lists but never populated therefore cannot become a column, which is the
    difference between a 13-period statement with 13 real numbers and a 13-period statement of
    nulls that reads as "nothing published".
    """
    present = set(dump)
    order = body.get("order", {}).get(order_root, {}).get(group)
    if not isinstance(order, list):
        raise StatementSchemaError(
            f"body.order.{order_root}.{group} is {type(order).__name__}, expected a list of "
            f"period labels. body.order carries {sorted(body.get('order', {}))}."
        )
    labels = [label for label in order if label in present]
    if frequency is Frequency.TTM:
        if TTM not in labels:
            raise StatementSchemaError(
                f"{dump_name} has no {TTM!r} period. Its periods are {sorted(present)[:4]}…"
            )
        return [TTM]
    if frequency is Frequency.ANNUAL:
        # TTM is a window, not a fiscal year, so it is never a column of the annual statement; it
        # is that statement's own TTM frequency. Dropped from both the advertised list and the
        # unlisted-period set *before* the warning below, or every annual read of a section whose
        # order does not itself mention TTM would warn about a period it is dropping on purpose.
        labels = [label for label in labels if label != TTM]
    extra = sorted(present - set(labels) - ({TTM} if frequency is Frequency.ANNUAL else set()))
    if extra:
        logger.warning(
            "trendlyne %s carries %d period(s) that body.order.%s.%s does not list (%s…); they "
            "are included, appended in sorted order, because the data is the evidence and the "
            "advertised order is not.",
            dump_name, len(extra), order_root, group, extra[:3],
        )
        labels.extend(extra)
    return labels


def parse_fundamental_results(
    payload: Any,
    request: StatementRequest,
) -> StatementResult:
    """Read one statement out of a ``get-fundamental_results-v2`` payload.

    Args:
        payload: The decoded JSON envelope (``{"head": …, "body": …}``) or an already-unwrapped
            ``body``.
        request: What to read, including the basis. Both bases ship in one response, so this is
            the only route in bfinance that can serve a standalone request.

    Returns:
        A :class:`~bfinance.statements.model.StatementResult` on ``request.basis``.

    Raises:
        StatementSchemaError: The envelope reported failure, or a key this module needs is absent
            or the wrong shape.
        StatementEmptyError: The statement's row set is empty in the dump being read — trap 3 —
            or every matched row came back with no number in any cell.
    """
    context = f"the trendlyne fundamentals payload for {request.symbol}"
    body = _body(payload, context)

    layout = _JSON_LAYOUT.get((request.kind, request.frequency))
    if layout is None:
        # Reached only by a caller bypassing the registry, which has already refused this cell
        # before any payload is read. Said here too so the provider is honest when called direct,
        # and with the arithmetic, because "no layout" is otherwise an unhelpfully internal phrase.
        raise StatementEmptyError(
            f"{request.kind.value}/{request.frequency.value} has no layout in the "
            f"get-fundamental_results-v2 payload, on either vendor. Measured: the quarterly dump "
            f"holds 75 '_Q' ids plus NP_TTM, EPS_TTM and 5 non-numeric keys, and NONE of the 260 "
            f"'_A' ids appears in it — the balance sheet, cash flow and ratios are annual-only "
            f"there, exactly as they are on screener.in. So body.order.quarter.bs and "
            f"body.order.quarter.cf, which each advertise 13 quarterly period labels, describe a "
            f"period set their own rows do not have: reading the balance sheet's 13 row ids "
            f"across them gives 0 of 169 non-null cells, every one, and the cash flow's 4 row ids "
            f"give 0 of 52. Across the annual columns the same rows read 143 of 143 (consolidated "
            f"balance sheet) and 44 of 44 (cash flow). An unasked question is not a published "
            f"absence, so this is refused rather than returned as a correctly shaped frame of "
            f"nulls."
        )
    dump_name, order_root, group = layout

    dumps = body.get(dump_name)
    if not isinstance(dumps, Mapping):
        raise StatementSchemaError(
            f"{context}: body.{dump_name} is {type(dumps).__name__}, expected an object keyed by "
            f"basis. Body keys: {sorted(body)[:12]}."
        )
    dump = dumps.get(request.basis.value)
    if not isinstance(dump, Mapping):
        raise StatementSchemaError(
            f"{context}: body.{dump_name} has no {request.basis.value!r} sub-object; it carries "
            f"{sorted(dumps)}."
        )

    wire_periods = _ordered_periods(body, dump, dump_name, order_root, group, request.frequency)
    if not wire_periods:
        raise StatementEmptyError(
            f"{context}: body.{dump_name}['{request.basis.value}'] is empty, so there is no "
            f"period to read and no statement to return."
        )
    periods = (TTM,) if request.frequency is Frequency.TTM else order_periods(wire_periods)
    wire_by_period = {canonical_period(label): label for label in wire_periods}

    # Trap 3, closed at the source: the row set comes from the frequency suffix on the ids the
    # dump actually holds, never from body.order.
    suffix = _JSON_SUFFIX[request.frequency]
    ids_present: set = set()
    for period in periods:
        row = dump.get(wire_by_period[period])
        if isinstance(row, Mapping):
            ids_present.update(row)
    members = {row_id for row_id in ids_present if row_id.endswith(suffix)}
    if request.frequency is Frequency.QUARTERLY:
        members |= {row_id for row_id in UNSUFFIXED_TTM_ROW_IDS if row_id in ids_present}
    section_ids = [
        entry["indCode"]
        for entry in body.get("level_first_dict_map", {}).get(group, [])
        if isinstance(entry, Mapping) and entry.get("indCode")
    ]
    ordered_ids = [row_id for row_id in section_ids if row_id in members]
    ordered_ids.extend(sorted(members - set(ordered_ids)))

    if not ordered_ids:
        advertised = body.get("order", {}).get(order_root, {}).get(group)
        raise StatementEmptyError(
            f"{context}: no {request.kind.value} row id carrying the {suffix!r} suffix appears in "
            f"body.{dump_name}['{request.basis.value}'] across the {len(periods)} period(s) "
            f"{periods[:4]}{'…' if len(periods) > 4 else ''}. body.order.{order_root}.{group!r} "
            f"advertises "
            f"{len(advertised) if isinstance(advertised, list) else 'n/a'} period(s) for this "
            f"statement, and the period list a vendor advertises is not the period set its rows "
            f"have: every balance-sheet, cash-flow and ratio row id is '_A'-suffixed, and 0 of "
            f"the 260 '_A' keys appears in the quarterly dump. Reading them across the "
            f"advertised quarterly columns gives 0 of 13 non-null, every cell. That is an unasked "
            f"question, not a published absence, and it is refused rather than returned as a "
            f"frame of nulls."
        )

    values: Dict[str, List[Optional[float]]] = {}
    labels: Dict[str, str] = {}
    with_number = 0
    for row_id in ordered_ids:
        cells: List[Optional[float]] = []
        has_number = False
        for period in periods:
            raw = _cell(dump.get(wire_by_period[period]), row_id)
            if isinstance(raw, bool) or raw is None:
                cells.append(None)
            elif isinstance(raw, (int, float)):
                has_number = True
                cells.append(float(raw))
            else:
                parsed = parse_indian_number(raw)
                has_number = has_number or parsed is not None
                cells.append(parsed)
        if not has_number:
            continue
        with_number += 1
        values[row_id] = cells
        labels[row_id] = row_id

    if not values:
        # The row set was non-empty (so the ids exist in this dump) and every one of them came
        # back null in every period. That is trap 3's exact shape, reached by a different route,
        # and the arithmetic is worth stating rather than paraphrasing.
        raise StatementEmptyError(
            f"{context}: {len(ordered_ids)} {request.kind.value} row id(s) carrying the "
            f"{suffix!r} suffix are present in body.{dump_name}['{request.basis.value}'] and NONE "
            f"of them carries a number in any of the {len(periods)} period(s) "
            f"{periods[:4]}{'…' if len(periods) > 4 else ''} — 0 of "
            f"{len(ordered_ids) * len(periods)} non-null cells. The period list a vendor "
            f"advertises is not the period set its rows have: a statement whose cells are all null "
            f"is an unasked question, not a published absence, and it is refused rather than "
            f"returned as a correctly shaped frame of nulls."
        )

    units_note = str(body.get("tableInsightCurrency") or "").strip() or _HTML_UNITS_NOTE
    spec_name = CAPABILITIES[(request.kind, request.frequency)].tables()["trendlyne-json"]
    return _assemble(
        request,
        provider="trendlyne-json",
        payload=JSON_PAYLOAD,
        basis=request.basis,
        spec_name=spec_name,
        periods=periods,
        values=values,
        row_labels=labels,
        units_note=units_note,
        omitted_rows=max(0, len(members) - with_number),
    )


def _cell(row: Any, row_id: str) -> Any:
    """One cell, or ``None`` when the period is not a mapping at all."""
    return row.get(row_id) if isinstance(row, Mapping) else None
