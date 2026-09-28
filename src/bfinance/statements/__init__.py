"""
bfinance.statements — one provider-agnostic interface over two independent statement sources.

**Opt-in, like :mod:`bfinance.trendlyne`.** ``bfinance/__init__.py`` does not import this
package, nothing in the main path imports it, and importing it opens no socket. It is reached
only by an explicit::

    from bfinance.statements import resolve, StatementKind, Frequency

What it is for
--------------
screener.in and trendlyne.com both publish a quarterly income statement and a trailing-twelve-month
income statement, and they publish them **independently** — two different vendors, two different
routes, no shared failure mode. That is the condition under which a failover is real redundancy
rather than ceremony, and this package is the thing that makes one call use both. Everything else
here exists because the two disagree, and every disagreement is measured rather than guessed:

============================  =======================================================
the seam                      what was measured, and what this package does about it
============================  =======================================================
**period labels**             trendlyne writes ``'Jun 2026'`` in JSON and ``"Jun '26"`` on
                              the page, in the same response. One canonical form
                              (:mod:`~bfinance.statements.periods`), converted explicitly, or a
                              silent outer join that drops every row.
**accounting basis**          screener.in is consolidated-only on this route. trendlyne ships
                              **both** bases in one request, and its SSR page renders
                              **standalone** while its own XHR says ``is_consolidated: true`` —
                              38,290.1 vs 40,979.6 for MOTHERSON at Mar 2026, same row, same
                              period. Every result records the basis it is on; a cross-provider
                              result on a basis other than the one requested is **flagged**;
                              nothing is ever blended or relabelled.
**row identity**              trendlyne's first cell holds a machine id followed by display
                              text. The label is truncated and duplicated; the id is the key —
                              and the three tables whose headers are byte-identical can only be
                              told apart by it.
**cell formatting**           the pages carry pre-formatted strings. ``'-'`` must not become
                              ``NaN`` and ``'3,827.7'`` must not lose its separator, or every
                              number in the statement is quietly wrong.
============================  =======================================================

The three traps, and what is done about each
--------------------------------------------
The full derivation is ``.scratch/build-2026-09-28/D3-QUARTERLY-STATEMENTS.md``; the module
docstrings carry the evidence. In short:

1. **trendlyne's five tab pages are decoys.** ``/fundamentals/balance-sheet/`` and its four
   siblings answer **HTTP 200** with an ~85 KB shell holding **zero** ``<table>``. They are not
   404s and nothing is robots-disallowed; they differ only in ``<title>`` and populate from one
   XHR. A parser pointed at one gets an empty frame that reads as "the vendor has no data", which
   is a false negative about the vendor. Reading one raises
   :class:`~bfinance.statements.errors.StatementNotADataPageError` naming the two routes that do
   carry data.
2. **Three of trendlyne's five tables have byte-identical header rows.** The repo's convention of
   identifying a table by its header signature is right and is followed wherever it works — but
   it is *insufficient* here, and a signature that matches all three of balance sheet, ratios and
   cash flow cannot choose between them. Those three are keyed on row ids, and
   :class:`~bfinance.statements.registry.TableSpec` **records that in the registry** so the next
   person does not try to signature-match them.
3. **``order.quarter.bs`` and ``order.quarter.cf`` advertise 13 quarterly periods and hold
   nothing.** Every balance-sheet, cash-flow and ratio row id is ``_A``-suffixed and **zero of
   the 260** appears in ``quarterlyDataDump``; reading them across the advertised quarterly
   columns gives **0 of 13 non-null, every cell**. This package never takes a period set from
   ``order`` — it takes the *set* from the dump and uses ``order`` only for ordering — and any
   statement that comes back with no number in any cell is
   :class:`~bfinance.statements.errors.StatementEmptyError` rather than a frame of nulls.

The refusal, which is a success
-------------------------------
**Quarterly balance sheet, quarterly cash flow, TTM balance sheet and TTM cash flow are not
available from any reachable source.** Both vendors were probed and the arithmetic is recorded in
:data:`~bfinance.statements.registry.UNAVAILABLE`. So those four cells raise
:class:`~bfinance.statements.errors.StatementCapabilityError` before any payload is read, with a
message that names both vendors, states that the limit is measured rather than assumed, and says
plainly that a later bfinance will not produce it. ``attrs`` claims nothing about them.

That is the correct outcome, not a gap. A package that fabricates a quarterly balance sheet is
worse than one that says it cannot get one — and the trap above is exactly what a fabricator
would have fallen into.

What is here
------------
- :mod:`~bfinance.statements.registry` — **read this first.** The measured capability matrix, the
  table-identification registry (including trap 2), and the refusal evidence.
- :mod:`~bfinance.statements.cascade` — the try-order, the fall-through, and the basis decision.
- :mod:`~bfinance.statements.model` — :class:`~bfinance.statements.model.StatementRequest` and
  :class:`~bfinance.statements.model.StatementResult`, with the ``attrs`` contract.
- :mod:`~bfinance.statements.periods` — one canonical period label, and the conversion into it.
- :mod:`~bfinance.statements.screener_provider` /
  :mod:`~bfinance.statements.fundamentals_provider` — the three payload readers, from the two
  vendors' pages and the JSON route behind the second vendor's tabs.
- :mod:`~bfinance.statements.errors` — the typed failures, each also in the closest
  ``bfinance.utils.exceptions`` hierarchy so existing ``except UpstreamServiceError`` handlers
  still work.

Two naming and structure decisions worth knowing about
-----------------------------------------------------
* The second vendor's module is :mod:`~bfinance.statements.fundamentals_provider`, named after
  the ``/fundamentals/`` routes it reads rather than after the vendor. That package is a **leaf**
  in this repo — ``tests/test_trendlyne_core.py::TestIsolation`` scans the AST of every other
  module under ``src/`` and fails on any reference to it — and this package reads its pages, so
  it does its own table extraction rather than importing that one. The two are compared cell for
  cell in the offline suite, so the duplication cannot drift.
* No URL is ever written to ``attrs``: the JSON route is addressed by a per-symbol token, and a
  credential has no business in a frame, a log line or a CSV export.
"""

