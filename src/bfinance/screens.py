"""
Quantitative stock screening engine inspired by Screener.in's popular screens and custom query builder.
Supports pre-built institutional strategies: Coffee Can, Magic Formula, Debt-Free Compounders, High Dividend, Undervalued Growth.

Unit contract (finengine parity):
- ROCE_% is raw percent (e.g. 20.0 means 20%).
- ROE_%/DivYield_% are percent; info holds decimals (0.15 -> 15.0).
- None stays None; missing thresholds fail (0.0 in filters).
"""

from typing import Any, Callable, Dict, List, Optional
import logging
import pandas as pd

from bfinance.ticker import Ticker
from bfinance.utils.symbols import normalize_symbol


logger = logging.getLogger(__name__)


def _parse_pct(value: Any) -> Optional[float]:
    """Parse '12.5%'/'12.5' to float; None when missing/unparseable."""
    if value is None:
        return None
    try:
        s = str(value).strip().replace("%", "").replace(",", "")
        if s in ("", "-", "NA", "N/A"):
            return None
        return float(s)
    except (ValueError, TypeError):
        return None


def _cagr_value(cagrs: Any, titles: List[str], periods: List[str]) -> Optional[float]:
    """First matching CAGR % from info['cagrs']; None if absent."""
    if not isinstance(cagrs, dict):
        return None
    for title, vals in cagrs.items():
        if not any(k.lower() in str(title).lower() for k in titles):
            continue
        if not isinstance(vals, dict):
            continue
        for per, raw in vals.items():
            if any(p.lower() in str(per).lower() for p in periods):
                v = _parse_pct(raw)
                if v is not None:
                    return v
    return None


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

    def __call__(
        self,
        universe: Optional[List[str]] = None,
        max_stocks: Optional[int] = None,
        show_progress: bool = False,
    ) -> pd.DataFrame:
        """Allow calling Screen instance directly: screen(...) as an alias to screen.run(...)."""
        return self.run(universe=universe, max_stocks=max_stocks, show_progress=show_progress)

    def run(
        self,
        universe: Optional[List[str]] = None,
        max_stocks: Optional[int] = None,
        show_progress: bool = False,
    ) -> pd.DataFrame:
        """
        Execute screening filter across equity universe.
        Returns sorted DataFrame of matching stocks and key ratios.

        Units: ROCE_% raw-%, ROE_%/DivYield_% percent (decimals x100);
        None stays None.
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
            except Exception as e:
                logger.warning("Screen %s: skipping %s (%s)", self.name, sym, e)
                continue

        if not matches:
            return pd.DataFrame(columns=["Symbol", "Name", "Price", "MarketCap_Cr", "PE", "ROCE_%", "ROE_%", "DivYield_%", "BookValue"])

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
        """Coffee Can: point-in-time ROCE>=15%, ROE>=15%, mcap>=5000Cr; 10Y sales/profit CAGR>0 required."""
        def _filter(t: Ticker) -> bool:
            r = t.info
            roce = r.get("returnOnCapitalEmployed") or 0.0
            roe = (r.get("returnOnEquity") or 0.0) * 100
            mcap = r.get("marketCapInCr") or 0.0
            if not (roce >= 15.0 and roe >= 15.0 and mcap >= 5000.0):
                return False
            sales10 = _cagr_value(r.get("cagrs"), ["sales"], ["10"])
            profit10 = _cagr_value(r.get("cagrs"), ["profit"], ["10"])
            if sales10 is None or profit10 is None:
                return False
            if sales10 <= 0 or profit10 <= 0:
                return False
            return True

        return Screen(
            name="Coffee Can Portfolio",
            description="Point-in-time ROCE>=15%, ROE>=15%, mcap>=5000Cr; 10Y sales/profit growth>0 required",
            filter_fn=_filter,
        )

    @property
    def debt_free_compounders(self) -> Screen:
        """High ROCE, negligible debt: ROCE>=20%, mcap>=10000Cr, D/E<=0.2 (exclude when missing).

        D/E falls back to statement-computed custom_ratios when info lacks it:
        zero-debt companies (Borrowings==0, e.g. INFY) report no screener
        "Debt to equity" value, and must PASS — not be fail-closed out.
        """
        def _filter(t: Ticker) -> bool:
            r = t.info
            roce = r.get("returnOnCapitalEmployed") or 0.0
            mcap = r.get("marketCapInCr") or 0.0
            if not (roce >= 20.0 and mcap >= 10000.0):
                return False
            de = r.get("debtToEquity")
            if de is None:
                try:
                    de = t.custom_ratios.get("debt_to_equity")
                except Exception:
                    de = None
            if de is None:
                return False
            try:
                return float(de) <= 0.2
            except (ValueError, TypeError):
                return False

        return Screen(
            name="Debt Free Compounders",
            description="ROCE>=20%, mcap>=10000Cr with D/E<=0.2 (fail-closed)",
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
        """P/E<=20, ROE>=15% plus growth (sales/profit CAGR>=10% or PEG<=2 when available)."""
        def _filter(t: Ticker) -> bool:
            r = t.info
            pe = r.get("trailingPE") or 999.0
            roe = (r.get("returnOnEquity") or 0.0) * 100
            if not (0 < pe <= 20.0 and roe >= 15.0):
                return False
            sales = _cagr_value(r.get("cagrs"), ["sales"], ["5", "3", "10"])
            profit = _cagr_value(r.get("cagrs"), ["profit"], ["5", "3", "10"])
            if sales is not None or profit is not None:
                return max(sales if sales is not None else -1, profit if profit is not None else -1) >= 10.0
            peg = r.get("pegRatio")
            if peg is not None:
                try:
                    return 0 < float(peg) <= 2.0
                except (ValueError, TypeError):
                    return False
            return True

        return Screen(
            name="Undervalued Growth",
            description="P/E<=20, ROE>=15% with sales/profit growth>=10% or PEG<=2 when available",
            filter_fn=_filter,
        )

    def custom(self, name: str, filter_fn: Callable[[Ticker], bool], description: str = "") -> Screen:
        """Create a custom quantitative stock screener."""
        return Screen(name=name, description=description or name, filter_fn=filter_fn)


# Global screens instance
screens = ScreenerRegistry()
