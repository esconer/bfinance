"""
The transport for :mod:`bfinance.statements`: symbol -> trendlyne.com fundamentals payload.

Why this module exists
----------------------
:mod:`bfinance.statements` ships three payload *readers* and no way to obtain a payload. The
cascade's transport is caller-supplied (``statements/cascade.py:96``,
``Fetch = Callable[[ProviderSpec], Any]``), ``resolve_from_payloads`` takes the bytes as
arguments, and no module in the repo ever fetched the routes those readers expect. This is that
module. Nothing else in the package imports it, and importing it opens no socket — the sitemap is
read on first use, not at import time.

The three routes, all measured live on 2026-10-09
-------------------------------------------------
Every path this module can build is in :data:`ALLOWED_ROUTES`, and a test asserts that set is
exactly what is reachable, so a route cannot be added without the evidence behind it.

* ``GET /fundamental-sitemap-quarter-result.xml`` -- **200**, 970,942 bytes, 6,201 ``<loc>``
  entries of the form ``/fundamentals/financials/{stock_id}/{SYMBOL}/{slug}/``. The only resolver:
  trendlyne has no search API for fundamentals, the equity sitemap cannot be substituted (its
  slug segment for NTPC is the number ``77683`` while the financials slug is ``ntpc-ltd``), and
  the sitemaps are declared in the vendor's own robots.txt.
* ``GET /fundamentals/balance-sheet/{stock_id}/{SYMBOL}/{slug}/`` -- **200**, 83,150 bytes,
  **zero** ``<table>``. This is one of the five decoy tab shells
  (:mod:`bfinance.statements.registry` records the five), and it is fetched here for exactly one
  thing: the ``data-tablesurl`` attribute on ``<main id="fundamental_tables">``, which carries
  the per-symbol JSON token. It is NOT parsed as a data page and an empty table count here is
  expected, not a failure.
* ``GET /fundamentals/get-fundamental_results-v2/{stock_id}/{token}/`` -- **200**, 841,461 bytes,
  the ``{"head": ..., "body": ...}`` envelope with ``head.status == "0"``. This is the payload
  :func:`bfinance.statements.fundamentals_provider.parse_fundamental_results` reads, and the only
  trendlyne route that serves the **consolidated** basis.

Where the token comes from, and why it is not on the financials page
------------------------------------------------------------------
An earlier draft of this work assumed the token was unreachable, because
``/fundamentals/financialals/958/NTPC/ntpc-ltd/`` carries no reference to
``get-fundamental_results-v2`` at all. That is true and irrelevant: the page was re-rendered as a
React island and ``id="fundamental_tables"`` is now a ``<div>`` with no ``data-tablesurl``, but
the **tab shell** still carries the attribute and answers 200. So the token is reachable, on a
route that is itself robots-allowed, and this module reaches it. Both facts are measured rather
than inferred.

**The token rotates.** The leaf client at ``bfinance/trendlyne/client.py`` records the same
per-symbol token differing between two page captures, and a rejected token arrives as
``head.status != "0"`` on an HTTP 200 -- which is why :func:`_read_json` re-extracts once and
then raises, rather than retrying an unchanged request that will keep being refused.

Pacing, and what the 1000 ms is
--------------------------------
One module-level global slot, the same scheme ``bfinance.screener.client`` uses
(``_GLOBAL_PACING_LOCK`` + monotonic clock), so a fetcher here and the screener client observe
**one** floor instead of two private ones. The floor is 1000 ms because trendlyne's robots.txt
carries ``Crawl-delay: 1`` in its ``User-agent: *`` group: one request per second is a compliance
number, not a tuning knob. The 10 s figure in that file is scoped to ``GPTBot``/``ClaudeBot``
and does not apply here. A caller who wants a different posture -- a test, a mirror, a private
quota -- passes ``min_request_interval_ms=``; ``0.0`` disables pacing outright.

Retries cover 429, 5xx and transport failures only, with exponential backoff that honours
``Retry-After`` (capped at 30 s). Every 4xx is terminal. A 444 -- which on this vendor means the
request did not look like it came from a page on the site -- is terminal and is never handed to
``.json()``: a zero-length body would surface as a decode error, which is the most confusing
possible symptom.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import threading
import time
import types
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional

import httpx

from bfinance.utils.exceptions import ParsingError

from .errors import StatementSourceError

__all__ = [
    "ALLOWED_ROUTES",
    "BASE_URL",
    "FUNDAMENTALS_JSON_PATH",
    "FUNDAMENTALS_SITEMAP_PATH",
    "FUNDAMENTALS_TAB_SHELL_PATH",
    "FundamentalsFetchError",
    "FundamentalsTokenError",
    "FundamentalsPage",
    "MIN_REQUEST_INTERVAL_MS",
    "USER_AGENTS",
    "fetch_fundamentals_json",
    "fetch_fundamentals_json_sync",
    "fundamentals_index",
]

logger = logging.getLogger("bfinance")

#: The vendor, named once. ``bfinance.trendlyne`` is a leaf package and this module neither
#: imports it nor names it, per ``tests/test_trendlyne_core.py::TestIsolation``.
BASE_URL = "https://trendlyne.com"

#: The three routes this module can address. **Every** path built below is derived from one of
#: them, and ``tests/test_statements_fundamentals_fetch.py`` asserts that no path this module
#: can build falls outside :data:`ALLOWED_ROUTES`. Deliberately three and not four: the
#: ``/fundamentals/financials/`` SSR page is standalone-only (measured: it renders standalone
#: while its own config says ``is_consolidated: true``) and is not needed, because
#: ``body.parameterMetadata[id]['sstr']`` is byte-identical to the label that page renders. A
#: fourth route that adds a request and no capability is not worth having.
FUNDAMENTALS_SITEMAP_PATH = "/fundamental-sitemap-quarter-result.xml"
FUNDAMENTALS_TAB_SHELL_PATH = "/fundamentals/balance-sheet/{stock_id}/{symbol}/{slug}/"
FUNDAMENTALS_JSON_PATH = "/fundamentals/get-fundamental_results-v2/{stock_id}/{token}/"

#: Every route shape this module will request, as a normalised prefix. A path is in this set only
#: when it has been fetched and measured; the test over it is what stops a template being added
#: for a route nobody has proven.
ALLOWED_ROUTES = frozenset(
    {
        FUNDAMENTALS_SITEMAP_PATH,
        "/fundamentals/financials/",
        "/fundamentals/balance-sheet/",
        "/fundamentals/cash-flow/",
        "/fundamentals/quarterly-results/",
        "/fundamentals/annual-results/",
        "/fundamentals/financial-ratios/",
        "/fundamentals/get-fundamental_results-v2/",
    }
)

#: The pacing floor in milliseconds. A **compliance** number, not a tuning knob: trendlyne's
#: robots.txt carries ``Crawl-delay: 1`` in its ``User-agent: *`` group (read live 2026-10-09,
#: alongside 28 ``Disallow`` rules), and one second is one request per second. ``0.0`` disables
#: pacing; anything below this default is a decision to outrun a published floor.
MIN_REQUEST_INTERVAL_MS = 1000.0

#: Retry-After cap (seconds), so a hostile or accidental header cannot stall a caller.
_MAX_RETRY_AFTER_SEC = 30.0

#: The five tab shells, listed so ``data-tablesurl`` can be taken from any of them and so the
#: error message can name what a caller actually fetched. They are measured to answer 200 with
#: zero ``<table>`` and to differ only in ``<title>``.
_TAB_SHELL_PREFIXES = (
    "/fundamentals/balance-sheet/",
    "/fundamentals/cash-flow/",
    "/fundamentals/quarterly-results/",
    "/fundamentals/annual-results/",
    "/fundamentals/financial-ratios/",
)

#: Desktop browser strings, the same three ``bfinance.trendlyne.client`` and
#: ``bfinance.screener.client`` use. **Referenced, not imported** -- that package is a leaf.
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
]

_SITEMAP_LOC_RE = re.compile(r"/fundamentals/financials/(\d+)/([^/]+)/([^/?#]+)/?")
_DATA_TABLES_URL_RE = re.compile(
    r'data-tablesurl\s*=\s*["\']([^"\']*get-fundamental_results-v2/[^"\']+)["\']'
)
_TOKEN_IN_URL_RE = re.compile(r"/get-fundamental_results-v2/(\d+)/([A-Za-z0-9=]+)/")

# Global pacing state, shared with every caller in this process on the same scheme
# ``bfinance.screener.client`` uses (``_GLOBAL_PACING_LOCK`` + monotonic clock). Deliberately not
# a second, private algorithm.
_GLOBAL_PACING_LOCK = threading.Lock()
_GLOBAL_LAST_REQUEST_TIME = 0.0


class FundamentalsFetchError(StatementSourceError):
    """The fundamentals payload could not be obtained: resolution, transport or envelope.

    Subclasses :class:`~bfinance.statements.errors.StatementSourceError`, so the cascade treats it
    as an unavailable provider and falls through, and an existing
    ``except UpstreamServiceError`` handler catches it without learning this module exists.
    """


class FundamentalsTokenError(FundamentalsFetchError, ParsingError):
    """The tab shell answered but carried no ``data-tablesurl``, so no token could be taken.

    Named separately from a transport failure because the remedy is different: the route worked
    and the vendor changed its markup, which is a signal to re-measure rather than to retry.
    """


@dataclass(frozen=True)
class FundamentalsPage:
    """One resolved symbol: enough to address both routes this module needs.

    Attributes:
        symbol: The sitemap's own symbol segment, verbatim and upper case for equities. It is the
            vendor's key and is not guaranteed to equal the NSE ticker, which is why
            :func:`fundamentals_index` is keyed on it exactly.
        stock_id: The integer path segment that identifies the stock to the vendor.
        slug: The lowercase, hyphenated company name used in every URL.
    """

    symbol: str
    stock_id: int
    slug: str

    @property
    def tab_shell_path(self) -> str:
        """The decoy tab shell that carries the JSON token. No ``<table>`` is expected here."""
        return FUNDAMENTALS_TAB_SHELL_PATH.format(
            stock_id=self.stock_id, symbol=self.symbol, slug=self.slug
        )

    @property
    def tab_shell_url(self) -> str:
        """Absolute URL of :attr:`tab_shell_path`."""
        return f"{BASE_URL}{self.tab_shell_path}"

    @property
    def financials_path(self) -> str:
        """The SSR financials page. Addressable, but this module does not fetch it."""
        return f"/fundamentals/financials/{self.stock_id}/{self.symbol}/{self.slug}/"

    @property
    def financials_url(self) -> str:
        """Absolute URL of :attr:`financials_path`."""
        return f"{BASE_URL}{self.financials_path}"


def json_url_for(page: FundamentalsPage, token: str) -> str:
    """The absolute JSON URL for one resolved page and its current token."""
    if not token:
        raise FundamentalsTokenError(
            f"no trendlyne JSON token for {page.symbol!r}; the token is taken from "
            f"{FUNDAMENTALS_TAB_SHELL_PATH.format(stock_id=page.stock_id, symbol=page.symbol, slug=page.slug)}"
            " on every fetch because it rotates."
        )
    return f"{BASE_URL}{FUNDAMENTALS_JSON_PATH.format(stock_id=page.stock_id, token=token)}"


# ------------------------------------------------------------------ pacing + transport


async def _throttle_pacing(min_request_interval_ms: float) -> None:
    """Sleep until the global slot frees, on the screener client's algorithm.

    Reserved under lock so concurrent callers do not burst through the floor, and the jitter is
    10-50 ms -- small relative to a 1000 ms floor, and there so that a caller making several
    requests does not produce a metronomic signal.
    """
    global _GLOBAL_LAST_REQUEST_TIME
    wait_sec = 0.0
    with _GLOBAL_PACING_LOCK:
        now = time.monotonic()
        elapsed_ms = (now - _GLOBAL_LAST_REQUEST_TIME) * 1000.0
        if elapsed_ms < min_request_interval_ms:
            wait_sec = ((min_request_interval_ms - elapsed_ms) / 1000.0) + random.uniform(0.01, 0.05)
            _GLOBAL_LAST_REQUEST_TIME = now + wait_sec
        else:
            _GLOBAL_LAST_REQUEST_TIME = now
    if wait_sec > 0.0:
        await asyncio.sleep(wait_sec)


def _headers() -> Dict[str, str]:
    """Browser headers, rotating the UA so one client does not look like one client."""
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
    }


def _retry_after_seconds(response: Optional[httpx.Response]) -> Optional[float]:
    """``Retry-After`` in seconds, capped, or ``None`` when absent or unparseable."""
    raw = response.headers.get("Retry-After") if response is not None else None
    if raw is None:
        return None
    raw = raw.strip()
    if raw.isdigit():
        return min(float(raw), _MAX_RETRY_AFTER_SEC)
    try:  # the HTTP-date form, which a vendor is entitled to send
        from email.utils import parsedate_to_datetime

        when = parsedate_to_datetime(raw)
        if when is None:
            return None
        delta = (when - datetime_now_utc()).total_seconds()
    except (TypeError, ValueError, OverflowError):
        return None
    return max(0.0, min(delta, _MAX_RETRY_AFTER_SEC))


def datetime_now_utc():
    """``datetime.now(timezone.utc)``, factored out so the import is local to this function."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


