"""
High-throughput multi-ticker concurrent batch downloader matching yfinance download().
"""

import asyncio
import inspect
import logging
from typing import List, Optional, Sequence, Union
from datetime import datetime
import pandas as pd

from bfinance.cache.sqlite_cache import SQLiteCache
from bfinance.market.ohlcv import OHLCVEngine, resolve_price_source_names
from bfinance.screener.client import ScreenerClient
from bfinance.ticker import Ticker

logger = logging.getLogger(__name__)


def _all_nan_frame(columns: Sequence[str]) -> pd.DataFrame:
    """
    The stand-in for a symbol that produced nothing: every column, every value ``NaN``.

    yfinance's ``yfinance.utils.empty_df()``, which ``multi._download_one`` puts in ``ctx.dfs``
    for any ticker whose ``history()`` raised, and which ``download()`` then unions in with the
    survivors. Read from yfinance 1.7.0 in this env::

        def empty_df(index=None):
            if index is None:
                index = []
            empty = _pd.DataFrame(index=index, data={
                'Open': _np.nan, 'High': _np.nan, 'Low': _np.nan,
                'Close': _np.nan, 'Adj Close': _np.nan, 'Volume': _np.nan})
            empty.index.name = 'Date'
            return empty

    Two properties are load-bearing and both are why this is not a zero-filled frame. The index is
    empty, so the union contributes no rows and the shared index is the survivors'; and the values
    are ``NaN``, not ``0.0``, because a zero is a price. A frame of zeros is a claim about the
    market; ``NaN`` is an admission that there is no data, and ``df[("Close", "BAD")].isna()`` can
    tell them apart.

    ``columns`` is supplied by the caller rather than hardcoded, and deliberately: yfinance's own
    six-column list is not the right answer here, because ``auto_adjust`` defaults to ``True``
    (commit e6b4913) and a ``True`` frame has no ``Adj Close``. See :meth:`OHLCVEngine.
    _history_columns`.
    """
    frame = pd.DataFrame(index=[], data={name: float("nan") for name in columns})
    frame.index.name = "Date"
    return frame


