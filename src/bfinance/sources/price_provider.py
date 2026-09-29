"""
A per-symbol daily-OHLC reader over the second vendor's **robots-allowed** routes, built without
the leaf package.

Why this module is called ``price_provider`` and not after the vendor
-------------------------------------------------------------------
``bfinance.trendlyne`` is a **leaf**. ``tests/test_trendlyne_core.py::TestIsolation`` scans the AST
of every module under ``src/`` outside that package and fails on any import that names it, and
``tests/test_sources_cascade.py::TestSubprocessProbes`` adds two guards over this directory
specifically: no filename here may contain the substring, and no import here may name the package
at all. So this module reads the same three routes and **carries its own reader** rather than
reaching into the leaf. That is a deliberate duplication, not an oversight, and it follows the
precedent :mod:`bfinance.statements.fundamentals_provider` set for the same vendor's fundamentals
pages ("the table parser is duplicated rather than imported"). The cost of the duplication is a
possible drift, and the mitigation is
:mod:`tests.test_sources_price_provider`, which drives this reader against the **same recorded
payloads** the leaf's own tests use and asserts parsed bars cell for cell. Drift is then a test
failure rather than silent staleness.

The three routes, and all three are robots-ALLOWED
--------------------------------------------------
Read from the live ``robots.txt`` on 2026-09-30. The ``User-agent: *`` group carries 28
``Disallow`` rules and a ``Crawl-delay: 1``; **none** of them covers a path below, which is the
reason this module can exist at all where a search route could not:

===============================  ===========================================================
route                            what it is for
===============================  ===========================================================
``/equity-sitemap-stocks.xml``    the only symbol resolver. 5,925 ``<loc>`` entries on
                                 2026-09-30, of the form ``/equity/{id}/{symbol}/{slug}/``.
                                 831 KB, so it is cached.
``/equity/{id}/{sym}/{slug}/``    the symbol's own page, which is the **only** place the
                                 per-symbol OHLC token exists.
``/mapp/v1/stock/web/ohlc/``      the daily EOD JSON, addressed by id **and** token.
===============================  ===========================================================

The disallowed sibling that is deliberately *not* here is the autocomplete under
``*/member/``. Its absence is not a limitation to be papered over: the sitemap is an exact-match
resolver and needs no opt-in, and the fuzzy ranked route is exactly the one that answers "a
question" with twenty candidates, where the first is the vendor's guess rather than an answer.

Three requests per symbol, two after the first
----------------------------------------------
The OHLC token is opaque, per-symbol, and exists nowhere but the ``data-eodurl`` attribute of the
symbol's own HTML page -- MOTHERSON's is ``TQZITAIRHO5FCC7SYAKGO6M7QA======``, RELIANCE's is a
different string entirely, and it **rotates**. So the chain is sitemap -> page -> JSON on the
first call and sitemap -> JSON afterwards, and this module caches both the index and the token for
the same reason: a token that is re-scraped per call doubles the cost of every symbol forever.

The Referer gate, which is not a bad token
-------------------------------------------
``/mapp/v1/`` answers **HTTP 444 with a zero-length body** unless the request carries
``Referer:`` naming a page on the site. Measured on the same valid URL:

=========================  ==========================================================
request                    result
=========================  ==========================================================
no ``Referer``             **HTTP 444**, zero-length body
``Referer: <the page>``    HTTP 200, 396 KB of JSON
``Referer``, wrong token   HTTP **200**, 92 bytes, ``head.status == "2"``
=========================  ==========================================================

Two different failures with two different shapes, and both are handled differently here because
collapsing them is how a routing decision is made on an error page. **444** means the request did
not look like it came from the site, and it is terminal -- retrying an unchanged request only
spends the vendor's patience, and the zero-length body must never reach a JSON decode. **A
rejected token is a 200**, so status-code checking alone would report success and hand the caller
an empty body; :func:`unwrap_envelope` treats any ``head.status != "0"`` as a failure, and
:meth:`PriceProvider.price_history` re-scrapes the token exactly once in case it rotated.

The wire, and the three things that surprise people
---------------------------------------------------
* Rows arrive **newest first**. The frame is sorted ascending; the transmission order is not
  preserved, because a caller who inherits it gets a reversed index rather than a series.
* The five freshest sessions live in a **separate** ``eodDataLastWeek`` key that ``eodData`` does
  not contain. Merged by default; ``eodData`` stays authoritative on a shared date, and a
  duplicate session is counted rather than quietly collapsed.
* ``close_price`` and ``last_price`` are **different fields** and disagree on most bars. ``Close``
  is ``close_price``, because that is the field consistent with the bar's own O/H/L, and the
  number of disagreeing rows is recorded in
  :data:`CLOSE_LAST_DIVERGENT_ATTR` rather than resolved silently.

The price basis, stated rather than implied
-------------------------------------------
:data:`PRICE_BASIS` is ``as_traded_split_bonus_adjusted`` and :data:`DEMERGER_APPLIED` is
``False``: as-traded, back-adjusted for **splits and bonuses only**, with demergers **not**
applied. It was never ``yfinance_back_adjusted`` on any symbol measured, and the label was
corrected rather than softened. Both facts ride on every frame, because a split/bonus-adjusted
series is **not** interchangeable with the as-traded bhavcopy: a caller who splices the two across
a split date gets a corporate-action-sized step that never happened, and a caller who splices them
across a demerger date gets the opposite error, because there it is the *exchange* print that
survives. The registry row's basis is :attr:`~bfinance.sources.model.Basis.SPLIT_ADJUSTED` for
this reason and no other.

A reader, not a client
----------------------
:class:`PriceProvider` is built **per call** by the adapter rather than memoised at module level.
It holds a sitemap index and a token cache, and a shared cache across capabilities is not a thing
a reader can verify; construction is free and opens no socket, and the TTLs are long enough that
the second request for a symbol in the same window still costs one page fetch.
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
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Tuple
from urllib.parse import unquote

import httpx
import numpy as np
import pandas as pd

from bfinance.utils.exceptions import (
    RateLimitExceededError,
    TickerNotFoundError,
    UpstreamServiceError,
)

__all__ = [
    "BASE_URL",
    "CLOSE_LAST_DIVERGENT_ATTR",
    "COLUMNS",
    "DEMERGER_APPLIED",
    "DEMERGER_APPLIED_ATTR",
    "INDEX_TTL_SECONDS",
    "LAST_WEEK_ATTR",
    "MIN_REQUEST_INTERVAL_MS",
    "NOTE_ATTR",
    "OHLC_PATH",
    "PRICE_BASIS",
    "PRICE_BASIS_ATTR",
    "PROVENANCE",
    "SITEMAP_PATH",
    "SOURCE_ATTR",
    "STOCK_PAGE_PATH",
    "SYNTHETIC_OHLC_ATTR",
    "TOKEN_TTL_SECONDS",
    "USER_AGENTS",
    "PriceProvider",
    "build_ohlc_path",
    "build_stock_page_path",
    "extract_ohlc_token",
    "narrow",
    "parse_ohlc_payload",
    "parse_sitemap",
    "price_history",
    "redact",
    "unwrap_envelope",
]
logger = logging.getLogger("bfinance")

#: Provenance tag, matching the value the leaf package and the recorded fixtures already use, so a
#: caller switching between the two readers sees one vendor string rather than two.
PROVENANCE = "trendlyne"

BASE_URL = "https://trendlyne.com"

#: The only symbol resolver the vendor has. 831 KB, 5,925 entries on 2026-09-30; cached, because
#: re-downloading it per symbol would dominate the cost of the three requests this module makes.
SITEMAP_PATH = "/equity-sitemap-stocks.xml"

#: The symbol's own page. Read only for its ``data-eodurl`` token -- and that page is also the
#: correct ``Referer`` for the JSON route below, which is the same URL computed twice rather than
#: a string built in two places.
STOCK_PAGE_PATH = "/equity/{stock_id}/{symbol}/{slug}/"

#: The daily EOD JSON. Per-symbol token required; see the module docstring's status matrix.
OHLC_PATH = "/mapp/v1/stock/web/ohlc/{stock_id}/{token}/"

#: Identical to the bhavcopy frame's columns, so a frame from here drops into the same consumers.
COLUMNS: Tuple[str, ...] = ("Open", "High", "Low", "Close", "Volume")

#: As-traded, back-adjusted for splits and bonuses only, demergers NOT applied. It is a constant
#: rather than a per-symbol derivation because one rule held on every symbol measured, and it is
#: **not** ``yfinance_back_adjusted``.
PRICE_BASIS = "as_traded_split_bonus_adjusted"

#: Whether demerger adjustment is applied. ``False``, stated separately because it is the half of
#: the basis a consumer is most likely to get backwards: on the demerger names it is the *exchange*
#: and yfinance that rewrite history, not this route.
DEMERGER_APPLIED = False

PRICE_BASIS_ATTR = "bfinance_sources_price_basis"
DEMERGER_APPLIED_ATTR = "bfinance_sources_demerger_applied"
SOURCE_ATTR = "bfinance_sources_price_source_url"
NOTE_ATTR = "bfinance_sources_price_note"
CLOSE_LAST_DIVERGENT_ATTR = "bfinance_sources_close_last_divergent_rows"
LAST_WEEK_ATTR = "bfinance_sources_last_week_rows_added"
SYNTHETIC_OHLC_ATTR = "bfinance_synthetic_ohlc"

#: The pacing floor in milliseconds, and a **compliance** number rather than a tuning knob. The
#: vendor's ``robots.txt`` ``User-agent: *`` group states ``Crawl-delay: 1`` -- one request per
#: second. The ``Crawl-delay: 10`` that also appears in that file is scoped to
#: ``GPTBot``/``ClaudeBot`` and does not apply to a caller that identifies as neither. ``0.0``
#: disables pacing, which is how the offline tests run and the only supported way to go faster.
MIN_REQUEST_INTERVAL_MS = 1000.0

#: The sitemap is a whole-market document, so it is cached for a day; the token is per-symbol and
#: rotates, so its window is deliberately short.
INDEX_TTL_SECONDS = 24 * 3600.0
TOKEN_TTL_SECONDS = 3600.0

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
]

#: ``/equity/{id}/{symbol}/{slug}/`` in the sitemap. Every ``<loc>`` on the live 5,925-entry
#: document matches it, and the segments are URL- and XML-escaped (``M&amp;M``), so both are
#: decoded before the symbol becomes a key -- otherwise a real, resolvable symbol 404s on lookup
#: and reads as unknown.
_SITEMAP_URL_RE = re.compile(r"/equity/(\d+)/([^/?#]+)/([^/?#]+)/?")

#: The OHLC token, captured out of the page's ``data-eodurl``. ``[A-Za-z0-9=]+`` because the token
#: is base64-ish and carries trailing ``=`` padding, which is used in the URL verbatim.
_TOKEN_RE = re.compile(r"/mapp/v1/stock/web/ohlc/\d+/([A-Za-z0-9=]+)/")
_TOKEN_IN_URL_RE = re.compile(r"(/ohlc/\d+/)[A-Za-z0-9=]+/")

#: Wire column names (``eodHeaders[].unique_name``) this reader requires.
_REQUIRED: Tuple[str, ...] = (
    "date", "open_price", "high_price", "low_price", "close_price", "volume",
)
#: Observed on the wire but deliberately not surfaced as a column; see the module docstring.
_EXTRA: Tuple[str, ...] = ("last_price",)

# Global pacing state, shared across instances so concurrent readers burst once rather than N
# times. The alternative -- a per-instance timer -- is a rate limiter that a caller can defeat by
# constructing a second reader, which is not a rate limiter.
_GLOBAL_PACING_LOCK = threading.Lock()
_GLOBAL_LAST_REQUEST_TIME = 0.0


# --------------------------------------------------------------------------- #
# symbol resolution
# --------------------------------------------------------------------------- #


def _decoded(value: str) -> str:
    """XML entity-decode **then** percent-decode a sitemap path segment.

    The order is not a preference. The document is XML, so a symbol containing an ampersand is
    stored escaped as ``M&amp;M``; percent-decoding first leaves the literal ``M&amp;M`` in the
    index, the lookup misses, and a real, resolvable symbol is reported as unknown.
    """
    return unquote(_html.unescape(value))


def parse_sitemap(xml_text: str) -> Dict[str, Tuple[int, str]]:
    """Parse the equity sitemap into ``symbol -> (stock_id, slug)``.

    A symbol that is *not* a ticker is handled rather than rejected: the tail of the live sitemap
    carries ``/equity/3622447/1216/bse-midsmall-private-banks/``, an index family keyed by a
    number. The segment is the key either way, and the slug carries the human-readable name for
    anyone who needs it.

    Raises:
        UpstreamServiceError: The document held no recognisable entry. Raised rather than returned
            as an empty index, because an empty index makes **every** symbol look unresolvable and
            turns a schema change into a silent, total outage of this source.
    """
    if not xml_text:
        raise UpstreamServiceError(
            "the equity sitemap response was empty. An empty index would make every symbol look "
            "unresolvable, so this is reported rather than returned."
        )
    index: Dict[str, Tuple[int, str]] = {}
    for match in _SITEMAP_URL_RE.finditer(xml_text):
        symbol = _decoded(match.group(2))
        index[symbol] = (int(match.group(1)), _decoded(match.group(3)))
    if not index:
        raise UpstreamServiceError(
            "no /equity/{id}/{symbol}/{slug}/ entries in the sitemap. First 300 chars: "
            f"{xml_text[:300]!r}. The document may have changed shape, or the request may have "
            "been served an error page -- and those are two different problems."
        )
    return index


def build_stock_page_path(stock_id: int, symbol: str, slug: str) -> str:
    """The symbol's own page path, which is also the ``Referer`` its JSON route requires."""
    return STOCK_PAGE_PATH.format(stock_id=stock_id, symbol=symbol, slug=slug)


