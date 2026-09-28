"""
High-throughput multi-ticker concurrent batch downloader matching yfinance download().
"""

import asyncio
import logging
from typing import List, Optional, Union
from datetime import datetime
import pandas as pd

from bfinance.ticker import Ticker

logger = logging.getLogger(__name__)


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
    raise_errors: bool = False,
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
        if raise_errors:
            raise
        return pd.DataFrame()


async def _download_batch_async(
    tickers: List[str],
    period: str,
    interval: str,
    start: Optional[Union[str, datetime]],
    end: Optional[Union[str, datetime]],
    actions: bool = False,
    auto_adjust: bool = False,
    back_adjust: bool = False,
    keepna: bool = False,
    prepost: bool = False,
    repair: bool = False,
    rounding: bool = False,
    group_by: str = "column",
    multi_level_index: bool = True,
    max_concurrency: int = 5,
    proxy: Optional[str] = None,
    raise_errors: bool = False,
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
                raise_errors=raise_errors,
            )
            return sym, df

    tasks = [_worker(sym) for sym in tickers]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    data_frames = {}
    first_upstream_error: Optional[BaseException] = None
    empty_symbols: list = []
    for res in results:
        if isinstance(res, (NotImplementedError, ValueError)):
            raise res
        if isinstance(res, BaseException):
            # An upstream failure for one symbol. Previously dropped on the floor with no
            # record at all, so a fully-blocked run and a run of bad symbols were
            # indistinguishable from each other.
            logger.warning("download: symbol failed: %s: %s", type(res).__name__, res)
            if first_upstream_error is None:
                first_upstream_error = res
            continue
        if isinstance(res, tuple):
            sym, df = res
            if df is None or df.empty:
                empty_symbols.append(sym)
                continue
            data_frames[sym.upper()] = df

    if first_upstream_error is not None and raise_errors:
        raise first_upstream_error

    if not data_frames:
        if empty_symbols and logger.isEnabledFor(logging.WARNING):
            logger.warning(
                "download: no data for %d/%d symbol(s): %s",
                len(empty_symbols),
                len(tickers),
                ", ".join(sorted(empty_symbols)[:10]),
            )
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
    auto_adjust: bool = False,
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
    raise_errors: bool = False,
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
        auto_adjust: Adjust all OHLC automatically. Defaults to False and must stay False:
            the bhavcopy carries no corporate-actions series, so True raises
            AdjustmentNotAvailableError rather than silently returning unadjusted prices.
        rounding: Round values to 2 decimal places (default False).
        multi_level_index: Always return a MultiIndex DataFrame (default True).
        proxy: Optional HTTP/HTTPS/SOCKS5 proxy URL string.
        raise_errors: Re-raise upstream failures instead of returning an empty frame
            (default False, matching yfinance's lenient default).

    Returns:
        pd.DataFrame: MultiIndex (or single) DataFrame with OHLCV data.

    Note:
        ``raise_errors`` is an EXPLICIT parameter. It was previously reachable only through
        ``**kwargs``, where it was silently discarded -- ``download("X", raise_errors=True)``
        accepted the argument, did nothing, and returned an empty DataFrame. Any caller who
        asked for strict behaviour got silent behaviour instead, which is the worst possible
        outcome for a debugging flag.

        Every other unsupported keyword now raises ``TypeError`` for the same reason. The
        intermediate state -- warn and continue -- is the one that was tried and rejected: a
        misspelled argument (``download(symbols, period="1mo", interval="1d")``, or
        ``raise_errorz=True``) was accepted, vanished, and produced a log line on a logger
        nobody reads, so the caller got a *different* window than the one they asked for with no
        signal at the call site. A warning is the same silent answer with extra steps.

        This is also what yfinance 1.7.0 does: its ``download()`` signature is closed (no
        ``**kwargs`` at all), so Python itself raises ``TypeError`` there. Verified with
        ``inspect.signature(yfinance.download)`` in this env. bfinance's only intentional
        pass-through was ``raise_errors``, which is now a named parameter -- nothing else was
        ever forwarded: ``**kwargs`` reached no callee.
    """
    # An unsupported keyword is a caller bug, not a runtime condition. Refusing it is the only
    # outcome that makes the mistake findable at the call site instead of in a log file.
    if kwargs:
        offending = ", ".join(repr(name) for name in sorted(kwargs))
        raise TypeError(
            f"download() got unsupported keyword argument(s): {offending}. bfinance's download() "
            f"accepts no arbitrary keyword arguments: everything it honours is a named parameter "
            f"(read them off the signature, or inspect.signature(bfinance.download)), and anything "
            f"else would be silently discarded -- which is how raise_errors went missing for years."
        )

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
        raise_errors=raise_errors,
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
    except (NotImplementedError, ValueError):
        # Programming/contract errors always propagate, regardless of raise_errors.
        raise
    except Exception:
        if raise_errors:
            raise
        return pd.DataFrame()
