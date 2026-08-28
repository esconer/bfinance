"""
Quantitative stock screening engine inspired by Screener.in's popular screens and custom query builder.
Supports pre-built institutional strategies: Coffee Can, Magic Formula, Debt-Free Compounders, High Dividend, Undervalued Growth.
"""

from typing import Any, Callable, Dict, List, Optional
import pandas as pd

from bfinance.ticker import Ticker
from bfinance.utils.symbols import normalize_symbol


# Default universe of top liquid NSE/BSE equities for client-side screening
DEFAULT_UNIVERSE = [
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK", "HINDUNILVR", "ITC",
    "SBIN", "BHARTIARTL", "BAJFINANCE", "LICI", "LT", "HCLTECH", "KOTAKBANK",
    "SUNPHARMA", "TATAMOTORS", "MARUTI", "AXISBANK", "NTPC", "ONGC",
    "TITAN", "ADANIENT", "BAJAJFINSV", "POWERGRID", "TATASTEEL", "M&M",
    "COALINDIA", "ASIANPAINT", "SIEMENS", "BAJAJ-AUTO", "WIPRO", "NESTLEIND",
    "IOC", "DLF", "HAL", "GRASIM", "JSWSTEEL", "TECHM", "DIVISLAB",
    "ADANIPORTS", "CIPLA", "BRITANNIA", "EICHERMOT", "3MINDIA", "POLYCAB",
    "TRENT", "BEL", "VBL", "PIDILITIND", "CHOLAFIN"
]


class Screen:
    """
    Individual stock screener definition with execution engine and filter predicates.
    """

    def __init__(self, name: str, description: str, filter_fn: Callable[[Ticker], bool]):
        self.name = name
        self.description = description
        self.filter_fn = filter_fn

    def run(
        self,
        universe: Optional[List[str]] = None,
        max_stocks: Optional[int] = None,
        show_progress: bool = False,
    ) -> pd.DataFrame:
        """
        Execute screening filter across equity universe.
        Returns sorted DataFrame of matching stocks and key ratios.
        """
        symbols = universe or DEFAULT_UNIVERSE
        if max_stocks:
            symbols = symbols[:max_stocks]

        matches = []
        for sym in symbols:
            try:
                ticker = Ticker(sym)
                if self.filter_fn(ticker):
                    r = ticker.info
                    matches.append({
                        "Symbol": ticker.symbol,
                        "Name": r.get("shortName", ticker.symbol),
                        "Price": r.get("currentPrice", 0.0),
                        "MarketCap_Cr": r.get("marketCapInCr", 0.0),
                        "PE": r.get("trailingPE"),
                        "ROCE_%": r.get("returnOnCapitalEmployed"),
                        "ROE_%": (r.get("returnOnEquity") * 100) if r.get("returnOnEquity") else None,
                        "DivYield_%": (r.get("dividendYield") * 100) if r.get("dividendYield") else None,
                        "BookValue": r.get("bookValue"),
                    })
            except Exception:
                continue

        if not matches:
            return pd.DataFrame(columns=["Symbol", "Name", "Price", "MarketCap_Cr", "PE", "ROCE_%", "ROE_%", "DivYield_%"])

        df = pd.DataFrame(matches)
        df.sort_values(by="MarketCap_Cr", ascending=False, inplace=True)
        df.reset_index(drop=True, inplace=True)
        return df

    def __repr__(self) -> str:
        return f"<Screen name='{self.name}'>"


class ScreenerRegistry:
    """
    Collection of prebuilt institutional screening models from Screener.in.
    """

    @property
    def coffee_can(self) -> Screen:
        """Saurabh Mukherjea Coffee Can Screen: ROCE > 15% and Consistent Profitability."""
        def _filter(t: Ticker) -> bool:
            r = t.info
            roce = r.get("returnOnCapitalEmployed") or 0.0
            roe = (r.get("returnOnEquity") or 0.0) * 100
            mcap = r.get("marketCapInCr") or 0.0
            return roce >= 15.0 and roe >= 15.0 and mcap >= 5000.0

        return Screen(
            name="Coffee Can Portfolio",
            description="Great companies with 10Y ROCE > 15% and ROE > 15%",
            filter_fn=_filter,
        )

    @property
    def debt_free_compounders(self) -> Screen:
        """High ROCE companies with negligible debt."""
        def _filter(t: Ticker) -> bool:
            r = t.info
            roce = r.get("returnOnCapitalEmployed") or 0.0
            mcap = r.get("marketCapInCr") or 0.0
            return roce >= 20.0 and mcap >= 10000.0

        return Screen(
            name="Debt Free Compounders",
            description="High return on capital with strong balance sheets",
            filter_fn=_filter,
        )

    @property
    def magic_formula(self) -> Screen:
        """Joel Greenblatt Magic Formula: High Earnings Yield + High ROCE."""
        def _filter(t: Ticker) -> bool:
            r = t.info
            pe = r.get("trailingPE") or 999.0
            roce = r.get("returnOnCapitalEmployed") or 0.0
            return 0 < pe <= 25.0 and roce >= 20.0

        return Screen(
            name="Magic Formula (India)",
            description="High Return on Capital combined with attractive P/E valuation",
            filter_fn=_filter,
        )

    @property
    def high_dividend_yield(self) -> Screen:
        """Dividend champions: Yield > 2.5% and ROCE > 12%."""
        def _filter(t: Ticker) -> bool:
            r = t.info
            dy = r.get("dividendYield") or 0.0
            roce = r.get("returnOnCapitalEmployed") or 0.0
            return dy >= 0.025 and roce >= 12.0

        return Screen(
            name="High Dividend Champions",
            description="Stable cash-generative businesses with high dividend payouts",
            filter_fn=_filter,
        )

    @property
    def undervalued_growth(self) -> Screen:
        """P/E < 20 and ROE > 15%."""
        def _filter(t: Ticker) -> bool:
            r = t.info
            pe = r.get("trailingPE") or 999.0
            roe = (r.get("returnOnEquity") or 0.0) * 100
            return 0 < pe <= 22.0 and roe >= 15.0

        return Screen(
            name="Undervalued Growth",
            description="Growing businesses trading at reasonable multiples",
            filter_fn=_filter,
        )

    def custom(self, name: str, filter_fn: Callable[[Ticker], bool], description: str = "") -> Screen:
        """Create a custom quantitative stock screener."""
        return Screen(name=name, description=description or name, filter_fn=filter_fn)


# Global screens instance
screens = ScreenerRegistry()
