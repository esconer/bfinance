"""
High-throughput multi-ticker concurrent batch downloader matching yfinance download().
"""

import asyncio
from typing import List, Optional, Union
from datetime import datetime
import pandas as pd

from bfinance.ticker import Ticker
from bfinance.utils.symbols import normalize_symbol


async def _download_single(
    symbol: str,
    period: str,
    interval: str,
    start: Optional[Union[str, datetime]],
    end: Optional[Union[str, datetime]],
    actions: bool,
    auto_adjust: bool,
    rounding: bool,
    proxy: Optional[str] = None,
) -> pd.DataFrame:
    t = Ticker(symbol, proxy=proxy)
    try:
        return await t.history_async(
            period=period,
            interval=interval,
            start=start,
            end=end,
            actions=actions,
            auto_adjust=auto_adjust,
            rounding=rounding,
        )
    except Exception:
        return pd.DataFrame()


async def _download_batch_async(
    tickers: List[str],
    period: str,
    interval: str,
    start: Optional[Union[str, datetime]],
    end: Optional[Union[str, datetime]],
    actions: bool = False,
    auto_adjust: bool = True,
    rounding: bool = False,
    group_by: str = "column",
    multi_level_index: bool = True,
    max_concurrency: int = 5,
    proxy: Optional[str] = None,
) -> pd.DataFrame:
    sem = asyncio.Semaphore(max_concurrency)

    async def _worker(sym: str):
        async with sem:
            df = await _download_single(
                sym,
                period=period,
                interval=interval,
                start=start,
                end=end,
                actions=actions,
                auto_adjust=auto_adjust,
                rounding=rounding,
                proxy=proxy,
            )
            return sym, df

    tasks = [_worker(sym) for sym in tickers]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    data_frames = {}
    for res in results:
        if isinstance(res, tuple):
            sym, df = res
            if not df.empty:
                data_frames[sym.upper()] = df

    if not data_frames:
        return pd.DataFrame()

    if len(tickers) == 1 and not multi_level_index and tickers[0].upper() in data_frames:
        return data_frames[tickers[0].upper()]

    if group_by.lower() == "ticker":
        # MultiIndex DataFrame with (Ticker, Metric)
        combined = pd.concat(data_frames, axis=1)
        combined.sort_index(axis=1, inplace=True)
        return combined

    # Default group_by='column': MultiIndex DataFrame with (Metric, Ticker) e.g. ('Close', 'RELIANCE')
    combined = pd.concat(data_frames, axis=1)
    combined.columns = combined.columns.swaplevel(0, 1)
    combined.sort_index(axis=1, inplace=True)
    return combined


def download(
    tickers: Union[str, List[str]],
    start: Optional[Union[str, datetime]] = None,
    end: Optional[Union[str, datetime]] = None,
    actions: bool = False,
    threads: bool = True,
    ignore_tz: Optional[bool] = None,
    group_by: str = "column",
    auto_adjust: bool = True,
    back_adjust: bool = False,
    repair: bool = False,
    keepna: bool = False,
    progress: bool = True,
    period: str = "1mo",
    interval: str = "1d",
    prepost: bool = False,
    rounding: bool = False,
    timeout: int = 10,
    proxy: Optional[str] = None,
    session: Optional[object] = None,
    multi_level_index: bool = True,
    **kwargs,
) -> pd.DataFrame:
    """
    Download market data for multiple tickers concurrently. Matches exact `yfinance.download()` signature.

    Args:
        tickers: String of space-separated symbols (e.g. 'RELIANCE TCS INFY') or list of symbols.
        period: Data period to download (e.g. '1mo', '1y', '5y', 'max').
        interval: Data interval ('1d').
        start: Start date string (YYYY-MM-DD) or datetime.
        end: End date string (YYYY-MM-DD) or datetime.
        actions: Download dividend + stock splits data (default False).
        threads: How many threads/connections to use for mass downloading (default True).
        group_by: Group by 'column' (default) or 'ticker'.
        auto_adjust: Adjust all OHLC automatically (default True).
        rounding: Round values to 2 decimal places (default False).
        multi_level_index: Always return a MultiIndex DataFrame (default True).
        proxy: Optional HTTP/HTTPS/SOCKS5 proxy URL string.

    Returns:
        pd.DataFrame: MultiIndex (or single) DataFrame with OHLCV data.
    """
    if isinstance(tickers, str):
        ticker_list = [t.strip() for t in tickers.replace(",", " ").split() if t.strip()]
    else:
        ticker_list = [str(t).strip() for t in tickers if str(t).strip()]

    if not ticker_list:
        return pd.DataFrame()

    coro = _download_batch_async(
        tickers=ticker_list,
        period=period,
        interval=interval,
        start=start,
        end=end,
        actions=actions,
        auto_adjust=auto_adjust,
        rounding=rounding,
        group_by=group_by,
        multi_level_index=multi_level_index,
        max_concurrency=10 if threads else 1,
        proxy=proxy,
    )

    try:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(lambda: asyncio.run(coro))
                return future.result()
        else:
            return asyncio.run(coro)
    except Exception:
        return pd.DataFrame()
