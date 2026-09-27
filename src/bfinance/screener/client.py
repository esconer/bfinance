"""
HTTP Client for Screener.in with connection pooling, retry mechanics, adaptive pacing, proxy support, and caching.
"""

import asyncio
import logging
import os
import random
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Union
import httpx
import pandas as pd

logger = logging.getLogger("bfinance")

#: Every screener.in company page carries its numeric id in ``data-company-id``. This is how a
#: symbol is resolved without touching the search route.
_COMPANY_ID_RE = re.compile(r'data-company-id="(\d+)"')


def extract_company_id(html: str) -> Optional[int]:
    """Read the numeric company id off a screener.in company page, or ``None`` if absent.

    screener.in's robots.txt contains ``Disallow: /*?q=``, and the search route is
    ``/api/company/search/?q=...`` — so the previous resolver was calling a disallowed endpoint,
    and the failure surfaced as a silently-empty profile that looked like missing data. The id
    rides on the page the client was already fetching, so removing the search call also removes a
    request rather than adding one.
    """
    if not html:
        return None
    m = _COMPANY_ID_RE.search(html)
    return int(m.group(1)) if m else None

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
from bfinance.models.company import CompanyProfile, PeerStock
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


class _DefaultCache:
    """Sentinel for the ``cache`` argument: the caller did not choose, so the
    ``cache_ttl_hours`` default applies.

    The parameter used to default to ``None``, which made two opposite intents
    spell the same thing. ``cache=None`` is the obvious way to say "do not cache",
    but ``cache or (SQLiteCache(...) if ...)`` collapsed it into "build the real
    home-directory cache" — a caller who believed they had disabled caching was
    silently reading and writing ``~/.bfinance/cache.db``. An omitted argument and
    an explicit ``None`` are now distinct values with distinct meanings.
    """

    def __repr__(self) -> str:
        return "<default cache>"


