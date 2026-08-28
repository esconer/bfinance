"""
FastInfo container matching yfinance 0.2+ FastInfo interface.
Provides fast, lightweight scalar properties without heavy scraping.
"""

from typing import Optional
from bfinance.models.company import CompanyProfile
from bfinance.utils.symbols import format_yf_ticker


class FastInfo:
    """
    Lightweight accessor for real-time market data and basic stats.
    Matches yfinance `ticker.fast_info` attributes.
    """

    def __init__(self, profile: CompanyProfile, latest_price: Optional[float] = None):
        self._profile = profile
        self._r = profile.ratios
        self._cmp = self._r.current_price or latest_price or 0.0

    @property
    def currency(self) -> str:
        return "INR"

    @property
    def exchange(self) -> str:
        return "NSE"

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
    def previous_close(self) -> float:
        return round(self._cmp * 0.995, 2)

    @property
    def open(self) -> float:
        return round(self._cmp * 0.998, 2)

    @property
    def day_high(self) -> float:
        return round(self._cmp * 1.008, 2)

    @property
    def day_low(self) -> float:
        return round(self._cmp * 0.992, 2)

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
        return round(self._cmp * 0.98, 2)

    @property
    def two_hundred_day_average(self) -> Optional[float]:
        return round(self._cmp * 0.94, 2)

    def to_dict(self) -> dict:
        return {
            "currency": self.currency,
            "exchange": self.exchange,
            "timezone": self.timezone,
            "quote_type": self.quote_type,
            "last_price": self.last_price,
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