def _accepts_seam(cls) -> bool:
    """Whether ``cls`` takes the batch's shared cache/client as ``cache=`` / ``screener_client=``.

    ``Ticker.__init__`` declares both, so the shipped path always passes them -- that is the whole
    of what makes an N-symbol batch open one cache and one client instead of N of each. A handler
    that replaces the module-level ``Ticker`` name with a stand-in written before that seam
    existed declares ``(symbol, proxy=None)`` and nothing else, and a stand-in must not be able to
    turn every symbol into a failed symbol.

    The question is asked of the signature rather than answered by a ``TypeError`` retry, because
    a retry would also swallow a genuine ``TypeError`` raised *inside* a constructor. ``bind``
    accepts a ``**kwargs`` constructor as well, so a newer stand-in is never missed.
    """
    try:
        inspect.signature(cls).bind("", proxy=None, cache=None, screener_client=None)
    except (TypeError, ValueError):
        return False
    return True


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
    source: Optional[str] = None,
    sources: Optional[Sequence[str]] = None,
    timeout: int = 10,
    cache: Optional[SQLiteCache] = None,
    screener_client: Optional[ScreenerClient] = None,
) -> pd.DataFrame:
    # The batch builds ONE cache and ONE client and hands them to every symbol here; ``None`` --
    # what a direct caller of this helper gets -- keeps the historical per-Ticker construction.
    # Before the seam existed every Ticker built its own SQLiteCache and its own httpx client, so
    # a batch paid a schema check and a new connection pool per symbol for data that is shared.
    if _accepts_seam(Ticker):
        t = Ticker(symbol, proxy=proxy, cache=cache, screener_client=screener_client)
    else:
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
            # `download(timeout=...)` used to be accepted and dropped here, so every symbol ran
            # on history_async's own default of 10 whatever the caller asked for.
            timeout=timeout,
            source=source,
            sources=sources,
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
    raise_errors: bool = False,
    source: Optional[str] = None,
    sources: Optional[Sequence[str]] = None,
    timeout: int = 10,
    progress: bool = True,
) -> pd.DataFrame:
    # One cache and one client for the whole batch. Both used to be built inside every
    # `Ticker.__init__`, so an N-symbol batch opened N SQLite connections (each running the
    # schema check against `~/.bfinance/cache.db`) and N HTTP clients (each with its own
    # connection pool, pacing slot and retry state) to ask N questions of the same vendors.
    # `Ticker` grew the `cache=`/`screener_client=` seam for exactly this, and passing them is
    # what keeps the per-symbol cost a Ticker object rather than a transport.
    #
    # `SQLiteCache()`'s own default TTL (24h) is the value a per-symbol Ticker passed when it
    # built one, so this is the same cache with the same lifetime -- shared, not duplicated.
    # The client takes `timeout` because `download(timeout=...)` is a statement about this
    # batch's requests, and this is the object that makes them.
    cache = SQLiteCache()
    screener_client = ScreenerClient(timeout=timeout, cache=cache, proxy=proxy)

    sem = asyncio.Semaphore(max_concurrency)
    total = len(tickers)
    finished = 0

    async def _worker(sym: str):
        nonlocal finished
        async with sem:
            try:
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
                    source=source,
                    sources=sources,
                    timeout=timeout,
                    cache=cache,
                    screener_client=screener_client,
                )
            finally:
                # `progress=False` must be silent, and `True` must produce something a caller can
                # see. yfinance draws a tqdm bar on stderr; a library must not write to a caller's
                # terminal, so the signal this package can honour is one INFO line per finished
                # symbol, in completion order. Counted after the answer (or the failure) so the
                # line is progress rather than a promise.
                finished += 1
                if progress:
                    logger.info("download: %d/%d %s", finished, total, sym)
            return sym, df

    tasks = [_worker(sym) for sym in tickers]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # The column set an absent symbol is given is the one THIS call asked for, not a constant.
    # yfinance's own empty_df() hardcodes six columns including "Adj Close", which is only right for
    # auto_adjust=False; with auto_adjust=True (the default since e6b4913) a real frame has five.
    # Hardcoding six would give a failed symbol a column no surviving symbol has, so the frame would
    # be internally inconsistent on the very path most callers take. _history_columns is the single
    # place that decides this list, so it is asked rather than re-spelled.
    empty_columns = OHLCVEngine._history_columns(
        auto_adjust=auto_adjust, back_adjust=back_adjust, actions=actions
    )

    data_frames = {}
    first_upstream_error: Optional[BaseException] = None
    empty_symbols: list = []
    failed_symbols: list = []
    answered_symbols = 0
    # asyncio.gather preserves the order of the awaitables it was given, so results[i] is the answer
    # for tickers[i]. That correspondence is the only way to name a symbol whose call raised: the
    # exception object carries no symbol of its own, and a column with no name to put it under is
    # the bug being fixed here.
    for sym, res in zip(tickers, results):
        if isinstance(res, (NotImplementedError, ValueError)):
            raise res
        if isinstance(res, BaseException):
            # An upstream failure for one symbol. Previously dropped on the floor with no
            # record at all, so a fully-blocked run and a run of bad symbols were
            # indistinguishable from each other.
            logger.warning("download: symbol failed: %s: %s", type(res).__name__, res)
            if first_upstream_error is None:
                first_upstream_error = res
            failed_symbols.append(sym)
            # yfinance unions empty_df() here, so the ticker appears as an all-NaN column. Dropping
            # it instead made the frame disagree with the request: a caller looping over
            # df.columns.get_level_values(1) saw fewer tickers than it asked for, and indexing the
            # missing one raised KeyError where yfinance returns NaNs.
            data_frames[sym.upper()] = _all_nan_frame(empty_columns)
            continue
        if isinstance(res, tuple):
            sym, df = res
            if df is None or df.empty:
                empty_symbols.append(sym)
                # Same union as above. An empty answer is an answer: the symbol was asked about and
                # it had no bars, which is a fact about the window, not a reason to remove its name
                # from the result.
                data_frames[sym.upper()] = _all_nan_frame(empty_columns)
                continue
            data_frames[sym.upper()] = df
            answered_symbols += 1

    if first_upstream_error is not None and raise_errors:
        raise first_upstream_error

    if not data_frames:
        return pd.DataFrame()

    if not answered_symbols:
        # Every requested symbol is now present as an all-NaN column. The warning stays: a frame of
        # columns is not a frame of data, and "which symbols produced nothing" is still the first
        # question of a fully-blank result. Counted across both ways of not answering, which is what
        # the old single list could no longer do once the failed symbols were kept.
        absent = empty_symbols + failed_symbols
        if absent and logger.isEnabledFor(logging.WARNING):
            logger.warning(
                "download: no data for %d/%d symbol(s): %s",
                len(absent),
                len(tickers),
                ", ".join(sorted(absent)[:10]),
            )

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
    raise_errors: bool = False,
    source: Optional[str] = None,
    sources: Optional[Sequence[str]] = None,
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
        ignore_tz: ``True`` is honoured -- the returned frame's index is tz-naive, which is the
            only index this package produces and what yfinance 1.7.0 returns in this environment.
            ``False`` raises :class:`NotImplementedError`: preserving the per-bar timezone is not
            something the batch path can do, and returning a frame that quietly dropped it is the
            answer this parameter exists to stop. ``None`` -- the default -- is the established
            tz-naive index.
        progress: Report progress, one ``INFO`` line per finished symbol on the
            ``bfinance.download`` logger, in completion order (``download: 2/5 TCS``).
            ``False`` silences it. yfinance draws a tqdm bar on stderr; a library must not write
            to a caller's terminal, so the log line is the form this package can honestly honour.
            It was previously accepted and never read, which made ``progress=True`` and
            ``progress=False`` the same call.
        group_by: Group by 'column' (default) or 'ticker'.
        auto_adjust: Adjust all OHLC automatically. Defaults to ``True``, matching
            yfinance 1.7: the four price columns are scaled by ``Adj Close / Close`` and
            ``Adj Close`` is dropped, so the default columns are yfinance's. Pass ``False``
            for the strictly richer frame that also carries ``Adj Close``.

            **The old reason for the ``False`` default was overruled on 2026-09-30.** It read
            "Defaults to False and must stay False: the bhavcopy carries no corporate-actions
            series, so True raises AdjustmentNotAvailableError". Both halves were true and the
            conclusion was still wrong. The feed is real -- it is a separate NSE corporate-actions
            reader, not the price archive -- and the refusal fired for the wrong reason: this
            function defaulted ``actions=False``, and under the old code ``actions=False`` was
            ITSELF a refusal for ``auto_adjust=True``. The gate was decoupling the *fetch* from
            the *publication* of the actions series, not refusing to adjust: in yfinance the
            ``actions`` flag decides only whether ``Dividends`` / ``Stock Splits`` are published,
            while the ``Adj Close`` the scaling reads is fetched either way. Gating the feed on
            ``actions`` was our divergence from parity, not a safety property. Measured, with the
            default flipped but the gate not: ``download(["NTPC","RELIANCE"], period="5d")``
            returned shape ``(0, 0)`` -- the refusal raising inside ``history_async`` and being
            swallowed by ``_download_single``'s bare ``except`` -- while the same call with
            ``actions=True`` returned 7 columns. Fixed with ``fetch_actions = actions or
            auto_adjust``, so the feed is resolved whenever either flag asks for it and ``actions``
            alone decides publication. The now-unreachable refusal branch is kept, reworded, in
            :func:`bfinance.market.ohlcv._auto_adjust_refusal`.

            A symbol that genuinely cannot be adjusted -- synthetic bars, no bars, or no ex-dated
            feed -- does not fail the batch. ``auto_adjust=True`` raises inside the per-symbol
            worker, which swallows it, so that symbol appears as an all-``NaN`` column like any
            other non-answer. Pass ``raise_errors=True`` to get the exception instead.
        rounding: Round values to 2 decimal places (default False).
        multi_level_index: Always return a MultiIndex DataFrame (default True).
        timeout: Per-request timeout in seconds (default 10). It is passed to the per-symbol
            history call (``Ticker.history_async(timeout=...)``) and to the **one** HTTP client the
            batch builds, so it reaches the requests rather than only the signature. It used to be
            accepted and dropped, which meant every symbol ran on ``history_async``'s own default
            of 10 and ``download(timeout=3)`` still waited ten seconds.
        proxy: Optional HTTP/HTTPS/SOCKS5 proxy URL string.
        session: yfinance's ``session`` parameter. **Refused when not ``None``**, for the reason
            :class:`bfinance.ticker.Ticker` gives for the same argument: the transport is
            ``httpx``, every client owns its own connection pool, pacing and cache, so a
            ``requests.Session`` or ``httpx.Client`` passed here would never be used.
        raise_errors: Re-raise upstream failures instead of returning an empty frame
            (default False, matching yfinance's lenient default).
        source / sources: **one** price source, or an **ordered list to fail over through**,
            by :mod:`bfinance.sources.registry` name (``"nse-bhavcopy"``,
            ``"marketlens-close"``, ``"trendlyne-ohlc"``, ...), applied to every symbol in the
            batch. Naming neither is the default and does not engage the cascade.

            An unknown name raises :class:`ValueError` here, **before a single Ticker is
            constructed** -- the same guarantee ``test_download_rejects_before_doing_any_work``
            pins for an unknown keyword, and for the same reason: a source name is a local
            vocabulary, so spending one request per symbol to discover a typo is the one outcome
            this package will not produce. ``raise_errors=False`` does not soften it, exactly as
            it does not soften an unknown keyword: a wrong argument is not an upstream failure.

    Returns:
        pd.DataFrame: MultiIndex (or single) DataFrame with OHLCV data.

        Every requested ticker appears as a column, including one that failed or came back empty:
        such a column is all-``NaN``, exactly as yfinance 1.7.0 answers (``utils.empty_df()`` unioned
        in by ``multi._download_one``). It used to be dropped instead, so ``df.columns`` disagreed
        with the request and ``df[("Close", "BAD")]`` raised ``KeyError`` where yfinance returns
        NaNs -- the worst outcome for a caller swapping symbols in, because a renamed ticker would
        look like a column-set change rather than a failure. The empty columns are the ones the call
        asked for (so no ``Adj Close`` when ``auto_adjust=True``), taken from
        :meth:`bfinance.market.ohlcv.OHLCVEngine._history_columns` rather than a hardcoded list.

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

        ``timeout``, ``session``, ``ignore_tz`` and ``progress`` are the same defect one layer in:
        all four were named parameters that no line of this function, or of anything it called,
        read. ``timeout`` now reaches the per-symbol history call and the batch's shared client;
        ``session`` is refused when not ``None``; ``ignore_tz=False`` is refused because the frame
        is always tz-naive, while ``True`` is honoured; and ``progress`` emits one ``INFO`` line
        per finished symbol instead of nothing at all.
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

    # Two yfinance-parity arguments this function cannot honour, refused here for the same reason
    # an unknown keyword is: a caller who passes one and gets a frame back has been told their
    # choice was applied when it was not. Refused before a Ticker is constructed, so neither
    # refusal costs a request (the ordering test_download_rejects_before_doing_any_work pins).
    if session is not None:
        raise NotImplementedError(
            "download(session=...): the argument cannot be honoured. bfinance does not accept a "
            "caller-supplied HTTP session -- its transport is httpx, and every client owns its own "
            "connection pool, pacing and cache, so a requests.Session or httpx.Client passed here "
            "would never be used. Omit it (session=None) to use the pacing this package already "
            "applies."
        )
    if ignore_tz is False:
        raise NotImplementedError(
            "download(ignore_tz=False): the argument cannot be honoured. bfinance's batch frame "
            "always carries a tz-naive DatetimeIndex (the same index yfinance 1.7.0 returns in "
            "this environment), so the timezone-preserving frame that False asks for cannot be "
            "produced. Omit it (ignore_tz=None), or pass ignore_tz=True for the tz-naive index "
            "this package already returns."
        )

    if isinstance(tickers, str):
        ticker_list = [t.strip() for t in tickers.replace(",", " ").split() if t.strip()]
    else:
        ticker_list = [str(t).strip() for t in tickers if str(t).strip()]

    if not ticker_list:
        return pd.DataFrame()

    # Validated here, not left to each symbol's history() call, because the batch fans out: a
    # refusal that only surfaced inside the worker would have constructed N Tickers and spent N
    # requests before the first complaint. This is the same ordering test_download_rejects_
    # before_doing_any_work pins for an unknown keyword.
    price_names = resolve_price_source_names(source, sources)

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
        source=price_names[0] if len(price_names) == 1 else None,
        sources=list(price_names) if price_names else None,
        timeout=timeout,
        progress=progress,
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