def build_ohlc_path(stock_id: int, token: str) -> str:
    """The daily-OHLC path. The token is inserted verbatim, padding included."""
    if not token:
        raise ValueError(
            "the OHLC route needs a per-symbol token, which exists only inside the symbol's own "
            "page. An empty token here would mint a well-formed URL for a route nobody can read."
        )
    return OHLC_PATH.format(stock_id=stock_id, token=token)


def extract_ohlc_token(html: str) -> str:
    """The per-symbol token, read out of a stock page.

    The first distinct match wins. The live page repeats the same ``data-eodurl`` six times and
    all six captures are byte-identical, so "first" is a measurement rather than a coin flip; a
    page carrying **more than one distinct** token is a markup change, and it is reported rather
    than resolved by picking one.

    Raises:
        UpstreamServiceError: The page carried no token. Terminal until the page is re-inspected,
            and never guessed at.
    """
    tokens: List[str] = []
    for token in _TOKEN_RE.findall(html or ""):
        if token not in tokens:
            tokens.append(token)
    if not tokens:
        raise UpstreamServiceError(
            "no OHLC token on this page: expected a URL matching "
            f"{_TOKEN_RE.pattern!r} in the stock chart's data-eodurl attribute. The token is the "
            "only way to reach the OHLC route, so this is terminal until the page is re-inspected "
            "and it is not worth guessing one."
        )
    if len(tokens) > 1:
        logger.warning(
            "stock page carries %d distinct OHLC tokens; using the first. Lengths: %s",
            len(tokens), [len(t) for t in tokens],
        )
    return tokens[0]


