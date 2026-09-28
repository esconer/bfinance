"""
The registry: which table is which, and which provider can answer which question.

This module is the part to read before changing anything about statement fetching. It holds two
things, and keeping them in one file is the point:

* :data:`TABLES` — **how to tell a statement table apart from its neighbours.** A
  :class:`TableSpec` says whether the identification is carried by the header row, by the row
  ids, or by the section id, and it says so in the spec itself rather than in a comment. The
  reason that matters is trap 2, below.
* :data:`CAPABILITIES` — **which providers can answer which (kind, frequency)**, in the order
  they are tried, plus, for the four combinations nobody can answer, the measured reason and
  the two vendors it was measured against.

The measured capability matrix (2026-09-28, both vendors probed)
----------------------------------------------------------------
============  ==================  ==================  ==========================
kind          annual               quarterly           ttm
============  ==================  ==================  ==========================
income        screener, trendlyne  screener, trendlyne  screener, trendlyne
balance_sheet screener, trendlyne  **unavailable**     **unavailable**
cash_flow     screener, trendlyne  **unavailable**     **unavailable**
============  ==================  ==================  ==========================

Every "unavailable" is a measurement, not an assumption. :data:`UNAVAILABLE` carries the evidence
inline, names both vendors, and is what :class:`~bfinance.statements.errors.
StatementCapabilityError` quotes. The three traps below are the reason the evidence is recorded
rather than merely remembered.

Trap 1 — trendlyne's five tab pages are decoys, not errors
----------------------------------------------------------
``/fundamentals/balance-sheet/``, ``/fundamentals/cash-flow/``, ``/fundamentals/quarterly-results/``,
``/fundamentals/annual-results/`` and ``/fundamentals/financial-ratios/`` all answer **HTTP 200**
with an ~85 KB shell holding **zero** ``<table>``. They are not 404s and nothing is
robots-Disallowed; they differ only in ``<title>`` and all populate from one XHR. Only
``/fundamentals/financials/{id}/{SYM}/{slug}/`` (SSR, standalone) and
``/fundamentals/get-fundamental_results-v2/{id}/{token}/`` (JSON, both bases) carry data. A
parser pointed at a tab page gets an empty answer that reads as "the vendor has no balance
sheet". The payloads this package accepts are named in :data:`TRENDLYNE_DATA_ROUTES`, and
:func:`bfinance.statements.fundamentals_provider.parse_financials_html` raises
:class:`~bfinance.statements.errors.StatementNotADataPageError` rather than returning that
empty frame.

Trap 2 — three of trendlyne's five tables have byte-identical header rows
------------------------------------------------------------------------
The repo's convention is "identify a table by its header signature, never by heading text", and
that convention is **right**: heading text on this site matches all three section keywords at
once, which is how a corporate-actions parser once returned a bonus ratio of 140.000 and 95
stock splits. It is also **insufficient here**, and the failure is worse than a miss:

============  ===  ==========================================  ===============================
table         #    header                                    discriminable by header?
============  ===  ==========================================  ===============================
quarterly P&L 0    ``Indicator, Graph, Jun '26 …``          yes — no ``CAGR``, no ``TTM``
annual P&L    1    ``Indicator, Graph, CAGR 3 Yrs, …, TTM``  yes — has ``TTM``
balance sheet 2    ``Indicator, Graph, CAGR 3 Yrs, CAGR 5 …`` **no**
ratios        3    **byte-identical to #2**                   **no**
cash flow     4    **byte-identical to #2**                   **no**
============  ===  ==========================================  ===============================

A header signature matches all three of #2/#3/#4 and cannot choose between them. The only
discriminator is the row id in the first cell of a data row: ``CFO_A``/``CFI_A``/``CFA_A``/
``NCF_A`` -> cash flow, ``TA_A``/``TL_A``/``CA_A``/``CL_A`` -> balance sheet, ``ROE_A``/
``ROCE_A``/``BVSH_A`` -> ratios. All five tables match **none** of the eight signatures already
registered in :data:`bfinance.trendlyne.tables.SIGNATURES`.

So: the three specs carry ``identified_by="row_id"`` and a ``note`` that says header
identification is impossible for them, and a test asserts the three header rows really are
byte-identical so nobody re-derives this and gets it wrong. **Do not try to signature-match
them.** Note also that the label is the *second* thing in the cell and the id is the first, and
the label is truncatable and duplicated (``Operating Exp.`` appears twice in the quarterly table
and twice in the annual one) while the id is stable — except that even the id repeats, which is
why :attr:`StatementResult.duplicate_rows` exists.

Trap 3 — ``order.quarter.bs`` and ``order.quarter.cf`` advertise 13 quarters and deliver nothing
-------------------------------------------------------------------------------------------
``body.order.quarter`` lists 13 period labels under **every** key, including ``bs`` and ``cf``.
Every balance-sheet, cash-flow and ratio row id is ``_A``-suffixed (annual), and **zero of the
260** ``_A`` keys appears in ``body.quarterlyDataDump`` — whose 82 keys are 75 ``_Q`` plus
``NP_TTM``, ``EPS_TTM`` and 5 non-numeric. Reading a balance sheet across the quarterly columns
returns **0 of 13 non-null, every cell**; across the annual columns, 13 of 13.

A parser that trusts ``order.quarter`` therefore builds a correctly-shaped frame of nulls and
reports a **verified negative** that is in fact an unasked question. This module refuses it three
ways, all measured:

1. the capability registry has **no provider** for quarterly balance sheet or cash flow, so the
   cascade raises before a payload is read;
2. :func:`bfinance.statements.fundamentals_provider.parse_fundamental_results` takes a statement's
   row set from the **frequency suffix on the row ids present in the dump it is reading**, not
   from ``order``, and an empty row set is
   :class:`~bfinance.statements.errors.StatementEmptyError`;
3. and :class:`~bfinance.statements.errors.StatementEmptyError` is raised by *any* provider whose
   statement has zero non-null cells, so a future shape change cannot reintroduce the same lie
   by a different route.

What is deliberately **not** done here
--------------------------------------
``bfinance.trendlyne.tables.SIGNATURES`` is not read, written or imported. That package is a
**leaf** in this repo — ``tests/test_trendlyne_core.py::TestIsolation`` fails any other module
under ``src/`` that names it — and the two table specs header identification *can* discriminate
would have been natural additions to it. So the classification lives here, next to the statement
knowledge, and the extraction is duplicated rather than shared; the offline suite compares the
two frames cell for cell so the duplication cannot drift. The isolation test also matches the
*substring* ``trendlyne`` in a module name, which is why the second vendor's reader in this
package is called ``fundamentals_provider`` rather than ``trendlyne_provider``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from .model import Basis, Frequency, StatementKind

__all__ = [
    "TableSpec",
    "ProviderSpec",
    "Capability",
    "TABLES",
    "PROVIDERS",
    "CAPABILITIES",
    "UNAVAILABLE",
    "TRENDLYNE_DATA_ROUTES",
    "TRENDLYNE_DECOY_ROUTES",
    "SCREENER_DATA_ROUTE",
    "VENDORS",
    "UPLIMIT_NOTE",
    "table",
    "providers_for",
    "unavailable_reason",
    "capability_report",
    "identify_by_header",
    "identify_by_row_ids",
]


#: The two vendors, named as they must appear in every refusal message.
VENDORS: Tuple[str, str] = ("screener.in", "trendlyne.com")

#: The sentence that makes the refusal actionable. Without it a caller reads "unsupported",
#: files a bug, and waits for a release that is not coming.
UPLIMIT_NOTE = (
    "This is an upstream limit measured on both vendors, not a bfinance gap: it will not be "
    "fixed by upgrading bfinance, and retrying will not help. Source this from the company's own "
    "quarterly filings instead."
)

#: trendlyne's one SSR route and one JSON route. Everything else under ``/fundamentals/`` is a
#: shell (trap 1).
TRENDLYNE_DATA_ROUTES: Tuple[str, ...] = (
    "/fundamentals/financials/{id}/{SYM}/{slug}/",
    "/fundamentals/get-fundamental_results-v2/{id}/{token}/",
)

#: The five tab pages that answer 200 with zero ``<table>``. Kept so the "not a data page"
#: error can name what was fetched instead of saying "wrong page".
TRENDLYNE_DECOY_ROUTES: Tuple[str, ...] = (
    "/fundamentals/balance-sheet/",
    "/fundamentals/cash-flow/",
    "/fundamentals/quarterly-results/",
    "/fundamentals/annual-results/",
    "/fundamentals/financial-ratios/",
)

#: screener.in's company page, which carries all five statement sections in one response.
SCREENER_DATA_ROUTE = "/company/{SYMBOL}/consolidated/"


# --------------------------------------------------------------------------- #
# table identification
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class TableSpec:
    """How to recognise one statement table, and what it is allowed to look like.

    Attributes:
        name: Registry key, also the value written to
            ``attrs["bfinance_statements_identified_by"]``'s sibling
            ``TableSpec.name`` in the not-found error message.
        identified_by: The **single** mechanism sufficient on its own — ``"section"``,
            ``"header"`` or ``"row_id"``. Recorded here rather than in a comment so the next
            person can see, from the registry, which of trendlyne's tables must not be
            signature-matched (trap 2).
        header_signature: Groups of header aliases. **Every** group must be satisfied and a group
            is satisfied by *any* of its aliases. Same semantics as
            :class:`bfinance.trendlyne.tables.TableSignature`, kept separate so importing this
            package does not mutate that package's global registry.
        require_headers: Header cells that must be present, as a single conjunction. Used for
            negatives too — a quarterly P&L forbids ``TTM`` and a balance sheet has no
            ``CAGR`` columns — which is how two tables with the same leading cells are told
            apart.
        forbid_headers: Header cells that must **not** be present.
        row_label_signature: Groups of row-label aliases, the screener.in analogue. screener.in
            puts periods in ``<thead>`` and metric names down the first column, so for that
            vendor the identifying cells are rows, not headers — stated rather than glossed.
        row_ids: trendlyne machine ids, any one of which identifies the table. The only usable
            discriminator for the three byte-identical-header tables.
        section_id: screener.in's ``<section id=…>``. A section id alone fails **silently** into
            an empty frame if the vendor renames it, which is why every spec here also carries a
            signature that fails loudly.
        note: Why the spec is shaped the way it is. Surfaced in error messages.
    """

    name: str
    identified_by: str
    header_signature: Tuple[Tuple[str, ...], ...] = ()
    require_headers: Tuple[str, ...] = ()
    forbid_headers: Tuple[str, ...] = ()
    row_label_signature: Tuple[Tuple[str, ...], ...] = ()
    row_ids: frozenset = frozenset()
    section_id: Optional[str] = None
    note: str = ""


def _sig(*groups: Tuple[str, ...]) -> Tuple[Tuple[str, ...], ...]:
    return tuple(tuple(group) for group in groups)


#: Every statement table this package knows how to recognise, across both vendors.
TABLES: Dict[str, TableSpec] = {
    # ---- screener.in. Identified by section id, guarded by a row-label signature.
    #
    # The row-label groups are the six P&L lines all four of screener's statement sections are
    # checked against, plus the section's own distinctive lines. `Sales+` and friends carry a
    # trailing "+" (the expandable-row marker) which is stripped before matching, so the aliases
    # are written without it.
    "screener_quarters": TableSpec(
        name="screener_quarters",
        identified_by="section",
        require_headers=(),
        forbid_headers=("TTM",),
        row_label_signature=_sig(
            ("Sales",), ("Expenses",), ("Operating Profit",), ("OPM %",),
            ("Net Profit",), ("EPS in Rs",),
        ),
        section_id="quarters",
        note=(
            "The only quarterly table screener.in publishes. 13 quarter columns and no TTM "
            "column; the absence of a TTM header is half the identification, because the annual "
            "P&L section carries an identical set of six row labels."
        ),
    ),
    "screener_profit_loss": TableSpec(
        name="screener_profit_loss",
        identified_by="section",
        require_headers=("TTM",),
        row_label_signature=_sig(
            ("Sales",), ("Expenses",), ("Operating Profit",), ("OPM %",),
            ("Net Profit",), ("EPS in Rs",),
        ),
        section_id="profit-loss",
        note=(
            "Carries 12 annual Mar year-ends plus a trailing TTM column. TTM marks THIS table, "
            "so treating a TTM column as a quarterly marker inverts the semantics — the trailing "
            "two columns of this table are the two most recent fiscal years plus TTM, not two "
            "quarters."
        ),
    ),
    "screener_balance_sheet": TableSpec(
        name="screener_balance_sheet",
        identified_by="section",
        forbid_headers=("TTM",),
        row_label_signature=_sig(
            ("Equity Capital",), ("Total Liabilities",), ("Total Assets",),
        ),
        section_id="balance-sheet",
        note="12 annual Mar year-ends. No quarterly variant exists (see UNAVAILABLE).",
    ),
    "screener_cash_flow": TableSpec(
        name="screener_cash_flow",
        identified_by="section",
        forbid_headers=("TTM",),
        row_label_signature=_sig(
            ("Cash from Operating Activity",), ("Net Cash Flow",), ("Free Cash Flow",),
        ),
        section_id="cash-flow",
        note="12 annual Mar year-ends. No quarterly or TTM variant exists (see UNAVAILABLE).",
    ),
    # ---- trendlyne. Two header-identified, three row-id-identified (trap 2).
    "trendlyne_quarterly_income": TableSpec(
        name="trendlyne_quarterly_income",
        identified_by="header",
        header_signature=_sig(("Indicator",), ("Graph",)),
        forbid_headers=("CAGR 3 Yrs", "CAGR 5 Yrs", "TTM"),
        note=(
            "Header-identified: it is the only one of the five with neither a CAGR column nor a "
            "TTM column. Its two TTM figures arrive as ROWS (NP_TTM, EPS_TTM) of this table, not "
            "as a column, which is why the TTM statement is cross-checked against this table "
            "rather than read from its header."
        ),
    ),
    "trendlyne_annual_income": TableSpec(
        name="trendlyne_annual_income",
        identified_by="header",
        header_signature=_sig(("Indicator",), ("Graph",), ("CAGR 3 Yrs",), ("TTM",)),
        note="The only trendlyne table with a TTM column; that is what tells it from #2/#3/#4.",
    ),
    "trendlyne_balance_sheet": TableSpec(
        name="trendlyne_balance_sheet",
        identified_by="row_id",
        header_signature=_sig(("Indicator",), ("Graph",), ("CAGR 3 Yrs",)),
        row_ids=frozenset({"TA_A", "TL_A", "CA_A", "CL_A"}),
        note=(
            "CANNOT be header-identified. Its header row is byte-identical to the ratios and cash "
            "flow tables (trap 2), and a signature matching all three cannot choose between them. "
            "The only discriminator is the row-id first cell. Do not try to signature-match it."
        ),
    ),
    "trendlyne_ratios": TableSpec(
        name="trendlyne_ratios",
        identified_by="row_id",
        header_signature=_sig(("Indicator",), ("Graph",), ("CAGR 3 Yrs",)),
        row_ids=frozenset({"ROE_A", "ROCE_A", "BVSH_A"}),
        note=(
            "CANNOT be header-identified — byte-identical header to the balance sheet and cash "
            "flow tables (trap 2). Do not try to signature-match it. Registered because it is the "
            "third of the three and the easiest to forget; :class:`StatementKind` does not include "
            "ratios, so this spec exists to make the discrimination total, not to serve a request."
        ),
    ),
    "trendlyne_cash_flow": TableSpec(
        name="trendlyne_cash_flow",
        identified_by="row_id",
        header_signature=_sig(("Indicator",), ("Graph",), ("CAGR 3 Yrs",)),
        row_ids=frozenset({"CFO_A", "CFI_A", "CFA_A", "NCF_A"}),
        note=(
            "CANNOT be header-identified — byte-identical header to the balance sheet and ratios "
            "tables (trap 2). Row-id first cell is the only discriminator. Do not try to "
            "signature-match it."
        ),
    ),
}


def table(name: str) -> TableSpec:
    """Look up a :class:`TableSpec`, with a message naming what is registered."""
    try:
        return TABLES[name]
    except KeyError:
        raise KeyError(
            f"unknown statement table {name!r}. Registered: {', '.join(sorted(TABLES))}."
        ) from None


# --------------------------------------------------------------------------- #
# provider ordering
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ProviderSpec:
    """One way of answering one question, and what it can honestly answer it on.

    Attributes:
        name: The provider's short name and the value on
            ``attrs["bfinance_statements_provider"]``. ``"screener"`` / ``"trendlyne"``.
        payload: Which bytes this provider reads — ``"screener_html"``,
            ``"trendlyne_html"`` or ``"trendlyne_json"``. One vendor can be more than one
            provider, because the two of its routes genuinely differ in what they can serve:
            the SSR page renders **standalone** while the JSON ships **both** bases.
        table: The :class:`TableSpec` this provider selects. ``None`` for the JSON provider,
            which reads a keyed dump and identifies nothing by shape.
        bases: The bases this provider can actually serve. A request for a basis not listed here
            skips the provider **with a recorded reason**; it does not ask and mis-serve.
        note: The measurement behind ``bases``, quoted in skip reasons.
    """

    name: str
    payload: str
    table: Optional[str]
    bases: Tuple[Basis, ...]
    note: str = ""


PROVIDERS: Dict[str, ProviderSpec] = {
    "screener": ProviderSpec(
        name="screener",
        payload="screener_html",
        table=None,  # per (kind, frequency); resolved via _SCREENER_TABLES
        bases=(Basis.CONSOLIDATED,),
        note=(
            "screener.in is consolidated-only on this route: the section tables live on "
            "/company/{SYMBOL}/consolidated/ and the standalone page carries a different set. A "
            "standalone request skips this provider rather than serving consolidated numbers "
            "under a standalone label."
        ),
    ),
    "trendlyne-html": ProviderSpec(
        name="trendlyne-html",
        payload="trendlyne_html",
        table=None,
        bases=(Basis.STANDALONE,),
        note=(
            "The SSR page /fundamentals/financials/ renders STANDALONE while the page's own "
            "XHR reports is_consolidated=true. Measured on MOTHERSON at Mar 2026: the page "
            "prints Total ShareHolders Funds 38,290.1 and the JSON's consolidated dump says "
            "40,979.6 for the same row and period. is_consolidated is a display default, not the "
            "payload's basis, so this provider is registered standalone-only and a consolidated "
            "request must go to the JSON provider."
        ),
    ),
    "trendlyne-json": ProviderSpec(
        name="trendlyne-json",
        payload="trendlyne_json",
        table=None,
        bases=(Basis.CONSOLIDATED, Basis.STANDALONE),
        note=(
            "get-fundamental_results-v2 ships BOTH bases in one response — quarterlyDataDump and "
            "annualDataDump each carry a 'consolidated' and a 'standalone' sub-object. This is "
            "the only route in the whole of bfinance that can serve a standalone statement at "
            "all, which is why it is a separate provider rather than an option on the HTML one."
        ),
    ),
}


@dataclass(frozen=True)
class Capability:
    """One (kind, frequency) cell of the matrix: who can answer it, and why not if nobody can.

    Present for all nine pairs. The four that cannot be served carry
    :attr:`unavailable_reason` and no providers, which is what stops a caller iterating
    :data:`CAPABILITIES` from reading their absence as a missing entry.
    """

    kind: StatementKind
    frequency: Frequency
    provider_names: Tuple[str, ...] = ()
    table_names: Mapping[str, str] = field(default_factory=dict)
    unavailable_reason: Optional[str] = None

    def tables(self) -> Dict[str, str]:
        return dict(self.table_names)

    @property
    def available(self) -> bool:
        """``True`` when at least one registered provider can serve this cell."""
        return bool(self.provider_names)


#: The four cells nobody can answer, each with the measurement that settles it. Declared before
#: :data:`CAPABILITIES` because every cell in that matrix reads its reason from here, which is
#: what makes the matrix complete: all nine (kind, frequency) pairs are keys, and the four that
#: cannot be served carry a reason instead of a provider.
UNAVAILABLE: Dict[Tuple[StatementKind, Frequency], str] = {
    (StatementKind.BALANCE_SHEET, Frequency.QUARTERLY): (
        "Neither vendor publishes a quarterly balance sheet. Measured 2026-09-28.\n"
        "  screener.in: the company page carries section#quarters (13 quarter columns, P&L rows "
        "only) and section#balance-sheet (12 annual Mar year-ends) and no other balance-sheet "
        "table. Its only per-quarter route, /company/source/quarter/*, is Disallow-ed by its own "
        "robots.txt ('Disallow: /company/source/quarter/*') and was not fetched.\n"
        "  trendlyne.com: the /fundamentals/financials/ page ships five tables, of which the "
        "balance sheet is annual-only; /fundamentals/balance-sheet/ is a decoy (HTTP 200, "
        "~84.7 KB, zero <table>); and in the get-fundamental_results-v2 payload "
        "body.order.quarter.bs advertises 13 quarterly periods while all 260 '_A'-suffixed row "
        "keys are absent from body.quarterlyDataDump — reading the balance sheet's 13 row ids "
        "across the advertised quarterly columns gives 0 of 169 non-null, every cell, and across "
        "the annual columns 143 of 143.\n"
        + UPLIMIT_NOTE
    ),
    (StatementKind.CASH_FLOW, Frequency.QUARTERLY): (
        "Neither vendor publishes a quarterly cash flow. Measured 2026-09-28.\n"
        "  screener.in: section#cash-flow carries 12 annual Mar year-ends and no TTM column; "
        "section#quarters holds no cash-flow line item at all. Its only per-quarter route is "
        "robots-disallowed and was not fetched.\n"
        "  trendlyne.com: the cash-flow table on /fundamentals/financials/ is annual-only; "
        "/fundamentals/cash-flow/ is a decoy (HTTP 200, ~84.7 KB, zero <table>); and "
        "body.order.quarter.cf advertises 13 quarterly periods while every cash-flow row key is "
        "'_A'-suffixed and absent from body.quarterlyDataDump — 0 of 52 non-null across the "
        "advertised quarterly columns (4 row ids x 13 periods), 44 of 44 across the annual "
        "columns.\n"
        + UPLIMIT_NOTE
    ),
    (StatementKind.BALANCE_SHEET, Frequency.TTM): (
        "Neither vendor publishes a trailing-twelve-month balance sheet. Measured 2026-09-28.\n"
        "  screener.in: section#balance-sheet has 12 annual Mar columns and no TTM column.\n"
        "  trendlyne.com: only the annual P&L table carries a TTM column. The balance sheet, "
        "ratios and cash-flow tables share a byte-identical header row with no TTM in it, so "
        "there is no TTM column to read; the JSON payload's body.order.annual.bs likewise lists "
        "11 annual periods and no TTM.\n"
        + UPLIMIT_NOTE
    ),
    (StatementKind.CASH_FLOW, Frequency.TTM): (
        "Neither vendor publishes a trailing-twelve-month cash flow. Measured 2026-09-28.\n"
        "  screener.in: section#cash-flow has 12 annual Mar columns and no TTM column.\n"
        "  trendlyne.com: only the annual P&L table carries a TTM column; the cash-flow table's "
        "header is the byte-identical annual one with no TTM in it, and body.order.annual.cf "
        "lists 11 annual periods and no TTM.\n"
        + UPLIMIT_NOTE
    ),
}


def _cell(
    kind: StatementKind,
    frequency: Frequency,
    screener: Optional[str] = None,
    trendlyne_html: Optional[str] = None,
    trendlyne_json: Optional[str] = None,
) -> Capability:
    """One matrix cell. The provider order here **is** the cascade's try-order.

    A cell with no table name is not an oversight — it is one of the four in
    :data:`UNAVAILABLE`, and it is built anyway so that iterating :data:`CAPABILITIES` walks all
    nine pairs rather than silently skipping the four that cannot be served. That is the whole
    defence against a caller (or a future edit) treating a refusal as a gap.
    """
    names: List[str] = []
    tables: Dict[str, str] = {}
    for provider_name, table_name in (
        ("screener", screener),
        ("trendlyne-html", trendlyne_html),
        ("trendlyne-json", trendlyne_json),
    ):
        if table_name is None:
            continue
        names.append(provider_name)
        tables[provider_name] = table_name
    return Capability(
        kind=kind,
        frequency=frequency,
        provider_names=tuple(names),
        table_names=tables,
        unavailable_reason=None if names else UNAVAILABLE[(kind, frequency)],
    )


#: The measured matrix, complete: all three kinds x all three frequencies. Read the module
#: docstring before adding a cell.
CAPABILITIES: Dict[Tuple[StatementKind, Frequency], Capability] = {
    (StatementKind.INCOME, Frequency.ANNUAL): _cell(
        StatementKind.INCOME, Frequency.ANNUAL,
        screener="screener_profit_loss", trendlyne_html="trendlyne_annual_income",
        trendlyne_json="trendlyne_annual_income",
    ),
    (StatementKind.INCOME, Frequency.QUARTERLY): _cell(
        StatementKind.INCOME, Frequency.QUARTERLY,
        screener="screener_quarters", trendlyne_html="trendlyne_quarterly_income",
        trendlyne_json="trendlyne_quarterly_income",
    ),
    (StatementKind.INCOME, Frequency.TTM): _cell(
        StatementKind.INCOME, Frequency.TTM,
        screener="screener_profit_loss", trendlyne_html="trendlyne_annual_income",
        trendlyne_json="trendlyne_annual_income",
    ),
    (StatementKind.BALANCE_SHEET, Frequency.ANNUAL): _cell(
        StatementKind.BALANCE_SHEET, Frequency.ANNUAL,
        screener="screener_balance_sheet", trendlyne_html="trendlyne_balance_sheet",
        trendlyne_json="trendlyne_balance_sheet",
    ),
    (StatementKind.BALANCE_SHEET, Frequency.QUARTERLY): _cell(
        StatementKind.BALANCE_SHEET, Frequency.QUARTERLY,
    ),
    (StatementKind.BALANCE_SHEET, Frequency.TTM): _cell(
        StatementKind.BALANCE_SHEET, Frequency.TTM,
    ),
    (StatementKind.CASH_FLOW, Frequency.ANNUAL): _cell(
        StatementKind.CASH_FLOW, Frequency.ANNUAL,
        screener="screener_cash_flow", trendlyne_html="trendlyne_cash_flow",
        trendlyne_json="trendlyne_cash_flow",
    ),
    (StatementKind.CASH_FLOW, Frequency.QUARTERLY): _cell(
        StatementKind.CASH_FLOW, Frequency.QUARTERLY,
    ),
    (StatementKind.CASH_FLOW, Frequency.TTM): _cell(
        StatementKind.CASH_FLOW, Frequency.TTM,
    ),
}


def providers_for(kind: StatementKind, frequency: Frequency) -> Tuple[ProviderSpec, ...]:
    """The ordered providers for one cell, or ``()`` when the capability does not exist.

    Order is the cascade's try-order and it is a decision, not an accident: screener.in first
    because it is the primary source bfinance is built on, it serves consolidated data, and one
    response carries all five statement sections; trendlyne second as independent redundancy.
    The two trendlyne providers are separate because they differ in basis (see
    :data:`PROVIDERS`), not because one is a backup of the other.
    """
    cell = CAPABILITIES.get((kind, frequency))
    if cell is None or not cell.available:
        return ()
    return tuple(PROVIDERS[name] for name in cell.provider_names)


def unavailable_reason(kind: StatementKind, frequency: Frequency) -> Optional[str]:
    """The measured reason a cell cannot be served, or ``None`` when it can.

    The reason is **data**, not a string built at the raise site, so the registry and the error
    message cannot drift apart.
    """
    return UNAVAILABLE.get((kind, frequency))


def capability_report() -> str:
    """A human-readable dump of the whole matrix, for an error message or a REPL.

    Never claims a provider for a cell that has none — the cell prints its reason instead. This
    is the one place the matrix is rendered, so the table in the module docstring and the answer
    a caller gets cannot disagree.
    """
    header = f"  {'':<14}{'annual':<46}{'quarterly':<46}ttm"
    lines = [
        "provider-agnostic statement capabilities (measured 2026-09-28 against both vendors)",
        header,
    ]
    for kind in StatementKind:
        cells: List[str] = []
        for frequency in (Frequency.ANNUAL, Frequency.QUARTERLY, Frequency.TTM):
            available = providers_for(kind, frequency)
            if available:
                cells.append(", ".join(p.name for p in available))
            else:
                reason = unavailable_reason(kind, frequency) or "no provider registered"
                first_line = reason.strip().splitlines()[0]
                cells.append(f"UNAVAILABLE - {first_line}")
        lines.append(f"  {kind.value:<14}{cells[0]:<46}{cells[1]:<46}{cells[2]}")
    lines.append(
        "  UNAVAILABLE cells name both vendors and the measurement in the raised error; they are "
        "not a missing provider."
    )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# classification primitives, shared by both HTML providers
# --------------------------------------------------------------------------- #


def _normalise(text: Optional[str]) -> str:
    """Whitespace-collapsed, lowercased, edge-punctuation stripped.

    The same folding :func:`bfinance.trendlyne.tables.normalize_header` performs, duplicated
    rather than imported so that a spec here is readable on its own and importing this package
    touches no other module's globals. ``"  CAGR 3 Yrs "`` -> ``"cagr 3 yrs"``.
    """
    if text is None:
        return ""
    return " ".join(str(text).split()).strip().strip(":").strip().lower()


def identify_by_header(headers: Sequence[str], spec: TableSpec) -> bool:
    """Does this header row satisfy ``spec``? All groups, plus the require/forbid cells.

    ``None`` is never returned and no fuzzy matching happens: a renamed column must fail, because
    a fail is a loud report and a near-miss is confident nonsense.
    """
    present = {_normalise(header) for header in headers}
    for group in spec.header_signature:
        if not any(_normalise(alias) in present for alias in group):
            return False
    if any(_normalise(required) not in present for required in spec.require_headers):
        return False
    if any(_normalise(forbidden) in present for forbidden in spec.forbid_headers):
        return False
    return True


def identify_by_row_ids(row_ids: Sequence[str], spec: TableSpec) -> bool:
    """Does any data row carry one of ``spec.row_ids``? The only discriminator for trap 2.

    Keyed on the machine id, never the label: the label is the second thing in the cell, it is
    truncated with an ellipsis on the page, and ``Operating Exp.`` occurs twice in one table.
    """
    if not spec.row_ids:
        return False
    return bool(spec.row_ids & set(row_ids))
