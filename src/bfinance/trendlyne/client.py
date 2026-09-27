"""
trendlyne.com client — optional, isolated, and traceable to the byte.

What this package is
--------------------
trendlyne.com is a **third-party** Indian market analytics site. It is not an exchange, not NSE,
and not a broker feed. It republishes and derives data that bfinance already gets from better
sources, and its value is coverage breadth: share-holding patterns, mutual-fund holdings, bulk
and block deals, insider trades, board meetings, research reports and news, none of which any
official Indian source exposes as a free no-auth route.

That is exactly why it lives in its own top-level package instead of under ``bfinance/nse/``, and
why the isolation is a hard rule rather than a preference:

1. **No inbound edges.** Nothing in the main path imports this package. The only import site is a
   user's explicit ``from bfinance.trendlyne import ...``, and ``bfinance/__init__.py`` does not
   import it. If trendlyne is unreachable, rate-limits us, changes its markup or disappears,
   every other bfinance call behaves exactly as before.
2. **No import-time I/O.** Constructing :class:`TrendlyneClient` opens no socket and reads no
   file. The sitemap, the symbol index and the per-symbol tokens are all fetched on first use.
3. **A typed error, or a frame that says what it is.** Every public call raises a
   :class:`~bfinance.trendlyne.errors.TrendlyneError` subclass or returns a frame carrying
   ``attrs["bfinance_provenance"] == "trendlyne"`` plus a truthful note. Nothing is filled,
   interpolated or smoothed. A missing number stays missing.

The price-basis warning — read this before concatenating anything
----------------------------------------------------------------
The OHLC route is **yfinance-basis**: back-adjusted for corporate actions. It is *not* as-traded.
Verified against RELIANCE on 2026-09-27: bars before the 2024-10-28 bonus are exactly 0.5x the
NSE bhavcopy as-traded close, and match yfinance to the paisa.

A positive control for that same capture, because "adjusted" is a claim that should be testable
from both sides: the 2026-09-24 RELIANCE bar here is ``Close 1219.2, Volume 13923795``, which is
byte-for-byte what :mod:`bfinance.nse.marketlens` returns for the same session (see the recorded
``tests/fixtures/marketlens/reliance_1y.json``). Two independent sources agreeing exactly on a
recent bar, and disagreeing by exactly the corporate-action factor on an old one, is what a
back-adjusted series should look like — and it is why the two must not be concatenated.

:mod:`bfinance.nse.bhavcopy` is as-traded. Concatenating the two produces a series with a
corporate-action-sized discontinuity in it — precisely the class of defect this package exists to
prevent. Every OHLC frame therefore carries::

    df.attrs["bfinance_trendlyne_price_basis"] = "yfinance_back_adjusted"
    df.attrs["bfinance_synthetic_ohlc"] = False

``synthetic_ohlc = False`` says the O/H/L are real (they are: the invariant
``High >= max(Open, Close)`` and ``Low <= min(Open, Close)`` held on **51,070 of 51,070** rows
across 10 symbols, and on all 5,107 rows of this module's own recorded capture). The basis attr is
what you need to decide whether the frame may be joined to another source's bars.

Endpoints — verified live 2026-09-27, one request at a time
----------------------------------------------------------
Base ``https://trendlyne.com``. No login, no API key, no cookie: a browser ``User-Agent`` is the
whole requirement. Templates live in :data:`ENDPOINTS`; build one with :func:`build_path` rather
than by string-formatting, so the snapshot-only and token rules are enforced locally.

Symbol resolution is a sitemap, because there is no search API
--------------------------------------------------------------
``GET /equity-sitemap-stocks.xml`` returns 5,922 ``<loc>`` entries of the form
``/equity/{ID}/{SYMBOL}/{slug}/``. Every guessed search route 404s, so this document is the only
resolver. It is 831 KB, hence the in-memory TTL cache (24 h default) and the opt-in
:class:`~bfinance.cache.sqlite_cache.SQLiteCache`.

**The symbol segment is not always a ticker.** The tail of the live sitemap holds
``/equity/3622447/1216/bse-midsmall-private-banks/`` — a BSE index family keyed by the number
``1216``. Lookup is exact-match on that segment; :meth:`TrendlyneClient.search` is the escape
hatch, because the slug carries the human-readable name.

The per-symbol token, and what 444 actually means
--------------------------------------------------
The OHLC route needs an opaque per-symbol token that exists **only** inside the symbol's own page,
in a ``data-eodurl`` attribute (``/mapp/v1/stock/web/ohlc/878/<token>/``). It is not a constant:
MOTHERSON's is ``TQZITAIRHO5FCC7SYAKGO6M7QA======``, RELIANCE's is
``4EL732ZR47YQ7OPFML7VCNHP6Y======``. Two requests per symbol — one HTML page, one JSON — after
which the token is cached.

The 444 is a bot gate, not a bad token. This corrects the most natural reading of the failure and
is the single most important thing to know about this API. Measured 2026-09-27 on the *same*
valid MOTHERSON URL:

=========================  =========================================================
request                    result
=========================  =========================================================
no ``Referer``             **HTTP 444, zero-length body**
``Referer: <the page>``    HTTP 200, 396 KB of JSON
``Referer``, wrong token   HTTP **200**, 92 bytes,
                           ``head.status == "2"`` / ``"Not Authorized !"``
=========================  =========================================================

Two consequences, and they need different types:

* **444 means the request did not look like it came from a page on the site.** It is terminal and
  not retried: retrying an unchanged request only spends the vendor's patience. It must never
  reach ``response.json()`` — a zero-length body would surface as a decode error, which is the
  most confusing possible symptom.
* **A rejected token is a 200.** Status-code checking alone reports success and hands the caller
  an empty body. :func:`parse_envelope` treats any ``head.status != "0"`` as
  :class:`~bfinance.trendlyne.errors.TrendlyneNotAuthorizedError`, and
  :meth:`TrendlyneClient.ohlc` re-scrapes the token **once** on that failure, in case it rotated.

``/mapp/v1/`` requests carry a ``Referer``; the HTML page routes do not need one (verified 200
without). HTML pages work with a bare browser ``User-Agent``.

Snapshots and series are different things — and the two holding routes disagree
-----------------------------------------------------------------------------
``share-holding`` serves **only** the literal path segment ``latest``. Verified 2026-09-27:
``.../Jun-2024/...`` and ``.../2024/...`` both return **HTTP 500**, not 404 and not an empty
table. So the share-holding route cannot address a period at all. The single ``latest`` page is
still informative — it embeds a wide multi-period grid (44 distinct period labels on MOTHERSON,
from Dec 2015 to Jun 2026) — but it is one request, not a series you can walk.

``monthly-mutual-fund-share-holding`` is **not** snapshot-only, contrary to what the same test
suggests for share-holding. Verified on MOTHERSON: ``latest`` -> 200 (355 rows, top period
Aug-2026), ``2024`` -> 200 (313 rows, top period Sep-2024), ``Jun-2024`` -> 200 (260 rows, top
period Jun-2024), ``2023`` -> 200 (179 rows). The segment genuinely filters.

The distinction is per-route, so it is data, not prose: :data:`SNAPSHOT_ONLY_ENDPOINTS` plus
:func:`build_path`, which raises :class:`~bfinance.trendlyne.errors.TrendlyneSnapshotOnlyError`
locally rather than spending a request to collect a 500. Deals and insider trades are the
opposite extreme — 10-16 years in one page.

``eodData`` is newest-first, and the freshest week is in a different key
-----------------------------------------------------------------------
The daily body is 5,107 bars spanning 2006-01-02 to 2026-09-18 **for every symbol measured** —
MOTHERSON, RELIANCE, TCS, INFY alike. It is a fixed vendor window, *not* listing history, and this
module never claims otherwise. The payload also carries ``eodDataLastWeek``, which on the same
capture holds 2026-09-21 to 2026-09-25 — i.e. the five most recent sessions that ``eodData`` does
**not** contain. A frame built from ``eodData`` alone is therefore a week behind the vendor's own
latest bar; ``ohlc(include_last_week=True)`` (the default) merges those five in, keeping
``eodData`` authoritative for any date both keys carry, and records the merge in ``attrs``.
Verified live 2026-09-27: RELIANCE returns 5,107 + 5 = **5,112** bars ending 2026-09-25, and TCS
the same. A frame built from ``eodData`` alone stops at 2026-09-18.

Two more measured details, both recorded in ``attrs`` rather than smoothed over:

* Rows arrive **newest-first**. The frame is sorted ascending; the wire order is not preserved.
* ``close_price`` and ``last_price`` are **different fields** and disagree on most bars — 5,024
  of 5,112 rows on a live RELIANCE pull, 82 of 120 on this module's recorded capture, typically
  by a few paisa. ``Close`` is ``close_price``, because that is the field consistent with the
  bar's O/H/L; the divergence count is recorded in
  ``attrs["bfinance_trendlyne_close_last_divergent_rows"]`` so the difference is visible rather
  than silently resolved.

A trap for whoever parses ``chart-data`` next
----------------------------------------------
``/mapp/v1/stock/chart-data/{ID}/{METRIC}/`` needs no token, and its ``eodData`` is
``[epoch_ms, value]`` pairs. The epochs are **midnight IST on quarter-end dates**
(2026-06-30, 2026-03-31, 2025-12-31, 2025-09-30 … all at 00:00 +05:30), so they must be converted
to ``Asia/Kolkata`` before the date is taken. Reading them as UTC dates lands every point one day
early. :meth:`TrendlyneClient.chart_metric` returns the raw payload; the parsing is deliberately
left to the sibling module that owns it.

Pacing, retries and caching
---------------------------
Pacing is the package's existing global-slot scheme, ported verbatim from
``bfinance.screener.client`` and ``bfinance.nse.bhavcopy`` (``_GLOBAL_PACING_LOCK``,
``_GLOBAL_LAST_REQUEST_TIME``, ``_throttle_pacing``) — the same algorithm, not a second private
one. The default interval is **750 ms**, deliberately far above the 100-150 ms used for
screener.in and MarketLens: this is a commercial site serving a heavy page per symbol, two
requests to the chart, and nothing here is worth being impatient with. Retry covers 429, 5xx and
transport failures only, with exponential backoff that honours ``Retry-After`` (capped at 30 s);
every 4xx is terminal.

Caching is opt-in on disk. The sitemap index and the tokens live in memory with a TTL. Passing
``cache=SQLiteCache(...)`` additionally persists the index; the default is ``None``, so importing
this package never creates a file in the user's home directory.
"""