async def _get(
    url: str,
    *,
    client: Optional[httpx.AsyncClient] = None,
    timeout: float = 30.0,
    max_retries: int = 3,
    min_request_interval_ms: float = MIN_REQUEST_INTERVAL_MS,
    referer: Optional[str] = None,
) -> httpx.Response:
    """One GET with pacing and retries. The single place this module opens a socket.

    Retries 429, 5xx and transport failures only; every 4xx is terminal; a 444 -- zero-length,
    the vendor's "this did not look like it came from the site" -- is terminal and is never
    handed to a decoder.

    **No branch sleeps after the final attempt.** All three retryable branches guard on
    ``attempt == max_retries - 1`` the same way, so the budget is ``max_retries - 1`` backoffs
    and not ``max_retries``: on a run of 5xx or a persistent 429 the loop falls straight through
    to the terminal message instead of adding a wait to a call that is already lost -- and with
    ``Retry-After`` at its 30 s cap, that wait was real seconds.

    ``referer`` matters on the JSON route and only there. Measured live 2026-10-09 on the *same*
    valid URL: without it the vendor answers **HTTP 444 with a zero-length body**; with the
    symbol's own financials page as the referer it answers 200 with 841,461 bytes of JSON. The
    plain HTML routes answer 200 either way. So the referer is set by the caller, exactly where
    the vendor requires it, rather than sent on every request and looking like a spoof.
    """
    headers = _headers()
    if referer:
        headers["Referer"] = referer
    saw_429 = False
    last_error: Optional[str] = None
    for attempt in range(max_retries):
        await _throttle_pacing(min_request_interval_ms)
        try:
            if client is not None:
                response = await client.get(url, headers=headers, timeout=timeout)
            else:
                async with httpx.AsyncClient(
                    timeout=timeout, headers=headers, follow_redirects=True
                ) as owned:
                    response = await owned.get(url)
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as e:
            last_error = str(e)
            if attempt == max_retries - 1:
                raise FundamentalsFetchError(
                    f"transport failure fetching {url} after {max_retries} attempt(s): {e}"
                ) from e
            await asyncio.sleep(1.0 * (attempt + 1) + random.uniform(0.2, 0.8))
            continue

        status = response.status_code
        if status == 429:
            saw_429 = True
            if attempt == max_retries - 1:
                # No sleep after the final attempt, exactly as the transport branch above: the loop
                # is done and the caller is about to be handed the rate-limit message, so the wait
                # buys nothing. With Retry-After at its 30 s cap that is a half-minute of dead time
                # on a call that was never going to succeed.
                continue
            backoff = (2**attempt) * 2.0 + random.uniform(0.5, 1.5)
            retry_after = _retry_after_seconds(response)
            if retry_after is not None:
                backoff = max(backoff, retry_after)
            logger.warning(
                "HTTP 429 from the fundamentals vendor on %s; backing off %.2fs (attempt %d/%d)",
                url, backoff, attempt + 1, max_retries,
            )
            await asyncio.sleep(backoff)
            continue
        if 500 <= status <= 599:
            if attempt == max_retries - 1:
                # Same guard as the transport branch: the final attempt falls straight through to
                # the terminal message rather than sleeping into a call that has already failed.
                continue
            backoff = 1.0 * (attempt + 1) + random.uniform(0.2, 0.8)
            retry_after = _retry_after_seconds(response)
            if retry_after is not None:
                backoff = max(backoff, retry_after)
            logger.warning(
                "HTTP %d from the fundamentals vendor on %s; retrying in %.2fs (attempt %d/%d)",
                status, url, backoff, attempt + 1, max_retries,
            )
            await asyncio.sleep(backoff)
            continue
        if status == 444 or (200 <= status < 300 and not response.content):
            # A zero-length 200/444 is a bot gate, not an empty page. Terminal, and never
            # decoded: an empty body reaching .json() is a confusing symptom of a clear cause.
            raise FundamentalsFetchError(
                f"the fundamentals vendor answered HTTP {status} with a zero-length body for "
                f"{url}. On this vendor that is a gate rather than a missing page: retrying an "
                f"unchanged request only spends its patience, so this is terminal."
            )
        if 400 <= status < 500:
            raise FundamentalsFetchError(
                f"the fundamentals vendor answered HTTP {status} for {url}. Every 4xx is "
                f"terminal: the route, not the retry count, is what is wrong."
            )
        if status >= 300:
            raise FundamentalsFetchError(
                f"the fundamentals vendor answered HTTP {status} for {url} after redirects."
            )
        return response

    if saw_429:
        raise FundamentalsFetchError(
            f"the fundamentals vendor rate-limited {url} after {max_retries} attempt(s)."
        )
    raise FundamentalsFetchError(
        f"the fundamentals vendor did not answer {url} after {max_retries} attempt(s)"
        f"{f' ({last_error})' if last_error else ''}."
    )