def redact(url: str) -> str:
    """Replace a per-symbol token in a URL with ``<token>``.

    The token is a per-symbol credential for a private API surface. It must not reach a log line,
    an exception message, or a frame's ``attrs`` on the way to a CSV export.
    """
    return _TOKEN_IN_URL_RE.sub(r"\1<token>/", url or "")


# --------------------------------------------------------------------------- #
# payload parsing
# --------------------------------------------------------------------------- #


def unwrap_envelope(payload: Any, *, context: str) -> Dict[str, Any]:
    """Unwrap ``{"head": ..., "body": ...}`` and verify the request succeeded.

    Every JSON route on this site answers **200** for a failed request: a rejected token is
    92 bytes of ``{"head": {"status": "2", "statusDescription": "Not Authorized !"}, "body": {}}``.
    Checking the status code is therefore not enough, and returning the body unchecked would hand
    the caller an empty frame that reads as a quiet market rather than as a refused request.

    Raises:
        UpstreamServiceError: ``head.status`` is not ``"0"`` -- a rejected or rotated token.
    """
    if not isinstance(payload, dict):
        raise UpstreamServiceError(
            f"{context}: expected a JSON object envelope, got {type(payload).__name__}."
        )
    head = payload.get("head")
    if not isinstance(head, dict):
        raise UpstreamServiceError(
            f"{context}: the response has no 'head' object, so the success flag is unreadable. "
            f"Top-level keys: {sorted(payload)[:8]}. The schema may have changed."
        )
    status = str(head.get("status", "")).strip()
    if status != "0":
        description = str(head.get("statusDescription") or "").strip()
        raise UpstreamServiceError(
            f"{context}: the vendor reported status={status!r} "
            f"({description or 'no description'}). Note this arrives as HTTP 200 with an empty "
            f"body, so a status-code check alone would have called it a success. For the OHLC "
            f"route it means the per-symbol token was rejected or has rotated."
        )
    body = payload.get("body")
    if not isinstance(body, dict):
        raise UpstreamServiceError(
            f"{context}: 'body' is {type(body).__name__}, expected an object. The schema may have "
            f"changed."
        )
    return body