from __future__ import annotations

import asyncio
import html as _html
import logging
import os
import random
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import quote_plus, unquote

import httpx
import numpy as np
import pandas as pd

from bfinance.utils.symbols import normalize_symbol

from .errors import (
    TrendlyneAccessDeniedError,
    TrendlyneError,
    TrendlyneHTTPError,
    TrendlyneNotAuthorizedError,
    TrendlyneNotFoundError,
    TrendlyneRateLimitedError,
    TrendlyneSchemaError,
    TrendlyneServerError,
    TrendlyneSnapshotOnlyError,
    TrendlyneSymbolNotFoundError,
    TrendlyneTransportError,
)
from .tables import PROVENANCE, extract_ohlc_token

logger = logging.getLogger("bfinance")

#: Provenance tag on every frame. Re-exported from :mod:`bfinance.trendlyne.tables`, which owns it.
BASE_URL = "https://trendlyne.com"

#: The only symbol resolver trendlyne has. 5,922 entries, 831 KB, refreshed daily.
SITEMAP_PATH = "/equity-sitemap-stocks.xml"

#: trendlyne's own autocomplete, used as the *fallback* resolver. The sitemap does not list
#: ETFs -- NIFTYBEES, BANKBEES, GOLDBEES, JUNIORBEES and SETFNIF50 all resolve here and serve
#: full OHLC, yet none appear among the 5,922 sitemap entries. Query-string endpoint, so it is
#: kept out of :data:`ENDPOINTS` (those are path templates validated by :func:`build_path`).
#: A miss answers ``[{"headline": "No matches found"}]``, which is a result, not an error.
_SYMBOL_SEARCH_PATH = "/member/api/ac_snames/?term={query}"

#: Every route this package knows, as ``str.format`` templates over
#: ``stock_id``, ``symbol``, ``slug``, ``period``, ``metric`` and ``token``. Use
#: :func:`build_path` instead of formatting these directly.
ENDPOINTS: Dict[str, str] = {
    "stock_page": "/equity/{stock_id}/{symbol}/{slug}/",
    "ohlc": "/mapp/v1/stock/web/ohlc/{stock_id}/{token}/",
    "chart_metric": "/mapp/v1/stock/chart-data/{stock_id}/{metric}/",
    "share_holding": "/equity/share-holding/{stock_id}/{symbol}/{period}/{slug}/",
    "mutual_fund_holdings":
        "/equity/monthly-mutual-fund-share-holding/{stock_id}/{symbol}/{period}/{slug}/",
    "bulk_deals": "/equity/bulk-block-deals/{symbol}/{stock_id}/{slug}/",
    "insider_trading": "/equity/insider-trading-sast/all/{symbol}/{stock_id}/{slug}/",
    "financials": "/fundamentals/financials/{stock_id}/{symbol}/{slug}/",
    "research_reports": "/research-reports/stock/{stock_id}/{symbol}/{slug}/",
    "rights": "/equity/rights/{symbol}/{stock_id}/{slug}-rights/",
    "board_meetings": "/equity/board-meeting/{symbol}/{stock_id}/{slug}-board_meeting/",
    "buy_sell_zone": "/tools/buy-sell-zone/{stock_id}/{symbol}/{slug}/",
    "news": "/latest-news/{stock_id}/{symbol}/{slug}/",
    "news_only": "/latest-news/only-news/{stock_id}/{symbol}/{slug}/",
    "news_analyst_calls": "/latest-news/analyst-calls/{stock_id}/{symbol}/{slug}/",
    "news_bse_announcements": "/latest-news/BSE-Announcements/{stock_id}/{symbol}/{slug}/",
    "corporate_actions": "/equity/corporate-actions/{symbol}/{stock_id}/{slug}/",
}

#: The only period segment the snapshot-only routes accept.
SNAPSHOT_PERIOD = "latest"

#: Routes that serve the latest snapshot and **cannot** address a period. Verified 2026-09-27:
#: ``share-holding`` answers HTTP 500 for ``Jun-2024`` and for ``2024``. Note that
#: ``mutual_fund_holdings`` is deliberately absent — it accepts ``latest``, a year and a
#: ``Mon-YYYY`` month, and each genuinely filters.
SNAPSHOT_ONLY_ENDPOINTS: frozenset = frozenset({"share_holding"})

#: Metrics the ``chart-data`` route accepts. Any other value returns 200 with a failed envelope,
#: so the set is enforced locally.
METRICS: Tuple[str, ...] = (
    "EPS_TTM",
    "PE_TTM_SHARE_NOW",
    "PBV_A_SHARE_NOW",
    "DIVIDEND_YIELD_TTM_Q",
)

#: Price basis of the OHLC route. Recorded on every frame; see the module docstring.
PRICE_BASIS = "yfinance_back_adjusted"

#: Output contract, identical to ``bfinance.nse.bhavcopy.COLUMNS`` so a frame from here drops
#: into the same consumers as a bhavcopy frame.
COLUMNS: Tuple[str, ...] = ("Open", "High", "Low", "Close", "Volume")

SNAPSHOT_ONLY_ATTR = "bfinance_trendlyne_snapshot_only"
PRICE_BASIS_ATTR = "bfinance_trendlyne_price_basis"
SOURCE_ATTR = "bfinance_trendlyne_source"
NOTE_ATTR = "bfinance_trendlyne_note"
#: Written by ``build_attrs(window=...)``, which prefixes its keyword arguments. A one-line
#: summary of the same facts as ``bfinance_trendlyne_first_date`` / ``_last_date`` / ``_bars``.
WINDOW_ATTR = "bfinance_trendlyne_window"
RETRIEVED_AT_ATTR = "bfinance_trendlyne_retrieved_at"

#: Wire column names (``eodHeaders[].unique_name``) this module requires, in output order.
_OHLC_REQUIRED: Tuple[str, ...] = (
    "date", "open_price", "high_price", "low_price", "close_price", "volume",
)

#: Wire columns observed but not surfaced as a column. ``last_price`` differs from
#: ``close_price`` on most bars; the count of disagreeing rows is recorded in ``attrs``.
_OHLC_EXTRA: Tuple[str, ...] = ("last_price",)

#: Conservative by design. The 100-150 ms used for screener.in and MarketLens is too aggressive
#: for a commercial site serving a 292 KB page per symbol.
DEFAULT_MIN_REQUEST_INTERVAL_MS = 750.0

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
]

# Global pacing state shared across all client instances, ported from
# ``bfinance.screener.client`` so concurrent clients respect one rate limit instead of
# bursting N x. Deliberately not a second, private scheme.
_GLOBAL_PACING_LOCK = threading.Lock()
_GLOBAL_LAST_REQUEST_TIME = 0.0