# ------------------------------------------------------------------ resolution


_INDEX_CACHE: Dict[str, Any] = {"loaded_at": 0.0, "pages": {}}
_INDEX_LOCK = threading.Lock()


def parse_fundamentals_sitemap(xml: str) -> Dict[str, FundamentalsPage]:
    """The symbol index from the fundamentals sitemap, keyed on the vendor's symbol segment.

    Exact match on that segment, upper-cased. Two measured properties shape this:

    * **Duplicate ``<loc>`` entries exist.** 20MICRONS appears twice in the live document. First
      wins, and the count is recorded rather than dropped silently, because a caller that sees
      6,201 URLs and 6,200 keys deserves to know why.
    * **The symbol segment is not always a ticker.** BSE index families are keyed by a number.
      Nothing here coerces a segment; an unrecognised key is simply absent, and
      :meth:`FundamentalsPage` is only ever built from what the vendor wrote.
    """
    pages: Dict[str, FundamentalsPage] = {}
    duplicates: List[str] = []
    for stock_id, symbol, slug in _SITEMAP_LOC_RE.findall(xml or ""):
        key = symbol.strip().upper()
        if not key or not stock_id.isdigit():
            continue
        if key in pages:
            duplicates.append(key)
            continue
        pages[key] = FundamentalsPage(
            symbol=key, stock_id=int(stock_id), slug=slug.strip().rstrip("/")
        )
    if duplicates:
        logger.debug(
            "fundamentals sitemap carries %d duplicate symbol segment(s): %s",
            len(duplicates), ", ".join(sorted(set(duplicates))[:10]),
        )
    return pages


