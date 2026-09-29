"""
trendlyne.com — optional, isolated third-party market data.

**This package is a leaf.** Nothing in bfinance imports it, ``bfinance/__init__.py`` does not
import it, and it performs no I/O at import time. If trendlyne is unreachable, rate-limits us,
changes its markup or is taken down, every other bfinance call is unaffected. The only way in is
an explicit import::

    from bfinance.trendlyne import TrendlyneClient

Why a separate top-level package: trendlyne.com is a **third-party analytics site**, not an
exchange source. Its numbers are derived and republished and its routes are undocumented, so
folding it into ``bfinance/nse/`` would imply a provenance it does not have. Its price basis is
**as-traded, back-adjusted for splits and bonuses only, demergers NOT applied**, which is neither
bhavcopy's basis nor yfinance's, and the module docstring carries the measurement. Read
:mod:`bfinance.trendlyne.client`'s module docstring before trusting a number from here — in
particular the price-basis warning and the fact that trendlyne's per-symbol OHLC token is the
only route to its chart data.

What is here
------------
- :mod:`~bfinance.trendlyne.client` — HTTP client, symbol index, token resolution, pacing, and
  the daily OHLCV frame. This is the working part.
- :mod:`~bfinance.trendlyne.sectors` — sector and industry support: the classification every OHLC
  frame already carries, the complete sector/industry taxonomy, and the constituent list with
  exact market-cap weights. The module docstring records why the market-cap **unit** is not
  asserted, and what was measured instead.
- :mod:`~bfinance.trendlyne.tables` — server-rendered table extraction classified by **header
  signature**, not by heading text. Usable against any saved page, and currently the only way to
  reach the section tables: no route in this package fetches one yet.
- :mod:`~bfinance.trendlyne.errors` — the typed failures. Every one is a
  :class:`~bfinance.trendlyne.errors.TrendlyneError`, and also a member of the closest shared
  ``BFinanceError`` hierarchy, so ``except UpstreamServiceError`` still catches them.

**That is the whole package.** There is no ``ownership``, ``deals`` or ``corporate`` module: the
remaining section tables trendlyne serves are written up in ``docs/UPSTREAM_SOURCES.md`` and not
yet extracted, and nothing in this package refers to code that does not exist. A caller who wants
one today composes :meth:`~bfinance.trendlyne.client.TrendlyneClient.fetch_text` with
:func:`~bfinance.trendlyne.tables.find_table` directly.

One route is opt-in, and off
---------------------------
trendlyne's ``robots.txt`` disallows ``/member/api/ac_snames/`` — trendlyne's own autocomplete —
under the broad rule ``Disallow: */member/``. It is the only resolver for **every ETF**, and the
equity sitemap carries none: its 5,922 entries hold equities, BSE index families and one bond
index, and nothing with ``BEES`` or ``ETF`` in the symbol segment. So
:meth:`~bfinance.trendlyne.client.TrendlyneClient.search_remote` raises
:class:`~bfinance.trendlyne.errors.TrendlyneRobotsDisallowedError` unless a caller passes
``allow_member_search=True``, and :meth:`~bfinance.trendlyne.client.TrendlyneClient.resolve_any`
**skips the fallback explicitly and records it** rather than reporting the instrument as unknown.
The consequence is stated rather than hidden: with the default configuration, ETFs are not
reachable through this package. :data:`~bfinance.trendlyne.client.ROBOTS_DISALLOWED_ROUTES` holds
the rules, and the 28 ``Disallow`` entries that were read to produce it are listed there.

A second route behind a second opt-in
-------------------------------
trendlyne also serves a **richer** autocomplete at ``/member/api/ac_snames/all/``, which returns 22
fields where the first returns 4, including a display name and the vendor's own ``category``.
:meth:`~bfinance.trendlyne.client.TrendlyneClient.search_full` reads it, and needs
**both** ``allow_member_search=True`` **and** ``allow_full_search=True`` (:data:`~bfinance.trendlyne.client.FULL_SEARCH_FLAG`).
Opening one without the other still refuses, and the message names the one that is actually missing.

It exists because the categories are not decoration. Measured across 136 distinct
``(category, symbol)`` pairs on 2026-09-30, two of the six categories trendlyne returns **cannot** be
resolved to a price history at all: ``Mutual Funds`` is a separate namespace carrying NAV, and
``Not Traded`` is delisted. So :class:`~bfinance.trendlyne.client.SymbolCategory` is **carried on
the result**, not used to filter (a filtered list would look exhaustive and would not be), and
:meth:`~bfinance.trendlyne.client.SymbolCategory.has_ohlc` says plainly which categories this
package can fetch bars for. An unrecognised category is reported as unknown, never guessed into
``equity``.

**It is not a resolver.** trendlyne's search is fuzzy and ranked, which is the defect commit
``06f6c7c`` fixed; this method returns candidates for a human to choose from, and it is deliberately
not wired into ``resolve_any``.

Nothing is fabricated. A table that is absent raises, a value that is missing stays missing, and
every returned frame carries its provenance and a note on what it does not contain.
"""