# Retry-After cap (seconds) so a hostile or accidental header cannot stall us.
_MAX_RETRY_AFTER_SEC = 30.0

_SITEMAP_URL_RE = re.compile(r"/equity/(\d+)/([^/]+)/([^/?#]+)/?")
# The month form is ``Mon-YYYY`` with the capitalisation trendlyne actually serves. Case matters
# in a path segment, and an unrecognised segment on the share-holding route costs a 500, so this
# is matched strictly rather than case-insensitively.
_PERIOD_RE = re.compile(r"^(?:latest|\d{4}|[A-Z][a-z]{2}-\d{4})$")
_TOKEN_IN_URL_RE = re.compile(r"(/ohlc/\d+/)[A-Za-z0-9=]+/")


# ------------------------------------------------------------------ symbol + paths


@dataclass(frozen=True)
class TrendlyneSymbol:
    """One entry of the equity sitemap: everything needed to address a symbol on trendlyne.

    Attributes:
        symbol: The sitemap's own symbol key, verbatim (uppercase for equities, but **numeric**
            for some entries — ``/equity/3622447/1216/bse-midsmall-private-banks/``). It is
            trendlyne's key, not guaranteed to equal the NSE ticker.
        stock_id: The integer path segment that identifies the stock to trendlyne.
        slug: The lowercase, hyphenated company name used in every URL.
    """

    symbol: str
    stock_id: int
    slug: str

    @property
    def page_path(self) -> str:
        """The symbol's own page path — also the correct ``Referer`` for its ``/mapp/v1/`` calls."""
        return ENDPOINTS["stock_page"].format(
            stock_id=self.stock_id, symbol=self.symbol, slug=self.slug
        )

    @property
    def page_url(self) -> str:
        """Absolute URL of :attr:`page_path`."""
        return f"{BASE_URL}{self.page_path}"


def _decoded(value: str) -> str:
    """XML entity-decode then percent-decode a sitemap path segment.

    ``&amp;`` must be resolved before percent-decoding: the sitemap stores ``M&amp;M`` and the
    live symbol is ``M&M``. Doing it in the other order would leave a stray ``&amp;``.
    """
    return unquote(_html.unescape(value))


def parse_sitemap(xml_text: str) -> Dict[str, TrendlyneSymbol]:
    """Parse the equity sitemap into a ``symbol -> TrendlyneSymbol`` index.

    Args:
        xml_text: The body of ``/equity-sitemap-stocks.xml``.

    Returns:
        A dict keyed by the sitemap's symbol segment. Over the live document (5,922 entries) every
        ``<loc>`` matches :data:`_SITEMAP_URL_RE`.

    Raises:
        TrendlyneSchemaError: The document contained no recognisable entry. A sitemap that
            changed shape is reported, not returned as an empty index — an empty index would make
            every symbol look unresolvable.
    """
    if not xml_text:
        raise TrendlyneSchemaError("sitemap response was empty")

    index: Dict[str, TrendlyneSymbol] = {}
    for match in _SITEMAP_URL_RE.finditer(xml_text):
        stock_id, symbol, slug = match.group(1), match.group(2), match.group(3)
        # XML entity-decode FIRST, then percent-decode. The sitemap is XML, so a symbol
        # containing an ampersand is stored escaped: "M&amp;M", not "M&M". Sixteen entries
        # are affected -- including M&M (id 807) and M&MFIN (id 808), two of the largest
        # NSE names. Percent-decoding alone leaves the literal "M&amp;M" in the index, which
        # then 404s on lookup and makes a real, resolvable symbol look unresolvable.
        # Verified live: after this fix M&M, M&MFIN, J&KBANK, ARE&M, GVT&D and IL&FSENGG all
        # resolve, and the pipeline returns their bars.
        index[_decoded(symbol)] = TrendlyneSymbol(
            symbol=_decoded(symbol), stock_id=int(stock_id), slug=_decoded(slug)
        )
    if not index:
        raise TrendlyneSchemaError(
            "no /equity/{id}/{symbol}/{slug}/ entries found in the sitemap. First 300 chars: "
            f"{xml_text[:300]!r}. trendlyne may have changed the document, or the request may "
            "have been served an error page."
        )
    return index


def validate_period(period: str) -> str:
    """Validate a period path segment before any request.

    Accepts ``latest``, a year (``2024``) and a ``Mon-YYYY`` month (``Jun-2024``) — the three
    forms verified live. Rejecting anything else locally turns what would be an unexplained
    HTTP 500 on the share-holding route into a message naming the accepted forms.
    """
    if not isinstance(period, str):
        raise ValueError(f"period must be a string, got {type(period).__name__}")
    clean = period.strip()
    if not _PERIOD_RE.match(clean):
        raise ValueError(
            f"period={period!r} is not a valid period segment. Use 'latest', a year "
            f"('2024') or a month ('Jun-2024')."
        )
    return clean


def build_path(
    name: str,
    record: TrendlyneSymbol,
    *,
    period: str = SNAPSHOT_PERIOD,
    metric: str = "",
    token: str = "",
) -> str:
    """Build a request path for a known endpoint, enforcing the rules that would otherwise
    produce a confusing server error.

    Args:
        name: A key of :data:`ENDPOINTS`.
        record: The resolved symbol.
        period: Period segment, for the routes that take one. See :func:`validate_period`.
        metric: Metric name, required by ``chart_metric``. See :data:`METRICS`.
        token: Per-symbol OHLC token, required by ``ohlc``.

    Returns:
        A path beginning with ``/``.

    Raises:
        ValueError: Unknown ``name``, an unrecognised ``metric``, or a missing ``token``.
        TrendlyneSnapshotOnlyError: ``name`` is in :data:`SNAPSHOT_ONLY_ENDPOINTS` and ``period``
            is not ``latest``. Raised locally, before a request is spent on a guaranteed 500.
    """
    if name not in ENDPOINTS:
        raise ValueError(f"unknown endpoint {name!r}. Known: {', '.join(sorted(ENDPOINTS))}.")
    if name in SNAPSHOT_ONLY_ENDPOINTS and period != SNAPSHOT_PERIOD:
        raise TrendlyneSnapshotOnlyError(
            f"endpoint {name!r} serves only the latest snapshot: period must be "
            f"{SNAPSHOT_PERIOD!r}, got {period!r}. Verified 2026-09-27 — the share-holding "
            f"route answers HTTP 500 for any other segment. Use the single 'latest' page and "
            f"read its multi-period grid, or use 'mutual_fund_holdings', which does filter."
        )
    if name == "chart_metric":
        if metric not in METRICS:
            raise ValueError(f"metric={metric!r} is not supported. Must be one of {', '.join(METRICS)}.")
    if name == "ohlc" and not token:
        raise ValueError(
            "the ohlc endpoint requires a per-symbol token. Call "
            "TrendlyneClient.ohlc_token(symbol) first — the token is not a constant."
        )
    validate_period(period)
    return ENDPOINTS[name].format(
        stock_id=record.stock_id,
        symbol=record.symbol,
        slug=record.slug,
        period=period,
        metric=metric,
        token=token,
    )


def redact(url: str) -> str:
    """Replace a per-symbol token in a URL with ``<token>``.

    Tokens are per-symbol credentials for trendlyne's private API surface. They must not reach a
    log line, an exception message, a ``repr`` of a frame, or a CSV export via ``attrs``.
    """
    return _TOKEN_IN_URL_RE.sub(r"\1<token>/", url or "")


def parse_envelope(payload: Any, *, context: str) -> Dict[str, Any]:
    """Unwrap trendlyne's ``{"head": ..., "body": ...}`` envelope and verify it succeeded.

    Every JSON route on this site answers **200** for a failed request, with
    ``head.status != "0"`` and a small or empty ``body``. Verified: a rejected OHLC token returns
    92 bytes of ``{"head": {"status": "2", "statusDescription": "Not Authorized !"},
    "body": {}}``. Checking the status code is therefore not enough, and returning the body
    unchecked would hand the caller an empty frame that looks like a quiet market.

    Args:
        payload: The decoded JSON.
        context: Human description of the request, used in the error message.

    Returns:
        The ``body`` mapping.

    Raises:
        TrendlyneNotAuthorizedError: ``head.status`` is not ``"0"``.
        TrendlyneSchemaError: The envelope, or the body, is not the expected shape.
    """
    if not isinstance(payload, dict):
        raise TrendlyneSchemaError(
            f"{context}: expected a JSON object envelope, got {type(payload).__name__}."
        )
    head = payload.get("head")
    if not isinstance(head, dict):
        raise TrendlyneSchemaError(
            f"{context}: response has no 'head' object. Top-level keys: "
            f"{sorted(payload)[:8]}. Upstream schema may have changed."
        )
    status = str(head.get("status", "")).strip()
    description = str(head.get("statusDescription") or "").strip()
    if status != "0":
        raise TrendlyneNotAuthorizedError(
            f"trendlyne reported status={status!r} ({description or 'no description'}) for "
            f"{context}. Note this arrives as HTTP 200 with an empty body — for the OHLC route "
            f"it means the per-symbol token was rejected or has rotated.",
            status_code=200,
        )
    body = payload.get("body")
    if not isinstance(body, dict):
        raise TrendlyneSchemaError(
            f"{context}: 'body' is {type(body).__name__}, expected an object. "
            f"Upstream schema may have changed."
        )
    return body