async def fundamentals_index(
    *,
    client: Optional[httpx.AsyncClient] = None,
    ttl_hours: float = 24.0,
    timeout: float = 30.0,
    min_request_interval_ms: float = MIN_REQUEST_INTERVAL_MS,
    force_refresh: bool = False,
) -> Mapping[str, FundamentalsPage]:
    """The symbol index, read once and held in memory for ``ttl_hours``.

    The document is 970,942 bytes for 6,201 entries, so a TTL cache is the difference between one
    request per day and one per lookup. In-memory only: importing this module creates no file,
    and a caller who wants persistence passes a cache of their own at a higher layer.

    **The returned mapping is a read-only view** (:class:`types.MappingProxyType`), on both the
    hit path and the store path, so it is what the ``Mapping`` annotation has always claimed.
    Every ``Ticker`` statement accessor in the process resolves through here, so handing out the
    mutable ``dict`` behind the annotation would have let one caller's ``pages["X"] = ...``
    corrupt the resolution every other caller then reads. ``.get`` / ``[]`` / ``in`` / ``len`` /
    iteration are unaffected -- only assignment raises. :func:`parse_fundamentals_sitemap` still
    returns a plain ``dict``: it is public, and a caller who parsed their own document may
    reasonably want to edit what they parsed.
    """
    now = time.monotonic()
    with _INDEX_LOCK:
        cached = _INDEX_CACHE["pages"]
        fresh = _INDEX_CACHE["loaded_at"] > 0.0 and (now - _INDEX_CACHE["loaded_at"]) < ttl_hours * 3600.0
    if cached and fresh and not force_refresh:
        return cached

    response = await _get(
        f"{BASE_URL}{FUNDAMENTALS_SITEMAP_PATH}",
        client=client,
        timeout=timeout,
        min_request_interval_ms=min_request_interval_ms,
    )
    pages = parse_fundamentals_sitemap(response.text)
    if not pages:
        raise FundamentalsFetchError(
            f"the fundamentals sitemap at {FUNDAMENTALS_SITEMAP_PATH} parsed to zero entries "
            f"({len(response.text)} bytes). The route is declared in the vendor's robots.txt and "
            f"answered 200 on 2026-10-09 with 6,201 <loc> entries, so an empty parse is a "
            f"markup change rather than a missing document."
        )
    # Stored and returned as the same read-only view, so the first caller and every caller the
    # TTL cache later serves hold the identical object and neither can write to it.
    view = types.MappingProxyType(pages)
    with _INDEX_LOCK:
        _INDEX_CACHE["pages"] = view
        _INDEX_CACHE["loaded_at"] = time.monotonic()
    return view


