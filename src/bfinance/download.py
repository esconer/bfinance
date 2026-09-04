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
    back_adjust: bool,
    keepna: bool,
    prepost: bool,
    repair: bool,
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
            back_adjust=back_adjust,
            keepna=keepna,
            prepost=prepost,
            repair=repair,
            rounding=rounding,
        )
    except (NotImplementedError, ValueError):
        raise
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
    back_adjust: bool = False,
    keepna: bool = False,
    prepost: bool = False,
    repair: bool = False,
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
                back_adjust=back_adjust,
                keepna=keepna,
                prepost=prepost,
                repair=repair,
                rounding=rounding,
                proxy=proxy,
            )
            return sym, df

    tasks = [_worker(sym) for sym in tickers]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    data_frames = {}
    for res in results:
        if isinstance(res, (NotImplementedError, ValueError)):
            raise res
        if isinstance(res, BaseException):
            continue
        if isinstance(res, tuple):
            sym, df = res
            if not df.empty:
                data_frames[sym.upper()] = df

    if not data_frames:
        return pd.DataFrame()

    def _yf_naive(frame: pd.DataFrame) -> pd.DataFrame:
        # yfinance download()/Tickers.history return tz-naive indexes in this
        # env even though Ticker.history is tz-aware; match yfinance exactly.
        if isinstance(frame.index, pd.DatetimeIndex) and frame.index.tz is not None:
            frame = frame.copy()
            frame.index = frame.index.tz_localize(None)
        return frame

    if len(tickers) == 1 and not multi_level_index and tickers[0].upper() in data_frames:
        flat = data_frames[tickers[0].upper()].copy()
        flat = flat.reindex(sorted(flat.columns), axis=1)
        return _yf_naive(flat)

    if group_by.lower() == "ticker":
        # MultiIndex DataFrame with (Ticker, Price)
        combined = pd.concat(data_frames, axis=1)
        combined.sort_index(axis=1, inplace=True)
        combined.columns.names = ["Ticker", "Price"]
        return _yf_naive(combined)

    # Default group_by='column': MultiIndex DataFrame with (Price, Ticker) e.g. ('Close', 'RELIANCE')
    combined = pd.concat(data_frames, axis=1)
    combined.columns = combined.columns.swaplevel(0, 1)
    combined.sort_index(axis=1, inplace=True)
    combined.columns.names = ["Price", "Ticker"]
    return _yf_naive(combined)


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
        back_adjust=back_adjust,
        keepna=keepna,
        prepost=prepost,
        repair=repair,
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
