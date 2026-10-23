"""
Tickers container for managing groups of tickers matching yfinance.Tickers.
"""

from typing import Dict, List, Optional, Sequence, Union
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
        multi_level_index: bool = True,
        statement_fallback: bool = False,
        trendlyne_sections: bool = False,
    ):
        """
        Args:
            statement_fallback: forwarded to every :class:`~bfinance.ticker.Ticker` this collection
                builds, with the same meaning there: when set, a statement accessor whose primary
                source carries no statement at all may fall back to a second vendor. Default
                ``False``, so a collection opens no second-vendor socket unless asked. Without it
                ``Tickers([...]).tickers["NTPC"].financials`` could never fall back, which reads
                as a broken flag rather than a collection that cannot set it. Named for what it
                does rather than for the vendor that currently serves it.
            trendlyne_sections: the same forwarding, for the other opt-in: when set, each
                ``Ticker``'s ``trendlyne_*`` section members and the trendlyne half of
                ``analyst_price_targets`` may run. Default ``False`` for the same reason -- a
                collection opens no trendlyne request unless asked -- and separate from
                ``statement_fallback`` rather than folded into it, because they are different
                capabilities against different routes. Without it
                ``Tickers([...]).tickers["TCS"].share_holding`` raises ``NotImplementedError`` on
                every symbol, which reads as a broken member rather than a collection that cannot
                turn it on.
        """
        if isinstance(cache_ttl_hours, bool):
            multi_level_index = cache_ttl_hours
            cache_ttl_hours = 24.0
        self.multi_level_index = multi_level_index
        if isinstance(tickers, str):
            self.symbols = [t.strip() for t in tickers.replace(",", " ").split() if t.strip()]
        else:
            self.symbols = [str(t).strip() for t in tickers if str(t).strip()]

        # Keyword-passed because only ``ticker`` is positional on
        # :class:`~bfinance.ticker.Ticker`: ``session`` is the second positional and every
        # parameter after it is keyword-only.
        self.tickers: Dict[str, Ticker] = {
            normalize_symbol(sym): Ticker(
                sym,
                cache_ttl_hours=cache_ttl_hours,
                timeout=timeout,
                proxy=proxy,
                session=session,
                statement_fallback=statement_fallback,
                trendlyne_sections=trendlyne_sections,
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
        source: Optional[str] = None,
        sources: Optional[Sequence[str]] = None,
        **kwargs,
    ) -> pd.DataFrame:
        """
        Download historical OHLCV data for all tickers in the group.

        ``source`` (one price source) and ``sources`` (an ordered list to fail over through) mean
        exactly what they mean on :meth:`bfinance.ticker.Ticker.history` and on
        :func:`bfinance.download`, and are applied to every symbol in the group. Naming neither is
        the default. See :meth:`bfinance.market.ohlcv.OHLCVEngine.fetch_history` for the contract.
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
            source=source,
            sources=sources,
            **kwargs,
        )

    def download(
        self,
        period: str = "1mo",
        interval: str = "1d",
        start: Optional[Union[str, datetime]] = None,
        end: Optional[Union[str, datetime]] = None,
        actions: bool = True,
        auto_adjust: bool = True,
        group_by: str = "column",
        threads: bool = True,
        source: Optional[str] = None,
        sources: Optional[Sequence[str]] = None,
        **kwargs,
    ) -> pd.DataFrame:
        """
        Download historical OHLCV data for all tickers in the group.

        yfinance 1.7.0 spells the same operation both ``Tickers.history`` and ``Tickers.download``;
        this is the second spelling, forwarding to :func:`bfinance.download` exactly as
        :meth:`history` does -- including ``actions=True``, which is yfinance's default here and
        is the one place this differs from :func:`bfinance.download`'s own ``actions=False``.
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
            source=source,
            sources=sources,
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
