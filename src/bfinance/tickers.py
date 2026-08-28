"""
Tickers container for managing groups of tickers matching yfinance.Tickers.
"""

from typing import Dict, List, Optional, Union
from datetime import datetime
import pandas as pd

from bfinance.download import download
from bfinance.ticker import Ticker
from bfinance.utils.symbols import normalize_symbol


class Tickers:
    """
    Multi-ticker collection manager matching yfinance 1.7.0 `yf.Tickers`.
    """

    def __init__(
        self,
        tickers: Union[str, List[str]],
        cache_ttl_hours: float = 24.0,
        timeout: float = 15.0,
        proxy: Optional[str] = None,
        session: Optional[object] = None,
    ):
        if isinstance(tickers, str):
            self.symbols = [t.strip() for t in tickers.replace(",", " ").split() if t.strip()]
        else:
            self.symbols = [str(t).strip() for t in tickers if str(t).strip()]

        self.tickers: Dict[str, Ticker] = {
            normalize_symbol(sym): Ticker(
                sym, cache_ttl_hours=cache_ttl_hours, timeout=timeout, proxy=proxy, session=session
            )
            for sym in self.symbols
        }

    def history(
        self,
        period: str = "1mo",
        interval: str = "1d",
        start: Optional[Union[str, datetime]] = None,
        end: Optional[Union[str, datetime]] = None,
        actions: bool = True,
        auto_adjust: bool = True,
        group_by: str = "column",
        threads: bool = True,
        **kwargs,
    ) -> pd.DataFrame:
        """
        Download historical OHLCV data for all tickers in the group.
        """
        return download(
            tickers=self.symbols,
            period=period,
            interval=interval,
            start=start,
            end=end,
            actions=actions,
            auto_adjust=auto_adjust,
            group_by=group_by,
            threads=threads,
            **kwargs,
        )

    def news(self) -> Dict[str, List[Dict[str, str]]]:
        """
        Get latest corporate announcements for all tickers in group.
        """
        return {sym: ticker.news for sym, ticker in self.tickers.items()}

    def __getitem__(self, symbol: str) -> Ticker:
        clean = normalize_symbol(symbol)
        if clean in self.tickers:
            return self.tickers[clean]
        for k, v in self.tickers.items():
            if k.upper() == clean.upper():
                return v
        raise KeyError(f"Ticker '{symbol}' not found in Tickers collection.")

    def __iter__(self):
        return iter(self.tickers.values())

    def __len__(self) -> int:
        return len(self.tickers)

    def __repr__(self) -> str:
        return f"<bfinance.Tickers count={len(self.tickers)} symbols={list(self.tickers.keys())}>"