def build_attrs(
    symbol: str,
    *,
    source: Optional[str] = None,
    snapshot_only: bool = False,
    note: Optional[str] = None,
    retrieved_at: Optional[datetime] = None,
    **extra: Any,
) -> Dict[str, Any]:
    """Build the ``attrs`` mapping every frame from this package carries.

    Sibling modules should call this rather than assembling attrs by hand, so that the keys a
    consumer can rely on are the same whichever module produced the frame.

    Args:
        symbol: The symbol the frame describes.
        source: The URL it came from. Redacted automatically when it carries a token.
        snapshot_only: ``True`` when the frame is a single snapshot that cannot be walked
            backwards. Always written, ``False`` included, so the key is never missing.
        note: One plain sentence on what this frame does and does not contain.
        retrieved_at: When the response arrived.
        **extra: Additional ``bfinance_trendlyne_*`` measurements.

    Returns:
        A dict suitable for ``frame.attrs.update(...)``.
    """
    attrs: Dict[str, Any] = {
        "bfinance_provenance": PROVENANCE,
        "bfinance_symbol": symbol,
        SNAPSHOT_ONLY_ATTR: bool(snapshot_only),
    }
    if source:
        attrs[SOURCE_ATTR] = redact(source)
    if note:
        attrs[NOTE_ATTR] = note
    if retrieved_at is not None:
        attrs[RETRIEVED_AT_ATTR] = retrieved_at.astimezone(timezone.utc).isoformat(
            timespec="seconds"
        )
    for key, value in extra.items():
        attrs[f"bfinance_trendlyne_{key}"] = value
    return attrs


# ------------------------------------------------------------------ OHLC parsing


def _rows_to_frame(
    rows: Sequence[Any],
    positions: Mapping[str, int],
    width: int,
    *,
    label: str,
) -> Tuple[pd.DataFrame, int, int]:
    """Turn wire rows into a typed frame.

    Returns:
        ``(frame, missing_close_rows, divergent_close_last_rows)``.

    Raises:
        TrendlyneSchemaError: A row is not a list of ``width`` values, a date will not parse, or
            every close is missing. Nothing is dropped or filled to recover from any of these.
    """
    if not rows:
        raise TrendlyneSchemaError(f"{label} is empty; nothing to parse.")
    if not isinstance(rows, (list, tuple)):
        raise TrendlyneSchemaError(f"{label} is {type(rows).__name__}, expected a list of rows.")

    bad_width = [i for i, row in enumerate(rows) if not isinstance(row, (list, tuple))
                 or len(row) != width]
    if bad_width:
        raise TrendlyneSchemaError(
            f"{label}: {len(bad_width)} of {len(rows)} rows do not have {width} values. "
            f"First offending row index: {bad_width[0]}. Upstream schema may have changed."
        )

    # The wire timestamp is a retrieval marker ('2026-09-18T10:01:00+00:00'), not a session time.
    # Only the literal date is taken: converting to a timezone first can move a bar onto the
    # wrong day (a 19:00 UTC bar is already the next morning in IST).
    raw_dates = [str(row[positions["date"]]) for row in rows]
    index = pd.to_datetime([d[:10] for d in raw_dates], format="%Y-%m-%d", errors="coerce")
    if index.isna().any():
        raise TrendlyneSchemaError(
            f"{label}: {int(index.isna().sum())} of {len(index)} dates are not YYYY-MM-DD "
            f"prefixed. First offending value: "
            f"{next(d for d, bad in zip(raw_dates, index.isna()) if bad)!r}"
        )

    data: Dict[str, Any] = {}
    for name in _OHLC_REQUIRED:
        if name == "date":
            continue
        # NOTE: values are passed as numpy arrays, not Series. A Series carries a RangeIndex
        # which pandas reindexes against the explicit DatetimeIndex below, silently producing an
        # all-NaN frame that still "succeeds". See the identical note in bfinance.nse.marketlens
        # and CONTEXT.md 9.22. ``np.asarray`` rather than ``.to_numpy()`` because pandas 3 returns
        # an ndarray from ``to_numeric`` where pandas 2 returned a Series.
        data[name] = np.asarray(pd.to_numeric([row[positions[name]] for row in rows],
                                             errors="coerce"))

    close = data["close_price"]
    if bool(pd.isna(close).all()):
        raise TrendlyneSchemaError(
            f"{label}: every close_price is missing or non-numeric. The series is unusable and "
            f"is not returned as an empty frame."
        )

    divergent = 0
    for name in _OHLC_EXTRA:
        if name not in positions:
            continue
        other = np.asarray(pd.to_numeric([row[positions[name]] for row in rows],
                                         errors="coerce"))
        both = ~pd.isna(close) & ~pd.isna(other)
        divergent += int((close[both] != other[both]).sum())

    frame = pd.DataFrame(
        {
            "Open": data["open_price"],
            "High": data["high_price"],
            "Low": data["low_price"],
            "Close": close,
            "Volume": data["volume"],
        },
        index=pd.DatetimeIndex(index, name="Date"),
    )
    return frame, int(pd.isna(close).sum()), divergent


