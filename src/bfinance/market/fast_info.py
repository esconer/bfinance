"""
FastInfo container matching yfinance 0.2+ FastInfo interface.
Provides fast, lightweight scalar properties without heavy scraping.

It is also a read-only mapping (``keys``/``items``/``values``/``get``/``in``/``len``), which
is what makes ``dict(ticker.fast_info)`` work as it does in yfinance 1.7.0. The mapping and
``to_dict()`` are the same set of keys, by construction: both are driven by ``_FAST_INFO_KEYS``
below, and ``__getitem__`` refuses a key that is not in it.
"""

from typing import Any, Iterator, Optional
from bfinance.models.company import CompanyProfile
from bfinance.market.quotes import (
    average_volume,
    fractional_change_from_history,
    moving_average,
    previous_close_from_history,
    resolve_exchange,
)


#: The mapping's keys, in ``to_dict()`` order, and the single source of truth for it.
#: A property that is not listed here is not part of the mapping -- that is the declared
#: contract, not an omission -- and ``__getitem__`` raises ``KeyError`` for it rather than
#: exposing a name ``keys()`` never promised.
_FAST_INFO_KEYS = (
    "currency",
    "exchange",
    "timezone",
    "quote_type",
    "last_price",
    "regular_market_price",
    "last_volume",
    "previous_close",
    "regular_market_previous_close",
    "open",
    "day_high",
    "day_low",
    "year_high",
    "year_low",
    "market_cap",
    "shares",
    "fifty_day_average",
    "two_hundred_day_average",
    "ten_day_average_volume",
    "three_month_average_volume",
    "year_change",
)


class FastInfo:
    """
    Lightweight accessor for real-time market data and basic stats.
    Matches yfinance `ticker.fast_info` attributes.
    """

    def __init__(self, profile: CompanyProfile, latest_price: Optional[float] = None,
                 history: Any = None):
        self._profile = profile
        self._r = profile.ratios
        self._cmp = self._r.current_price or latest_price or 0.0
        # Already-fetched chart/history series; no extra network fan-out.
        self._history = history

    @property
    def currency(self) -> str:
        return "INR"

    @property
    def exchange(self) -> str:
        return resolve_exchange(self._profile.symbol or "")

    @property
    def timezone(self) -> str:
        return "Asia/Kolkata"

    @property
    def quote_type(self) -> str:
        name = (self._profile.name or "").upper()
        sym = (self._profile.symbol or "").upper()
        if "REIT" in name or "REIT" in sym or "REAL ESTATE INVESTMENT TRUST" in name:
            return "REIT"
        if "INVIT" in name or "INVIT" in sym or "INFRASTRUCTURE INVESTMENT TRUST" in name:
            return "INVIT"
        if any(k in name or k in sym for k in ["ETF", "BEES", "INDEX FUND", "FOF", "SCHEME", "MUTUAL FUND"]):
            return "ETF"
        # Structural signal: if company has no P&L and has Fund/Trust in name
        if not self._profile.profit_loss.rows and any(k in name for k in ["FUND", "TRUST", "INDEX", "GROWTH", "GOLD", "SILVER"]):
            return "ETF"
        return "EQUITY"

    @property
    def last_price(self) -> float:
        return self._cmp

    @property
    def regular_market_price(self) -> float:
        """yfinance alias for last_price (finengine reads this as fallback)."""
        return self._cmp

    @property
    def last_volume(self) -> Optional[int]:
        # Volume only from DataFrame history; None otherwise (honestly unavailable).
        try:
            import pandas as pd

            if self._history is None or not isinstance(self._history, pd.DataFrame):
                return None
            if "Volume" not in self._history.columns or len(self._history) == 0:
                return None
            return int(self._history["Volume"].iloc[-1])
        except Exception:
            return None

    @property
    def previous_close(self) -> Optional[float]:
        if self._history is None:
            return None
        return previous_close_from_history(self._history)

    @property
    def regular_market_previous_close(self) -> Optional[float]:
        """yfinance's name for :attr:`previous_close`; same number, same source."""
        return self.previous_close

    @property
    def open(self) -> Optional[float]:
        return None  # intraday unavailable from EOD history

    @property
    def day_high(self) -> Optional[float]:
        return None  # intraday unavailable from EOD history

    @property
    def day_low(self) -> Optional[float]:
        return None  # intraday unavailable from EOD history

    @property
    def year_high(self) -> Optional[float]:
        return self._r.high_52w

    @property
    def year_low(self) -> Optional[float]:
        return self._r.low_52w

    @property
    def market_cap(self) -> Optional[float]:
        return (self._r.market_cap * 1e7) if self._r.market_cap else None

    @property
    def shares(self) -> Optional[int]:
        mcap = self.market_cap
        return int(mcap / self._cmp) if (mcap and self._cmp > 0) else None

    @property
    def fifty_day_average(self) -> Optional[float]:
        if self._history is None:
            return None
        return moving_average(self._history, 50)

    @property
    def two_hundred_day_average(self) -> Optional[float]:
        if self._history is None:
            return None
        return moving_average(self._history, 200)

    @property
    def ten_day_average_volume(self) -> Optional[int]:
        """Mean of the last 10 daily Volume bars; None when the frame has fewer than 10.

        yfinance's ``ten_day_average_volume``, computed off the already-fetched frame (no
        extra request). The window is a bar count and is not shortened on a short frame.
        """
        return average_volume(self._history, 10)

    @property
    def three_month_average_volume(self) -> Optional[int]:
        """Mean of the last 63 daily Volume bars; None when the frame has fewer than 63.

        ``3mo`` is 63 **trading** bars here, not 63 calendar days: the frame is daily OHLCV
        and bars are what it actually contains.
        """
        return average_volume(self._history, 63)

    @property
    def year_change(self) -> Optional[float]:
        """Fractional change from the **earliest close in the supplied frame** to the latest.

        Same basis as yfinance 1.7.0 (measured in ``scrapers/quote.py:474-476``:
        ``(Close[-1] - Close[0]) / Close[0]``), so on the ~1y / ~247-bar frame ``Ticker``
        supplies it covers that frame's first to last close. It is deliberately **not**
        called a 52-week change: the window is whatever frame the constructor was handed, and
        a short frame yields None rather than a number pretending to be annual.
        """
        return fractional_change_from_history(self._history)

    def to_dict(self) -> dict:
        """The mapping as a plain dict. Keys are exactly ``_FAST_INFO_KEYS``."""
        return {key: self[key] for key in _FAST_INFO_KEYS}

    # ------------------------------------------------------------- read-only mapping
    def keys(self):
        """Exactly the keys :meth:`to_dict` returns, in its order."""
        return self.to_dict().keys()

    def values(self):
        return self.to_dict().values()

    def items(self):
        return self.to_dict().items()

    def get(self, key: str, default=None):
        """``self[key]`` for a declared key, `default` for anything else (mapping semantics)."""
        return self.to_dict().get(key, default)

    def __contains__(self, key: object) -> bool:
        return key in _FAST_INFO_KEYS

    def __iter__(self) -> Iterator[str]:
        return iter(_FAST_INFO_KEYS)

    def __len__(self) -> int:
        return len(_FAST_INFO_KEYS)

    def __getitem__(self, key: str):
        if key not in _FAST_INFO_KEYS:
            raise KeyError(key)
        return getattr(self, key)

    def __repr__(self) -> str:
        return f"<FastInfo last_price={self.last_price} mcap={self.market_cap}>"
