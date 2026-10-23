"""
NSE MarketLens — official close + volume, 30 years deep, zero authentication required.

Endpoint
--------
``https://marketlens.nseindia.com/api/stocks/{SYMBOL}/price-history?period={PERIOD}``

Verified live 2026-09-26. This is the **only source in bfinance with no anti-bot exposure**: a
bare request with no cookie, no session, and no ``Referer`` returns ``200``. Contrast
``nseindia.com/api/quote-equity``, which returns **403 from datacenter IPs**, and the
``/api/*`` family generally, which needs a session cookie.

Coverage boundary — read this before using the output
-----------------------------------------------------
This endpoint returns **close and volume only**. Row keys are exactly ``date``, ``price``,
``volume``. It has **no Open/High/Low**.

Every frame returned here carries::

    df.attrs["bfinance_marketlens_partial_coverage"] = True

A consumer must **never** fill ``Open``/``High``/``Low`` from this frame to satisfy an OHLCV-shaped
contract. Doing so fabricates an intraday range that the source never provided — which is exactly
the defect ``bfinance_synthetic_ohlc`` marks on the screener.in feed. If you need O/H/L, use the
NSE CM bhavcopy.

Periods are UPPERCASE ONLY
-------------------------
All twelve periods must be uppercase. Every lowercase variant returns
``{"success": true, "data": [], "error": null}`` — byte-identical to a nonexistent ticker, with no
error. This module therefore uppercases and validates the period **before** issuing a request.

Symbols are NSE-shaped
---------------------
MarketLens carries no BSE coverage. ``RELIANCE.BO``, ``RELIANCE-BO`` and the numeric BSE scrip
codes (``500112``) all return zero rows. A ``.NS`` suffix is accepted and stripped — the endpoint
does not understand it either. Anything else raises :class:`NotAnNseSymbolError`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import re
import threading
import time
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

import httpx
import numpy as np
import pandas as pd

from bfinance.utils.exceptions import (
    NotAnNseSymbolError,
    RateLimitExceededError,
    TickerNotFoundError,
    UpstreamServiceError,
)

logger = logging.getLogger("bfinance")

IST = ZoneInfo("Asia/Kolkata")

#: Accepted periods. **Uppercase only** — see the module docstring.
PERIODS: tuple[str, ...] = (
    "1D", "1W", "1M", "3M", "6M", "1Y",
    "5Y", "10Y", "15Y", "20Y", "25Y", "30Y",
)

#: The period that returns intraday bars rather than daily bars.
INTRADAY_PERIOD = "1D"

#: Attr set on every frame so the coverage limit is detectable downstream.
PARTIAL_COVERAGE_ATTR = "bfinance_marketlens_partial_coverage"

#: Provenance tag recorded on every frame.
PROVENANCE = "nse_marketlens"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
]

_GLOBAL_PACING_LOCK = threading.Lock()
_GLOBAL_LAST_REQUEST_TIME = 0.0

_BSE_SUFFIXES = (".BO", ".BSE")
_NSE_SUFFIX = ".NS"
_NSE_SYMBOL_RE = re.compile(r"^[A-Z0-9&\-.]+$")


def validate_period(period: str) -> str:
    """Uppercase and validate a period **before** any network request.

    The endpoint does not reject a bad period — it returns an empty ``data`` list with
    ``error: null``. Validating here is what turns a silent zero-row result into a loud failure.
    """
    if not isinstance(period, str):
        raise ValueError(f"period must be a string, got {type(period).__name__}")
    normalised = period.strip().upper()
    if normalised not in PERIODS:
        raise ValueError(
            f"period={period!r} is not supported. Must be one of {', '.join(PERIODS)} "
            f"(uppercase; lowercase values return an empty result with no error)."
        )
    return normalised


def validate_nse_symbol(symbol: str) -> str:
    """Normalise and validate an NSE symbol.

    A ``.NS`` suffix is accepted and stripped — the endpoint does not understand it and would
    return zero rows. BSE forms are rejected loudly, because a zero-row result for ``.BO`` is
    indistinguishable from a vendor outage.
    """
    if not isinstance(symbol, str):
        raise NotAnNseSymbolError(f"symbol must be a string, got {type(symbol).__name__}")

    cleaned = symbol.strip().upper()
    if not cleaned:
        raise NotAnNseSymbolError("symbol must be a non-empty string")

    if cleaned.endswith(_NSE_SUFFIX):
        cleaned = cleaned[: -len(_NSE_SUFFIX)].strip()
        if not cleaned:
            raise NotAnNseSymbolError(f"symbol={symbol!r} is only a suffix")

    for suffix in _BSE_SUFFIXES:
        if cleaned.endswith(suffix):
            raise NotAnNseSymbolError(
                f"symbol={symbol!r} is a BSE ticker. MarketLens carries NSE coverage only — "
                f"use the bhavcopy or a BSE-capable source."
            )
    if cleaned.endswith("-BO"):
        raise NotAnNseSymbolError(
            f"symbol={symbol!r} is a BSE ticker. MarketLens carries NSE coverage only."
        )
    if cleaned.isdigit():
        raise NotAnNseSymbolError(
            f"symbol={symbol!r} looks like a BSE scrip code. MarketLens keys on NSE symbols."
        )
    if cleaned.startswith("^"):
        raise NotAnNseSymbolError(
            f"symbol={symbol!r} is an index. MarketLens has no index endpoint "
            f"(/api/index/NIFTY50 returns 404)."
        )
    if not _NSE_SYMBOL_RE.match(cleaned):
        raise NotAnNseSymbolError(
            f"symbol={symbol!r} is not a valid NSE symbol. Expected letters, digits, "
            f"'&', '-' or '.' (e.g. RELIANCE, M&M, BAJAJ-AUTO)."
        )
    return cleaned


def _run_async(coro):
    """Bridge to sync, matching ``Ticker._run_async`` semantics."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            return executor.submit(lambda: asyncio.run(coro)).result()
    return asyncio.run(coro)


