"""
Lazy bindings: the one place that knows what each source's entry point actually looks like.

Two rules, both load-bearing
----------------------------
**Nothing is imported at module level.** Every function here does its ``import`` inside its own
body, on the first call that needs it. That is the whole mechanism behind the isolation tests:
``import bfinance.sources`` builds a table of strings and a set of dataclasses, and not one of
the six modules enters ``sys.modules``. A module-level ``from bfinance.costs import quote`` would
have cost the layer its entire point — the moment anyone imports the registry, a caller who
wanted a report and nothing else pays for six modules.

**No filename in this package may name a vendor package.** ``tests/test_trendlyne_core.py::
TestIsolation`` matches the *substring* ``trendlyne`` in any module name an ``ImportFrom`` node
references, and it has already caught a module that was merely *named*
``trendlyne_provider`` and imported nothing at all. So the files in this package are named for
what they do — ``bindings``, ``registry``, ``cascade``, ``series``, ``model``, ``errors``,
``health`` — and this one, which is the only file that would want to say the vendor's name, does
not need to: it does not import that package. The second vendor's adjusted price history is
declared-but-unwired in the registry, with the reason recorded, rather than imported past the
test. See :data:`~bfinance.sources.registry.UNWIRED_NOTE`.

What an adapter is for
----------------------
The registry's rows are data, so they name an ``entry`` by string and cannot know its signature.
Most entries take a request mapping and need no help. Three kinds do not, and each gets a named
adapter here:

* an entry that is a **class** — it must be constructed before it can answer, and the request
  decides the constructor arguments;
* an entry whose parameters are **named and non-uniform** across capabilities served by one row;
* an entry that returns a **frame the cascade must not hand back raw**, so that the basis
  declaration is attached to the value rather than left implicit in the row.

An adapter has one signature, ``(entry, request) -> value``, and must not swallow exceptions: a
raise is a fall-through and the cascade records it, so an adapter that returns ``None`` on
failure would turn a recorded failure into a silent "this source has no data", which is the
specific confusion this package exists to prevent.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Dict, Mapping


def _run_async(coro):
    """Bridge to sync, matching ``nse.bhavcopy._run_async`` and ``Ticker._run_async``.

    When an event loop is already running in this thread the coroutine is handed to a
    one-worker thread, because ``asyncio.run`` cannot nest. The thread is joined before
    returning, so the value is fully materialised at the call site rather than escaping
    into the caller's frame.
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


def _resolved(value: Any) -> Any:
    """
    Drive a coroutine to completion, or pass a plain value straight through.

    Why this exists, measured rather than reasoned: a registry row names its entry by
    string, and an entry that is a CLIENT METHOD may be a coroutine function while an entry
    that is a module-level function is not. ``screener_chart_history`` resolves to
    ``ScreenerClient.get_chart_timeseries``, which is ``async def``, so the adapter's
    ``return reader(**given)`` handed the cascade a coroutine OBJECT where a price series
    belonged. Confirmed directly:

        >>> type(bindings.screener_chart_history(ScreenerClient, {...}))
        <class 'coroutine'>

    and the cascade's next step raised ``TypeError: object of type 'coroutine' has no
    len()``. The row looked wired, passed every surface test, and could never answer.

    The failure is silent in the worst way: a coroutine is a legal Python object, so
    nothing complains at the boundary, and the real error surfaces far from its cause, or
    never surfaces at all if the caller only inspects ``attrs``. Resolving here means the
    class of defect cannot recur in any adapter, present or future, rather than fixing the
    one instance found today.

    Deliberately not a rename. The obvious alternative is to rename every client method to
    ``*_async`` and add a synchronous twin under the plain name, which is the convention the
    other clients in this package already follow. That is a public API break, and it would
    silently un-await every duck-typed stub: ``ohlcv.py`` resolves the profile loader with
    ``getattr(self.screener, "get_company_profile", None)`` and awaits whatever it gets, so
    renaming the real method would leave test doubles returning ``None`` where a coroutine
    is expected. Resolving the value keeps both worlds working and removes the hazard
    without touching a single call site.
    """
    if asyncio.iscoroutine(value):
        return _run_async(value)
    return value

__all__ = [
    "call_entry",
    "PATH",
    "statements_cascade",
    "nse_bhavcopy_range",
    "marketlens_history",
    "screener_chart_history",
    "macro_index_series",
    "participant_flows",
    "amfi_aum",
    "universe_constituents",
    "universe_archive",
    "costs_quote",
    "ownership_read",
    "ADAPTERS",
    "load_adapter",
]