# ------------------------------------------------------------------ the payload


def extract_json_token(shell_html: str, *, symbol: str = "") -> str:
    """The per-symbol JSON token from a tab shell's ``data-tablesurl``.

    The shell is a decoy as a *data* page and is fetched here for exactly this attribute. Taken
    from the attribute rather than from any JSON body, because the attribute is the only place
    the token is published: the SSR financials page no longer carries it (it was re-rendered,
    and ``id="fundamental_tables"`` is now a ``<div>`` with no such attribute).
    """
    match = _DATA_TABLES_URL_RE.search(shell_html or "")
    if not match:
        raise FundamentalsTokenError(
            f"no data-tablesurl on the fundamentals tab shell for {symbol or 'this symbol'}. "
            f"The attribute was present on 2026-10-09 on <main id=\"fundamental_tables\"> and "
            f"carried the only route serving the consolidated basis, so its absence is a markup "
            f"change to re-measure, not a reason to retry."
        )
    token_match = _TOKEN_IN_URL_RE.search(match.group(1))
    if not token_match:
        raise FundamentalsTokenError(
            f"data-tablesurl on the fundamentals tab shell does not name a JSON route with a "
            f"token: {match.group(1)[:120]!r}."
        )
    return token_match.group(2)


def _envelope_status(payload: Any) -> Optional[str]:
    """``head.status`` off the envelope, or ``None`` when the payload is not that shape.

    The vendor answers **HTTP 200** for a rejected token, so this -- not the status code -- is
    what tells a valid payload from a refusal. A client that checks only the status sees success
    and then an empty statement.
    """
    if not isinstance(payload, Mapping):
        return None
    head = payload.get("head")
    if not isinstance(head, Mapping):
        return None
    status = head.get("status")
    return None if status is None else str(status)


