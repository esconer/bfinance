"""
``bfinance.Search`` — free-text symbol lookup backed by NSE MarketLens.

yfinance's :class:`Search` queries Yahoo's ``/v1/finance/search`` and exposes ``.quotes`` (a
list of dicts) and ``.all`` (a dict whose ``quotes`` key holds the same list). This is that
surface over the only Indian search index bfinance already speaks to:
:meth:`bfinance.nse.marketlens.MarketLensClient.search`, which is a **method on the client**,
not a module-level function.

What the endpoint publishes
---------------------------
``GET /api/search?q=`` returns row dicts. A live call on **2026-10-22** returned seven keys:
``ticker``, ``name``, ``description``, ``matchScore``, ``marketCap``, ``id`` and ``type``.
(The client's own ``search_async`` docstring lists five; the payload is a superset.) They are
passed through **verbatim**: this wrapper does not rename ``ticker`` to yfinance's ``symbol``,
add a ``.NS`` suffix, or invent ``shortname`` / ``quoteType`` / ``exchange`` fields the vendor
never sent. A caller who wants a bfinance ``Ticker`` from a hit can pass the ``ticker`` value
straight in, and a field the vendor adds later flows through without a change here.

``limit`` is applied **client-side** by ``DataFrame.head``; the endpoint has no count parameter.
It is still one request per construction, and reading ``.quotes`` or ``.all`` again does not
refetch. Failures are never converted into an empty list — see :class:`Search`.

The index is equities-only
--------------------------
This is measured behaviour, not a bug. On 2026-09-26, ``q=nifty`` returned **zero** results
despite ``NIFTYBEES`` trading, and the backing index has no ETF or mutual-fund entries at all.
Re-measured 2026-10-22: ``Search("nifty")`` and ``Search("niftybees")`` both returned zero
quotes. Any other index/ETF query returns an empty list rather than a match. MarketLens is
also NSE-only: BSE scrips are not in it.

An empty result is not an error here (unlike :meth:`MarketLensClient.price_history`, where zero
rows raise). That asymmetry is deliberate: "no equity matches this text" is an expected outcome
of a fuzzy search, whereas a price history that resolves to nothing indicates a bad symbol. The
consequence is that an empty ``quotes`` list cannot distinguish "no match" from "index/ETF",
which is why the limitation is stated here instead of being guessed at by the caller.

No cache, no session, no new transport: one :class:`MarketLensClient` is constructed per
``Search`` and its existing ``httpx`` path is reused unchanged.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .nse.marketlens import MarketLensClient

__all__ = ["Search"]

#: yfinance's own default for ``max_results``, kept so the two spellings agree.
DEFAULT_MAX_RESULTS = 8


class Search:
    """Resolve a free-text query to NSE equities, with yfinance's ``.quotes`` / ``.all`` shape.

    Args:
        query: Company name or symbol fragment, e.g. ``"reliance"``. Must be a non-empty
            string; a blank query raises :class:`ValueError` **before** any request.
        max_results: Maximum number of quotes to return. Passed to
            :meth:`MarketLensClient.search` as ``limit`` and applied there with
            ``DataFrame.head`` — the endpoint itself has no count parameter. Must be a
            positive integer.
        timeout: Per-request timeout in seconds, forwarded to :class:`MarketLensClient`.
        max_retries: Attempts for a network error, 429 or 5xx, forwarded to
            :class:`MarketLensClient`.
        proxy: HTTP proxy, forwarded to :class:`MarketLensClient`. ``None`` lets the client
            read ``HTTPS_PROXY`` / ``HTTP_PROXY`` as it already does.

    Attributes:
        query: The query as supplied.
        max_results: The cap as supplied.

    **Index and ETF queries return an empty** ``quotes`` **list.** Measured 2026-10-22: both
    ``Search("nifty")`` and ``Search("niftybees")`` returned zero rows, although NIFTYBEES
    trades. The index behind this endpoint is equities-only — that is measured behaviour, not a
    bug — and MarketLens is NSE-only besides, so a BSE scrip cannot match either.

    Raises:
        ValueError: The query is not a non-empty string, or ``max_results`` is not a positive
            integer. Both are checked before a socket opens.
        TickerNotFoundError: The endpoint has no route for the request (HTTP 404).
        UpstreamServiceError: Transport failure, 429/5xx after retries, a non-JSON body, or
            any other unexpected status. **Never swallowed into an empty list.**
        RateLimitExceededError: HTTP 429 on the final attempt.

    The quotes are the vendor's own rows — measured live on 2026-10-22 with keys ``ticker``,
    ``name``, ``description``, ``matchScore``, ``marketCap``, ``id`` and ``type`` — and nothing
    is added to or renamed in them. ``.all`` is ``{"quotes": [...]}`` only: yfinance also
    carries ``news``, ``lists``, ``research`` and ``nav`` because Yahoo serves them, while
    MarketLens serves none, and an always-empty list would read as "asked, found none" rather
    than "not published".

    On the yfinance signature: ``news_count``, ``lists_count``, ``include_cb``,
    ``include_nav_links``, ``include_research``, ``include_cultural_assets``,
    ``enable_fuzzy_query``, ``recommended`` and ``session`` are **not** accepted. The first
    eight describe Yahoo payloads this source does not have; ``session`` cannot be honoured
    (the client speaks ``httpx``), and accepting it silently is the defect elsewhere fixed by
    raising. ``raise_errors`` is likewise absent: this wrapper always raises, because turning
    an upstream failure into ``[]`` is exactly the "no matches" lie the tests pin against.
    """

    def __init__(
        self,
        query: str,
        max_results: int = DEFAULT_MAX_RESULTS,
        *,
        timeout: float = 20.0,
        max_retries: int = 3,
        proxy: Optional[str] = None,
    ) -> None:
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a non-empty string")
        if isinstance(max_results, bool) or not isinstance(max_results, int):
            raise ValueError(
                f"max_results must be an integer, got {type(max_results).__name__}"
            )
        if max_results < 1:
            raise ValueError(f"max_results must be at least 1, got {max_results}")

        self.query = query
        self.max_results = max_results
        self._client = MarketLensClient(
            timeout=timeout, max_retries=max_retries, proxy=proxy
        )
        self._quotes: List[Dict[str, Any]] = []
        self._all: Dict[str, List[Dict[str, Any]]] = {"quotes": self._quotes}
        self._search()

    def _search(self) -> "Search":
        """Issue the one request this instance makes and store its rows."""
        frame = self._client.search(self.query, limit=self.max_results)
        self._quotes = frame.to_dict(orient="records")
        self._all = {"quotes": self._quotes}
        return self

    @property
    def quotes(self) -> List[Dict[str, Any]]:
        """The matched rows as a list of dicts, in the vendor's ranking order.

        Empty when the index has no equity match, including for every index/ETF query.
        """
        return self._quotes

    @property
    def all(self) -> Dict[str, List[Dict[str, Any]]]:
        """yfinance-compatible result dict: ``{"quotes": [...]}``, holding the same list."""
        return self._all