USE_DEFAULT_CACHE = _DefaultCache()


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
        cache: Union[SQLiteCache, None, _DefaultCache] = USE_DEFAULT_CACHE,
    ):
        self.timeout = timeout
        self.max_retries = max_retries
        self.proxy = proxy or os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
        self.min_request_interval_ms = min_request_interval_ms
        self._last_request_time = 0.0
        # Omitted -> honour cache_ttl_hours. Explicit None -> no caching at all.
        # Explicit cache -> use that object. The three cases used to be two.
        if isinstance(cache, _DefaultCache):
            self.cache = SQLiteCache(default_ttl_hours=cache_ttl_hours) if cache_ttl_hours > 0 else None
        else:
            self.cache = cache
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

        Raises:
            UpstreamServiceError: the search endpoint did not answer 200. Screener.in
                IP-blocks (HTTP 403), and returning an empty list for that made a block
                indistinguishable from a genuine no-match, which callers then reported
                to the user as a delisted ticker. An empty list now means only one
                thing: the request succeeded and nothing matched.
            RateLimitExceededError: HTTP 429 after the retries are exhausted.

        ⚠ **This route is disallowed by screener.in's robots.txt** (``Disallow: /*?q=``), so there is
        no compliant way to use it. **No internal code path calls it any more** —
        :meth:`resolve_company_id` reads ``data-company-id`` off the company page instead, which is
        both compliant and one request cheaper. It is kept only because it is public API and is the
        sole way to search by company *name*. A caller who invokes it is knowingly stepping outside
        the published policy. This is an interim state pending a name-search replacement, not a
        recommendation.
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
            # This URL is a fixed route, not symbol-keyed, so a non-200 here can never
            # mean "no such ticker" — there is no ticker in it. _send_with_retry has
            # already exhausted its retries on 429/5xx, so whatever lands here is a
            # hard failure (403 block, 404 route change, 4xx). Classify, don't guess.
            raise UpstreamServiceError(
                f"Screener.in search returned HTTP {resp.status_code} for '{clean_q}'. "
                f"This is an upstream failure, not an empty result set "
                f"(a 403 means the request was blocked)."
            )

        try:
            results = resp.json()
            if self.cache and results:
                self.cache.set(cache_key, results, category="search", ttl_hours=168.0)  # 7 days
            return results
        except Exception:
            return []

    async def resolve_company_id(self, symbol: str) -> Optional[int]:
        """Resolve the numeric company id from a symbol.

        Reads ``data-company-id`` off ``/company/{symbol}/``.

        ⚠ This **used to** call ``/api/company/search/?q=...`` via :meth:`search`. screener.in's
        robots.txt disallows ``/*?q=``, so the entire fundamentals path was riding on a disallowed
        endpoint — and when it was blocked, ``search()`` returned an empty list, ``None`` was
        returned here, and the caller carried on with ``company_id=0`` and produced a silently
        empty profile. A block was indistinguishable from missing data.

        The page carries the id, so the compliant route costs the same one request and the
        ``data-company-id`` extraction is exact rather than a best-match guess against search
        ordering.

        Returns:
            The company id, or ``None`` if the page has no id attribute (a page shape change, which
            is now visible as a missing attribute rather than a swallowed HTTP error).
        """
        clean = normalize_symbol(symbol)
        resp = await self._send_with_retry(f"{self.BASE_URL}/company/{clean}/")
        if resp.status_code != 200:
            raise UpstreamServiceError(
                f"Could not resolve company id for '{clean}': Screener.in returned "
                f"HTTP {resp.status_code} for /company/{clean}/"
            )
        comp_id = extract_company_id(resp.text)
        if comp_id is None:
            logger.warning(
                "No data-company-id on /company/%s/ - Screener.in markup may have changed.", clean
            )
        return comp_id

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

        # 1. Fetch Main HTML (Consolidated first, fallback to Standalone)
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

        # 2. Company id rides on the page we just fetched.
        #    This used to be a separate resolve_company_id() call first, which hit the
        #    robots-disallowed /api/company/search/?q= route. Reading it here costs no extra request
        #    and cannot be silently blocked into a company_id=0 profile.
        comp_id = extract_company_id(main_html)
        if comp_id is None:
            logger.warning(
                "No data-company-id on %s - peers will be skipped and company_id will be 0.", final_url
            )

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
        # Reachable only when search() actually succeeded and matched nothing, so
        # TickerNotFoundError here is now a claim about the symbol, not about the
        # upstream being unreachable.
        comp_id = await self.resolve_company_id(symbol)
        if not comp_id:
            raise TickerNotFoundError(f"Cannot resolve company ID for '{symbol}'")

        q_param = ScreenerChartEngine.get_query_param(metric)
        cache_key = f"screener_chart_{comp_id}_{q_param}_{days}"

        raw_json = None
        if not force_refresh and self.cache:
            raw_json = self.cache.get(cache_key)

        if raw_json is None:
            # `companyId` first, deliberately. screener.in's robots.txt carries
            # `Disallow: /*?q=`, where `*` is the only wildcard and `?` is literal - so it forbids
            # a query string whose FIRST parameter is `q`, and permits the same parameter later.
            # This URL used to be `?q={q_param}&days={days}`, which matches that rule on every
            # call. Putting `companyId` first does not change the response (both chart recordings
            # in tests/fixtures/screener/ were taken from exactly this shape) - it only stops the
            # request from being a disallowed one.
            url = (
                f"{self.BASE_URL}/api/company/{comp_id}/chart/"
                f"?companyId={comp_id}&q={q_param}&days={days}"
            )
            resp = await self._send_with_retry(url)
            if resp.status_code != 200:
                raise UpstreamServiceError(f"Chart API error for '{symbol}' ({q_param}): {resp.status_code}")
            raw_json = resp.json()
            if self.cache:
                self.cache.set(cache_key, raw_json, category="chart", ttl_hours=6.0)

        return ScreenerChartEngine.parse_chart_json_to_dataframe(raw_json)

    # ============================================================ the /market/ tree

    #: ``/market/`` is unadvertised (not in the nav, not linked from a company page) and needs no
    #: query string, so no ``robots.txt`` rule can reach it. The aggregates move slowly, hence the
    #: long default; the leaf pages below move faster.
    MARKET_OVERVIEW_URL = f"{BASE_URL}/market/"

    async def get_market_industries(self, force_refresh: bool = False) -> "pd.DataFrame":
        """Fetch the ``/market/`` Industries Overview: one row per leaf industry, with aggregates.

        **One request, no query string.** ``https://www.screener.in/market/`` is 177,020 bytes of
        HTML whose single ``table.data-table`` carries all **188 leaf industries**, each with its
        full four-level code path and eight aggregates — the sector taxonomy of the whole Indian
        market in a single fetch.

        ⚠ **It is an unadvertised entry point.** It is not linked from the top navigation and not
        from any company page; a company page's ``/market/`` breadcrumb points at a *leaf* page
        (``/market/IN04/IN0401/IN040107/IN040107001/``), never at this one. If you cannot find the
        link in the UI, nothing is wrong.

        ⚠ **Three company-count universes exist for the same node and none of them is the truth.**
        Summed market-wide on 2026-09-27: this page's own "No. of Companies" column = **3,319**;
        the leaf pages' own ``N results found`` = **5,468**; the browse dropdown's child count =
        **7,068**. The filters behind each are undetermined, and they disagree *within one node on
        one day* (Pharmaceuticals: 159 here, 218 on its leaf page, 225 implied by 9 pages x 25).
        **This page's own count is the most conservative of the three**, and it is the one returned,
        under ``attrs["bfinance_market_companies_total_leaf_table"]`` — the key names its basis so a
        consumer can never mistake it for the other two. Nothing is averaged or reconciled.

        Returns:
            A ``pandas.DataFrame`` of 188 rows, indexed by the leaf code
            (``IN020101002``; ``index.name == "industry_code"``), with columns ``industry``,
            ``code_path`` (the full ``IN02/IN0201/IN020101/IN020101002`` path) and the eight
            aggregates ``no_of_companies``, ``total_market_cap``, ``median_market_cap``,
            ``median_pe``, ``sales_growth``, ``opm``, ``wtd_avg_roce``, ``median_1y_return``. Market
            caps are ₹ crore, percentages are whole numbers, and an aggregate screener.in does not
            report is ``None`` rather than 0.0.

            ``attrs``:

            ==========================  ====================================================
            key                          meaning
            ==========================  ====================================================
            ``bfinance_source_url``      the URL fetched
            ``bfinance_retrieved_at``    UTC ISO-8601 timestamp of that fetch
            ``bfinance_market_row_count``          rows parsed (188)
            ``bfinance_market_skipped_rows``       non-industry ``<tr>`` skipped (13 repeated
                                           header rows on the recording)
            ``bfinance_market_companies_total_leaf_table``
                                           sum of the page's own "No. of Companies" (3,319)
            ``bfinance_market_single_request``     always ``True``: one request, no pagination.
                                           Contrast with
                                           ``bfinance_constituents_pagination_robots_blocked``
                                           on the leaf route, which *is* a ceiling
            ==========================  ====================================================

        Raises:
            UpstreamServiceError: HTTP 200 was not the answer. A page that is not the overview is
                an upstream failure, and returning an empty frame for it would be indistinguishable
                from a market with no industries.
        """
        cache_key = "screener_market_industries"
        html = None
        if not force_refresh and self.cache:
            html = self.cache.get(cache_key)

        url = self.MARKET_OVERVIEW_URL
        if html is None:
            resp = await self._send_with_retry(url)
            if resp.status_code != 200:
                raise UpstreamServiceError(
                    f"Screener.in /market/ returned HTTP {resp.status_code}. This is an upstream "
                    f"failure, not an empty industry set (a 403 means the request was blocked)."
                )
            html = resp.text
            # The cached value is the raw HTML, not the frame: the cache stores plain strings, and
            # re-parsing 99 KB of table is cheaper than inventing a frame serialiser that nobody
            # else in this package would use.
            if self.cache:
                self.cache.set(cache_key, html, category="market", ttl_hours=24.0)

        frame = self.parser.parse_market_industries(html)
        frame.attrs["bfinance_source_url"] = url
        frame.attrs["bfinance_retrieved_at"] = datetime.now(timezone.utc).isoformat()
        frame.attrs["bfinance_market_single_request"] = True
        if not len(frame):
            logger.warning("/market/ parsed to zero industries; Screener.in markup may have changed.")
        return frame

    #: The leaf page's own pager emits ``?page=`` and ``?limit=``, both of which robots.txt
    #: disallows, so a compliant fetch can only ever see the first page.
    INDUSTRY_PAGE_SIZE = 25

    @staticmethod
    def _leaf_url(code: str) -> str:
        """Build the ``/market/`` URL for an industry code or code path.

        A **bare leaf code** is expanded to its full four-level path by screener.in's own numbering
        rule — ``IN040107001`` -> ``IN04/IN0401/IN040107/IN040107001`` — so the index of
        :meth:`get_market_industries` is directly usable as a key here, with no lookup request in
        between. The rule was checked against all 188 leaves of the recorded overview and reproduces
        every ``code_path`` exactly; the offline suite keeps it honest.

        A path is passed through unchanged rather than second-guessed, so nothing here invents a URL
        shape. Note that only the four-segment (**leaf**) form is known to carry a constituents
        table — a shorter path is a sector/group/industry page, and what it yields (if anything) is
        screener.in's business to decide, reported honestly either way.

        Raises:
            ValueError: ``code`` is not a screener.in industry code or path. Raised locally, because
                building a 404 out of a typo would spend a request to learn something knowable.
        """
        path = str(code).strip().strip("/")
        if path.lower().startswith("market/"):
            path = path[len("market/"):]
        if not re.fullmatch(r"[A-Za-z0-9]+(?:/[A-Za-z0-9]+){0,3}", path):
            raise ValueError(
                f"{code!r} is not a screener.in industry code or code path (expected e.g. "
                f"'IN040107001' or 'IN04/IN0401/IN040107/IN040107001')"
            )
        if "/" not in path and len(path) > 8:
            # A bare leaf code, not a sector/group/industry code: expand it. Sector codes are short
            # (IN04, IN0401, IN040107) and are deliberately left alone - they are real pages too.
            path = "/".join((path[:4], path[:6], path[:8], path))
        return f"{ScreenerClient.BASE_URL}/market/{path.upper()}/"

    async def get_industry_constituents(
        self, code: str, force_refresh: bool = False
    ) -> "pd.DataFrame":
        """Fetch the companies screener.in lists for one leaf industry — **a partial list, always**.

        ⚠ **A complete constituent list is not robots-obtainable from this source.** The page's own
        pager links to ``?page=2…`` and its page-size control to ``?limit=10/25/50&page=1``; all of
        those match ``Disallow: /*?page=`` and ``Disallow: /*?limit=``. The bulk alternative,
        ``POST /api/export/screen/?url_name=browse_industry&mes_code=IN04``, answers **403**
        unauthenticated. So one compliant request returns the first **25** rows of a list that can
        hold **159** — measured across all 188 leaves: **153 fit in one page, 35 are truncated, and
        1,246 companies are unreachable** without violating robots.txt.

        This method therefore does not pretend the frame is complete. Partiality is recorded, not
        implied:

        ==============================  =====================================================
        ``attrs`` key                  meaning
        ==============================  =====================================================
        ``bfinance_constituents_partial``  ``True`` when the page says more rows exist
        ``bfinance_constituents_total_reported``  the page's own ``N results found``
        ``bfinance_constituents_returned``         rows actually parsed
        ``bfinance_constituents_page_size``        the fixed 25, not a request parameter
        ``bfinance_constituents_page`` / ``..._pages``  the page's ``Showing page X of Y``
        ``bfinance_constituents_not_returned``     ``total_reported - returned`` when positive,
                                        else 0
        ``bfinance_constituents_pagination_robots_blocked``  always ``True``: why this is partial
        ``bfinance_source_url`` / ``bfinance_retrieved_at``  provenance
        ==============================  =====================================================

        ⚠ **``not_returned`` and the market-wide "1,246 unreachable" are different numbers and
        must not be added together or compared.** The 1,246 is derived from ``/market/``'s own
        (most conservative) per-leaf counts; ``not_returned`` is derived from this page's own,
        *larger* ``results found`` claim. The two disagree even for one node — Pharmaceuticals
        reports 218 here and 159 in the overview — and the page's arithmetic does not close either
        (9 pages x 25 = 225). Both numbers are kept separate and named after the count they came
        from rather than merged into one confident figure.

        ``partial`` is ``None`` — a third state, neither "complete" nor "truncated" — when the page
        carries no ``div[data-page-info]`` at all, because then the source has said nothing about
        completeness and this method will not speak for it. Check the key, not the row count: a
        25-row frame from a 5-company industry and a 25-row frame from a 218-company industry are
        the same shape and mean opposite things.

        Args:
            code: a leaf code (``"IN040107001"`` — expanded to its full path by screener.in's own
                numbering rule), a code path (``"IN04/IN0401/IN040107/IN040107001"``), or the
                ``/market/``-prefixed form. A bare code is exactly the index
                :meth:`get_market_industries` returns.
            force_refresh: bypass the cache.

        Returns:
            A ``pandas.DataFrame`` indexed by screener.in symbol (falling back to the company name
            for a row that links nowhere), with the 11 columns :meth:`_parse_peers` fills:
            ``rank``, ``name``, ``symbol``, ``cmp``, ``pe``, ``market_cap_cr``,
            ``dividend_yield``, ``net_profit_qtr``, ``qtr_profit_var``, ``sales_qtr``,
            ``qtr_sales_var``, ``roce``.

        Raises:
            ValueError: ``code`` is not an industry code or path. Raised locally, because
                building a 404 out of a typo would be a request spent to learn something knowable.
            UpstreamServiceError: a non-200 answer. Screener.in answers 404 for a valid-looking
                code that is not a leaf, and that must not look like an industry with no companies.
        """
        url = self._leaf_url(code)
        cache_key = f"screener_industry_{url.rsplit('/market/', 1)[-1]}"
        html = None
        if not force_refresh and self.cache:
            html = self.cache.get(cache_key)

        if html is None:
            resp = await self._send_with_retry(url)
            if resp.status_code != 200:
                raise UpstreamServiceError(
                    f"Screener.in industry page {url} returned HTTP {resp.status_code}. A valid "
                    f"leaf code always answers 200, so this is an upstream failure (404 = not a "
                    f"leaf industry, 403 = blocked) and not an industry with no companies."
                )
            html = resp.text
            if self.cache:
                self.cache.set(cache_key, html, category="market", ttl_hours=12.0)

        peers, page_info = self.parser.parse_industry_constituents(html)
        frame = (
            pd.DataFrame([p.model_dump() for p in peers], columns=list(PeerStock.model_fields))
            .set_index(pd.Index([p.symbol or p.name for p in peers], name="symbol", dtype="object"))
            if peers
            else pd.DataFrame(columns=list(PeerStock.model_fields)).set_index(
                pd.Index([], name="symbol", dtype="object")
            )
        )

        total = page_info["results_found"]
        pages = page_info["pages"]
        if total is None or pages is None:
            # The page made no completeness claim. `partial` stays None on purpose.
            partial: Optional[bool] = None
        else:
            partial = pages > 1 or (len(peers) < total)
        if partial:
            logger.warning(
                "%s returned %d of %s companies (page %s of %s). screener.in's robots.txt blocks "
                "the ?page=/?limit= parameters needed to fetch the rest, so this list is "
                "permanently partial - see df.attrs['bfinance_constituents_partial'].",
                url, len(peers), total, page_info["page"], pages,
            )

        frame.attrs.update(
            bfinance_source_url=url,
            bfinance_retrieved_at=datetime.now(timezone.utc).isoformat(),
            bfinance_industry_code=url.rsplit("/market/", 1)[-1].strip("/").rsplit("/", 1)[-1],
            bfinance_constituents_partial=partial,
            bfinance_constituents_total_reported=total,
            bfinance_constituents_returned=len(peers),
            bfinance_constituents_page_size=self.INDUSTRY_PAGE_SIZE,
            bfinance_constituents_page=page_info["page"],
            bfinance_constituents_pages=pages,
            bfinance_constituents_not_returned=(
                max(0, total - len(peers)) if total is not None else None
            ),
            bfinance_constituents_pagination_robots_blocked=True,
        )
        return frame
