"""
NSE-official market data clients.

Three independent official NSE routes, verified 2026-09-27. They are **complementary, not
interchangeable** — see each module's docstring for the exact coverage boundary.

- :mod:`bfinance.nse.marketlens` — close + volume only, no O/H/L, 30 years deep.
- :mod:`bfinance.nse.bhavcopy` — full truthful OHLCV from NSE's own daily archives.
- :mod:`bfinance.nse.bhavcopy.UDIFF_CUTOFF` — where the UDiFF route takes over from legacy.

``bfinance.nse.bhavcopy`` is deliberately **not** re-exported here. The ticket-mandated function
and the module share a name, and a package attribute can only be one of them: re-exporting the
function would turn ``bfinance.nse.bhavcopy`` from a module into a callable and make
``bfinance.nse.bhavcopy.parse_bhavcopy`` an ``AttributeError``. Keeping the module wins, so use::

    from bfinance.nse.bhavcopy import bhavcopy, bhavcopy_range
"""

from .bhavcopy import (
    COLUMNS,
    EXCHANGES,
    MISSING_DATES_ATTR,
    PROVENANCE,
    SYNTHETIC_OHLC_ATTR,
    UDIFF_CUTOFF,
    BhavcopyClient,
    archive_url,
    bhavcopy_range,
    empty_bhavcopy_frame,
    is_weekday,
    iter_trading_days,
    parse_bhavcopy,
    unzip_bhavcopy,
    validate_exchange,
    validate_series,
)
from .marketlens import (
    INTRADAY_PERIOD,
    PERIODS,
    MarketLensClient,
    intraday,
    intraday_async,
    price_history,
    price_history_async,
)

__all__ = [
    "PERIODS",
    "INTRADAY_PERIOD",
    "MarketLensClient",
    "price_history",
    "price_history_async",
    "intraday",
    "intraday_async",
    # bhavcopy
    "COLUMNS",
    "EXCHANGES",
    "MISSING_DATES_ATTR",
    "PROVENANCE",
    "SYNTHETIC_OHLC_ATTR",
    "UDIFF_CUTOFF",
    "BhavcopyClient",
    "archive_url",
    "bhavcopy_range",
    "empty_bhavcopy_frame",
    "is_weekday",
    "iter_trading_days",
    "parse_bhavcopy",
    "unzip_bhavcopy",
    "validate_exchange",
    "validate_series",
]