#: Dotted-path -> adapter, for the registry to name. Kept as a dict of strings to functions so a
#: test can assert that every adapter this package references is present *without* calling
#: :func:`load_adapter`, and therefore without importing a single source module.
ADAPTERS: Dict[str, Callable[[Any, Mapping[str, Any]], Any]] = {}


def _register(path: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register a callable under a dotted path and return it unchanged.

    A decorator rather than a list, so an adapter cannot be referenced by a name that is not
    registered — the failure mode where a typo in the registry's ``adapter=`` string is only
    discovered when a call is made against a live source.
    """

    def decorate(func: Callable[..., Any]) -> Callable[..., Any]:
        ADAPTERS[path] = func
        return func

    return decorate


def load_adapter(path: str) -> Callable[[Any, Mapping[str, Any]], Any]:
    """Resolve an adapter by dotted path, at call time.

    The registry stores the path as a string precisely so this is the only place a name becomes a
    callable, and only when a resolution actually needs it.
    """
    adapter = ADAPTERS.get(path)
    if adapter is None:
        raise LookupError(
            f"the registry names adapter {path!r}, which this package does not define. Known: "
            f"{sorted(ADAPTERS)}. An unknown adapter is a registry bug — a row can never be "
            f"called, and the cascade would report it as a source failure rather than as the "
            f"typo it is."
        )
    return adapter


#: Short name -> dotted path, so the registry table can name an adapter without repeating this
#: module's own path eleven times. Values are strings; importing this module resolves no adapter
#: and no source.
PATH: Dict[str, str] = {
    "statements": f"{__name__}.statements_cascade",
    "bhavcopy": f"{__name__}.nse_bhavcopy_range",
    "marketlens": f"{__name__}.marketlens_history",
    "screener_chart": f"{__name__}.screener_chart_history",
    "macro": f"{__name__}.macro_index_series",
    "participant_flows": f"{__name__}.participant_flows",
    "amfi": f"{__name__}.amfi_aum",
    "constituents": f"{__name__}.universe_constituents",
    "archive": f"{__name__}.universe_archive",
    "costs": f"{__name__}.costs_quote",
    "ownership": f"{__name__}.ownership_read",
}


def call_entry(entry: Any, request: Mapping[str, Any]) -> Any:
    """The default adapter: call ``entry(request)``.

    Tries the mapping positionally, then the zero-argument form. The second attempt is not
    leniency for its own sake — a source with no request parameters is a legitimate thing to
    register, and probing for it here means the registry can hold both shapes without a column
    that says which is which.
    """
    try:
        return entry(dict(request))
    except TypeError as exc:
        # Only a signature complaint is retried. A ``TypeError`` raised *inside* a working call
        # would be re-issued against a different signature, turning one real error into two
        # confusing ones; the guard reads the message rather than the type for that reason.
        if "argument" not in str(exc) and "positional" not in str(exc):
            raise
        return entry()


def _require(request: Mapping[str, Any], *keys: str) -> Dict[str, Any]:
    """The subset of ``request`` named by ``keys``, refusing a request that is missing one.

    Every adapter with a required argument goes through here, and the reason is not tidiness. A
    required argument that is silently dropped becomes a ``TypeError`` from the callee — or worse,
    a call with a *default* that means something different from what the caller asked for. A
    cost schedule is the sharpest case: a missing trade date would pick a regime rate, and the
    result would be a number with the right unit and the wrong date attached.

    Raising before touching the entry point also means a malformed request can never reach the
    network, which is what keeps this layer's adapters safe to call from a test.
    """
    missing = [key for key in keys if key not in request]
    if missing:
        raise ValueError(
            f"this request is missing required key(s) {missing}; it carried "
            f"{sorted(request)}. Refusing to guess, because a default substituted here would be "
            f"a value the caller did not ask for and cannot see."
        )
    return {key: request[key] for key in keys}


# --------------------------------------------------------------------------- #
# adapters, one per source that needs one
# --------------------------------------------------------------------------- #


@_register("bfinance.sources.bindings.statements_cascade")
def statements_cascade(entry: Any, request: Mapping[str, Any]) -> Any:
    """Drive :mod:`bfinance.statements` from a request mapping.

    The sub-cascade takes a :class:`~bfinance.statements.model.StatementRequest` and a ``fetch``
    callable, which is the right shape for it and the wrong one for a uniform provider protocol.
    This translates, and it delegates the *basis* decision rather than making it: the request
    already carries ``basis``, and the sub-cascade has its own per-basis routing with a measured
    reason for every skip. Re-deciding here would duplicate a table that is already correct.

    The sub-cascade raises
    :class:`~bfinance.statements.errors.StatementCapabilityError` for its four unbuildable cells,
    and that raise is allowed to escape: the cascade catches it, records it against this source,
    and falls through — which is exactly the right outcome, because there is no second source of
    statements and the caller should see the sub-cascade's own measured refusal rather than this
    layer's paraphrase of it.
    """
    from bfinance.statements import Basis, Frequency, StatementKind, StatementRequest, resolve

    given = _require(request, "symbol")
    symbol = given["symbol"]
    if not isinstance(symbol, str):
        raise ValueError(
            f"statements.symbol must be one symbol, got {type(symbol).__name__}. A statement is "
            f"about a company, and this layer will not fan a statement request out across a list "
            f"silently — the sub-cascade reports per-symbol refusals that would otherwise be lost."
        )
    statement_request = StatementRequest(
        symbol=symbol,
        kind=StatementKind(str(request.get("kind", "income")).lower()),
        frequency=Frequency(str(request.get("frequency", "annual")).lower()),
        basis=Basis(str(request.get("basis", "consolidated")).lower()),
    )
    return _resolved(
        resolve(
            statement_request,
            fetch=_payload_fetch(dict(request.get("payloads") or {})),
        )
    )


def _payload_fetch(payloads: Mapping[str, Any]) -> Callable[[Any], Any]:
    """A ``fetch`` for the statements sub-cascade, over payloads the caller already has.

    A provider with no payload is a failure, not a ``None``: the sub-cascade records the reason,
    so "I only have one vendor's page" and "that vendor is down" produce the same fall-through and
    the same record instead of one of them looking like a successful empty answer.
    """

    def fetch(spec: Any) -> Any:
        payload = payloads.get(spec.payload)
        if payload is None:
            raise KeyError(
                f"no payload was supplied for {spec.name!r} (payload kind {spec.payload!r})"
            )
        return payload

    return fetch


@_register("bfinance.sources.bindings.nse_bhavcopy_range")
def nse_bhavcopy_range(entry: Any, request: Mapping[str, Any]) -> Any:
    """Read the bhavcopy over a date range.

    The signature is ``(start, end, *, series, exchange, on_missing, progress)`` and both dates
    are positional and required. There is no symbol argument at all: the bhavcopy is a whole-market
    file and filtering to one symbol is the caller's job. That is recorded in the registry row's
    note too, because a row that implied per-symbol granularity would send a caller looking for a
    filter this layer does not have.
    """
    given = _require(request, "start", "end")
    return _resolved(entry(**given))


@_register("bfinance.sources.bindings.marketlens_history")
def marketlens_history(entry: Any, request: Mapping[str, Any]) -> Any:
    """Read a symbol's price history from MarketLens.

    The signature is ``price_history(symbol, period='1Y')``. A symbol is required; the period
    defaults on the vendor side, and forwarding ``None`` for it would not be the same request as
    omitting it.
    """
    given = _require(request, "symbol")
    return _resolved(entry(**given))


@_register("bfinance.sources.bindings.screener_chart_history")
def screener_chart_history(entry: Any, request: Mapping[str, Any]) -> Any:
    """Read a symbol's chart history from screener.in.

    The entry is ``ScreenerClient`` and the reader is ``get_chart_timeseries(ticker, metric,
    days, force_refresh)``. Note what the registry row points at: the *client*, not
    ``bfinance.screener.charts.ScreenerChartEngine``, which is a query-parameter builder and a
    JSON parser with no way to fetch anything on its own. Naming the parser as a source would
    have been a row that looked wired and could never answer.

    The client is built per call rather than memoised. A memoised client would be module-level
    mutable state shared by every caller and every capability, and it holds a cache; construction
    is cheap, and a shared cache across capabilities is not a design anyone can reason about.
    """
    client = entry() if isinstance(entry, type) else entry
    reader = getattr(client, "get_chart_timeseries", None)
    if reader is None:
        raise TypeError(
            f"a screener client built from {type(client).__name__} has no get_chart_timeseries "
            f"reader, so this layer cannot read a series from it. Point the registry entry at the "
            f"method that does exist rather than guessing at one."
        )
    given = _require(request, "ticker")
    return _resolved(reader(**given))


@_register("bfinance.sources.bindings.macro_index_series")
def macro_index_series(entry: Any, request: Mapping[str, Any]) -> Any:
    """Dispatch on the capability, because one row serves two.

    ``india_vix`` and ``index_valuation`` are different series behind one source, and the registry
    row says so in its note. Splitting them into two rows would imply two sources, and the report
    that says "1 source, no redundancy" is the thing this capability most needs to be able to say.

    Both entry points take a single required ``day``, and the index is selected from the returned
    frame rather than requested — so ``index_valuation`` is served by the same call and narrowed by
    :func:`bfinance.nse.macro.select_index`.
    """
    given = _require(request, "day")
    frame = entry(given["day"])
    capability = str(request.get("capability") or "")
    if capability == "index_valuation" and request.get("index"):
        from bfinance.nse.macro import select_index

        return _resolved(select_index(frame, str(request["index"])))
    return frame


@_register("bfinance.sources.bindings.participant_flows")
def participant_flows(entry: Any, request: Mapping[str, Any]) -> Any:
    """Read participant flows over a date range.

    The signature is ``participant_flows_range(start, end, *, measure='oi', on_missing='skip')``.
    Both dates are required: a range with an open end is a different question, and defaulting one
    would silently change which days are covered.
    """
    return _resolved(entry(**_require(request, "start", "end")))


@_register("bfinance.sources.bindings.amfi_aum")
def amfi_aum(entry: Any, request: Mapping[str, Any]) -> Any:
    """Read the AMFI monthly report.

    The signature is ``amfi_monthly_report(workbook_url, *, month=None)`` — the workbook URL is
    required and positional. The coverage question is answered by
    :func:`bfinance.flows.amfi_workbook_coverage`, which is a separate call and not something this
    adapter guesses at.
    """
    return _resolved(entry(**_require(request, "workbook_url")))


@_register("bfinance.sources.bindings.universe_constituents")
def universe_constituents(entry: Any, request: Mapping[str, Any]) -> Any:
    """Parse one index's constituents.

    The signature is ``parse_constituents(text, *, index='', url='', retrieved_utc=None)`` — it
    takes the page **body**, not a symbol, and it parses rather than fetches. So this row serves
    "here is a page, read it", and the fetching is the caller's. The registry note says so; a row
    that implied this source fetches would send someone looking for a client it does not have.
    """
    return _resolved(entry(**_require(request, "text")))


@_register("bfinance.sources.bindings.universe_archive")
def universe_archive(entry: Any, request: Mapping[str, Any]) -> Any:
    """Query the local snapshot archive for membership on a date.

    ``membership_asof(index, on)`` is the read. The archive is built per call for the same reason
    as the chart client: a recorder holds state, and shared state across capabilities is not
    something a reader can verify.
    """
    archive = request.get("archive") or (entry() if isinstance(entry, type) else entry)
    reader = getattr(archive, "membership_asof", None)
    if reader is None:
        raise TypeError(
            f"a universe archive built from {type(archive).__name__} has no membership_asof "
            f"reader; point the registry entry at the method that exists rather than guessing"
        )
    return _resolved(reader(**_require(request, "index", "on")))


@_register("bfinance.sources.bindings.costs_quote")
def costs_quote(entry: Any, request: Mapping[str, Any]) -> Any:
    """Quote the statutory cost schedule for a trade.

    The signature is ``quote(buy_shape, sell_shape, *, trade_date, buy_amount, sell_amount=None,
    premium=None, ...)``. All four are required, and ``trade_date`` in particular is never
    defaulted: a date-keyed schedule asked for a rate with no date would return today's regime,
    which is a wrong number wearing a correct-looking unit — a 2019 trade charged at 2026 rates.
    """
    return _resolved(
        entry(**_require(request, "buy_shape", "sell_shape", "trade_date", "buy_amount"))
    )


@_register("bfinance.sources.bindings.ownership_read")
def ownership_read(entry: Any, request: Mapping[str, Any]) -> Any:
    """Dispatch an ownership read on the kind of data asked for.

    One row, four capabilities, one failure mode — see the row's note in the registry for why
    splitting them would hide a partially-broken source behind a clean "unavailable".

    The two readers are ``shareholding_patterns(symbol=None, index='equities',
    force_refresh=False)`` and ``deals(kinds=None, expected_date=None, force_refresh=False)``.
    The deal kinds this layer names map onto ``kinds``, so a caller asking for ``bulk_deals`` does
    not have to know the vendor's own spelling of a category.
    """
    client = entry() if isinstance(entry, type) else entry
    capability = str(request.get("capability") or "shareholding_pattern")
    if capability in ("bulk_deals", "block_deals", "short_deals"):
        reader = getattr(client, "deals", None)
        if reader is None:
            raise TypeError(
                f"an ownership client built from {type(client).__name__} has no deals reader; "
                f"point the registry entry at the method that exists rather than guessing"
            )
        given = _require(request, "expected_date")
        return _resolved(reader(kinds=[capability.removesuffix("_deals")], **given))
    reader = getattr(client, "shareholding_patterns", None)
    if reader is None:
        raise TypeError(
            f"an ownership client built from {type(client).__name__} has no shareholding_patterns "
            f"reader; point the registry entry at the method that exists rather than guessing"
        )
    return _resolved(reader(**_require(request, "symbol")))
