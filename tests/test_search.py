"""
``bfinance.Search`` — offline tests for the MarketLens-backed symbol lookup.

The seam
--------
:class:`MarketLensClient` builds its own ``httpx.AsyncClient`` inside ``_get_json`` and has no
transport parameter (the plan's "no new transport" rule), so these tests replace the ``httpx``
name **in the marketlens module** with a shim whose ``AsyncClient`` is the real one plus
``transport=httpx.MockTransport(handler)``. That is the module-level replacement
``tests/test_ownership.py::offline`` uses, and the ``MockTransport`` arrangement
``tests/test_trendlyne_core.py::mock_client`` uses; the shim itself is local only because no
shared helper knows this client's module. Every handler records the URL it served, so "one
request per call" is counted rather than assumed.

What is pinned
--------------
- one request per construction, and no refetch when ``.quotes`` / ``.all`` are read again;
- ``quotes`` is a list of the vendor's dicts, in the vendor's order, with **no invented keys**;
- a 200 with ``data: []`` is an empty list — not ``None``, not an exception. This is also what
  every index/ETF query returns; the limitation is documented on :class:`Search`;
- ``max_results`` reaches :meth:`MarketLensClient.search` as ``limit``, and caps the rows;
- an upstream failure raises instead of becoming ``[]``, with a negative control that proves
  the assertion can see a wrapper which *does* swallow it.

The rows below are synthetic: this file tests the wiring, not the vendor's current records.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Callable, Dict, List

import httpx
import pandas as pd
import pytest

from bfinance import Search
from bfinance.nse import marketlens as ml
from bfinance.utils.exceptions import UpstreamServiceError

#: The seven fields a live ``GET /api/search?q=`` returned on 2026-10-22, and nothing else.
QUOTE_KEYS = ("ticker", "name", "description", "matchScore", "marketCap", "id", "type")

#: Synthetic matches, already in the vendor's ranking order.
ROWS: tuple[Dict[str, Any], ...] = (
    {
        "ticker": "RELIANCE",
        "name": "Reliance Industries Ltd",
        "description": "Refining, petrochemicals and telecom",
        "matchScore": 100.0,
        "marketCap": 1_900_000_000_000,
        "id": "1234",
        "type": "equity",
    },
    {
        "ticker": "RELINFRA",
        "name": "Reliance Infrastructure Ltd",
        "description": "Power and infrastructure",
        "matchScore": 62.5,
        "marketCap": 90_000_000_000,
        "id": "5678",
        "type": "equity",
    },
    {
        "ticker": "RPOWER",
        "name": "Reliance Power Ltd",
        "description": "Power generation",
        "matchScore": 41.0,
        "marketCap": 60_000_000_000,
        "id": "9012",
        "type": "equity",
    },
)


def body(*rows: Dict[str, Any]) -> httpx.Response:
    """The endpoint's success envelope: rows under ``data``, ``error`` null."""
    return httpx.Response(200, json={"success": True, "data": list(rows), "error": None})