def _rows_to_frame(
    rows: Any, positions: Mapping[str, int], width: int, *, label: str
) -> Tuple[pd.DataFrame, int, int]:
    """Turn wire rows into a typed frame: ``(frame, missing_closes, close_last_divergent)``.

    Raises on a wrong row width, an unparseable date, or an all-missing close. Nothing is dropped
    or filled to recover from any of those -- a frame that is short a bar and a frame that says
    which bar is missing are different artefacts, and only one of them is data.
    """
    if not isinstance(rows, (list, tuple)):
        raise UpstreamServiceError(
            f"{label}: {type(rows).__name__}, expected a list of rows."
        )
    if not rows:
        raise UpstreamServiceError(f"{label} is empty; nothing to parse.")

    bad_width = [
        i for i, row in enumerate(rows)
        if not isinstance(row, (list, tuple)) or len(row) != width
    ]
    if bad_width:
        raise UpstreamServiceError(
            f"{label}: {len(bad_width)} of {len(rows)} rows do not have {width} values. First "
            f"offending row index: {bad_width[0]}. The schema may have changed."
        )

    # The wire timestamp is a retrieval marker ('2026-09-18T10:01:00+00:00'), not a session time.
    # Only the literal date is taken: converting to a timezone first can move a bar onto the wrong
    # day, because a 19:00 UTC bar is already the next morning in IST.
    raw_dates = [str(row[positions["date"]]) for row in rows]
    index = pd.to_datetime([d[:10] for d in raw_dates], format="%Y-%m-%d", errors="coerce")
    if index.isna().any():
        first_bad = next(d for d, bad in zip(raw_dates, index.isna()) if bad)
        raise UpstreamServiceError(
            f"{label}: {int(index.isna().sum())} of {len(index)} dates are not YYYY-MM-DD "
            f"prefixed. First offending value: {first_bad!r}."
        )

    data: Dict[str, Any] = {}
    for name in _REQUIRED:
        if name == "date":
            continue
        # Values go in as ndarrays, not Series. A Series carries a RangeIndex which pandas
        # reindexes against the explicit DatetimeIndex below, silently producing an all-NaN frame
        # that still "succeeds".
        data[name] = np.asarray(
            pd.to_numeric([row[positions[name]] for row in rows], errors="coerce")
        )

    close = data["close_price"]
    if bool(pd.isna(close).all()):
        raise UpstreamServiceError(
            f"{label}: every close_price is missing or non-numeric. The series is unusable and is "
            f"not returned as an empty frame."
        )

    divergent = 0
    for name in _EXTRA:
        if name not in positions:
            continue
        other = np.asarray(
            pd.to_numeric([row[positions[name]] for row in rows], errors="coerce")
        )
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


