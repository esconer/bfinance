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
  signature**, not by heading text, plus the structural extractors for the pages whose tables share a
  header row or carry rotating month labels. Usable against any saved page, and the parser behind the
  client's section methods below.
- :mod:`~bfinance.trendlyne.smartoptions` — the option-chain client for the separate host
  **smartoptions.trendlyne.com**, reached over a separate connection with its own crawl-delay.
  Off by default behind ``allow_smart_options``; see below.
- :mod:`~bfinance.trendlyne.errors` — the typed failures. Every one is a
  :class:`~bfinance.trendlyne.errors.TrendlyneError`, and also a member of the closest shared
  ``BFinanceError`` hierarchy, so ``except UpstreamServiceError`` still catches them.

There is no ``ownership``, ``deals`` or ``corporate`` module: nothing in this package refers to code
that does not exist. The section routes trendlyne serves that are **not** wired are written up in
``docs/UPSTREAM_SOURCES.md`` — the consensus/forecaster page and ``/equity/delivery-analysis/`` are
client-rendered with no clean JSON sibling on the day this was measured (2026-10-10), and the per-holder
``/equity/holding-overtime/`` routes have not been measured yet. Everything measured and clean — corporate
actions, bulk/block deals, shareholding, board meetings, research reports, latest news, stock alerts,
insider/SAST, monthly MF holdings, the buy/sell zone and the SWOT fact tables — is fetchable through
:mod:`~bfinance.trendlyne.client` and parseable through :func:`~bfinance.trendlyne.tables.find_table`.
A caller who wants a page no method covers yet composes
:meth:`~bfinance.trendlyne.client.TrendlyneClient.fetch_text` with that parser directly.

SmartOptions: a second host, behind a second opt-in
---------------------------------------------------
``smartoptions.trendlyne.com`` serves the option chain for one symbol at one expiry — strike-wise
OI, IV, delta/gamma/vega/rho/theta, max pain, ATM and MWPL — as public JSON with no login.
:meth:`~bfinance.trendlyne.smartoptions.SmartOptionsClient.option_chain` reads it, and is **off by
default**: pass ``allow_smart_options=True`` or the call raises
:class:`~bfinance.trendlyne.errors.TrendlyneSmartOptionsError` before a socket is opened.

That gate is **not** a robots gate, and the distinction is worth stating rather than blurring. The
host's own ``robots.txt`` (read 2026-10-10) disallows only ``*/my-alerts/*`` and
``*/strategy-builder/user/`` under ``User-agent: *``, with ``Crawl-delay: 1`` — and the chain route
matches neither rule. It is off because an operator recorded on 2026-10-10 that SmartOptions ships
as a **secondary** F&O source: a different host, a different contract, and a provenance tag of
``"smartoptions"`` rather than ``"trendlyne"``. Both opt-ins here are cheap, explicit and
reversible; a caller who wants the chain asks for it by name.

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
    TrendlyneSmartOptionsError,
    TrendlyneSymbolNotFoundError,
    TrendlyneTableNotFoundError,
    TrendlyneTokenError,
    TrendlyneTransportError,
    TrendlyneWafChallengeError,
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
from .smartoptions import (
    OPTION_CHAIN_PATH,
    SMART_OPTIONS_FLAG,
    OptionChainResult,
    SmartOptionsClient,
    parse_option_chain,
    validate_expiry,
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
    # smartoptions
    "OPTION_CHAIN_PATH",
    "SMART_OPTIONS_FLAG",
    "OptionChainResult",
    "SmartOptionsClient",
    "parse_option_chain",
    "validate_expiry",
    # errors
    "TrendlyneError",
    "TrendlyneHTTPError",
    "TrendlyneAccessDeniedError",
    "TrendlyneNotAuthorizedError",
    "TrendlyneNotFoundError",
    "TrendlyneRateLimitedError",
    "TrendlyneRobotsDisallowedError",
    "TrendlyneServerError",
    "TrendlyneSmartOptionsError",
    "TrendlyneTransportError",
    "TrendlyneSymbolNotFoundError",
    "TrendlyneTokenError",
    "TrendlyneSchemaError",
    "TrendlyneTableNotFoundError",
    "TrendlyneWafChallengeError",
]
