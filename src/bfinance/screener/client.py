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
from typing import Any, Dict, List, Optional, Tuple, Union
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


def _company_id_cache_key(symbol: str) -> str:
    """The single cache key a symbol's company id is stored under.

    ``symbol`` is expected **normalised**, which is what both call sites already have in hand:
    :meth:`ScreenerClient.resolve_company_id` normalises to build the page URL, and
    :meth:`ScreenerClient.get_company_profile` normalises for every other key it writes. This is the
    one key where a second spelling costs a request rather than a row, so a caller that hands over a
    raw ticker turns one entry into three -- ``TCS``, ``tcs`` and ``TCS.NS`` must be the same
    company, and ``tests/test_screener_company_id_cache.py`` drives those three spellings through
    the public methods to keep it that way.
    """
    return f"screener_company_id_{symbol}"

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

    #: TTL, in hours, for every category this client caches -- the whole vocabulary, in one place,
    #: because a TTL that lives only at the call site that happens to use it is not reviewable.
    #:
    #: The rule behind the numbers: a TTL may be no longer than the fastest-moving thing in the
    #: payload, and each entry below is the fastest-moving thing in *its* payload.
    #:
    #: =====================  ======  ===========================================================
    #: key                    hours   what moves in it, and why the number is that one
    #: =====================  ======  ===========================================================
    #: ``search``             168.0  company-name -> id resolution: static for the life of a
    #:                                  listing, so it can afford the longest life here.
    #: ``company_id``         168.0  symbol -> id: the same kind of fact ``search`` resolves, read
    #:                                  off the company page instead of the disallowed search
    #:                                  route. An id does not change for the life of a listing, so
    #:                                  it takes the longest life in this table; and it is the key
    #:                                  the chart cache is *built* from, so a shorter life here
    #:                                  would put a live request back in front of every chart read.
    #: ``profile``               1.0  carries ``ratios.current_price`` and ``market_cap``. A price
    #:                                  is not "slightly old", it is a *wrong number* the moment
    #:                                  the market moves, so this sits BELOW the 5-year chart
    #:                                  series: 60 minutes, not 24 hours. The 24h this replaced was
    #:                                  never a decision -- it was ``SQLiteCache``'s
    #:                                  ``default_ttl_hours`` reached by accident, because this was
    #:                                  the one call site of the five that passed no ``ttl_hours``.
    #: ``chart``                 6.0  a 5-year daily price series, re-asked far less often than a
    #:                                  quote.
    #: ``market_overview``      24.0  ``/market/``: 188 industries of aggregates, moving daily at
    #:                                  worst.
    #: ``market_leaf``          12.0  a leaf industry page: 25 companies, re-ranked intraday.
    #: =====================  ======  ===========================================================
    #:
    #: The keys name what is cached, which is why the two ``/market/`` rows differ -- both are
    #: stored under ``category="market"`` (the cache table has one category column), but they are
    #: separate pages with separate ages.
    #:
    #: A caller who needs a different number sets it on the instance
    #: (``client.CATEGORY_TTL_HOURS["profile"] = 168.0``); each instance gets its own copy, so one
    #: client cannot silently retune another.
    CATEGORY_TTL_HOURS: Dict[str, float] = {
        "search": 168.0,
        "company_id": 168.0,
        "profile": 1.0,
        "chart": 6.0,
        "market_overview": 24.0,
        "market_leaf": 12.0,
    }

    def ttl_hours(self, category: str) -> float:
        """The explicit TTL for a cache category, in hours.

        Every ``cache.set`` in this client goes through here, so no cached payload can take a TTL
        by falling off the end of a call. An unknown key raises rather than defaulting: a typo in a
        category name must not become "some other category's TTL".
        """
        return self.CATEGORY_TTL_HOURS[category]

    def __init__(
        self,
        timeout: float = 15.0,
        cache_ttl_hours: float = 24.0,
        max_retries: int = 3,
        proxy: Optional[str] = None,
        min_request_interval_ms: float = 150.0,
        cache: Union[SQLiteCache, None, _DefaultCache] = USE_DEFAULT_CACHE,
    ):
        """Args:
            timeout: per-request HTTP timeout, seconds.
            cache_ttl_hours: **A default, not a ceiling.** When ``cache`` is omitted it becomes
                ``SQLiteCache(default_ttl_hours=...)`` -- the TTL for a category that does not name
                one -- and a value of ``0`` or less skips the cache entirely (no ``SQLiteCache`` is
                built at all). Pass an explicit ``cache`` and the argument is ignored: that object
                carries its own default.

                Every category this client actually caches names its own TTL, and every one of them
                therefore *ignores* this argument: ``search`` 168.0h, ``company_id`` 168.0h,
                ``profile`` 1.0h, ``chart`` 6.0h, ``market_overview`` 24.0h, ``market_leaf`` 12.0h
                (see :data:`CATEGORY_TTL_HOURS`, and ``ttl_hours``). To retune one, set it on the
                instance. Before that was true, this argument was the *only* lever on the
                price-bearing ``profile`` entry -- which is precisely how a 24-hour-old
                ``currentPrice`` became the shipped default without anyone choosing it.
            max_retries: attempts per request before the 5xx/429 backoff gives up. The error text
                on exhaustion names this number ("failed after N attempts"), so it is asserted by
                ``tests/test_nse_bhavcopy.py`` and ``tests/test_nse_corporate_actions.py``.
            proxy: optional HTTP/HTTPS/SOCKS5 proxy URL; falls back to ``HTTPS_PROXY``/``HTTP_PROXY``.
            min_request_interval_ms: floor on the gap between two requests from any client in the
                process.
            cache: ``USE_DEFAULT_CACHE`` (omit) to build the home-directory
                :class:`~bfinance.cache.sqlite_cache.SQLiteCache`; ``None`` to cache nothing;
                a :class:`~bfinance.cache.sqlite_cache.SQLiteCache` to use that one.
        """
        self.timeout = timeout
        self.max_retries = max_retries
        self.proxy = proxy or os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
        self.min_request_interval_ms = min_request_interval_ms
        self._last_request_time = 0.0
        # Per-instance copy of the TTL vocabulary, so `client.CATEGORY_TTL_HOURS[...] = x` retunes
        # this client and not every client in the process.
        self.CATEGORY_TTL_HOURS = dict(type(self).CATEGORY_TTL_HOURS)
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
            # Brotli is deliberately NOT advertised. httpx decodes gzip/deflate natively but
            # only decodes brotli when the optional `brotli` (or `brotlicffi`) package is
            # installed; without it `resp.text` is the raw compressed bytes, so the parser sees no
            # tables and every screener-backed member degrades to empty. Measured live
            # 2026-10-11: screener.in answers `Accept-Encoding: gzip, deflate, br` with
            # `Content-Encoding: br`, and the same request with br removed returns gzip which
            # decodes to HTML with `data-company-id` present. Re-add br only if the dependency
            # is added to pyproject.toml.
            "Accept-Encoding": "gzip, deflate",
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

    # ================================================================= cache plumbing

    def _cache_read(self, cache_key: str) -> Tuple[Optional[Any], str]:
        """Read a cache entry **and** the
        :class:`~bfinance.cache.sqlite_cache.CacheLookup` name for how it was found.

        Copied from :meth:`bfinance.ownership.OwnershipClient._cache_read` rather than invented,
        because the vocabulary is now shared: the status is the ``CacheLookup.status`` string and
        nothing else, so a consumer learns it from one key in one package.

        The previous form of this call was ``self.cache.get(cache_key)``, which is
        :meth:`SQLiteCache.get` — and that returns ``.value`` and throws the status away. Every
        one of this client's five cache reads did it, so ``cached is not None`` was the only signal
        any caller had, and it is a signal about *presence*, not about *provenance*: a row holding
        JSON ``null``, a row that had expired and a row that could not be read all arrive as
        ``None`` and are all followed by a refetch. That is the safe direction for *fetching*, and
        it is why nothing was ever wrong — but it means the frame a caller finally received could
        not say whether anything was cached at all.

        ``"disabled"`` is returned when this client has no cache. It is not a value the frame will
        ever carry (a disabled cache also returns no value, so the caller fetches and reports
        ``"miss"``), and it is here for the same reason it is in ``ownership``: a sentinel beats a
        ``None`` status, which would be indistinguishable from a real one.
        """
        if not self.cache:
            return None, "disabled"
        lookup = self.cache.get_result(cache_key)
        return lookup.value, lookup.status

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
            # Deliberately NOT routed through `_cache_read`. This method returns a plain
            # `List[Dict[str, Any]]`, and there is nowhere on a list to publish a status: the
            # attrs vocabulary (`attrs["bfinance_cache_status"]`, ownership's) is a DataFrame
            # convention and inventing a parallel one for lists would be a second vocabulary for
            # the same fact. Switching the read would therefore change nothing observable. It also
            # stays `get()` rather than `get_result()` because there is no caller here that could
            # use the extra distinction: `None` already means "fetch it", which is the safe
            # reading of a miss, an expired row, a stored null and a corrupt row alike.
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
                self.cache.set(
                    cache_key, results, category="search", ttl_hours=self.ttl_hours("search")
                )
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

        **The mapping is cached** (``category="company_id"``, 168h: see
        :data:`CATEGORY_TTL_HOURS`), because an id is *identity*, not a price: it does not change
        for the life of a listing, so re-reading the page can only discover whether the page is
        reachable today. That is not a local detail -- ``get_chart_timeseries`` *builds* its cache
        key from the id, so while this call always went to the network, a warm six-hour chart row
        still cost one request per call and the chart cache was defeated (measured: chart calls
        whose frame already said ``'hit'`` were spending one request each). A caller who has read a
        profile pays nothing even for the first resolve, because :meth:`get_company_profile` writes
        this row from the page it had already fetched; the profile path deliberately does **not**
        call this method, which would add a request there rather than remove one here.

        A row holding anything but an ``int`` -- a stored ``null``, a hand-edited value -- is read
        as a miss and the page is re-read. The page is the only authority, and a wrong id does not
        mean "no chart", it means another company's chart.

        Returns:
            The company id, or ``None`` if the page has no id attribute (a page shape change, which
            is now visible as a missing attribute rather than a swallowed HTTP error). ``None`` is
            never cached: there is no id to remember, and a negative answer cached for a week would
            outlive the markup break that produced it.
        """
        clean = normalize_symbol(symbol)
        cache_key = _company_id_cache_key(clean)
        # `_cache_read` rather than `self.cache.get`: one read path, and the value arrives with the
        # `CacheLookup` status beside it. The status is not published, for the same reason
        # `search()` documents for its own read -- an `int` is not a frame and has nowhere to
        # carry it.
        cached_id = self._cache_read(cache_key)[0]
        if isinstance(cached_id, int):
            return cached_id

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
        elif self.cache:
            self.cache.set(
                cache_key, comp_id, category="company_id", ttl_hours=self.ttl_hours("company_id")
            )
        return comp_id

    async def get_company_profile(self, ticker: str, force_refresh: bool = False) -> CompanyProfile:
        """
        Fetch and parse complete company fundamental profile.

        The returned :class:`~bfinance.models.company.CompanyProfile` carries ``cache_status``:
        ``"hit"`` when the profile came out of the cache, ``"miss"`` when this call fetched it
        (which includes ``force_refresh=True`` and a client built with ``cache=None``). It is the
        model's equivalent of ``attrs['bfinance_cache_status']`` on the frame methods, and it is
        excluded from ``model_dump()``, so the cached payload has the shape it always had.
        """
        symbol = normalize_symbol(ticker)
        cache_key = f"screener_profile_{symbol}"

        # The store's word for "there was no lookup to make", exactly as in the chart and /market/
        # readers below -- and, also as there, never what the caller receives: a profile carries
        # `cache_status`, and every path that builds one has either measured a hit or fetched.
        cache_status = "disabled"
        if not force_refresh:
            # This read goes through `_cache_read` like every other one, and the status now has
            # somewhere to live. It used to be `self.cache.get`, and the comment here used to call
            # that a real gap: the return value is a `CompanyProfile` -- a pydantic model, not a
            # frame -- so `attrs` has nowhere to go, and adding a field to the model would change
            # `model_dump()`, which is exactly what the write below stores.
            #
            # The model now carries `cache_status` with `Field(exclude=True)`, which is what makes
            # that objection false rather than merely inconvenient: the status travels on the
            # object, and `model_dump()` -- and therefore the cached payload -- stays byte-for-byte
            # what it was before the field existed. So a hit below reports the lookup's own name,
            # and the live path reports "miss"; the one-hour-old `ratios.current_price` and
            # `market_cap` this used to replay silently can now say that they are old.
            cached, cache_status = self._cache_read(cache_key)
            if cached is not None:
                try:
                    p = CompanyProfile.model_validate(cached)
                    if p.ratios.current_price is not None or p.ratios.market_cap is not None:
                        # The lookup's own word, not a hardcoded second spelling of it: a non-None
                        # value is exactly `CacheLookup.status == "hit"`, and if that contract ever
                        # grows a value-bearing status this line publishes it instead of guessing.
                        p.cache_status = cache_status
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
        #
        #    ...and the id is cached from here, also for free. The direction matters: this method
        #    writes the row that `resolve_company_id` reads, and never calls the resolver -- routing
        #    the profile through it would add a request to *this* path, while the chart path is
        #    where a request is removed (its cache key is built from the id).
        comp_id = extract_company_id(main_html)
        if comp_id is None:
            logger.warning(
                "No data-company-id on %s - peers will be skipped and company_id will be 0.", final_url
            )
        elif self.cache:
            self.cache.set(
                _company_id_cache_key(symbol), comp_id, category="company_id",
                ttl_hours=self.ttl_hours("company_id"),
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

        # A fetch happened, so whatever the lookup said before it -- "miss", "expired", "null",
        # "corrupt" -- the object the caller receives was built from the pages fetched just now,
        # and the one status a frame is allowed to carry for that is "miss". "disabled" is not
        # published either, for the same reason: it describes the store, not this profile.
        profile.cache_status = "miss"

        # Cache profile. The TTL is stated here because this payload carries a price
        # (`ratios.current_price`, `market_cap`) and a price cannot be cached for a day and still
        # be a price. It used to pass no `ttl_hours` at all and inherited 24h from the cache
        # default -- the only one of the five call sites that did, so the number nobody chose was
        # also the one that shipped.
        if self.cache:
            # `model_dump()` leaves `cache_status` out (`Field(exclude=True)`), so this payload has
            # the same shape it had before the field existed -- which is what keeps the status a
            # provenance fact rather than the first key of a cache-format change.
            self.cache.set(
                cache_key, profile.model_dump(), category="profile",
                ttl_hours=self.ttl_hours("profile"),
            )

        return profile

    async def get_chart_timeseries(
        self, ticker: str, metric: str = "price", days: int = 1825, force_refresh: bool = False
    ) -> Any:
        """
        Fetch historical price or valuation time series as a Pandas DataFrame.
        metric options: 'price', 'pe', 'margins', 'ev_ebitda', 'pb', 'mcap_sales'.
        days: 30 (1M), 180 (6M), 365 (1Y), 1095 (3Y), 1825 (5Y), 3652 (10Y), 10000 (Max).

        The frame carries ``attrs['bfinance_cache_status']`` — the real
        :class:`~bfinance.cache.sqlite_cache.CacheLookup` status for the read, ``'hit'`` when the
        raw JSON came out of the cache and ``'miss'`` when this call fetched it. Before that, a
        cached chart frame was indistinguishable from a live one, because this parser published no
        attrs at all; see :func:`bfinance.screener.charts.ScreenerChartEngine.parse_chart_json_to_dataframe`.
        """
        symbol = normalize_symbol(ticker)
        # Reachable only when the symbol's company page answered 200 *without* a
        # `data-company-id`, which is where the id comes from now that `/api/company/search/?q=`
        # is robots-disallowed. So TickerNotFoundError here is a claim about that page -- either
        # a symbol screener.in has no id for, or a markup break that removed the attribute -- and
        # not about the upstream being unreachable: a 403, 404 or 5xx raises UpstreamServiceError
        # inside the resolver, before this line is reached.
        comp_id = await self.resolve_company_id(symbol)
        if not comp_id:
            raise TickerNotFoundError(f"Cannot resolve company ID for '{symbol}'")

        q_param = ScreenerChartEngine.get_query_param(metric)
        cache_key = f"screener_chart_{comp_id}_{q_param}_{days}"

        raw_json = None
        cache_status = "disabled"
        if not force_refresh:
            raw_json, cache_status = self._cache_read(cache_key)

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
                self.cache.set(
                    cache_key, raw_json, category="chart", ttl_hours=self.ttl_hours("chart")
                )
            cache_status = "miss"

        frame = ScreenerChartEngine.parse_chart_json_to_dataframe(raw_json)
        frame.attrs["bfinance_cache_status"] = cache_status
        return frame

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
            ``bfinance_retrieved_at``    UTC ISO-8601 timestamp of **this call**, not of the
                                           fetch — on a cache hit it is the moment the cached
                                           HTML was re-parsed. Pair it with
                                           ``bfinance_cache_status``, which says which.
            ``bfinance_cache_status``    ``"hit"`` when the HTML came from the cache,
                                           ``"miss"`` when this call fetched it. The real
                                           :class:`~bfinance.cache.sqlite_cache.CacheLookup`
                                           status, never inferred from ``html is not None``.
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
        cache_status = "disabled"
        if not force_refresh:
            html, cache_status = self._cache_read(cache_key)

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
                self.cache.set(
                    cache_key, html, category="market", ttl_hours=self.ttl_hours("market_overview")
                )
            cache_status = "miss"

        frame = self.parser.parse_market_industries(html)
        frame.attrs["bfinance_source_url"] = url
        frame.attrs["bfinance_retrieved_at"] = datetime.now(timezone.utc).isoformat()
        frame.attrs["bfinance_market_single_request"] = True
        frame.attrs["bfinance_cache_status"] = cache_status
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
        ``bfinance_cache_status``                            ``"hit"`` when the HTML came from
                                        the cache, ``"miss"`` when this call fetched it
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
        cache_status = "disabled"
        if not force_refresh:
            html, cache_status = self._cache_read(cache_key)

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
                self.cache.set(
                    cache_key, html, category="market", ttl_hours=self.ttl_hours("market_leaf")
                )
            cache_status = "miss"

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
            bfinance_cache_status=cache_status,
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
