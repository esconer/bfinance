"""
FastInfo container matching yfinance 0.2+ FastInfo interface.
Provides fast, lightweight scalar properties without heavy scraping.
"""

from typing import Any, Optional
from bfinance.models.company import CompanyProfile
from bfinance.market.quotes import moving_average, previous_close_from_history, resolve_exchange


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

    def to_dict(self) -> dict:
        return {
            "currency": self.currency,
            "exchange": self.exchange,
            "timezone": self.timezone,
            "quote_type": self.quote_type,
            "last_price": self.last_price,
            "regular_market_price": self.regular_market_price,
            "last_volume": self.last_volume,
            "previous_close": self.previous_close,
            "open": self.open,
            "day_high": self.day_high,
            "day_low": self.day_low,
            "year_high": self.year_high,
            "year_low": self.year_low,
            "market_cap": self.market_cap,
            "shares": self.shares,
            "fifty_day_average": self.fifty_day_average,
            "two_hundred_day_average": self.two_hundred_day_average,
        }

    def __getitem__(self, key: str):
        return getattr(self, key)

    def __repr__(self) -> str:
        return f"<FastInfo last_price={self.last_price} mcap={self.market_cap}>"