def parse_ohlc_payload(
    payload: Any,
    symbol: str,
    *,
    source_url: Optional[str] = None,
    retrieved_at: Optional[datetime] = None,
    include_last_week: bool = True,
) -> pd.DataFrame:
    """Parse a daily-OHLC payload into a ``DatetimeIndex`` frame with truthful ``attrs``.

    The whole of this function is a *reader*: it takes bytes the caller already has. Nothing here
    opens a socket, which is what lets the offline test drive it through a mock transport and
    assert parsed bars rather than asserting that a request went out.

    Args:
        payload: The decoded JSON. Unwrapped and status-checked by :func:`unwrap_envelope`.
        symbol: Recorded on ``attrs``. Not cross-checked against the payload, because the payload's
            own ``stockData`` is not part of this frame's contract.
        source_url: The URL used, for ``attrs``. The token is redacted.
        retrieved_at: When the response arrived.
        include_last_week: Merge ``eodDataLastWeek``. See the module docstring.

    Returns:
        Columns :data:`COLUMNS`, ascending ``Date`` index, carrying the price basis, the demerger
        flag, the vendor window and the close/last divergence count.

    Raises:
        UpstreamServiceError: A failed envelope, a missing or renamed column, a row of the wrong
            width, an unparseable date, or an all-missing close.
    """
    body = unwrap_envelope(payload, context=f"the OHLC payload for {symbol}")
    for key in ("eodHeaders", "eodData"):
        if key not in body:
            raise UpstreamServiceError(
                f"the OHLC payload for {symbol} has no {key!r}. Body keys: {sorted(body)[:12]}. "
                f"The schema may have changed."
            )

    headers = body["eodHeaders"]
    if not isinstance(headers, list) or not headers:
        raise UpstreamServiceError(
            f"the OHLC payload for {symbol} has an empty 'eodHeaders'; the column order is "
            f"undefined, and guessing it is exactly the mistake this reader refuses to make."
        )
    try:
        names = [
            str(header.get("unique_name") or header.get("name") or "").strip().lower()
            for header in headers
        ]
    except AttributeError as exc:
        raise UpstreamServiceError(
            f"the OHLC payload for {symbol} has a malformed 'eodHeaders' entry: {headers[0]!r}"
        ) from exc

    missing = [column for column in _REQUIRED if column not in names]
    if missing:
        raise UpstreamServiceError(
            f"the OHLC payload for {symbol} is missing required column(s) {missing}. Present: "
            f"{names}. The schema may have changed."
        )
    positions = {name: index for index, name in enumerate(names)}

    eod_frame, missing_close, divergent = _rows_to_frame(
        body["eodData"], positions, len(names), label=f"eodData for {symbol}"
    )

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
            divergent += week_divergent

    frame = frame.sort_index()

    # Duplicate sessions are impossible in a correct series. Keep the last occurrence and say so
    # rather than silently collapsing one.
    duplicated = int(frame.index.duplicated().sum())
    if duplicated:
        frame = frame[~frame.index.duplicated(keep="last")]

    first_date = frame.index[0].date().isoformat()
    last_date = frame.index[-1].date().isoformat()
    attrs: Dict[str, Any] = {
        "bfinance_provenance": PROVENANCE,
        "bfinance_symbol": symbol,
        PRICE_BASIS_ATTR: PRICE_BASIS,
        DEMERGER_APPLIED_ATTR: DEMERGER_APPLIED,
        SYNTHETIC_OHLC_ATTR: False,
        CLOSE_LAST_DIVERGENT_ATTR: divergent,
        LAST_WEEK_ATTR: last_week_added,
        "bfinance_sources_price_bars": len(frame),
        "bfinance_sources_price_first_date": first_date,
        "bfinance_sources_price_last_date": last_date,
        "bfinance_sources_price_missing_close_rows": missing_close,
        "bfinance_sources_price_duplicate_dates": duplicated,
        "bfinance_sources_price_exchange": body.get("exchange"),
        NOTE_ATTR: (
            f"Daily EOD series from {BASE_URL}, {first_date}..{last_date} ({len(frame)} bars). "
            f"Basis: {PRICE_BASIS} -- as-traded, back-adjusted for splits and bonuses only, "
            f"demergers NOT applied. That is not the NSE bhavcopy basis, so these bars must not be "
            f"spliced onto bhavcopy bars across a split or bonus date; and on a demerger name it "
            f"is the exchange print that survives, so a series that rewrites demerger history is "
            f"the one that is wrong. The token is per-symbol, rotates, and is never recorded on "
            f"attrs or in a log line."
        ),
    }
    if source_url:
        attrs[SOURCE_ATTR] = redact(source_url)
    if retrieved_at is not None:
        attrs["bfinance_sources_price_retrieved_at"] = retrieved_at.astimezone(
            timezone.utc
        ).isoformat(timespec="seconds")
    frame.attrs.update(attrs)
    return frame[list(COLUMNS)]


