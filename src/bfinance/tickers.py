"""
Tickers container for managing groups of tickers matching yfinance.Tickers.
"""

from typing import Dict, List, Optional, Union
from datetime import datetime
import pandas as pd

from bfinance.download import download
from bfinance.ticker import Ticker
from bfinance.utils.symbols import normalize_symbol


def _apply_ticker_bool_shim() -> None:
    """Bool 2nd positional is multi_level_index, never cache_ttl_hours.

    yfinance 1.7.0 ``Ticker("X", True)`` raises (2nd positional is
    ``session``); bfinance's 2nd positional is ``cache_ttl_hours``, so a
    bare bool would silently become a TTL. Treat it as multi_level_index
    instead (stored; single-ticker history stays flat like yfinance).
    Lives here because Ticker itself is out of scope for this change; the
    patch mutates the shared class object so every import path sees it.
    """
    if getattr(Ticker.__init__, "__bfinance_mli_shim__", False):
        return

    orig_init = Ticker.__init__

    def __init__(self, ticker, cache_ttl_hours=24.0, *args, **kwargs):
        mli = kwargs.pop("multi_level_index", None)
        if isinstance(cache_ttl_hours, bool):
            mli = cache_ttl_hours if mli is None else mli
            cache_ttl_hours = 24.0
        orig_init(self, ticker, *args, cache_ttl_hours=cache_ttl_hours, **kwargs)
        self.multi_level_index = True if mli is None else bool(mli)

    __init__.__bfinance_mli_shim__ = True
    Ticker.__init__ = __init__


_apply_ticker_bool_shim()


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
        multi_level_index: bool = True,
    ):
        if isinstance(cache_ttl_hours, bool):
            multi_level_index = cache_ttl_hours
            cache_ttl_hours = 24.0
        self.multi_level_index = multi_level_index
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
        kwargs.setdefault("multi_level_index", self.multi_level_index)
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