async def fetch_fundamentals_json(
    symbol: str,
    *,
    client: Optional[httpx.AsyncClient] = None,
    index: Optional[Mapping[str, FundamentalsPage]] = None,
    timeout: float = 60.0,
    max_retries: int = 3,
    min_request_interval_ms: float = MIN_REQUEST_INTERVAL_MS,
) -> Any:
    """The decoded fundamentals payload for one symbol, on the vendor's JSON route.

    Three requests at most: the sitemap (cached), the tab shell for the token, and the JSON. A
    rejected token re-scrapes the shell **once** -- the token rotates, so the one retry is
    measured to help and a second only spends the vendor's patience.

    Raises:
        FundamentalsFetchError: the symbol is not in the index, or a route did not answer.
        FundamentalsTokenError: no ``data-tablesurl``, or a token still rejected after one
            re-scrape.
    """
    key = (symbol or "").strip().upper()
    if not key:
        raise FundamentalsFetchError("a symbol is required; refusing to resolve an empty one.")

    pages = index if index is not None else await fundamentals_index(
        client=client, min_request_interval_ms=min_request_interval_ms
    )
    page = pages.get(key)
    if page is None:
        known = ", ".join(sorted(pages)[:10])
        raise FundamentalsFetchError(
            f"{key!r} is not in the fundamentals sitemap "
            f"({len(pages)} entries indexed). The sitemap is the only resolver this vendor "
            f"offers, and a miss means the symbol segment does not match -- not that the company "
            f"is missing. First ten indexed: {known}"
        )

    token = extract_json_token(
        (
            await _get(
                page.tab_shell_url,
                client=client,
                timeout=timeout,
                max_retries=max_retries,
                min_request_interval_ms=min_request_interval_ms,
            )
        ).text,
        symbol=key,
    )

    # The JSON route is an XHR in a browser, and the vendor says so: without a Referer naming a
    # page on its own site it answers HTTP 444 with a zero-length body (measured 2026-10-09 on the
    # same valid URL that returns 200 with a referer). The referer used is the symbol's own
    # financials page, which is the page a browser is on when this request is issued.
    referer = page.financials_url

    for attempt in (0, 1):
        response = await _get(
            json_url_for(page, token),
            client=client,
            timeout=timeout,
            max_retries=max_retries,
            min_request_interval_ms=min_request_interval_ms,
            referer=referer,
        )
        if not response.content:
            raise FundamentalsFetchError(
                f"zero-length body from the fundamentals JSON route for {key!r}."
            )
        try:
            payload = response.json()
        except ValueError as e:  # json.JSONDecodeError, and httpx's own decoder error
            raise FundamentalsFetchError(
                f"the fundamentals JSON route for {key!r} did not return decodable JSON "
                f"({len(response.content)} bytes): {e}"
            ) from e
        status = _envelope_status(payload)
        if status == "0":
            return payload
        if attempt == 0:
            # The token rotates, and a rejected token arrives as an HTTP 200 with
            # head.status != "0" -- so this is the one retry that is worth spending.
            logger.debug("fundamentals token for %s was rejected; re-scraping the shell once", key)
            token = extract_json_token(
                (
                    await _get(
                        page.tab_shell_url,
                        client=client,
                        timeout=timeout,
                        max_retries=max_retries,
                        min_request_interval_ms=min_request_interval_ms,
                    )
                ).text,
                symbol=key,
            )
            continue
        raise FundamentalsTokenError(
            f"the fundamentals JSON route rejected the token for {key!r} after one re-scrape "
            f"(head.status={status!r}). The vendor answers HTTP 200 for a refused token, so the "
            f"status code is not evidence of success; a second rejection means the token is not "
            f"the problem."
        )
    raise FundamentalsFetchError(  # pragma: no cover - the loop always returns or raises
        f"the fundamentals JSON route for {key!r} did not produce a payload."
    )


def fetch_fundamentals_json_sync(symbol: str, **kwargs: Any) -> Any:
    """Blocking wrapper over :func:`fetch_fundamentals_json`.

    One ``asyncio.run`` per call, matching the sync/async split the rest of this package uses.
    Never call this from inside a running loop -- that is what ``asyncio.run`` raises for, and a
    nested loop would be a caller bug, not a fallback to swallow.
    """
    return asyncio.run(fetch_fundamentals_json(symbol, **kwargs))