class MarketLensClient:
    """Client for NSE's official MarketLens price-history endpoint.

    Stateless per call. Safe to construct once and reuse; a long-lived instance is fine
    because the endpoint has no session state and no cache-staleness problem (unlike
    ``Ticker.info``, which is frozen for the process lifetime).
    """

    BASE_URL = "https://marketlens.nseindia.com"
    API_PATH = "/api/stocks/{symbol}/price-history"

    def __init__(
        self,
        timeout: float = 20.0,
        max_retries: int = 3,
        proxy: Optional[str] = None,
        min_request_interval_ms: float = 100.0,
    ) -> None:
        self.timeout = timeout
        self.max_retries = max(1, int(max_retries))
        self.proxy = proxy or os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
        self.min_request_interval_ms = min_request_interval_ms
        self._last_request_time = 0.0

    # ---------------------------------------------------------------- internals

    def _headers(self) -> Dict[str, str]:
        return {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
        }

    async def _throttle_pacing(self) -> None:
        """Pace requests. No anti-bot to evade, but the 30Y payloads are large — be polite."""
        global _GLOBAL_LAST_REQUEST_TIME
        with _GLOBAL_PACING_LOCK:
            now = time.monotonic()
            last = max(self._last_request_time, _GLOBAL_LAST_REQUEST_TIME)
            elapsed_ms = (now - last) * 1000.0
            if elapsed_ms < self.min_request_interval_ms:
                wait_sec = (self.min_request_interval_ms - elapsed_ms) / 1000.0 + random.uniform(0.01, 0.05)
                _GLOBAL_LAST_REQUEST_TIME = now + wait_sec
            else:
                wait_sec = 0.0
                _GLOBAL_LAST_REQUEST_TIME = now
        if wait_sec > 0.0:
            await asyncio.sleep(wait_sec)
        self._last_request_time = time.monotonic()
        with _GLOBAL_PACING_LOCK:
            _GLOBAL_LAST_REQUEST_TIME = max(_GLOBAL_LAST_REQUEST_TIME, self._last_request_time)

    async def _get_json(self, url: str) -> Dict[str, Any]:
        """GET with retry on 429/5xx/network. Raises typed errors, never returns None."""
        last_error: Optional[Exception] = None

        for attempt in range(self.max_retries):
            await self._throttle_pacing()
            try:
                async with httpx.AsyncClient(
                    timeout=self.timeout, proxy=self.proxy, follow_redirects=True
                ) as client:
                    response = await client.get(url, headers=self._headers())
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(0.5 * (2**attempt))
                continue

            if response.status_code == 429:
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(0.5 * (2**attempt))
                    continue
                raise RateLimitExceededError(
                    f"MarketLens rate limited (HTTP 429) for {url}"
                )

            if response.status_code >= 500:
                last_error = UpstreamServiceError(
                    f"MarketLens returned HTTP {response.status_code} for {url}"
                )
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(0.5 * (2**attempt))
                continue

            if response.status_code == 404:
                raise TickerNotFoundError(f"MarketLens has no route for {url}")

            if response.status_code != 200:
                raise UpstreamServiceError(
                    f"MarketLens returned unexpected HTTP {response.status_code} for {url}"
                )

            try:
                return response.json()
            except ValueError as exc:
                raise UpstreamServiceError(
                    f"MarketLens returned a non-JSON body for {url} "
                    f"(first 160 chars: {response.text[:160]!r})"
                ) from exc

        raise UpstreamServiceError(
            f"MarketLens failed after {self.max_retries} attempts: {url}"
        ) from last_error

    @staticmethod
    def _parse(payload: Dict[str, Any], symbol: str, period: str) -> pd.DataFrame:
        """Turn a MarketLens payload into a DataFrame.

        Daily periods yield a naive ``DatetimeIndex``; ``1D`` yields a tz-aware IST index because
        the raw timestamps are epoch milliseconds spanning the 09:15-15:59 IST session.

        Raises rather than returning an empty frame — an empty ``data`` list is what the endpoint
        returns for a bad symbol, a bad period, or a genuine outage, and those must not look alike.
        """
        rows = payload.get("data")
        if not isinstance(rows, list) or not rows:
            raise TickerNotFoundError(
                f"MarketLens returned no rows for {symbol} (period={period}). "
                f"This is indistinguishable between an unresolvable symbol, a delisted scrip, "
                f"and a genuine outage — verify the symbol before treating it as 'no data'."
            )

        frame = pd.DataFrame(rows)
        missing = {"date", "price", "volume"} - set(frame.columns)
        if missing:
            raise UpstreamServiceError(
                f"MarketLens payload for {symbol} is missing expected field(s): {sorted(missing)}. "
                f"Upstream schema may have changed."
            )

        raw_dates = frame["date"].astype(str)
        if period == INTRADAY_PERIOD:
            index = pd.to_datetime(raw_dates.astype("int64"), unit="ms", utc=True).dt.tz_convert(IST)
        else:
            index = pd.to_datetime(raw_dates, format="%Y-%m-%d")

        if index.isna().any():
            raise UpstreamServiceError(
                f"MarketLens returned unparseable date(s) for {symbol}: "
                f"{int(index.isna().sum())} of {len(index)} rows."
            )

        # NOTE: values are passed as numpy arrays, not Series. A Series carries a RangeIndex
        # which pandas reindexes against the explicit DatetimeIndex below, silently producing
        # an all-NaN frame that still "succeeds". See CONTEXT.md 9.22.
        close = pd.to_numeric(frame["price"], errors="coerce").to_numpy()
        volume = pd.to_numeric(frame["volume"], errors="coerce").fillna(0).to_numpy()

        # The intraday feed emits impossible volumes. Measured on the 2026-09-25 RELIANCE
        # session: 100 of 372 bars carried a NEGATIVE volume (-1 to -4) and 75 carried 0, so
        # 47% of bars have an unusable volume and the session sums to 439. The daily series is
        # clean (0 negative, 0 zero, min 2,066,169 on the same ticker). Clamp rather than drop so
        # bar count and timestamps survive, and record the count so a consumer can see the damage.
        negative_volume = int((volume < 0).sum())
        if negative_volume:
            logger.warning(
                "MarketLens intraday volume for %s contains %d negative value(s); clamped to 0. "
                "Intraday volume from this feed is not usable.",
                symbol, negative_volume,
            )
            volume = np.where(volume < 0, 0, volume)

        nonpositive_close = int((close <= 0).sum())

        out = pd.DataFrame(
            {"Close": close, "Volume": volume.astype("int64")},
            index=pd.DatetimeIndex(index),
        )
        out.index.name = "Date"
        out.sort_index(inplace=True)
        out.dropna(subset=["Close"], inplace=True)

        if out.empty:
            raise TickerNotFoundError(
                f"MarketLens returned no usable rows for {symbol} (period={period}) after parsing."
            )

        out.attrs[PARTIAL_COVERAGE_ATTR] = True
        out.attrs["bfinance_provenance"] = PROVENANCE
        out.attrs["bfinance_symbol"] = symbol
        out.attrs["bfinance_period"] = period
        out.attrs["bfinance_negative_volume_bars"] = negative_volume
        out.attrs["bfinance_nonpositive_close_bars"] = nonpositive_close
        return out

    async def _fetch(self, symbol: str, period: str) -> pd.DataFrame:
        clean_symbol = validate_nse_symbol(symbol)
        clean_period = validate_period(period)
        url = f"{self.BASE_URL}{self.API_PATH.format(symbol=clean_symbol)}?period={clean_period}"
        payload = await self._get_json(url)
        return self._parse(payload, clean_symbol, clean_period)

    # ------------------------------------------------------------------ public

    async def price_history_async(
        self, symbol: str, period: str = "1Y"
    ) -> pd.DataFrame:
        """Daily close + volume. Columns ``['Close', 'Volume']`` — **no O/H/L**.

        Args:
            symbol: NSE symbol. A ``.NS`` suffix is accepted and stripped.
            period: One of :data:`PERIODS`, uppercase. ``1D`` returns intraday; use
                :meth:`intraday_async` for that so the intent is explicit.

        Returns:
            A ``DataFrame`` with a ``DatetimeIndex`` named ``Date`` and
            ``attrs['bfinance_marketlens_partial_coverage'] = True``.

        Raises:
            ValueError: ``period`` is not in :data:`PERIODS`.
            NotAnNseSymbolError: ``symbol`` is BSE-shaped, an index, or a scrip code.
            TickerNotFoundError: The endpoint returned no rows.
            UpstreamServiceError: Transport failure or a changed schema.
        """
        return await self._fetch(symbol, period)

    def price_history(self, symbol: str, period: str = "1Y") -> pd.DataFrame:
        """Sync variant of :meth:`price_history_async`."""
        return _run_async(self.price_history_async(symbol, period))

    async def intraday_async(self, symbol: str) -> pd.DataFrame:
        """Nominally one-minute bars for the **current session only**.

        Returns 372 bars spanning 09:15-15:59 IST with a tz-aware ``Asia/Kolkata`` index.
        No historical intraday is available.

        .. warning::
           This feed is materially weaker than the daily series. Measured on the 2026-09-25
           RELIANCE session: 372 nominally-1-minute bars, median spacing 60s, with two gaps of
           ~18 min after 15:15 and 15:32 as the closing session thins out.

           **Volume is unusable.** 100 of 372 bars carried a *negative* volume (-1 to -4) and a
           further 75 carried 0 — 47% of bars. The session sums to 439 against an EOD volume in
           the millions. The daily series is clean on the same ticker. Negatives are clamped to 0
           here and counted in ``attrs['bfinance_negative_volume_bars']``, but that only makes
           the frame *safe*, not *correct*.

           **Use the Close series only. Take volume from** :meth:`price_history_async`.
        """
        return await self._fetch(symbol, INTRADAY_PERIOD)

    def intraday(self, symbol: str) -> pd.DataFrame:
        """Sync variant of :meth:`intraday_async`."""
        return _run_async(self.intraday_async(symbol))

    async def search_async(self, query: str, limit: int = 10) -> pd.DataFrame:
        """Resolve a free-text query to NSE symbols.

        ``GET /api/search?q=`` returned **seven** payload keys when measured on 2026-10-23:
        ``id``, ``type``, ``name``, ``ticker``, ``description``, ``matchScore`` and ``marketCap``.
        They are passed through verbatim; the frame has those seven columns on a hit and the five
        original columns (``ticker``, ``name``, ``description``, ``matchScore``, ``marketCap``) on
        an empty answer. Requires a non-empty query; returns an empty frame rather than raising
        when nothing matches, since a fuzzy miss is an expected outcome here.

        Note: an equities-only index appears to back this endpoint — ``q=nifty`` returned zero
        results on 2026-09-26 despite NIFTYBEES existing.
        """
        clean = (query or "").strip()
        if not clean:
            raise ValueError("query must be a non-empty string")
        url = f"{self.BASE_URL}/api/search"
        payload = await self._get_json(f"{url}?q={clean}")
        rows = payload.get("data") or []
        if not rows:
            return pd.DataFrame(columns=["ticker", "name", "description", "matchScore", "marketCap"])
        return pd.DataFrame(rows).head(limit)

    def search(self, query: str, limit: int = 10) -> pd.DataFrame:
        """Sync variant of :meth:`search_async`."""
        return _run_async(self.search_async(query, limit=limit))


_DEFAULT_CLIENT: Optional[MarketLensClient] = None


def _default_client() -> MarketLensClient:
    global _DEFAULT_CLIENT
    if _DEFAULT_CLIENT is None:
        _DEFAULT_CLIENT = MarketLensClient()
    return _DEFAULT_CLIENT


def price_history(symbol: str, period: str = "1Y") -> pd.DataFrame:
    """Module-level shortcut for :meth:`MarketLensClient.price_history`.

    >>> import bfinance.nse as nse
    >>> nse.price_history("RELIANCE", "5Y").tail()      # doctest: +SKIP
    """
    return _default_client().price_history(symbol, period)


def price_history_async(symbol: str, period: str = "1Y") -> pd.DataFrame:
    """Async variant of :func:`price_history`."""
    return _default_client().price_history_async(symbol, period)


def intraday(symbol: str) -> pd.DataFrame:
    """Module-level shortcut for :meth:`MarketLensClient.intraday`."""
    return _default_client().intraday(symbol)


def intraday_async(symbol: str) -> pd.DataFrame:
    """Async variant of :func:`intraday`."""
    return _default_client().intraday_async(symbol)