from .client import (
    BASE_URL,
    COLUMNS,
    DEFAULT_MIN_REQUEST_INTERVAL_MS,
    DEMERGER_APPLIED,
    DEMERGER_APPLIED_ATTR,
    ENDPOINTS,
    FULL_SEARCH_FLAG,
    GROUP_ENDPOINTS,
    LISTING_DATE_ATTR,
    MEMBER_SEARCH_FLAG,
    METRICS,
    NOTE_ATTR,
    PRICE_BASIS,
    PRICE_BASIS_ATTR,
    PROVENANCE,
    RETRIEVED_AT_ATTR,
    ROBOTS_DISALLOWED_ROUTES,
    ROWS_BEFORE_LISTING_ATTR,
    SITEMAP_PATH,
    SNAPSHOT_ONLY_ATTR,
    SNAPSHOT_PERIOD,
    SOURCE_ATTR,
    SymbolCategory,
    VOLUME_BEFORE_LISTING_ATTR,
    WINDOW_ATTR,
    TrendlyneClient,
    TrendlyneSymbol,
    build_attrs,
    build_path,
    ohlc,
    ohlc_async,
    parse_envelope,
    parse_ohlc,
    parse_sitemap,
    parse_symbol_category,
    redact,
    resolve,
    symbol_index,
    validate_period,
)
from .errors import (
    TrendlyneAccessDeniedError,
    TrendlyneError,
    TrendlyneHTTPError,
    TrendlyneNotAuthorizedError,
    TrendlyneNotFoundError,
    TrendlyneRateLimitedError,
    TrendlyneRobotsDisallowedError,
    TrendlyneSchemaError,
    TrendlyneServerError,
    TrendlyneSymbolNotFoundError,
    TrendlyneTableNotFoundError,
    TrendlyneTokenError,
    TrendlyneTransportError,
)
from .sectors import (
    AGGREGATE_SIG,
    CONSTITUENTS_SIG,
    SUSPICIOUS_ROW_MULTIPLE,
    TAXONOMY_SELECT_ID,
    ConstituentResult,
    TrendlyneGroup,
    constituent_attrs,
    declared_group,
    parse_classification,
    parse_constituents,
    parse_industry_aggregate,
    parse_taxonomy,
    sparkline_points,
    taxonomy_counts,
)
from .tables import (
    SIGNATURES,
    TableSignature,
    classify_table,
    extract_ohlc_token,
    find_ohlc_tokens,
    find_table,
    find_table_elements,
    find_tables,
    known_signatures,
    normalize_header,
    page_signatures,
    register_signature,
)

__all__ = [
    # client
    "BASE_URL",
    "COLUMNS",
    "DEFAULT_MIN_REQUEST_INTERVAL_MS",
    "DEMERGER_APPLIED",
    "DEMERGER_APPLIED_ATTR",
    "ENDPOINTS",
    "FULL_SEARCH_FLAG",
    "GROUP_ENDPOINTS",
    "LISTING_DATE_ATTR",
    "MEMBER_SEARCH_FLAG",
    "METRICS",
    "NOTE_ATTR",
    "PRICE_BASIS",
    "PRICE_BASIS_ATTR",
    "PROVENANCE",
    "RETRIEVED_AT_ATTR",
    "ROBOTS_DISALLOWED_ROUTES",
    "ROWS_BEFORE_LISTING_ATTR",
    "SITEMAP_PATH",
    "SNAPSHOT_ONLY_ATTR",
    "SNAPSHOT_PERIOD",
    "SOURCE_ATTR",
    "SymbolCategory",
    "VOLUME_BEFORE_LISTING_ATTR",
    "WINDOW_ATTR",
    "TrendlyneClient",
    "TrendlyneSymbol",
    "build_attrs",
    "build_path",
    "ohlc",
    "ohlc_async",
    "parse_envelope",
    "parse_ohlc",
    "parse_sitemap",
    "parse_symbol_category",
    "redact",
    "resolve",
    "symbol_index",
    "validate_period",
    # tables
    "SIGNATURES",
    "TableSignature",
    "classify_table",
    "extract_ohlc_token",
    "find_ohlc_tokens",
    "find_table",
    "find_table_elements",
    "find_tables",
    "known_signatures",
    "normalize_header",
    "page_signatures",
    "register_signature",
    # sectors
    "AGGREGATE_SIG",
    "CONSTITUENTS_SIG",
    "SUSPICIOUS_ROW_MULTIPLE",
    "TAXONOMY_SELECT_ID",
    "ConstituentResult",
    "TrendlyneGroup",
    "constituent_attrs",
    "declared_group",
    "parse_classification",
    "parse_constituents",
    "parse_industry_aggregate",
    "parse_taxonomy",
    "sparkline_points",
    "taxonomy_counts",
    # errors
    "TrendlyneError",
    "TrendlyneHTTPError",
    "TrendlyneAccessDeniedError",
    "TrendlyneNotAuthorizedError",
    "TrendlyneNotFoundError",
    "TrendlyneRateLimitedError",
    "TrendlyneRobotsDisallowedError",
    "TrendlyneServerError",
    "TrendlyneTransportError",
    "TrendlyneSymbolNotFoundError",
    "TrendlyneTokenError",
    "TrendlyneSchemaError",
    "TrendlyneTableNotFoundError",
]