def offline(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> List[str]:
    """Serve every MarketLens request from ``handler``; return the URLs actually requested."""
    served: List[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        served.append(str(request.url))
        return handler(request)

    shim = SimpleNamespace(
        AsyncClient=lambda *args, **kwargs: httpx.AsyncClient(
            *args, transport=httpx.MockTransport(respond), **kwargs
        ),
        TimeoutException=httpx.TimeoutException,
        TransportError=httpx.TransportError,
    )
    monkeypatch.setattr(ml, "httpx", shim)
    return served


def outcome(build: Callable[[], Any]) -> str:
    """How a ``Search`` construction ended, as one comparable token.

    The token is the measurement both the honesty test and its negative control use: an
    upstream failure must read ``raised:UpstreamServiceError``, never ``returned:0``.
    """
    try:
        return f"returned:{len(build().quotes)}"
    except UpstreamServiceError:
        return "raised:UpstreamServiceError"


class SwallowsUpstreamErrors:
    """The defect, built on purpose: an upstream failure reported as "no matches".

    A wrapper that caught :class:`UpstreamServiceError` and returned ``[]`` is
    indistinguishable from a genuine miss at the call site. It exists here only so the test
    below can show that :func:`outcome` distinguishes the two answers.
    """

    def __init__(self, query: str, max_results: int = 8) -> None:
        try:
            self._quotes = Search(query, max_results=max_results).quotes
        except UpstreamServiceError:
            self._quotes = []

    @property
    def quotes(self) -> List[Dict[str, Any]]:
        return self._quotes


# ------------------------------------------------------------------ request contract


def test_one_request_per_construction_and_properties_do_not_refetch(monkeypatch):
    """One ``Search(...)`` is one request, however many times the results are read."""
    served = offline(monkeypatch, lambda request: body(*ROWS))

    search = Search("reliance")

    assert len(served) == 1, f"expected one request, saw {served}"
    assert "q=reliance" in served[0]
    assert search.all["quotes"] is search.quotes
    assert len(search.quotes) == len(ROWS)
    assert len(search.all["quotes"]) == len(ROWS)
    assert len(served) == 1, f"reading the results refetched: {served}"


def test_quotes_is_a_list_of_the_vendors_rows_in_order_and_nothing_is_invented(monkeypatch):
    """The quote dicts are pass-through: no ``symbol``, no ``.NS`` suffix, no renaming."""
    offline(monkeypatch, lambda request: body(*ROWS))

    search = Search("reliance")

    assert isinstance(search.quotes, list)
    assert [quote["ticker"] for quote in search.quotes] == [
        "RELIANCE",
        "RELINFRA",
        "RPOWER",
    ]
    assert set(search.quotes[0]) == set(QUOTE_KEYS), (
        "Search renamed or invented quote keys; it must pass the vendor's rows through "
        f"unchanged, and a live row carried {sorted(QUOTE_KEYS)}"
    )
    assert "symbol" not in search.quotes[0]
    # yfinance's news/lists/research/nav are absent because MarketLens publishes none of them:
    # an always-empty list would read as "asked, found none".
    assert list(search.all) == ["quotes"]


def test_a_zero_result_response_is_an_empty_list_not_none_and_not_an_exception(monkeypatch):
    """The shape every index/ETF query gets, and every genuine fuzzy miss.

    The endpoint answers 200 with ``data: []``; ``MarketLensClient.search`` turns that into an
    empty frame. The wrapper must expose ``[]`` and must not raise for it.
    """
    served = offline(monkeypatch, lambda request: body())

    search = Search("niftybees")

    assert isinstance(search.quotes, list)
    assert search.quotes == []
    assert search.quotes is not None
    assert search.all == {"quotes": []}
    assert len(served) == 1


def test_max_results_reaches_the_client_as_limit(monkeypatch):
    """The wrapper forwards ``max_results`` as ``limit``; the default is yfinance's 8."""
    seen: List[tuple[str, int]] = []

    def spy(self, query: str, limit: int = 10) -> pd.DataFrame:
        seen.append((query, limit))
        return pd.DataFrame([ROWS[0]])

    monkeypatch.setattr(ml.MarketLensClient, "search", spy)

    Search("reliance", max_results=3)
    Search("reliance")

    assert seen == [("reliance", 3), ("reliance", 8)]


def test_max_results_caps_the_rows_returned(monkeypatch):
    """``limit`` is applied by the client's ``DataFrame.head``; three rows, two wanted."""
    offline(monkeypatch, lambda request: body(*ROWS))

    assert len(Search("reliance", max_results=2).quotes) == 2


# --------------------------------------------------------------- local argument checks


@pytest.mark.parametrize("query", ["", "   ", None, 42])
def test_a_non_string_or_blank_query_is_refused_before_any_request(monkeypatch, query):
    served = offline(monkeypatch, lambda request: body(*ROWS))

    with pytest.raises(ValueError, match="query"):
        Search(query)

    assert served == [], f"a bad query opened a socket: {served}"


@pytest.mark.parametrize("bad", [0, -1, 2.5, True])
def test_a_non_positive_or_non_integer_max_results_is_refused_before_any_request(monkeypatch, bad):
    served = offline(monkeypatch, lambda request: body(*ROWS))

    with pytest.raises(ValueError, match="max_results"):
        Search("reliance", max_results=bad)

    assert served == [], f"a bad max_results opened a socket: {served}"


# ------------------------------------------------------- no silent empty on failure


def failing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Serve a non-retried error status, so ``UpstreamServiceError`` is the first outcome."""
    offline(
        monkeypatch,
        lambda request: httpx.Response(400, json={"error": "malformed request"}),
    )


def test_an_upstream_failure_is_not_reported_as_no_matches(monkeypatch):
    """A failure raises; it is never an empty result.

    ``[]`` is reserved for "the vendor answered and had nothing", which is the only reading a
    caller can act on. A 400 has no rows because the request was not answered at all.
    """
    failing(monkeypatch)

    assert outcome(lambda: Search("reliance")) == "raised:UpstreamServiceError"


def test_that_assertion_goes_red_on_a_wrapper_that_swallows_the_error(monkeypatch):
    """The negative control for the test above.

    If :class:`Search` ever caught ``UpstreamServiceError`` and returned ``[]``, the honest
    reading would become ``returned:0`` — the exact token this control produces on purpose.
    Two different answers from the same measurement is what makes the assertion evidence.
    """
    failing(monkeypatch)

    honest = outcome(lambda: Search("reliance"))
    swallowing = outcome(lambda: SwallowsUpstreamErrors("reliance"))

    assert swallowing == "returned:0", (
        f"the control did not reproduce the defect, so the assertion above is not tested: "
        f"{swallowing!r}"
    )
    assert honest != swallowing, (
        "a swallowing wrapper is indistinguishable from Search, so the test above cannot see "
        "an error turned into []"
    )


# ------------------------------------------------------------------------------- live


@pytest.mark.live
def test_live_search_returns_quotes_for_reliance():
    """One real call. Excluded by ``-m "not live"``; run with ``-m live``."""
    search = Search("reliance")

    assert isinstance(search.quotes, list)
    assert search.quotes, "the live index returned no matches for 'reliance'"
    assert all("ticker" in quote for quote in search.quotes)
    assert search.all == {"quotes": search.quotes}
