"""
HTTP Client for Screener.in with connection pooling, retry mechanics, adaptive pacing, proxy support, and caching.
"""

import asyncio
import logging
import os
import random
import threading
import time
from typing import Any, Dict, List, Optional
import httpx

logger = logging.getLogger("bfinance")

# Global pacing state shared across all client instances so concurrent
# Ticker clients respect one rate limit instead of bursting N×.
_GLOBAL_PACING_LOCK = threading.Lock()
_GLOBAL_LAST_REQUEST_TIME = 0.0

# Retry-After cap (seconds) so a malicious/huge header can't stall us.
_MAX_RETRY_AFTER_SEC = 30.0


def _parse_retry_after(resp: httpx.Response) -> Optional[float]:
    """Parse Retry-After header (delta-seconds), capped; None if absent/invalid."""
    raw = resp.headers.get("Retry-After") if resp is not None else None
    if raw is None:
        return None
    try:
        delay = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    if delay < 0:
        return None
    return min(delay, _MAX_RETRY_AFTER_SEC)

from bfinance.cache.sqlite_cache import SQLiteCache
from bfinance.models.company import CompanyProfile
from bfinance.screener.charts import ScreenerChartEngine
from bfinance.screener.parser import ScreenerHTMLParser
from bfinance.utils.exceptions import RateLimitExceededError, TickerNotFoundError, UpstreamServiceError
from bfinance.utils.symbols import normalize_symbol


# Realistic desktop browser User-Agent pool for stealth rotation
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:130.0) Gecko/20100101 Firefox/130.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.6; rv:130.0) Gecko/20100101 Firefox/130.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36 Edg/128.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
]


