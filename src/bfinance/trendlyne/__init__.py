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
- :mod:`~bfinance.trendlyne.tables` — server-rendered table extraction classified by **header
  signature**, not by heading text. Usable against any saved page, and currently the only way to
  reach the section tables: no route in this package fetches one yet.
- :mod:`~bfinance.trendlyne.errors` — the typed failures. Every one is a
  :class:`~bfinance.trendlyne.errors.TrendlyneError`, and also a member of the closest shared
  ``BFinanceError`` hierarchy, so ``except UpstreamServiceError`` still catches them.

**That is the whole package.** There is no ``ownership``, ``deals`` or ``corporate`` module: the
section tables trendlyne serves are written up in ``docs/UPSTREAM_SOURCES.md`` and not yet
extracted, and nothing in this package refers to code that does not exist. A caller who wants one
today composes :meth:`~bfinance.trendlyne.client.TrendlyneClient.fetch_text` with
:func:`~bfinance.trendlyne.tables.find_table` directly.

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
    METRICS,
    NOTE_ATTR,
    PRICE_BASIS,
    PRICE_BASIS_ATTR,
    PROVENANCE,
    RETRIEVED_AT_ATTR,
    SITEMAP_PATH,
    SNAPSHOT_ONLY_ATTR,
    SNAPSHOT_ONLY_ENDPOINTS,
    SNAPSHOT_PERIOD,
    SOURCE_ATTR,
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
    TrendlyneSchemaError,
    TrendlyneServerError,
    TrendlyneSnapshotOnlyError,
    TrendlyneSymbolNotFoundError,
    TrendlyneTableNotFoundError,
    TrendlyneTokenError,
    TrendlyneTransportError,
)
from .tables import (
    SIGNATURES,
    TableSignature,
    classify_table,
    extract_ohlc_token,
    find_ohlc_tokens,
    find_table,
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
    "METRICS",
    "NOTE_ATTR",
    "PRICE_BASIS",
    "PRICE_BASIS_ATTR",
    "PROVENANCE",
    "RETRIEVED_AT_ATTR",
    "SITEMAP_PATH",
    "SNAPSHOT_ONLY_ATTR",
    "SNAPSHOT_ONLY_ENDPOINTS",
    "SNAPSHOT_PERIOD",
    "SOURCE_ATTR",
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
    "find_tables",
    "known_signatures",
    "normalize_header",
    "page_signatures",
    "register_signature",
    # errors
    "TrendlyneError",
    "TrendlyneHTTPError",
    "TrendlyneAccessDeniedError",
    "TrendlyneNotAuthorizedError",
    "TrendlyneNotFoundError",
    "TrendlyneRateLimitedError",
    "TrendlyneServerError",
    "TrendlyneTransportError",
    "TrendlyneSymbolNotFoundError",
    "TrendlyneTokenError",
    "TrendlyneSchemaError",
    "TrendlyneTableNotFoundError",
    "TrendlyneSnapshotOnlyError",
]