# --------------------------------------------------------------------------- #
# the reader
# --------------------------------------------------------------------------- #


def _run_async(coro):
    """Bridge to sync. A running loop in this thread gets a one-worker thread, because
    ``asyncio.run`` cannot nest; the thread is joined, so the value is materialised at the call
    rather than escaping into the caller's frame.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            return executor.submit(lambda: asyncio.run(coro)).result()
    return asyncio.run(coro)


class PriceProvider:
    """One symbol's daily OHLC, over the three routes the module docstring names.

    Constructing this performs **no I/O**: the sitemap and the token are fetched on first use and
    cached in memory with a TTL. That is what lets a registry row name it as a module attribute
    and a binding construct it per call without the construction costing a request.

    Args:
        timeout: Per-request timeout, seconds.
        max_retries: Attempts for 429, 5xx and transport failures. Every other status is terminal.
        proxy: Proxy URL, falling back to ``HTTPS_PROXY``/``HTTP_PROXY`` as elsewhere in bfinance.
        min_request_interval_ms: The global pacing slot. :data:`MIN_REQUEST_INTERVAL_MS` is the
            robots-derived floor; ``0.0`` disables it, which is how the offline tests run.
        transport: An ``httpx`` transport. The offline tests pass ``httpx.MockTransport``, and
            that is the only reason this parameter exists.
    """

    def __init__(
        self,
        timeout: float = 20.0,
        max_retries: int = 3,
        proxy: Optional[str] = None,
        min_request_interval_ms: float = MIN_REQUEST_INTERVAL_MS,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self.timeout = timeout
        self.max_retries = max(1, int(max_retries))
        self.proxy = proxy or os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
        self.min_request_interval_ms = min_request_interval_ms
        self.transport = transport
        self._index: Optional[Dict[str, Tuple[int, str]]] = None
        self._index_at: float = 0.0
        self._tokens: Dict[int, Tuple[float, str]] = {}
        self._last_request_time = 0.0
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- internals

    def _headers(self, referer: Optional[str] = None) -> Dict[str, str]:
        headers = {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "application/json, text/html;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        if referer:
            # Required by the ``/mapp/v1/`` family; the page routes do not need it. Sending it
            # everywhere would be a lie about what the request is.
            headers["Referer"] = referer
        return headers

    async def _throttle_pacing(self) -> None:
        """Reserve a global pacing slot, so concurrent readers burst once rather than N times."""
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
        return path if path.startswith("http") else f"{BASE_URL}{path}"

    async def _get(self, path: str, *, referer: Optional[str] = None) -> httpx.Response:
        """GET with pacing and retry. Returns the 200 response or raises a typed error.

        Retry covers 429, 5xx and transport failures. Every other status is terminal, and 444 is
        called out separately because it is the ``/mapp/v1/`` request gate rather than a bad
        symbol: the identical URL returns 200 with a ``Referer``, so retrying an unchanged request
        cannot succeed and only spends the vendor's patience.
        """
        url = self._absolute(path)
        last_error: Optional[Exception] = None

        for attempt in range(self.max_retries):
            await self._throttle_pacing()
            kwargs: Dict[str, Any] = {
                "timeout": self.timeout,
                "follow_redirects": True,
            }
            if self.proxy:
                kwargs["proxy"] = self.proxy
            if self.transport is not None:
                kwargs["transport"] = self.transport
            try:
                async with httpx.AsyncClient(**kwargs) as client:
                    response = await client.get(url, headers=self._headers(referer))
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(min(2.0 * (2**attempt), 8.0) + random.uniform(0, 0.5))
                continue

            status = response.status_code
            if status == 200:
                return response
            if status == 444:
                raise UpstreamServiceError(
                    f"the vendor returned HTTP 444 for {redact(url)}. This is its request gate for "
                    f"the /mapp/v1/ family, not a bad symbol: the identical URL answers 200 when "
                    f"the request carries a Referer. It is terminal, and the zero-length body must "
                    f"never reach a JSON decode."
                )
            if status == 429:
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(min(2.0 * (2**attempt), 8.0) + random.uniform(0.5, 1.5))
                    continue
                raise RateLimitExceededError(
                    f"the vendor rate limited this reader (HTTP 429) after {self.max_retries} "
                    f"attempts for {redact(url)}. The index and token caches remain valid."
                )
            if status >= 500:
                last_error = UpstreamServiceError(
                    f"the vendor returned HTTP {status} for {redact(url)}."
                )
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(min(2.0 * (2**attempt), 8.0) + random.uniform(0, 0.5))
                continue
            raise UpstreamServiceError(
                f"the vendor returned HTTP {status} for {redact(url)}."
            )

        raise UpstreamServiceError(
            f"the vendor failed after {self.max_retries} attempts for {redact(url)}."
        ) from last_error

    async def _get_text(self, path: str, *, referer: Optional[str] = None) -> str:
        return (await self._get(path, referer=referer)).text

    async def _get_json(self, path: str, *, referer: Optional[str] = None) -> Any:
        response = await self._get(path, referer=referer)
        try:
            return response.json()
        except ValueError as exc:
            raise UpstreamServiceError(
                f"a non-JSON body came back for {redact(self._absolute(path))} (first 160 chars: "
                f"{response.text[:160]!r})."
            ) from exc

    # ---------------------------------------------------------------- reading

    async def symbol_index_async(self) -> Dict[str, Tuple[int, str]]:
        """The whole-market symbol index, fetched at most once per TTL."""
        with self._lock:
            if self._index is not None and (time.monotonic() - self._index_at) <= INDEX_TTL_SECONDS:
                return self._index
        xml_text = await self._get_text(SITEMAP_PATH)
        index = parse_sitemap(xml_text)
        with self._lock:
            self._index = index
            self._index_at = time.monotonic()
        return index

    async def _token_async(self, stock_id: int, page_path: str, *, force_refresh: bool) -> str:
        with self._lock:
            cached = self._tokens.get(stock_id)
        if cached and not force_refresh and (time.monotonic() - cached[0]) <= TOKEN_TTL_SECONDS:
            return cached[1]
        html = await self._get_text(page_path)
        token = extract_ohlc_token(html)
        with self._lock:
            self._tokens[stock_id] = (time.monotonic(), token)
        return token

    async def price_history_async(
        self,
        symbol: str,
        *,
        start: Optional[str] = None,
        end: Optional[str] = None,
        include_last_week: bool = True,
    ) -> pd.DataFrame:
        """Daily OHLCV for one symbol, ascending, with the price basis published on ``attrs``.

        Args:
            symbol: The ticker as the sitemap spells it. An ETF will not be found: none of the
                5,925 entries on 2026-09-30 is one, and the only route that resolves them is
                robots-disallowed. That is stated rather than papered over, because a frame for
                the wrong instrument is worse than a refusal.
            start: Inclusive lower bound, ``'YYYY-MM-DD'``. Applied **locally**, after the fetch:
                this route has no period segment, so narrowing here cannot cost the vendor a
                request and cannot change which rows come back.
            end: Inclusive upper bound, same form and same reasoning.
            include_last_week: Merge the five freshest sessions, which live in their own key.

        Raises:
            TickerNotFoundError: The symbol is not in the sitemap.
            UpstreamServiceError: A failed envelope, a schema change, a missing token, or a
                transport failure.
            ValueError: ``start`` after ``end``.
        """
        if start and end and str(start) > str(end):
            raise ValueError(
                f"start={start!r} is after end={end!r}. An empty window is a question, and this "
                f"returns a series or raises; it does not quietly return nothing."
            )
        index = await self.symbol_index_async()
        record = index.get(symbol)
        if record is None:
            raise TickerNotFoundError(
                f"{symbol!r} is not in the equity sitemap, which holds {len(index)} entries. ETFs "
                f"are absent from it entirely and the one route that resolves them is "
                f"robots-disallowed, so an ETF miss is expected rather than a typo; a miss on an "
                f"ordinary equity is one of the two."
            )
        stock_id, slug = record
        page_path = build_stock_page_path(stock_id, symbol, slug)
        page_url = self._absolute(page_path)
        retrieved_at = datetime.now(timezone.utc)
        token = await self._token_async(stock_id, page_path, force_refresh=False)
        # The fetch is inside the retry and the parse is deliberately OUTSIDE it. A rejected token
        # and a schema change are both UpstreamServiceError, and re-scraping the token fixes only
        # the first; wrapping the parse would spend a page request discovering that a renamed
        # column is not a stale credential.
        payload, source_url = await self._fetch_ohlc(
            stock_id, token, page_path=page_path, page_url=page_url
        )
        return narrow(
            parse_ohlc_payload(
                payload,
                symbol,
                source_url=source_url,
                retrieved_at=retrieved_at,
                include_last_week=include_last_week,
            ),
            start=start,
            end=end,
        )

    async def _fetch_ohlc(
        self, stock_id: int, token: str, *, page_path: str, page_url: str
    ) -> Tuple[Any, str]:
        """The OHLC JSON, re-scraping the token **once** if the envelope says it was rejected.

        Returns the decoded payload and the URL it came from, so the caller can parse outside
        this method and keep a schema failure from being mistaken for a stale credential.
        """
        path = build_ohlc_path(stock_id, token)
        try:
            return await self._get_json(path, referer=page_url), self._absolute(path)
        except UpstreamServiceError:
            # The token rotates. Re-scrape exactly once, and only if it actually changed, so a
            # genuinely rejected token costs one page fetch rather than a retry storm.
            fresh = await self._token_async(stock_id, page_path, force_refresh=True)
            if fresh == token:
                raise
            logger.info("the OHLC token for stock %s rotated; retried once.", stock_id)
            retry_path = build_ohlc_path(stock_id, fresh)
            return await self._get_json(retry_path, referer=page_url), self._absolute(retry_path)

    def price_history(
        self,
        symbol: str,
        *,
        start: Optional[str] = None,
        end: Optional[str] = None,
        include_last_week: bool = True,
    ) -> pd.DataFrame:
        """Sync variant of :meth:`price_history_async`."""
        return _run_async(
            self.price_history_async(
                symbol, start=start, end=end, include_last_week=include_last_week
            )
        )


def narrow(
    frame: pd.DataFrame, *, start: Optional[str] = None, end: Optional[str] = None
) -> pd.DataFrame:
    """Slice a parsed frame to ``[start, end]`` inclusive, without re-fetching.

    Narrowing here rather than in the request is a property of the route, not a shortcut: this
    vendor publishes **no** period segment, so the whole history is one request however narrow the
    caller's window. The dates are compared as strings, which is exact for ``'YYYY-MM-DD'`` and is
    why that is the only accepted form.
    """
    if start is None and end is None:
        return frame
    mask = slice(None)
    if start is not None:
        mask = frame.index >= pd.Timestamp(start)
    if end is not None:
        upper = frame.index <= pd.Timestamp(end)
        mask = upper if start is None else (mask & upper)
    return frame[mask]


def price_history(
    symbol: str,
    *,
    start: Optional[str] = None,
    end: Optional[str] = None,
    include_last_week: bool = True,
) -> pd.DataFrame:
    """One symbol's daily OHLC, on a reader built for this call.

    The registry row's entry point. A module-level shortcut rather than a shared client because
    the reader holds a sitemap index and a token cache, and a cache shared across capabilities is
    not a thing a reader can verify; construction opens no socket, so a fresh reader per call costs
    nothing until the first request.
    """
    provider = PriceProvider()
    return provider.price_history(
        symbol, start=start, end=end, include_last_week=include_last_week
    )