def parse_ohlc(
    payload: Any,
    symbol: str,
    *,
    source_url: Optional[str] = None,
    include_last_week: bool = True,
    retrieved_at: Optional[datetime] = None,
    stock_id: Optional[int] = None,
) -> pd.DataFrame:
    """Parse an OHLC payload into a ``DatetimeIndex`` frame with truthful ``attrs``.

    Args:
        payload: The decoded ``/mapp/v1/stock/web/ohlc/{id}/{token}/`` response.
        symbol: The symbol, for ``attrs`` only. It is not cross-checked against the payload —
            the payload's own ``stockData`` is not part of the OHLC frame's contract.
        source_url: The URL used, for ``attrs``. The token is redacted.
        include_last_week: Merge ``eodDataLastWeek`` into the series. See the module docstring:
            the freshest five sessions live in that separate key, so a frame built from
            ``eodData`` alone stops a week short of the vendor's own latest bar. Dates already
            present in ``eodData`` are **never** overwritten — ``eodData`` is authoritative.
        retrieved_at: When the response arrived.
        stock_id: trendlyne's internal id for the symbol, recorded in ``attrs`` when known.

    Returns:
        Columns :data:`COLUMNS`, ascending ``Date`` index, with ``attrs`` recording provenance,
        the price basis, the vendor window, the merge, the invariant check and every missing value.

    Raises:
        TrendlyneNotAuthorizedError: The envelope reports a failed request (rejected token).
        TrendlyneSchemaError: Missing ``eodHeaders``/``eodData``, a renamed column, a row of the
            wrong width, an unparseable date, or an all-missing close.
    """
    body = parse_envelope(payload, context=f"the OHLC payload for {symbol}")
    for key in ("eodHeaders", "eodData"):
        if key not in body:
            raise TrendlyneSchemaError(
                f"the OHLC payload for {symbol} has no {key!r}. Body keys: "
                f"{sorted(body)[:12]}. Upstream schema may have changed."
            )

    headers = body["eodHeaders"]
    if not isinstance(headers, list) or not headers:
        raise TrendlyneSchemaError(
            f"the OHLC payload for {symbol} has an empty 'eodHeaders'; column order is "
            f"undefined, and guessing it is exactly the mistake this module refuses to make."
        )
    try:
        names = [
            str(header.get("unique_name") or header.get("name") or "").strip().lower()
            for header in headers
        ]
    except AttributeError as exc:
        raise TrendlyneSchemaError(
            f"the OHLC payload for {symbol} has a malformed 'eodHeaders' entry: {headers[0]!r}"
        ) from exc

    missing = [column for column in _OHLC_REQUIRED if column not in names]
    if missing:
        raise TrendlyneSchemaError(
            f"the OHLC payload for {symbol} is missing required column(s) {missing}. "
            f"Present: {names}. Upstream schema may have changed."
        )
    positions = {name: index for index, name in enumerate(names)}

    eod_frame, eod_missing_close, eod_divergent = _rows_to_frame(
        body["eodData"], positions, len(names), label=f"eodData for {symbol}"
    )

    eod_rows = len(eod_frame)
    last_week_added = 0
    frame = eod_frame
    if include_last_week:
        week_rows = body.get("eodDataLastWeek")
        if isinstance(week_rows, list) and week_rows:
            week_frame, _, week_divergent = _rows_to_frame(
                week_rows, positions, len(names), label=f"eodDataLastWeek for {symbol}"
            )
            # eodData wins on a shared date; the last-week key only contributes dates it has alone.
            fresh = week_frame[~week_frame.index.isin(eod_frame.index)]
            last_week_added = len(fresh)
            if last_week_added:
                frame = pd.concat([eod_frame, fresh])
            eod_divergent += week_divergent
    elif isinstance(body.get("eodDataLastWeek"), list) and body["eodDataLastWeek"]:
        last_week_added = -1  # -1 = present upstream, deliberately not merged

    # The wire is newest-first (verified: eodData[0] is 2026-09-18 and eodData[-1] is 2006-01-02),
    # so the frame is sorted here rather than inheriting the transmission order. Done
    # unconditionally: with include_last_week=False there is no concat to sort.
    frame = frame.sort_index()

    # Duplicate sessions are impossible in a correct series. Rather than silently dropping one,
    # keep the last occurrence and report the count.
    duplicated = int(frame.index.duplicated().sum())
    if duplicated:
        logger.warning(
            "trendlyne eodData for %s carries %d duplicate session date(s); keeping the last "
            "occurrence of each. attrs records the count.", symbol, duplicated,
        )
        frame = frame[~frame.index.duplicated(keep="last")]

    volume = frame["Volume"]
    integral = volume.dropna().map(lambda v: float(v).is_integer()).all()
    if bool(integral) and not volume.isna().any():
        frame["Volume"] = volume.astype("int64")
    else:
        non_integral = int((~volume.dropna().map(lambda v: float(v).is_integer())).sum())
        if non_integral:
            logger.warning(
                "trendlyne Volume for %s carries %d non-integral value(s); kept as float rather "
                "than truncated.", symbol, non_integral,
            )

    # The OHLC invariant, measured rather than assumed: 51,070/51,070 rows across 10 symbols, and
    # 5,107/5,107 on this module's own capture. Counted and reported, never repaired.
    upper = frame[["Open", "High", "Close"]].max(axis=1)
    lower = frame[["Open", "Low", "Close"]].min(axis=1)
    comparable = frame[["Open", "High", "Low", "Close"]].notna().all(axis=1)
    violations = int(((frame["High"] < upper) | (frame["Low"] > lower))[comparable].sum())
    if violations:
        logger.warning(
            "trendlyne OHLC for %s violates High>=max(O,C)/Low<=min(O,C) on %d of %d rows; "
            "reported in attrs, not repaired.", symbol, violations, int(comparable.sum()),
        )

    missing_values = {
        column: int(frame[column].isna().sum())
        for column in COLUMNS
        if int(frame[column].isna().sum())
    }
    first_date = frame.index[0].date().isoformat()
    last_date = frame.index[-1].date().isoformat()
    note = (
        f"Daily EOD series from trendlyne.com, vendor-fixed window {first_date}..{last_date} "
        f"({len(frame)} bars) — this is the vendor's window, not the listing history. Prices are "
        f"back-adjusted (yfinance basis), NOT as-traded: do not concatenate or splice these bars "
        f"with bfinance.nse.bhavcopy bars, which are as-traded."
    )
    frame.attrs.update(
        build_attrs(
            symbol,
            source=source_url,
            snapshot_only=False,
            note=note,
            retrieved_at=retrieved_at,
            stock_id=stock_id,
            exchange=body.get("exchange"),
            price_basis=PRICE_BASIS,
            window=f"{first_date}..{last_date} ({len(frame)} bars)",
            bars=len(frame),
            first_date=first_date,
            last_date=last_date,
            eod_rows=eod_rows,
            last_week_rows_added=last_week_added,
            missing_close_rows=eod_missing_close,
            close_last_divergent_rows=eod_divergent,
            ohlc_invariant_violations=violations,
            duplicate_dates=duplicated,
            missing_values=missing_values,
        )
    )
    frame.attrs["bfinance_synthetic_ohlc"] = False
    frame.attrs[PRICE_BASIS_ATTR] = PRICE_BASIS
    return frame[list(COLUMNS)]


def _parse_retry_after(response: httpx.Response) -> Optional[float]:
    """Parse a delta-seconds ``Retry-After`` header, capped. ``None`` if absent or unusable."""
    raw = response.headers.get("Retry-After") if response is not None else None
    if raw is None:
        return None
    try:
        delay = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    if delay < 0:
        return None
    return min(delay, _MAX_RETRY_AFTER_SEC)