class ScreenerClient:
    """
    Production-hardened client with IP protection, rate-limit avoidance, proxy routing, and caching.
    """

    BASE_URL = "https://www.screener.in"

    def __init__(
        self,
        timeout: float = 15.0,
        cache_ttl_hours: float = 24.0,
        max_retries: int = 3,
        proxy: Optional[str] = None,
        min_request_interval_ms: float = 150.0,
        cache: Optional[SQLiteCache] = None,
    ):
        self.timeout = timeout
        self.max_retries = max_retries
        self.proxy = proxy or os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
        self.min_request_interval_ms = min_request_interval_ms
        self._last_request_time = 0.0
        self.cache = cache or (SQLiteCache(default_ttl_hours=cache_ttl_hours) if cache_ttl_hours > 0 else None)
        self.parser = ScreenerHTMLParser()

    def _get_browser_headers(self) -> Dict[str, str]:
        """Generate realistic randomized browser headers to blend with normal traffic."""
        ua = random.choice(USER_AGENTS)
        headers = {
            "User-Agent": ua,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9,hi;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "Sec-Ch-Ua": '"Chromium";v="128", "Not;A=Brand";v="24", "Google Chrome";v="128"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
        }
        return headers

    async def _throttle_pacing(self):
        """Enforce request pacing with randomized jitter to prevent burst detection."""
        global _GLOBAL_LAST_REQUEST_TIME
        # Reserve a global slot under lock so concurrent clients don't burst;
        # single-client timing behavior is unchanged.
        with _GLOBAL_PACING_LOCK:
            now = time.monotonic()
            last = max(self._last_request_time, _GLOBAL_LAST_REQUEST_TIME)
            elapsed_ms = (now - last) * 1000.0
            if elapsed_ms < self.min_request_interval_ms:
                # Sleep remainder + 10-50ms random jitter
                jitter = random.uniform(0.01, 0.05)
                wait_sec = ((self.min_request_interval_ms - elapsed_ms) / 1000.0) + jitter
                _GLOBAL_LAST_REQUEST_TIME = now + wait_sec
            else:
                wait_sec = 0.0
                _GLOBAL_LAST_REQUEST_TIME = now
        if wait_sec > 0.0:
            await asyncio.sleep(wait_sec)
        self._last_request_time = time.monotonic()
        with _GLOBAL_PACING_LOCK:
            _GLOBAL_LAST_REQUEST_TIME = max(_GLOBAL_LAST_REQUEST_TIME, self._last_request_time)

    async def _send_with_retry(
        self,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        follow_redirects: bool = True,
    ) -> httpx.Response:
        req_headers = self._get_browser_headers()
        if headers:
            req_headers.update(headers)

        saw_429 = False
        for attempt in range(self.max_retries):
            await self._throttle_pacing()
            try:
                client_kwargs = {
                    "timeout": self.timeout,
                    "headers": req_headers,
                    "follow_redirects": follow_redirects,
                }
                if self.proxy:
                    client_kwargs["proxy"] = self.proxy

                async with httpx.AsyncClient(**client_kwargs) as client:
                    resp = await client.get(url, params=params)
                    if resp.status_code == 429:
                        saw_429 = True
                        retry_after = _parse_retry_after(resp)
                        backoff = (2 ** attempt) * 2.0 + random.uniform(0.5, 1.5)
                        if retry_after is not None:
                            backoff = max(backoff, retry_after)
                        logger.warning(
                            "HTTP 429 Rate Limit from Screener.in on %s. Backing off for %.2fs (attempt %d/%d)...",
                            url, backoff, attempt + 1, self.max_retries
                        )
                        await asyncio.sleep(backoff)
                        continue
                    if 500 <= resp.status_code <= 599:
                        retry_after = _parse_retry_after(resp)
                        backoff = 1.0 * (attempt + 1) + random.uniform(0.2, 0.8)
                        if retry_after is not None:
                            backoff = max(backoff, retry_after)
                        logger.warning(
                            "HTTP %d from Screener.in on %s. Retrying in %.2fs (attempt %d/%d)...",
                            resp.status_code, url, backoff, attempt + 1, self.max_retries
                        )
                        await asyncio.sleep(backoff)
                        continue
                    return resp
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as e:
                if attempt == self.max_retries - 1:
                    logger.error("Failed to connect to Screener.in after %d attempts: %s", self.max_retries, e)
                    raise UpstreamServiceError(f"Network error communicating with Screener.in: {e}") from e
                backoff = 1.0 * (attempt + 1) + random.uniform(0.2, 0.8)
                logger.warning(
                    "Network error on %s: %s. Retrying in %.2fs (attempt %d/%d)...",
                    url, e, backoff, attempt + 1, self.max_retries
                )
                await asyncio.sleep(backoff)

        if saw_429:
            raise RateLimitExceededError("Screener.in rate limit exceeded after maximum retries.")
        raise UpstreamServiceError("Screener.in request failed after maximum retries.")

    async def search(self, query: str) -> List[Dict[str, Any]]:
        """
        Search companies on Screener.in by name, symbol, or scrip code.
        Returns list of {'id': int, 'name': str, 'url': str}.
        """
        clean_q = normalize_symbol(query)
        cache_key = f"screener_search_{clean_q}"
        if self.cache:
            cached = self.cache.get(cache_key)
            if cached is not None:
                return cached

        url = f"{self.BASE_URL}/api/company/search/"
        resp = await self._send_with_retry(url, params={"q": clean_q})
        if resp.status_code != 200:
            return []

        try:
            results = resp.json()
            if self.cache and results:
                self.cache.set(cache_key, results, category="search", ttl_hours=168.0)  # 7 days
            return results
        except Exception:
            return []

    async def resolve_company_id(self, symbol: str) -> Optional[int]:
        """Resolve numeric company ID from symbol."""
        clean = normalize_symbol(symbol)
        results = await self.search(clean)
        if results:
            for item in results:
                url = item.get("url", "")
                if f"/company/{clean}/" in url or f"/company/{clean.upper()}/" in url:
                    return item.get("id")
            # If no exact match, return first item's ID
            return results[0].get("id")
        return None

    async def get_company_profile(self, ticker: str, force_refresh: bool = False) -> CompanyProfile:
        """
        Fetch and parse complete company fundamental profile.
        """
        symbol = normalize_symbol(ticker)
        cache_key = f"screener_profile_{symbol}"

        if not force_refresh and self.cache:
            cached = self.cache.get(cache_key)
            if cached is not None:
                try:
                    p = CompanyProfile.model_validate(cached)
                    if p.ratios.current_price is not None or p.ratios.market_cap is not None:
                        return p
                except Exception:
                    pass

        # 1. Resolve Company ID
        comp_id = await self.resolve_company_id(symbol)

        # 2. Fetch Main HTML (Consolidated first, fallback to Standalone)
        is_consolidated = True
        url = f"{self.BASE_URL}/company/{symbol}/consolidated/"
        resp = await self._send_with_retry(url)

        if resp.status_code == 404:
            is_consolidated = False
            url = f"{self.BASE_URL}/company/{symbol}/"
            resp = await self._send_with_retry(url)
            if resp.status_code == 404:
                raise TickerNotFoundError(f"Ticker '{symbol}' not found on Screener.in")

        if resp.status_code != 200:
            raise UpstreamServiceError(f"Screener.in returned status code {resp.status_code}")

        main_html = resp.text
        final_url = str(resp.url)

        # 3. Fetch Dynamic Peers HTML if company ID is available
        peers_html = None
        if comp_id:
            try:
                peers_resp = await self._send_with_retry(f"{self.BASE_URL}/api/company/{comp_id}/peers/")
                if peers_resp.status_code == 200:
                    peers_html = peers_resp.text
            except Exception:
                pass

        # 4. Parse full profile
        profile = self.parser.parse_full_profile(
            symbol=symbol,
            company_id=comp_id or 0,
            main_html=main_html,
            peers_html=peers_html,
            is_consolidated=is_consolidated,
            page_url=final_url,
        )

        # 4b. Standalone fallback if consolidated page lacks ratios (e.g. IRFC, BSE, standalone entities)
        if is_consolidated and (profile.ratios.current_price is None or profile.ratios.market_cap is None):
            try:
                st_url = f"{self.BASE_URL}/company/{symbol}/"
                st_resp = await self._send_with_retry(st_url)
                if st_resp.status_code == 200:
                    st_profile = self.parser.parse_full_profile(
                        symbol=symbol,
                        company_id=comp_id or 0,
                        main_html=st_resp.text,
                        peers_html=peers_html,
                        is_consolidated=False,
                        page_url=str(st_resp.url),
                    )
                    if st_profile.ratios.current_price is not None or st_profile.ratios.market_cap is not None:
                        profile = st_profile
            except Exception as e:
                logger.debug("Failed standalone fallback for %s: %s", symbol, e)

        # Cache profile
        if self.cache:
            self.cache.set(cache_key, profile.model_dump(), category="profile")

        return profile

    async def get_chart_timeseries(
        self, ticker: str, metric: str = "price", days: int = 1825, force_refresh: bool = False
    ) -> Any:
        """
        Fetch historical price or valuation time series as a Pandas DataFrame.
        metric options: 'price', 'pe', 'margins', 'ev_ebitda', 'pb', 'mcap_sales'.
        days: 30 (1M), 180 (6M), 365 (1Y), 1095 (3Y), 1825 (5Y), 3652 (10Y), 10000 (Max).
        """
        symbol = normalize_symbol(ticker)
        comp_id = await self.resolve_company_id(symbol)
        if not comp_id:
            raise TickerNotFoundError(f"Cannot resolve company ID for '{symbol}'")

        q_param = ScreenerChartEngine.get_query_param(metric)
        cache_key = f"screener_chart_{comp_id}_{q_param}_{days}"

        raw_json = None
        if not force_refresh and self.cache:
            raw_json = self.cache.get(cache_key)

        if raw_json is None:
            url = f"{self.BASE_URL}/api/company/{comp_id}/chart/?q={q_param}&days={days}"
            resp = await self._send_with_retry(url)
            if resp.status_code != 200:
                raise UpstreamServiceError(f"Chart API error for '{symbol}' ({q_param}): {resp.status_code}")
            raw_json = resp.json()
            if self.cache:
                self.cache.set(cache_key, raw_json, category="chart", ttl_hours=6.0)

        return ScreenerChartEngine.parse_chart_json_to_dataframe(raw_json)