from .cascade import (
    PAYLOAD_PARSE,
    capability_error_message,
    parse_payload,
    resolve,
    resolve_from_payloads,
)
from .errors import (
    PeriodLabelError,
    StatementCapabilityError,
    StatementEmptyError,
    StatementError,
    StatementNotADataPageError,
    StatementSchemaError,
    StatementSourceError,
    StatementTableError,
)
from .fundamentals_provider import (
    classify_html_tables,
    html_row_ids,
    html_table_headers,
    parse_financials_html,
    parse_fundamental_results,
)
from .model import (
    ATTR_BASIS,
    ATTR_BASIS_MISMATCH,
    ATTR_BASIS_NOTE,
    ATTR_BASIS_REQUESTED,
    ATTR_DROPPED_ROWS,
    ATTR_DUPLICATE_ROWS,
    ATTR_FALLBACK,
    ATTR_FALLBACK_FROM,
    ATTR_FREQUENCY,
    ATTR_IDENTIFIED_BY,
    ATTR_IDENTIFY_NOTE,
    ATTR_KEYS,
    ATTR_KIND,
    ATTR_OMITTED_ROWS,
    ATTR_PAYLOAD,
    ATTR_PERIODS,
    ATTR_PROVIDER,
    ATTR_PROVIDERS_TRIED,
    ATTR_SKIP_REASONS,
    ATTR_SYMBOL,
    ATTR_UNITS_NOTE,
    PROVENANCE,
    Basis,
    Frequency,
    StatementKind,
    StatementRequest,
    StatementResult,
)
from .periods import (
    CANONICAL_RE,
    MONTHS,
    TTM,
    canonical_period,
    expand_short_year,
    is_canonical_period,
    order_periods,
    period_sort_key,
    try_canonical_period,
)
from .registry import (
    CAPABILITIES,
    PROVIDERS,
    SCREENER_DATA_ROUTE,
    TABLES,
    TRENDLYNE_DATA_ROUTES,
    TRENDLYNE_DECOY_ROUTES,
    UNAVAILABLE,
    VENDORS,
    Capability,
    ProviderSpec,
    TableSpec,
    capability_report,
    identify_by_header,
    identify_by_row_ids,
    providers_for,
    table,
    unavailable_reason,
)
from .screener_provider import parse_company_page

__all__ = [
    # entry points
    "resolve",
    "resolve_from_payloads",
    "parse_payload",
    "parse_company_page",
    "parse_financials_html",
    "parse_fundamental_results",
    "classify_html_tables",
    "html_row_ids",
    "html_table_headers",
    "capability_error_message",
    # model
    "Basis",
    "Frequency",
    "StatementKind",
    "StatementRequest",
    "StatementResult",
    "PROVENANCE",
    "ATTR_KEYS",
    "ATTR_BASIS",
    "ATTR_BASIS_MISMATCH",
    "ATTR_BASIS_NOTE",
    "ATTR_BASIS_REQUESTED",
    "ATTR_DROPPED_ROWS",
    "ATTR_DUPLICATE_ROWS",
    "ATTR_FALLBACK",
    "ATTR_FALLBACK_FROM",
    "ATTR_FREQUENCY",
    "ATTR_IDENTIFIED_BY",
    "ATTR_IDENTIFY_NOTE",
    "ATTR_KIND",
    "ATTR_OMITTED_ROWS",
    "ATTR_PAYLOAD",
    "ATTR_PERIODS",
    "ATTR_PROVIDER",
    "ATTR_PROVIDERS_TRIED",
    "ATTR_SKIP_REASONS",
    "ATTR_SYMBOL",
    "ATTR_UNITS_NOTE",
    # registry
    "CAPABILITIES",
    "PROVIDERS",
    "TABLES",
    "UNAVAILABLE",
    "VENDORS",
    "SCREENER_DATA_ROUTE",
    "TRENDLYNE_DATA_ROUTES",
    "TRENDLYNE_DECOY_ROUTES",
    "Capability",
    "ProviderSpec",
    "TableSpec",
    "capability_report",
    "identify_by_header",
    "identify_by_row_ids",
    "providers_for",
    "table",
    "unavailable_reason",
    "PAYLOAD_PARSE",
    # periods
    "CANONICAL_RE",
    "MONTHS",
    "TTM",
    "canonical_period",
    "expand_short_year",
    "is_canonical_period",
    "order_periods",
    "period_sort_key",
    "try_canonical_period",
    # errors
    "PeriodLabelError",
    "StatementCapabilityError",
    "StatementEmptyError",
    "StatementError",
    "StatementNotADataPageError",
    "StatementSchemaError",
    "StatementSourceError",
    "StatementTableError",
]
