"""
screener.in as a statement provider.

One response, five statement sections
-------------------------------------
``/company/{SYMBOL}/consolidated/`` carries every statement this vendor publishes on a single
page, so one request answers any (kind, frequency) the registry says screener can serve. The
sections, as measured 2026-09-28 on ``/company/ITC/consolidated/`` (HTTP 200, 210,834 bytes, no
query string, so no ``robots.txt`` rule can match it):

==================  ====  ==============================================  ==========
section id          cols  periods                                          TTM?
==================  ====  ==============================================  ==========
``quarters``        13    ``Jun 2023 … Jun 2026``                         no
``profit-loss``     13    ``Mar 2015 … Mar 2026`` (12 annual) + ``TTM``  **yes**
``balance-sheet``   12    ``Mar 2015 … Mar 2026`` (all FY ends)          no
``cash-flow``       12    ``Mar 2015 … Mar 2026`` (all FY ends)          no
``ratios``          12    ``Mar 2015 … Mar 2026`` (all FY ends)          no
==================  ====  ==============================================  ==========

Three consequences the parser is built around.

**A ``TTM`` header marks the annual table, not a quarterly one.** ``quarters`` and
``profit-loss`` carry the *same six row labels* — the only header-level discriminator is the
presence of ``TTM``, and the only row-level one is that ``quarters`` ends in ``Raw PDF`` while
``profit-loss`` ends in ``Dividend Payout %``. Treating a ``TTM`` column as a quarterly marker
inverts the semantics: the trailing two columns of ``profit-loss`` are the two most recent
fiscal years *plus* the trailing window, not two quarters.

**Identification is a section id plus a loud guard, not the section id alone.** A renamed
``<section id=…>`` fails *silently* into an empty frame if the id is the only test; a renamed
row label fails loudly. So the spec in :data:`~bfinance.statements.registry.TABLES` carries both
and this module requires both. The two P&L sections additionally differ on ``TTM``, checked per
spec.

**Basis is consolidated and only consolidated on this route.** The standalone page is a
different URL with a different set of tables, so there is nothing to switch to here: a standalone
request skips this provider with a recorded reason rather than serving consolidated numbers
under a standalone label.

Units
-----
Values are Rs Cr, except the rows that carry a ``%`` in the cell, which are the percentage they
say they are. Screener.in prints no unit note on the page, so the note recorded on ``attrs`` is
this measured description, and it says that the ``%`` rows are not Rs Cr.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional, Sequence, Tuple

from bs4 import BeautifulSoup

from bfinance.utils.formatting import parse_indian_number

from .errors import StatementEmptyError, StatementTableError
from .model import Basis, Frequency, StatementRequest, StatementResult
from .periods import TTM, canonical_period, order_periods
from .registry import CAPABILITIES, TABLES, identify_by_header

logger = logging.getLogger("bfinance")

__all__ = ["PAYLOAD", "SCREENER_SECTIONS", "parse_company_page"]

#: The payload discriminator this provider reads. Matches ``ProviderSpec.payload``.
PAYLOAD = "screener_html"

#: Measured units note. Screener.in prints none of its own, so this is bfinance's description of
#: what was read, and it names the exception.
UNITS_NOTE = (
    "screener.in: values in Rs Cr except the rows whose cell carries a '%', which are the "
    "percentage they print. No unit note is published on the page; this is a measured "
    "description, not a quote."
)

#: section id -> spec name, for the error message a caller gets when a section is missing.
SCREENER_SECTIONS: Tuple[str, ...] = (
    "quarters", "profit-loss", "balance-sheet", "cash-flow", "ratios",
)


def _visible(cell) -> str:
    """The cell's own text, which is what screener.in renders for a reader.

    screener.in's expandable rows carry a trailing ``+``; it is a control, not part of the
    metric's name, and it is stripped so ``"Sales+"`` and ``"Sales"`` are the same row. The
    ``plus`` marks are not stripped from the value in ``attrs`` because nothing here invents a
    label the page did not print.
    """
    return cell.get_text(" ", strip=True).rstrip("+").strip()


def _section_table(soup: BeautifulSoup, section_id: str):
    section = soup.find("section", id=section_id)
    return None if section is None else section.find("table", class_="data-table")


def _header_cells(table) -> List[str]:
    """Period labels from ``<thead>``, empty cells dropped.

    The first ``<th>`` is empty on every screener.in statement table — it is the row-label
    gutter, not a period. Dropping empties rather than slicing index 0 is what makes
    ``TTM`` detection work: the empty gutter is present in both P&L tables and cannot be used to
    tell them apart.
    """
    thead = table.find("thead")
    scope = thead if thead is not None else table
    first_row = scope.find("tr")
    if first_row is None:
        return []
    cells = first_row.find_all("th") or first_row.find_all("td")
    return [cell.get_text(" ", strip=True) for cell in cells if cell.get_text(" ", strip=True)]


def _row_label_signature_ok(labels: Sequence[str], spec) -> Optional[str]:
    """Check every row-label group, returning the first unsatisfied one or ``None``.

    The label comparison is case-insensitive and ``+``-insensitive because both are rendering
    choices; it is *not* fuzzy, so a genuine rename fails and says which group failed.
    """
    present = {" ".join(label.split()).lower() for label in labels}
    for group in spec.row_label_signature:
        if not any(" ".join(alias.split()).lower() in present for alias in group):
            return f"none of {list(group)} is a row label on this table"
    return None


def parse_company_page(
    html: str,
    request: StatementRequest,
    *,
    spec_name: Optional[str] = None,
) -> StatementResult:
    """Read one statement out of a screener.in company page.

    Args:
        html: A company page, verbatim or as the ``tests/fixtures/screener/`` recording of one.
        request: What to read. ``request.basis`` must be
            :attr:`~bfinance.statements.model.Basis.CONSOLIDATED`; this route has no standalone
            variant, and accepting the argument silently would put consolidated numbers under a
            standalone label.
        spec_name: The :class:`~bfinance.statements.registry.TableSpec` to use. Defaults to the
            one the capability cell registers for this (kind, frequency).

    Returns:
        A :class:`~bfinance.statements.model.StatementResult` on the consolidated basis, metric
        rows x canonical period columns.

    Raises:
        StatementTableError: The section or its table is absent, or the table's row labels do not
            satisfy the spec's signature — a markup change, reported rather than returned empty.
        StatementEmptyError: The table was read and no cell carried a number.
        ValueError: ``request.basis`` is not consolidated.
    """
    if request.basis is not Basis.CONSOLIDATED:
        raise ValueError(
            f"screener.in is consolidated-only on this route and cannot serve the "
            f"{request.basis} basis. The standalone page is a different URL with a different set "
            f"of tables, so there is nothing here to switch to. Ask trendlyne's JSON provider."
        )

    spec = TABLES[spec_name] if spec_name else _spec_for(request)
    if spec.section_id is None:  # pragma: no cover - guarded by the registry
        raise StatementTableError(
            f"spec {spec.name!r} has no screener.in section id; it belongs to another vendor."
        )

    soup = BeautifulSoup(html or "", "lxml")
    table = _section_table(soup, spec.section_id)
    if table is None:
        present = [
            section.get("id")
            for section in soup.find_all("section")
            if section.find("table", class_="data-table") is not None
        ]
        raise StatementTableError(
            f"screener.in section #{spec.section_id!r} (spec {spec.name!r}) is not on this page. "
            f"Sections that do carry a data table: {present or '<none>'}. Known section ids: "
            f"{list(SCREENER_SECTIONS)}. A section that renames itself fails here, loudly, "
            f"rather than returning an empty statement."
        )

    headers = _header_cells(table)
    if not headers:
        raise StatementTableError(
            f"screener.in section #{spec.section_id!r} has a table with no header cells, so its "
            f"periods are undefined. Refusing to guess them."
        )
    # The section id and the header rule have to agree. For the two P&L sections they are the
    # only discriminator, and a mismatch means the page changed under a name that stayed.
    if not identify_by_header(headers, spec):
        raise StatementTableError(
            f"screener.in section #{spec.section_id!r} does not look like {spec.name!r}: its "
            f"header cells are {headers[:6]}{'…' if len(headers) > 6 else ''}, which "
            f"contradicts the spec ({'missing ' + ', '.join(spec.require_headers) if spec.require_headers else ''}"
            f"{' and must not contain ' + ', '.join(spec.forbid_headers) if spec.forbid_headers else ''}). "
            f"{spec.note}"
        )

    body = table.find("tbody") or table
    raw_labels: List[str] = []
    raw_values: List[List[Optional[float]]] = []
    for tr in body.find_all("tr"):
        cells = tr.find_all("td")
        if not cells:
            continue
        label = _visible(cells[0])
        if not label:
            continue
        raw_labels.append(label)
        raw_values.append([parse_indian_number(cell.get_text(" ", strip=True)) for cell in cells[1:]])

    if not raw_labels:
        raise StatementTableError(
            f"screener.in section #{spec.section_id!r} has a table with no data rows."
        )
    failed = _row_label_signature_ok(raw_labels, spec)
    if failed is not None:
        raise StatementTableError(
            f"screener.in section #{spec.section_id!r} is not {spec.name!r}: {failed}. Row "
            f"labels found: {raw_labels[:12]}{'…' if len(raw_labels) > 12 else ''}. Identification "
            f"is by row label, not by heading text, so a heading change alone cannot cause this — "
            f"it means the markup changed."
        )

    # Header and body widths must agree before anything is indexed; screener.in re-emits the
    # header row inside long tables, and such a row carries no <td> so it is already skipped
    # above, but a genuinely ragged row is still a markup change and not a short row.
    width_mismatch = [
        label
        for label, values in zip(raw_labels, raw_values, strict=True)
        if len(values) != len(headers)
    ]
    if width_mismatch:
        raise StatementTableError(
            f"screener.in section #{spec.section_id!r}: {len(width_mismatch)} of "
            f"{len(raw_labels)} row(s) do not have {len(headers)} value(s) for the periods "
            f"{headers[:4]}…{headers[-2:]}. First offending row: {width_mismatch[0]!r}. Refusing "
            f"to build a statement from a ragged row."
        )

    # TTM is a column of the annual P&L table. Selecting or excluding it is what makes one
    # table answer two frequencies, and it is the only place TTM appears on screener.in.
    keep = [
        index
        for index, header in enumerate(headers)
        if (header == TTM) == (request.frequency is Frequency.TTM)
    ]
    if not keep:
        raise StatementTableError(
            f"screener.in section #{spec.section_id!r} has no {request.frequency.value!r} column. "
            f"Its periods are {headers[:4]}…{headers[-2:]}."
        )

    # Columns are addressed by *canonical period*, not by position. The vendors disagree on
    # direction — screener.in is oldest-first, trendlyne newest-first — and ordering the rows by
    # one thing while ordering the periods by another is a statement whose every value sits in
    # the wrong column. Ordering both through the same key makes that impossible.
    index_by_period = {canonical_period(headers[index]): index for index in keep}
    periods = order_periods(index_by_period)

    rows: Dict[str, Tuple[Optional[float], ...]] = {}
    row_labels: Dict[str, str] = {}
    dropped: List[str] = []
    duplicate_labels = 0
    for label, values in zip(raw_labels, raw_values, strict=True):
        selected = tuple(values[index_by_period[period]] for period in periods)
        non_null = sum(1 for value in selected if value is not None)
        if non_null == 0:
            # A row with no number in any cell contributes nothing to a numeric statement. The
            # one such row screener.in prints is "Raw PDF", a link. It is dropped and named, not
            # dropped quietly: a row that disappeared from a statement should be findable.
            dropped.append(label)
            continue
        if label in rows:
            # screener.in prints no repeated row label within a section, so this is a markup
            # change. Keeping the first and counting is a report, not a silent overwrite.
            duplicate_labels += 1
            logger.warning(
                "screener.in %s carries row label %r more than once; keeping the first "
                "occurrence and recording the count on attrs.",
                spec.section_id, label,
            )
            continue
        rows[label] = selected
        row_labels[label] = label

    result = StatementResult(
        request=request,
        provider="screener",
        payload=PAYLOAD,
        basis=Basis.CONSOLIDATED,
        periods=periods,
        rows=rows,
        row_ids={label: label for label in rows},
        row_labels=row_labels,
        identified_by=spec.identified_by,
        identify_note=spec.note,
        duplicate_rows=duplicate_labels,
        dropped_rows=tuple(dropped),
        units_note=UNITS_NOTE,
    )
    if result.non_null_cells() == 0:
        raise StatementEmptyError(  # pragma: no cover - unreachable: non_null==0 rows are dropped
            f"screener.in section #{spec.section_id!r} produced no numbers at all."
        )
    return result


def _spec_for(request: StatementRequest):
    """The spec the capability cell registers for this request, with a loud miss."""
    cell = CAPABILITIES.get((request.kind, request.frequency))
    name = cell.tables().get("screener") if cell else None
    if name is None:
        raise StatementTableError(
            f"screener.in is not registered for {request.kind.value}/{request.frequency.value}."
        )
    return TABLES[name]