def _run_async(coro):
    """Bridge to sync, matching ``Ticker._run_async`` and ``MarketLensClient`` semantics."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            return executor.submit(lambda: asyncio.run(coro)).result()
    return asyncio.run(coro)


# ------------------------------------------------------------------ client


class TrendlyneClient:
    """Client for trendlyne.com. Constructing it performs **no I/O**.

    Everything — the 831 KB sitemap, the per-symbol token, every page — is fetched on first use
    and cached in memory with a TTL. A long-lived instance is the intended usage: it is what makes
    the second call for a symbol cost one request instead of two.

    Args:
        timeout: Per-request timeout, seconds.
        max_retries: Attempts for 429, 5xx and transport failures. Terminal statuses are never
            retried.
        proxy: Proxy URL. Falls back to ``HTTPS_PROXY``/``HTTP_PROXY``, as elsewhere in bfinance.
        min_request_interval_ms: Global pacing slot. See
            :data:`DEFAULT_MIN_REQUEST_INTERVAL_MS` for why the default is deliberately slow.
        index_ttl_hours: How long the parsed sitemap is reused.
        token_ttl_hours: How long a per-symbol token is reused. Tokens were observed to be stable
            across page loads, so this is a safety net rather than a necessity — and
            :meth:`ohlc` re-scrapes on rejection regardless of the TTL.
        cache: Optional duck-typed cache with ``get(key)``/``set(key, payload, category=, ttl_hours=)``
            — a :class:`~bfinance.cache.sqlite_cache.SQLiteCache` satisfies it. ``None`` (the
            default) keeps everything in memory, so importing this package never writes to the
            user's home directory. The type is not imported, keeping this module free of an
            import edge to ``bfinance.cache``.
        transport: Optional ``httpx`` transport. ``httpx.MockTransport`` is how the test suite
            exercises the 444/404/5xx paths with no network; production callers leave it ``None``.
    """

    #: Absolute base for every request. Exposed as a class attribute so a subclass can point at a
    #: mirror without having to touch the module-level helpers.
    BASE_URL = BASE_URL

    def __init__(
        self,
        timeout: float = 20.0,
        max_retries: int = 3,
        proxy: Optional[str] = None,
        min_request_interval_ms: float = DEFAULT_MIN_REQUEST_INTERVAL_MS,
        index_ttl_hours: float = 24.0,
        token_ttl_hours: float = 12.0,
        cache: Optional[Any] = None,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self.timeout = timeout
        self.max_retries = max(1, int(max_retries))
        self.proxy = proxy or os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
        self.min_request_interval_ms = float(min_request_interval_ms)
        self.index_ttl_seconds = max(0.0, float(index_ttl_hours) * 3600.0)
        self.token_ttl_seconds = max(0.0, float(token_ttl_hours) * 3600.0)
        self.cache = cache
        self.transport = transport
        self._last_request_time = 0.0
        # A threading.Lock, not asyncio.Lock: the sync wrappers call asyncio.run() per call, so
        # every call gets a fresh event loop and an asyncio primitive bound to an earlier one
        # would raise. The guarded sections hold no awaits.
        self._state_lock = threading.Lock()
        self._index: Optional[Dict[str, TrendlyneSymbol]] = None
        self._index_at: float = 0.0
        self._index_source_bytes: int = 0
        self._tokens: Dict[int, Tuple[float, str]] = {}

    # ---------------------------------------------------------------- internals

    def _headers(self, referer: Optional[str] = None, *, xhr: bool = False) -> Dict[str, str]:
        """
        Args:
            referer: Sent verbatim when given. Required by ``/mapp/v1/`` routes (see the module
                docstring's status matrix) and unnecessary on the HTML page routes.
            xhr: Adds ``X-Requested-With: XMLHttpRequest``. Needed by trendlyne's own
                autocomplete route, which answers **HTTP 200 with a non-JSON body** when the
                header is missing - a silent failure, not an error status. Not sent
                unconditionally, because a real browser does not send it on a navigation and
                adding it to the HTML page routes would misreport a page fetch as an XHR.
        """
        headers = {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "application/json, text/html;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        if referer:
            headers["Referer"] = referer
        if xhr:
            headers["X-Requested-With"] = "XMLHttpRequest"
        return headers

    async def _throttle_pacing(self) -> None:
        """Reserve a global pacing slot, so concurrent clients burst once rather than N times."""
        global _GLOBAL_LAST_REQUEST_TIME
        with _GLOBAL_PACING_LOCK:
            now = time.monotonic()
            last = max(self._last_request_time, _GLOBAL_LAST_REQUEST_TIME)
            elapsed_ms = (now - last) * 1000.0
            if elapsed_ms < self.min_request_interval_ms:
                wait_sec = (self.min_request_interval_ms - elapsed_ms) / 1000.0 + random.uniform(
                    0.01, 0.05
                )
                _GLOBAL_LAST_REQUEST_TIME = now + wait_sec
            else:
                wait_sec = 0.0
                _GLOBAL_LAST_REQUEST_TIME = now
        if wait_sec > 0.0:
            await asyncio.sleep(wait_sec)
        self._last_request_time = time.monotonic()
        with _GLOBAL_PACING_LOCK:
            _GLOBAL_LAST_REQUEST_TIME = max(_GLOBAL_LAST_REQUEST_TIME, self._last_request_time)

    def _absolute(self, path: str) -> str:
        return path if path.startswith("http") else f"{self.BASE_URL}{path}"

    async def _send(
        self, path: str, *, referer: Optional[str] = None, xhr: bool = False
    ) -> httpx.Response:
        """GET with pacing and retry. Returns the 200 response or raises a typed error.

        Retry policy: 429 and 5xx and transport failures are retried with exponential backoff.
        Every 4xx — **including 444** — is terminal, because retrying an unchanged request cannot
        change its outcome and does cost the vendor a request.
        """
        url = self._absolute(path)
        last_error: Optional[Exception] = None
        last_response_error: Optional[TrendlyneError] = None

        for attempt in range(self.max_retries):
            await self._throttle_pacing()
            client_kwargs: Dict[str, Any] = {
                "timeout": self.timeout,
                "follow_redirects": True,
            }
            if self.proxy:
                client_kwargs["proxy"] = self.proxy
            if self.transport is not None:
                client_kwargs["transport"] = self.transport
            try:
                async with httpx.AsyncClient(**client_kwargs) as client:
                    response = await client.get(url, headers=self._headers(referer, xhr=xhr))
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(min(2.0 * (2**attempt), 8.0) + random.uniform(0, 0.5))
                continue

            status = response.status_code
            if status == 200:
                return response

            if status == 429:
                if attempt < self.max_retries - 1:
                    backoff = min(2.0 * (2**attempt), 8.0) + random.uniform(0.5, 1.5)
                    retry_after = _parse_retry_after(response)
                    if retry_after is not None:
                        backoff = max(backoff, retry_after)
                    logger.warning(
                        "trendlyne rate limited (%d) on %s; backing off %.1fs (attempt %d/%d).",
                        status, redact(url), backoff, attempt + 1, self.max_retries,
                    )
                    await asyncio.sleep(backoff)
                    continue
                raise TrendlyneRateLimitedError(
                    f"trendlyne rate limited this client (HTTP 429) after {self.max_retries} "
                    f"attempts for {redact(url)}. The index and token caches remain valid.",
                    status_code=status,
                    url=redact(url),
                )

            if status == 444:
                # Verified 2026-09-27: the /mapp/v1/ family answers 444 with a zero-length body
                # unless the request carries a Referer. Terminal, and never a decode error.
                raise TrendlyneAccessDeniedError(
                    f"trendlyne returned HTTP 444 for {redact(url)}. This is the site's request "
                    f"gate for /mapp/v1/, not a bad symbol: the identical URL returns 200 when "
                    f"the request carries a Referer. It is not retried, because retrying an "
                    f"unchanged request cannot succeed.",
                    status_code=status,
                    url=redact(url),
                )

            if status in (401, 403):
                raise TrendlyneAccessDeniedError(
                    f"trendlyne returned HTTP {status} for {redact(url)}. The route is public, so "
                    f"this is a request-shape or access change rather than bad credentials.",
                    status_code=status,
                    url=redact(url),
                )

            if status == 404:
                raise TrendlyneNotFoundError(
                    f"trendlyne has no route or resource at {redact(url)} (HTTP 404).",
                    status_code=status,
                    url=redact(url),
                )

            if status >= 500:
                last_response_error = TrendlyneServerError(
                    f"trendlyne returned HTTP {status} for {redact(url)}.",
                    status_code=status,
                    url=redact(url),
                )
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(min(2.0 * (2**attempt), 8.0) + random.uniform(0, 0.5))
                continue

            raise TrendlyneHTTPError(
                f"trendlyne returned unexpected HTTP {status} for {redact(url)}.",
                status_code=status,
                url=redact(url),
            )

        # A response that arrived and said "5xx" must surface as a server error, not as a
        # transport error: the two send the reader to completely different places.
        if last_response_error is not None:
            raise last_response_error
        raise TrendlyneTransportError(
            f"trendlyne request failed after {self.max_retries} attempts for {redact(url)}: "
            f"{last_error}"
        ) from last_error

    async def _get_text(self, path: str, *, referer: Optional[str] = None) -> str:
        response = await self._send(path, referer=referer)
        return response.text

    async def _get_json(
        self, path: str, *, referer: Optional[str] = None, xhr: bool = False
    ) -> Any:
        response = await self._send(path, referer=referer, xhr=xhr)
        try:
            return response.json()
        except ValueError as exc:
            # A 200 with an HTML or empty body: report the type, never a bare decode error.
            hint = ""
            if path.startswith(_SYMBOL_SEARCH_PATH.split("?")[0]):
                hint = (
                    " This is the autocomplete route, which answers HTTP 200 with a non-JSON "
                    "body when X-Requested-With: XMLHttpRequest is absent."
                )
            raise TrendlyneSchemaError(
                f"trendlyne returned a non-JSON body (HTTP {response.status_code}) for "
                f"{redact(self._absolute(path))}. Content-Type: "
                f"{response.headers.get('Content-Type', '<none>')!r}. First 160 chars: "
                f"{response.text[:160]!r}.{hint}"
            ) from exc

    # ------------------------------------------------------------------ public I/O

    async def fetch_text_async(
        self, path: str, *, referer: Optional[str] = None
    ) -> str:
        """GET a path and return the raw body. The primitive every sibling module builds on.

        Args:
            path: A path from :data:`ENDPOINTS` (built with :func:`build_path`), or an absolute
                URL.
            referer: Sent as the ``Referer`` header. Required by ``/mapp/v1/`` routes, optional
                elsewhere.

        Returns:
            The response body, unparsed.

        Raises:
            TrendlyneAccessDeniedError: HTTP 444/401/403.
            TrendlyneNotFoundError: HTTP 404.
            TrendlyneRateLimitedError: HTTP 429 after every retry.
            TrendlyneServerError: HTTP 5xx after every retry.
            TrendlyneTransportError: The request never completed.
            TrendlyneHTTPError: Any other unexpected status.
        """
        return await self._get_text(path, referer=referer)

    def fetch_text(self, path: str, *, referer: Optional[str] = None) -> str:
        """Sync variant of :meth:`fetch_text_async`."""
        return _run_async(self.fetch_text_async(path, referer=referer))

    async def fetch_json_async(
        self, path: str, *, referer: Optional[str] = None, xhr: bool = False
    ) -> Any:
        """GET a path and decode JSON, raising a typed error on a non-JSON body.

        Does **not** unwrap the ``head``/``body`` envelope — call :func:`parse_envelope` for that,
        so a failed-but-200 response can never be mistaken for data.

        Args:
            xhr: Send ``X-Requested-With: XMLHttpRequest``. Required by the autocomplete route,
                which otherwise answers 200 with a non-JSON body.
        """
        return await self._get_json(path, referer=referer, xhr=xhr)

    def fetch_json(self, path: str, *, referer: Optional[str] = None) -> Any:
        """Sync variant of :meth:`fetch_json_async`."""
        return _run_async(self.fetch_json_async(path, referer=referer))

    # ------------------------------------------------------------------ symbol index

    def load_symbol_index(self, xml_text: str) -> int:
        """Populate the symbol index from a sitemap document without any I/O.

        Exposed so a caller holding a cached sitemap — or a test holding a fixture — can resolve
        symbols offline. Returns the number of symbols indexed.

        Raises:
            TrendlyneSchemaError: The document held no recognisable entry.
        """
        index = parse_sitemap(xml_text)
        with self._state_lock:
            self._index = index
            self._index_at = time.monotonic()
            self._index_source_bytes = len(xml_text.encode("utf-8"))
        return len(index)

    def clear_caches(self) -> None:
        """Forget the sitemap index and every cached token. The next call re-fetches."""
        with self._state_lock:
            self._index = None
            self._index_at = 0.0
            self._index_source_bytes = 0
            self._tokens.clear()

    def _cached_index(self) -> Optional[Dict[str, TrendlyneSymbol]]:
        with self._state_lock:
            if self._index is None or self.index_ttl_seconds <= 0.0:
                return None
            if (time.monotonic() - self._index_at) > self.index_ttl_seconds:
                return None
            return self._index

    async def symbol_index_async(
        self, *, force_refresh: bool = False
    ) -> Dict[str, TrendlyneSymbol]:
        """The ``symbol -> TrendlyneSymbol`` index, from memory or from the sitemap.

        The live document holds 5,922 entries in 831 KB. A benign race can issue two GETs when
        two coroutines miss the cache simultaneously; that is left alone deliberately, because an
        ``asyncio.Lock`` would not survive the per-call event loop that the sync wrappers create.

        Args:
            force_refresh: Ignore the TTL and re-fetch.

        Returns:
            The index mapping. The returned dict is the client's own; treat it as read-only.
        """
        if not force_refresh:
            cached = self._cached_index()
            if cached is not None:
                return cached

        cache_key = "trendlyne:sitemap:v1"
        if not force_refresh and self.cache is not None:
            try:
                stored = self.cache.get(cache_key)
            except Exception:  # a broken cache must never break a data call
                logger.warning("trendlyne sitemap cache read failed; fetching instead.", exc_info=True)
                stored = None
            if isinstance(stored, str):
                index = parse_sitemap(stored)
                with self._state_lock:
                    self._index = index
                    self._index_at = time.monotonic()
                return index

        text = await self._get_text(SITEMAP_PATH)
        index = parse_sitemap(text)
        with self._state_lock:
            self._index = index
            self._index_at = time.monotonic()
            self._index_source_bytes = len(text.encode("utf-8"))
        if self.cache is not None:
            try:
                self.cache.set(cache_key, text, category="trendlyne", ttl_hours=24.0)
            except Exception:
                logger.warning("trendlyne sitemap cache write failed; continuing.", exc_info=True)
        return index

    def symbol_index(self, *, force_refresh: bool = False) -> Dict[str, TrendlyneSymbol]:
        """Sync variant of :meth:`symbol_index_async`."""
        return _run_async(self.symbol_index_async(force_refresh=force_refresh))

    @property
    def index_size(self) -> Optional[int]:
        """Number of symbols in the cached index, or ``None`` if it has not been loaded."""
        with self._state_lock:
            return None if self._index is None else len(self._index)

    @property
    def index_source_bytes(self) -> int:
        """Size of the sitemap document the cached index was parsed from.

        A diagnostic, not bookkeeping: the live document is ~831 KB for 5,922 symbols, so a much
        smaller value means the index came from an excerpt, a cached copy, or a truncated
        response — which is the first thing to check when a symbol "does not exist".
        """
        with self._state_lock:
            return self._index_source_bytes

    async def resolve_async(self, symbol: str) -> TrendlyneSymbol:
        """Resolve a symbol to its sitemap record.

        Uses :func:`bfinance.utils.symbols.normalize_symbol`, so ``reliance.ns``, ``RELIANCE``
        and ``RELIANCE-EQ`` all resolve to the same entry. Matching against the sitemap is exact:
        a symbol trendlyne does not carry raises rather than being guessed at.

        Raises:
            TrendlyneSymbolNotFoundError: Not in the sitemap. The message names the form that was
                tried, so a wrong ticker is distinguishable from a coverage gap.
            ValueError: ``symbol`` is empty or not a string.
        """
        cleaned = normalize_symbol(symbol or "")
        if not cleaned:
            raise ValueError("symbol must be a non-empty string")
        index = await self.symbol_index_async()
        record = index.get(cleaned)
        if record is None:
            hint = ""
            if len(index) > 1000:
                hint = (
                    f" The index holds {len(index)} symbols; use "
                    f"TrendlyneClient.search() to look one up by name."
                )
            raise TrendlyneSymbolNotFoundError(
                f"{cleaned!r} is not in trendlyne's equity sitemap (tried {cleaned!r}). "
                f"trendlyne has no symbol-search API, so the sitemap is the only resolver and "
                f"this package will not guess a URL.{hint}"
            )
        return record

    def resolve(self, symbol: str) -> TrendlyneSymbol:
        """Sync variant of :meth:`resolve_async`."""
        return _run_async(self.resolve_async(symbol))

    async def search_async(self, query: str, limit: int = 10) -> List[TrendlyneSymbol]:
        """Substring search over the **local** index — no request beyond loading the sitemap.

        The sitemap's symbol segment is trendlyne's own key and is not always a ticker: the live
        tail holds ``/equity/3622447/1216/bse-midsmall-private-banks/``. The slug carries the
        company name, so both are searched.

        Args:
            query: Matched case-insensitively against the symbol and the slug.
            limit: Maximum results. Exact symbol matches are ranked first.

        Returns:
            Up to ``limit`` records, best match first. Empty when nothing matches — a fuzzy miss
            is an expected outcome of a local search, not an error.
        """
        clean = (query or "").strip().lower()
        if not clean:
            raise ValueError("query must be a non-empty string")
        index = await self.symbol_index_async()
        exact, partial = [], []
        for symbol, record in index.items():
            if symbol.lower() == clean:
                exact.append(record)
            elif clean in symbol.lower() or clean in record.slug.lower():
                partial.append(record)
        return (exact + partial)[: max(1, int(limit))]

    def search(self, query: str, limit: int = 10) -> List[TrendlyneSymbol]:
        """Sync variant of :meth:`search_async`."""
        return _run_async(self.search_async(query, limit=limit))

    # ------------------------------------------------------- remote symbol search

    async def search_remote_async(self, query: str, *, limit: int = 10) -> List[TrendlyneSymbol]:
        """Resolve a symbol through trendlyne's own **remote** autocomplete.

        This is the only route to instruments the equity sitemap does not carry. Verified live:
        ``NIFTYBEES`` (id 1588), ``BANKBEES`` (1581), ``GOLDBEES`` (1582), ``JUNIORBEES`` (1584)
        and ``SETFNIF50`` (1753) all resolve here and serve full OHLC, yet **none of them appear
        in ``/equity-sitemap-stocks.xml``** - the sitemap's 5,922 entries are equities, indices
        and BSE scrip codes only. Searching the sitemap for an ETF therefore returns nothing and
        the symbol looks unresolvable when it is not.

        Use this when :meth:`search_async` finds nothing; the two are complementary, not
        competing. Costs one request and needs no token.

        Args:
            query: Symbol or company-name fragment. trendlyne matches either.
            limit: Maximum records to return.

        Returns:
            Matching records. Empty when trendlyne has no match - it answers a miss with
            ``[{"headline": "No matches found"}]``, which is an expected outcome, not an error.
        """
        clean = (query or "").strip()
        if not clean:
            raise ValueError("query must be a non-empty string")

        path = _SYMBOL_SEARCH_PATH.format(query=quote_plus(clean))
        payload = await self.fetch_json_async(path, xhr=True)
        if isinstance(payload, dict):
            payload = payload.get("data") or payload.get("results") or []
        if not isinstance(payload, list):
            raise TrendlyneSchemaError(
                f"symbol search returned {type(payload).__name__}, expected a list. "
                f"First 200 chars: {str(payload)[:200]!r}"
            )

        out: List[TrendlyneSymbol] = []
        for item in payload:
            if not isinstance(item, dict) or "k" not in item:
                # The no-match sentinel, or a record without an id. Skip rather than invent one.
                continue
            symbol = str(item.get("id") or item.get("value") or "").strip()
            slug = str(item.get("slugname") or "").strip()
            if not symbol or not slug:
                continue
            try:
                stock_id = int(item["k"])
            except (TypeError, ValueError):
                continue
            out.append(TrendlyneSymbol(symbol=symbol, stock_id=stock_id, slug=slug))
            if len(out) >= max(1, int(limit)):
                break
        return out

    def search_remote(self, query: str, *, limit: int = 10) -> List[TrendlyneSymbol]:
        """Sync variant of :meth:`search_remote_async`."""
        return _run_async(self.search_remote_async(query, limit=limit))

    async def resolve_any_async(self, symbol: str) -> TrendlyneSymbol:
        """Resolve via the sitemap index first, then fall back to the remote search.

        The sitemap is one bulk request that covers 5,922 instruments, so it is tried first and
        is the cheaper path for equities. The remote search is the fallback that rescues ETFs and
        anything else trendlyne indexes but does not list.

        A miss raises :class:`TrendlyneSymbolNotFoundError` naming **both** routes, because
        "not in the sitemap" and "not on trendlyne" are different facts and the caller should not
        have to guess which one applies.
        """
        try:
            return await self.resolve_async(symbol)
        except TrendlyneSymbolNotFoundError:
            matches = await self.search_remote_async(symbol, limit=5)
            if matches:
                return matches[0]
            raise TrendlyneSymbolNotFoundError(
                f"{symbol!r} resolved by neither route: not in the equity sitemap "
                f"({self.index_size} entries) and trendlyne's own search returned no match. "
                f"Mutual funds are a separate namespace (/mutual-fund/nav/{{id}}/) and are not "
                f"reachable from here."
            ) from None

    def resolve_any(self, symbol: str) -> TrendlyneSymbol:
        """Sync variant of :meth:`resolve_any_async`."""
        return _run_async(self.resolve_any_async(symbol))

    # ------------------------------------------------------------------ token

    async def ohlc_token_async(self, symbol: str, *, force_refresh: bool = False) -> str:
        """The per-symbol OHLC token, cached.

        Costs one HTML request the first time per symbol and none afterwards. The token lives only
        in the symbol page's ``data-eodurl`` attribute and differs per symbol.

        Args:
            symbol: Anything :meth:`resolve_async` accepts.
            force_refresh: Re-scrape even if a cached token is still fresh.

        Returns:
            The token, verbatim, including any ``=`` padding.

        Raises:
            TrendlyneSymbolNotFoundError: The symbol resolves by neither the sitemap nor
                trendlyne's own search.
            TrendlyneTokenError: The page carried no token (a markup change).
            TrendlyneAccessDeniedError / TrendlyneNotFoundError: The page itself failed.
        """
        record = await self.resolve_any_async(symbol)
        if not force_refresh and self.token_ttl_seconds > 0.0:
            with self._state_lock:
                cached = self._tokens.get(record.stock_id)
            if cached and (time.monotonic() - cached[0]) <= self.token_ttl_seconds:
                return cached[1]

        html = await self._get_text(record.page_path)
        token = extract_ohlc_token(html)
        with self._state_lock:
            self._tokens[record.stock_id] = (time.monotonic(), token)
        return token

    def ohlc_token(self, symbol: str, *, force_refresh: bool = False) -> str:
        """Sync variant of :meth:`ohlc_token_async`."""
        return _run_async(self.ohlc_token_async(symbol, force_refresh=force_refresh))

    # ------------------------------------------------------------------ payloads

    async def ohlc_async(
        self,
        symbol: str,
        *,
        include_last_week: bool = True,
    ) -> pd.DataFrame:
        """Daily OHLCV, yfinance basis. Two requests the first time, one thereafter.

        Args:
            symbol: Anything :meth:`resolve_async` accepts.
            include_last_week: Merge ``eodDataLastWeek`` — the five most recent sessions, which
                the main ``eodData`` key does not contain. See the module docstring.

        Returns:
            A ``DatetimeIndex`` frame with columns :data:`COLUMNS` and ``attrs`` recording
            provenance, price basis, vendor window and every missing value. Never empty: a
            symbol with no usable bars raises instead.

        Raises:
            TrendlyneSymbolNotFoundError: The symbol is not in the sitemap.
            TrendlyneTokenError: No token in the symbol page.
            TrendlyneAccessDeniedError: HTTP 444 — the ``/mapp/v1/`` gate.
            TrendlyneNotAuthorizedError: The envelope rejected the token, and re-scraping it once
                did not produce a different one.
            TrendlyneSchemaError: The payload did not have the expected shape.
        """
        record = await self.resolve_any_async(symbol)
        retrieved_at = datetime.now(timezone.utc)
        token = await self.ohlc_token_async(record.symbol)
        path = build_path("ohlc", record, token=token)
        payload = await self._get_json(path, referer=record.page_url)

        try:
            return parse_ohlc(
                payload,
                record.symbol,
                source_url=self._absolute(path),
                include_last_week=include_last_week,
                retrieved_at=retrieved_at,
                stock_id=record.stock_id,
            )
        except TrendlyneNotAuthorizedError:
            # The token may have rotated upstream. Re-scrape once — and only if it actually
            # changed, so a genuinely rejected token costs one page fetch, not a retry storm.
            fresh = await self.ohlc_token_async(record.symbol, force_refresh=True)
            if fresh == token:
                raise
            logger.info(
                "trendlyne rotated the OHLC token for %s; retried once with the new one.",
                record.symbol,
            )
            retry_path = build_path("ohlc", record, token=fresh)
            payload = await self._get_json(retry_path, referer=record.page_url)
            return parse_ohlc(
                payload,
                record.symbol,
                source_url=self._absolute(retry_path),
                include_last_week=include_last_week,
                retrieved_at=retrieved_at,
                stock_id=record.stock_id,
            )

    def ohlc(self, symbol: str, *, include_last_week: bool = True) -> pd.DataFrame:
        """Sync variant of :meth:`ohlc_async`."""
        return _run_async(self.ohlc_async(symbol, include_last_week=include_last_week))

    async def chart_metric_async(self, symbol: str, metric: str) -> Any:
        """The raw ``chart-data`` payload for a metric. No token needed.

        Args:
            symbol: Anything :meth:`resolve_async` accepts.
            metric: One of :data:`METRICS`; validated locally, because an unknown metric comes
                back as a 200 with a failed envelope rather than a 404.

        Returns:
            The decoded JSON, envelope included. Parsing is left to the sibling module that owns
            metrics, because the timestamp handling is a real trap: ``body["eodData"]`` is
            ``[epoch_ms, value]`` pairs stamped at **midnight IST on quarter-end dates**, so the
            epoch must be converted to ``Asia/Kolkata`` before the date is taken — read as UTC,
            every point lands one day early. Verified 2026-09-27 on MOTHERSON ``EPS_TTM``:
            1782757800000 is 2026-06-30 00:00 +05:30, not 2026-06-29.

        Raises:
            ValueError: ``metric`` is not in :data:`METRICS`.
            TrendlyneSymbolNotFoundError: The symbol resolves by neither route.
            TrendlyneAccessDeniedError: HTTP 444 from the ``/mapp/v1/`` gate.
        """
        record = await self.resolve_any_async(symbol)
        path = build_path("chart_metric", record, metric=metric)
        return await self._get_json(path, referer=record.page_url)

    def chart_metric(self, symbol: str, metric: str) -> Any:
        """Sync variant of :meth:`chart_metric_async`."""
        return _run_async(self.chart_metric_async(symbol, metric))


#: Shared client for the module-level shortcuts. Created on first use, never at import.
_DEFAULT_CLIENT: Optional[TrendlyneClient] = None


def _default_client() -> TrendlyneClient:
    global _DEFAULT_CLIENT
    if _DEFAULT_CLIENT is None:
        _DEFAULT_CLIENT = TrendlyneClient()
    return _DEFAULT_CLIENT


def ohlc(symbol: str, *, include_last_week: bool = True) -> pd.DataFrame:
    """Module-level shortcut for :meth:`TrendlyneClient.ohlc`.

    >>> from bfinance.trendlyne import ohlc
    >>> ohlc("RELIANCE").tail()          # doctest: +SKIP
    """
    return _default_client().ohlc(symbol, include_last_week=include_last_week)


def ohlc_async(symbol: str, *, include_last_week: bool = True) -> pd.DataFrame:
    """Async variant of :func:`ohlc`."""
    return _default_client().ohlc_async(symbol, include_last_week=include_last_week)


def resolve(symbol: str) -> TrendlyneSymbol:
    """Module-level shortcut for :meth:`TrendlyneClient.resolve`."""
    return _default_client().resolve(symbol)


def symbol_index(*, force_refresh: bool = False) -> Dict[str, TrendlyneSymbol]:
    """Module-level shortcut for :meth:`TrendlyneClient.symbol_index`."""
    return _default_client().symbol_index(force_refresh=force_refresh)
