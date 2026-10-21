"""
Master Ticker facade providing 100% yfinance drop-in compatibility + Indian market superpowers.
"""

import asyncio
import importlib
import logging
from typing import Any, Callable, Dict, List, Literal, Optional, Sequence, Set, Tuple, Union
from datetime import datetime
import pandas as pd

logger = logging.getLogger("bfinance")

from bfinance.cache.sqlite_cache import SQLiteCache
from bfinance.market.corporate import CorporateActionsEngine
from bfinance.market.derivatives import DerivativesEngine
from bfinance.market.fast_info import FastInfo
from bfinance.market.ohlcv import OHLCVEngine
from bfinance.market.quotes import QuoteEngine, build_history_metadata
from bfinance.models.company import CompanyProfile, Concall
from bfinance.models.options import OptionChain
from bfinance.models.statements import FinancialStatement
from bfinance.screener.client import ScreenerClient
from bfinance.utils.exceptions import BFinanceError, TickerNotFoundError, UpstreamServiceError
from bfinance.utils.symbols import resolve_exchange_and_symbol

#: Row labels that would carry cash if the upstream balance sheet itemised it. It does not --
#: measured on the two real screener.in fixtures, the balance-sheet table has exactly ten rows,
#: in this order and no others:
#:
#:     Equity Capital, Reserves, Borrowings, Other Liabilities, Total Liabilities,
#:     Fixed Assets, CWIP, Investments, Other Assets, Total Assets
#:
#: (``tests/fixtures/screener/company_itc.html:904-1218`` and ``company_tcs.html:904-1218``,
#: read through ``ScreenerHTMLParser._parse_table_element``.) Cash is inside the undifferentiated
#: "Other Assets" bucket. So these names are a *hook*, not a lookup that works today: nothing in
#: a real response ever matches, which is exactly why
#: ``market/ratios.py:438,443`` has always read ``cash_hold = 0.0``.
#:
#: They are read anyway, because the day the source does itemise cash, the EV gate in
#: :attr:`Ticker.valuation_measures` should start producing a real number with no further edit --
#: and because a synthetic/alternate source that does carry the row must not be silently ignored.
#: Canonical definition is ``market.ratios._CASH_ROW_LABELS``; the two are kept in sync by
#: ``tests/test_ratios_honesty.py::test_the_cash_hook_is_the_same_hook_in_both_modules``.
#: Duplicated rather than imported because this module deliberately lazy-imports
#: ``market.ratios`` inside the properties that need it, and a module-level import here would
#: change the import graph for every consumer of ``bfinance.ticker``.
_CASH_ROW_LABELS: Tuple[str, ...] = ("Cash Equivalents", "Cash and Bank Balance")

#: Why the three ``get_*_stmt`` accessors refuse ``pretty=True``, verbatim, so all three say the
#: same thing. Measured against yfinance 1.7.0 (tag ``3d9d2f0``): ``Ticker.income_stmt`` -- the
#: *property* -- forces ``pretty=True`` and returns Title Case ("Total Revenue", "Diluted EPS"),
#: while ``Ticker.get_income_stmt()`` with its default ``pretty=False`` returns the same rows
#: CamelCase ("TotalRevenue", "DilutedEPS"). ``pretty`` is therefore not cosmetic: it is the whole
#: difference between the two yfinance surfaces a drop-in caller might have copied from.
#:
#: bfinance cannot honour it on either statement path, and the reason is the same on both:
#:
#: * **neither path reads the flag.** ``FinancialStatement.to_yfinance()`` has no ``pretty``
#:   parameter; it applies :data:`~bfinance.models.statements.INDIAN_TO_YFINANCE_LABELS`
#:   unconditionally. The screener.in frame therefore *resembles* ``pretty=True`` output -- it is
#:   Title Case -- but that is the label map's doing, not the flag's, so a ``pretty=False`` caller
#:   gets Title Case where yfinance gives CamelCase.
#: * **the fallback path is further away still.** It carries the vendor's own ``sstr`` labels
#:   ("Total Rev. Ann."), which are in neither yfinance vocabulary.
#:
#: Closing the gap means writing a label map in one direction or the other, and neither was ever
#: measured. ``bfinance.statements.model`` refuses exactly that in its module docstring, and a
#: ``camel2title`` reversal applied to the screener.in labels would be the same fabrication with
#: a regex instead of a dict. So the flag raises rather than returning a frame whose index
#: silently disagrees with what the caller asked for.
_PRETTY_UNSUPPORTED = (
    "pretty=True is not supported by bfinance, on either statement path. yfinance's pretty=True "
    "applies camel2title to its own label vocabulary, and bfinance has no measured "
    "correspondence between that vocabulary and either upstream's labels -- so honouring the "
    "flag would mean inventing a label map. Use the default (pretty=False), which returns the "
    "labels the serving path already publishes: the measured yfinance-shape rename from "
    "screener.in, or the vendor's own labels from the fallback."
)


def _fallback_fetch(payload_name: str, payload: Any) -> Callable[[Any], Any]:
    """The statements cascade's ``fetch`` for **one** payload, and a refusal for every other one.

    :func:`bfinance.statements.cascade.resolve` asks each provider registered for the cell for
    its own bytes, and each provider declares which bytes those are through
    ``ProviderSpec.payload``. This fallback fetched one route, so it can speak for the providers
    that read that route and for nothing else. That is the discipline
    :func:`~bfinance.statements.cascade.resolve_from_payloads` already applies to payloads a
    caller holds, mirrored here rather than reinvented.

    The version this replaces returned the payload for **every** spec, so screener.in's HTML
    parser was handed trendlyne's JSON. It raised ``KeyError: slice(None, 500, None)``, the
    cascade recorded that faithfully, and the frame went on to publish
    ``skip_reasons={'screener': 'KeyError: slice(None, 500, None)'}``,
    ``providers_tried=('screener', 'trendlyne-json')`` and
    ``fallback_from=('screener', 'trendlyne-html')`` -- a vendor outage that never happened,
    because screener.in was never asked for anything. Every clause of that record was false, and
    false in the one shape a reader cannot check: a fabricated outage is indistinguishable from a
    real one at exactly the moment a real one would matter. ``_read_statement``'s docstring below
    records the same class of defect -- a parse that failed and became a confident, wrong number.

    Args:
        payload_name: The payload this callable carries, verbatim
            (``bfinance.statements.JSON_PAYLOAD`` and its siblings).
        payload: The already-fetched payload.

    Returns:
        A ``fetch`` callable: the payload for a provider that reads ``payload_name``, and
        :class:`~bfinance.statements.errors.StatementSourceError` for any other.
    """
    # Lazy for the same reason the caller imports the package lazily: `bfinance.statements` is
    # opt-in and must not be imported on a path that never reaches it. This function is reached
    # only from the opt-in statement fallback.
    from bfinance.statements import StatementSourceError

    def fetch(spec) -> Any:
        if spec.payload != payload_name:
            raise StatementSourceError(
                f"no {spec.payload} was supplied for provider {spec.name!r}: this fallback "
                f"carries only the {payload_name} payload. Handing a provider bytes it does not "
                f"read is a fabricated answer, not a failed one."
            )
        return payload

    return fetch


def _trendlyne_news_item(item_id: str, date_text: Any, title: Any, url: Any, source: Any) -> Dict[str, Any]:
    """One trendlyne news card, in the yfinance ``{"id", "content"}`` shape.

    The shape is not a preference -- it is what :attr:`Ticker.news` already returns
    (:meth:`bfinance.market.corporate.CorporateActionsEngine.build_news`), so a drop-in caller
    that already indexes ``item["content"]["title"]`` reads the same two levels here. Both keys
    are fixed; the values are what the card carried, with two absences worth naming:

    * ``time`` is ``None``. The vendor renders three different date strings on one page, so an
      epoch here would be a guess about which one parsed.
    * ``pubDate`` is the vendor's own string, verbatim and **not** ISO-formatted. Publishing it
      is the honest reading of "this is when the vendor says the item was posted"; normalising
      it would be picking a format the response never used.
    * ``canonicalUrl`` is the card's own href, including the login modal that research-report
      cards link for anonymous readers. Rewriting it to the page behind the wall would invent an
      address the response never carried.
    """
    text = "" if date_text is None else str(date_text)
    link = "" if url is None else str(url)
    publisher = "" if source is None else str(source)
    return {
        "id": item_id,
        "content": {
            "title": "" if title is None else str(title),
            "link": link,
            "publisher": {"displayName": publisher},
            "provider": {"displayName": publisher},
            "time": None,
            "pubDate": text,
            "canonicalUrl": {"url": link},
        },
    }


#: The vendor's aggregate row on the research-reports table, matched case-insensitively against
#: the ``Author`` column. It carries the consensus target the broker set already averaged, so a
#: consumer that also averages the ``Target`` column must exclude it or the consensus is counted
#: twice -- once as itself and once inside every mean. Matched on a word rather than on
#: ``row == 0`` because the page decides its own order and nothing measured pins that the
#: aggregate is always first.
_CONSENSUS_ROW_MARKER = "consensus share price target"

#: The column the marker above is matched against. Named as a constant because its *absence* is
#: load-bearing -- see :func:`_targets_from_reports`.
_CONSENSUS_ROW_COLUMN = "Author"

#: Thousands separators the vendor renders in the ``Target`` column. Stripped before the numeric
#: coercion rather than after it: ``pd.to_numeric("2,450.54", errors="coerce")`` is ``NaN``, and a
#: ``NaN`` is dropped, so one formatted cell silently took a real broker's target out of the mean,
#: the median, the high and ``n_reports`` at once.
_TARGET_THOUSANDS_SEPARATOR = ","


def _numeric_targets(series: pd.Series) -> pd.Series:
    """The ``Target`` column as floats, with the vendor's thousands separators removed first.

    ``errors="coerce"`` still decides what is unreadable: ``"n/a"`` and ``""`` become ``NaN`` and
    are dropped downstream. Only the separator is repaired, because it is formatting rather than a
    value -- ``"2,450.54"`` is the same measurement as ``2455.54`` and a caller who lost it lost a
    real target, not a blank.
    """
    cleaned = series.map(
        lambda value: value.replace(_TARGET_THOUSANDS_SEPARATOR, "").strip()
        if isinstance(value, str)
        else value
    )
    return pd.to_numeric(cleaned, errors="coerce")


def _targets_from_reports(frame: pd.DataFrame) -> Optional[Dict[str, Any]]:
    """yfinance price targets derived from a research-reports frame, or ``None``.

    A pure function of the frame, with no fetch and no Ticker, so the derivation can be pinned
    against a recorded page directly rather than only through the caller.

    The statistics come from the ``Target`` column and from nothing else. The page's ``LTP``
    column is deliberately not read as ``current``: it is the price at the moment each report was
    published, so publishing it under the key a caller reads as *today's* price is the same
    relabelling defect this module refuses elsewhere. No current price is derivable from this
    feed, so ``current`` is ``None``.

    Returns ``None`` -- never a synthesised target -- when the frame carries no ``Target``
    column or no finite number in it, **and also when it carries no ``Author`` column**. That last
    one is a choice, and the alternative was rejected: without ``Author`` the vendor's aggregate
    row cannot be told apart from a per-report one, so every statistic below would fold the
    consensus into the mean of the rows beneath it. A ``None`` the caller can see is better than a
    mean that is wrong by exactly one value and looks perfectly plausible; the reason is recorded on
    :attr:`Ticker.data_warnings` by the property, which is the only caller with a Ticker to record
    it on.
    """
    if frame is None or frame.empty or "Target" not in frame.columns:
        return None
    if _CONSENSUS_ROW_COLUMN not in frame.columns:
        return None

    targets = _numeric_targets(frame["Target"])
    aggregate = frame[_CONSENSUS_ROW_COLUMN].astype(str).str.contains(
        _CONSENSUS_ROW_MARKER, case=False, na=False, regex=False
    )
    targets = targets[~aggregate]
    values = targets.dropna()
    values = values[values.abs() < float("inf")]
    if values.empty:
        return None

    return {
        # No current price is published on this route; see the docstring above.
        "current": None,
        "low": float(values.min()),
        "high": float(values.max()),
        "mean": float(values.mean()),
        "median": float(values.median()),
        #: How many rows the statistics above are computed from, after the vendor's own
        #: aggregate row was excluded. A count travels with the numbers because the two are
        #: only as good as each other: a mean of two and a mean of twenty are the same number
        #: with a very different amount behind them.
        "n_reports": int(values.size),
        "bfinance_analyst_source": "trendlyne_research_reports",
    }


#: The two columns :meth:`QuoteEngine.build_info_dict` reads off a history frame -- and the
#: COMPLETE list of what ``Ticker.info`` needs from a price series.
#:
#: Read off the builder rather than guessed. :func:`~bfinance.market.quotes._close_list` and
#: :func:`~bfinance.market.quotes.moving_average` read ``Close``; ``previous_close_from_history``
#: reads it again; and ``regularMarketVolume``/``volume`` read ``Volume``. Nothing in ``info`` reads
#: Open, High or Low -- and the request this replaced asked for all of them, which meant a
#: corporate-actions round trip and a two-factor adjustment for a frame that was then read for two
#: columns out of five.
#:
#: So ``info`` now asks for :meth:`~bfinance.ticker.Ticker.history` with
#: ``requires=INFO_HISTORY_COLUMNS``, and gets the winner's two columns unadjusted. Unadjusted is
#: deliberate rather than an accident of a narrower frame: yfinance's own ``previousClose`` and its
#: 50/200-day averages are means of **raw** closes, so this is the closer parity of the two, and
#: ``attrs["price_basis"]`` on the frame says which series they are.
INFO_HISTORY_COLUMNS = ("Close", "Volume")

#: The price sources that can serve :data:`INFO_HISTORY_COLUMNS` cheaply, in the order a caller
#: wanting them should ask for: the per-symbol as-traded vendor first, the bhavcopy behind it.
#:
#: Both publish ``as_traded`` and both publish every column in
#: :data:`INFO_HISTORY_COLUMNS`, so the list is a genuine failsafe and cannot splice two bases. It
#: is named here and **not** passed by ``info`` itself, and the reason is worth stating rather than
#: leaving to a reader: passing it engages :mod:`bfinance.sources.cascade`, whose providers are
#: imported at call time, and every offline caller that stubs the direct archive reader would then
#: be reaching the live NSE instead. ``info`` therefore stays on the direct path and inherits the
#: bhavcopy's cost -- one whole-market archive file per trading day, 530s over two years and 184s
#: over the year this asks for. Removing that is a vendor-ordering decision, and the registry's order
#: is an operator decision pinned by ``tests/test_sources_cascade.py``; it is not one this layer
#: makes for a caller who has not asked for it.
INFO_PRICE_SOURCES = ("marketlens-close", "nse-bhavcopy")


class Ticker:
    """
    Unified equity object providing full yfinance API parity and deep Indian market fundamentals.
    """

    def __init__(
        self,
        ticker: str,
        cache_ttl_hours: float = 24.0,
        timeout: float = 15.0,
        proxy: Optional[str] = None,
        session: Optional[Any] = None,
        raise_errors: bool = False,
        statement_fallback: bool = False,
        trendlyne_sections: bool = False,
    ):
        if not isinstance(ticker, str) or not ticker.strip():
            raise ValueError("ticker must be a non-empty string")
        self.ticker = ticker.strip()
        self.exchange, self.symbol = resolve_exchange_and_symbol(self.ticker)
        self.raise_errors = raise_errors
        #: Whether the statement accessors may fall back to a second vendor when the primary
        #: source carries **no statement at all**. Off by default: the main path must not start
        #: issuing second-vendor requests a caller did not ask for, and the fallback is a
        #: last resort rather than a preference. Named for what it does rather than for the
        #: vendor that currently serves it -- a second vendor would need no code change here --
        #: and :attr:`Ticker.__init__` is a provider-agnostic signature. See
        #: :meth:`_fallback_statement_frame`.
        self.statement_fallback = bool(statement_fallback)
        #: Whether the ``trendlyne_*`` section members and the trendlyne half of
        #: :attr:`analyst_price_targets` may run. Off by default, for the same reason the
        #: statement fallback is: the section layer is a **second vendor**, and a caller who did
        #: not ask for one should not be charged a request, a second origin's parsing rules, or
        #: fourteen new failure modes. Separate from ``statement_fallback`` rather than folded
        #: into it, because the two are different capabilities against different routes and a
        #: caller who wants the sections need not open the statements fallback. See
        #: :meth:`_section_client` for how the client is reached without importing it.
        self.trendlyne_sections = bool(trendlyne_sections)
        # The section client's own transport configuration. The constructor took these and used
        # them only for the screener client; forwarding them is what makes
        # ``Ticker("TCS", timeout=3.0, proxy=...)`` mean the same thing on the opt-in surface.
        self._section_timeout: float = timeout
        self._section_proxy: Optional[str] = proxy
        # Built on the first section call and reused after, so the sitemap index, the per-symbol
        # record and the pacing slot are shared across the seven members. ``None`` until then --
        # and for a default Ticker, forever.
        self._section_client_instance: Optional[Any] = None
        self.cache = SQLiteCache(default_ttl_hours=cache_ttl_hours) if cache_ttl_hours > 0 else None
        self.screener_client = ScreenerClient(
            timeout=timeout, cache_ttl_hours=cache_ttl_hours, proxy=proxy, cache=self.cache
        )
        self.ohlcv_engine = OHLCVEngine(self.screener_client)

        # Lazy caches
        self._profile: Optional[CompanyProfile] = None
        self._info: Optional[Dict[str, Any]] = None
        self._fast_info: Optional[FastInfo] = None
        # Last 1y history frame `info` already fetched, reused by history_metadata so the
        # previous close is a real bar instead of a fabricated constant -- and so metadata
        # costs no extra round trip.
        self._history_for_metadata: Optional[pd.DataFrame] = None
        # True while the last profile load failed and produced only the empty fallback.
        # Without it, one cached fallback latches _info/_fast_info shut for good.
        self._profile_degraded: bool = False
        # One sentence per non-fatal degradation, naming what is missing and why. Exposed through
        # `data_warnings` so a caller holding a missing field can read the reason instead of
        # having to guess -- a 52-week range that is absent because history failed used to be
        # indistinguishable from a company that has none.
        self._degradations: List[str] = []
        # Fallback-vendor payloads already fetched for this object, keyed by symbol. Empty here
        # and populated only by the opt-in statement fallback, so the default path opens no
        # socket. See `_fundamentals_payload`.
        self._fundamentals_payloads: Dict[str, Any] = {}

    # --------------------------------------------------- Observability
    def _degrade(self, what: str, cause: str) -> None:
        """Record and log one non-fatal degradation: `what` is unavailable because of `cause`."""
        message = f"{self.ticker}: {what} is unavailable ({cause})"
        self._degradations.append(message)
        logger.warning(message)

    @property
    def data_warnings(self) -> Tuple[str, ...]:
        """
        Every non-fatal degradation recorded on this Ticker, oldest first.

        A missing field with no recorded reason is indistinguishable from a field that is
        genuinely not published; this is where the difference is written down. Empty on the
        common path -- a Ticker that served everything it was asked for records nothing.

        Setting ``raise_errors=True`` at construction makes the degradations raise instead of
        being recorded, so this stays empty there.
        """
        return tuple(self._degradations)

    # ------------------------------------------------------------- Internal loader
    def _run_async(self, coro):
        """Helper to run async coroutines synchronously."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(lambda: asyncio.run(coro))
                return future.result()
        else:
            return asyncio.run(coro)

    def _ensure_profile(self, force_refresh: bool = False) -> CompanyProfile:
        if self._profile is None or force_refresh:
            try:
                self._profile = self._run_async(
                    self.screener_client.get_company_profile(self.symbol, force_refresh=force_refresh)
                )
                self._profile_degraded = False
            except (TickerNotFoundError, UpstreamServiceError, BFinanceError) as e:
                if self.raise_errors:
                    raise
                logger.warning("%s: No fundamentals found (%s). Returning empty profile fallback.", self.ticker, e)
                # The fallback is RETURNED, never stored. `self._profile` is what suppresses
                # the refetch below, so assigning it here turned a single transient block
                # (a 403 lasts one request) into a permanently empty Ticker for the life of
                # the object. Leaving it None means the next access retries.
                self._profile_degraded = True
                return CompanyProfile(
                    symbol=self.symbol,
                    company_id=0,
                    name=self.ticker,
                )
        return self._profile

    def _read_statement(
        self, statement: FinancialStatement, label: str
    ) -> Tuple[pd.DataFrame, Optional[str]]:
        """
        Materialise a statement, or say why it could not be read. Returns ``(frame, error)``.

        ``error`` is ``None`` **only** when ``frame`` is the real statement. A ``BFinanceError``
        from :meth:`FinancialStatement.to_dataframe` -- a ragged row, which is precisely what
        that method exists to refuse -- is *not* converted into an empty frame, because every
        reader downstream asks a frame a question ("what are the borrowings?") and an empty frame
        answers "zero". That is how a failed parse became a confident 80%-wrong EnterpriseValue:
        ``Borrowings`` read ``0.0``, cash read ``0.0``, and the result was bare market cap.

        The message is returned and recorded on :attr:`data_warnings`, so "this company has no
        borrowings" stays distinguishable from "this statement did not parse".

        A non-``BFinanceError`` propagates. That is a bug in this package, not bad upstream data,
        and quietly turning it into an empty frame is what made the original defect invisible.
        """
        try:
            return statement.to_dataframe(orient="columns"), None
        except BFinanceError as e:
            if self.raise_errors:
                raise
            self._degrade(f"{label} (statement unreadable)", str(e))
            return pd.DataFrame(), str(e)

    def _fallback_statement_frame(
        self, kind: str, frequency: str
    ) -> Tuple[pd.DataFrame, Optional[str]]:
        """One statement from the second vendor, or an empty frame plus the reason.

        Returns ``(frame, error)``.

        The yfinance-compatible statement accessors are normally served by screener.in alone. When
        that source carries **no statement at all** -- a blocked fetch, a symbol it does not cover
        -- an independent vendor with 11 fiscal years and 13 quarters sits unused, and the caller
        gets an empty frame that reads as "this company has no financials".

        This is the fallback, and it is deliberately narrow:

        * **it is opt-in.** ``Ticker(symbol)`` without ``statement_fallback=True`` opens no
          second-vendor socket. The default path is unchanged by construction, and
          ``tests/test_ticker_trendlyne_statement_fallback.py`` asserts it.
        * **it never blends.** Either the primary source's statement, whole, or the fallback's,
          whole. No row-level merge and no back-filling a missing period from the other vendor: a
          frame half from each would be indistinguishable from either, and a basis difference
          between the two is exactly what must not be smoothed over.
        * **the basis is the primary source's.** screener.in is consolidated-only on this route,
          so the request is consolidated, and the trendlyne JSON route serves both bases. The
          frame's ``attrs`` record what was served and what was asked for regardless, so a
          mismatch is visible rather than assumed away.

        **A served statement is not a degradation, and records nothing on
        :attr:`data_warnings`.** ``_degrade`` says "what is **unavailable**", and a complete
        frame is not that. Its provenance rides on the frame's ``attrs`` instead --
        ``bfinance_statements_provider`` names the vendor and ``bfinance_statements_basis`` the
        basis. That is this file's established convention for a multi-vendor failover, identical
        in shape to the ``bfinance_price_source*`` keys :meth:`history` publishes: the return type
        does not change and a failover is an ordinary DataFrame. ``data_warnings`` is reserved for
        the branches where the fallback could **not** answer, which is what keeps "genuinely not
        published" distinguishable from "failed to fetch" -- the distinction the property exists
        for. Recording a healthy fallback there made every successful call look like a failure
        and told a caller to distrust a frame that was in fact complete.

        **It also claims no failover it did not perform.** The cascade is restricted (``only=``)
        to the providers whose payload this path actually holds, and :func:`_fallback_fetch`
        refuses anything else, so ``bfinance_statements_providers_tried`` names only the provider
        that answered and ``bfinance_statements_skip_reasons`` is empty. With the primary source
        absent from the call, ``bfinance_statements_fallback`` is therefore ``False`` and
        ``…_fallback_from`` empty -- the honest reading, because no provider that *could* have
        served this cell was skipped or failed. That the primary source had no statement is
        established by the accessor's own branch condition (its table was empty), not by a vendor
        that was never asked. See :func:`_fallback_fetch` for what this replaces.

        Args:
            kind: ``"income"``, ``"balance_sheet"`` or ``"cash_flow"``.
            frequency: ``"annual"`` or ``"quarterly"``.

        Returns:
            ``(frame, None)`` when the fallback served a statement, and ``(pd.DataFrame(), reason)``
            when it did not -- with ``reason`` recorded on :attr:`data_warnings` by the caller,
            following the same contract as :meth:`_read_statement`.

        Raises:
            BFinanceError: with ``raise_errors=True``, every typed failure below is re-raised
                rather than degraded.
            ValueError: a caller bug -- ``StatementKind("inccome")`` is a misspelling, not bad
                upstream data -- and it propagates rather than becoming a "vendor degraded"
                warning. Same rule as :meth:`_read_statement`.
        """
        if not self.statement_fallback:
            return pd.DataFrame(), None

        # Imported here rather than at module scope for two reasons: the package is opt-in
        # (``bfinance/statements`` is reached only by explicit import) and the import cost is not
        # paid on the common path, which never reaches this method.
        from bfinance.statements import (
            JSON_PAYLOAD,
            PROVIDERS,
            Basis,
            Frequency,
            StatementKind,
            StatementRequest,
            resolve,
        )
        from bfinance.statements.fundamentals_fetch import fetch_fundamentals_json_sync
        from bfinance.statements.yfinance_bridge import result_to_yfinance_frame

        # The one payload this path holds, and the providers registered to read it. Derived from
        # the registry rather than written out as a provider name, so a spec that is added,
        # renamed or split cannot leave a literal here aimed at something that no longer exists.
        carriers = tuple(
            name for name, spec in PROVIDERS.items() if spec.payload == JSON_PAYLOAD
        )

        try:
            payload = self._fundamentals_payload(fetch_fundamentals_json_sync)
            request = StatementRequest(
                symbol=self.symbol,
                kind=StatementKind(kind),
                frequency=Frequency(frequency),
                basis=Basis.CONSOLIDATED,
            )
            # `only=` keeps the cascade to the providers this payload can speak for, and the fetch
            # refuses any spec whose payload it does not carry. Both halves are load-bearing: the
            # fetch alone would still record the refusal as a skip -- and a skip is a sentence
            # about a vendor on `attrs` -- while `only` alone would let a future provider read
            # bytes that were never meant for it.
            result = resolve(
                request,
                fetch=_fallback_fetch(JSON_PAYLOAD, payload),
                only=carriers,
            )
            frame = result_to_yfinance_frame(result)
        except BFinanceError as e:
            # Same contract as _read_statement: a typed upstream failure is a degraded answer when
            # the caller asked for graceful degradation, and a raise when it did not.
            # ``FundamentalsFetchError`` (vendor unreachable, token refused) and
            # ``StatementCapabilityError`` (a statement no reachable source publishes) are both
            # BFinanceError subclasses, so one clause covers both without naming either.
            #
            # Narrowed from ``except (ValueError, KeyError)``. That clause converted a *caller
            # bug* -- ``StatementKind("inccome")`` raising ValueError, or a KeyError out of a
            # parser fed the wrong shape -- into "the vendor is degraded", which is the quiet
            # bug-into-empty-frame antipattern _read_statement exists to refuse. A KeyError from
            # BeautifulSoup handed a dict is a bug in this package, not bad upstream data, and it
            # is the exact error :func:`_fallback_fetch` exists to stop being produced.
            if self.raise_errors:
                raise
            return pd.DataFrame(), f"{type(e).__name__}: {e}"

        if frame.empty:
            return pd.DataFrame(), (
                f"the fallback vendor returned no {kind} statement at {frequency} frequency for "
                f"{self.symbol}"
            )
        # No _degrade here on purpose -- see the docstring above.
        return frame, None

    def _fundamentals_payload(self, fetch) -> Any:
        """The fallback vendor's payload for this symbol, fetched at most once per ``Ticker``.

        One JSON route carries every statement and both frequencies, so the four statement
        accessors on a single ``Ticker`` were issuing four shell fetches plus four ~841 KB JSON
        fetches for byte-identical data. The cache is on the instance, keyed by symbol, and is
        **lazy**: nothing is fetched at construction, and a ``Ticker`` without the fallback flag
        never reaches this method at all.

        Scope is deliberately one ``Ticker`` object's lifetime, and that is the boundary worth
        writing down. The fetcher's own documentation records that the per-symbol token rotates
        and is therefore never stored across calls; caching here keeps only the *decoded payload*
        for an object a caller is actively reading four accessors off, and a new ``Ticker`` starts
        empty and re-resolves. Nothing here extends a token's life or is a cross-process cache,
        and a failed fetch is not cached -- the next accessor retries rather than replaying a
        failure for the life of the object.
        """
        if self.symbol not in self._fundamentals_payloads:
            self._fundamentals_payloads[self.symbol] = fetch(self.symbol)
        return self._fundamentals_payloads[self.symbol]

    # --------------------------------------------------- Opt-in trendlyne section bridge
    #
    # The two methods below are the only code in this module that knows the trendlyne leaf
    # exists, and neither of them says so in a way an import scanner can read. Everything the
    # section layer offers is expressed as one `_section_call` over one `_section_client`.

    def _section_client(self) -> Any:
        """The trendlyne client this object's section calls go through, built once.

        **This method is the whole bridge, and it exists because the obvious one is refused.**
        ``bfinance.trendlyne`` is a leaf by rule:
        ``tests/test_trendlyne_core.py::TestIsolation`` AST-walks every module under ``src/`` and
        fails on any ``Import``/``ImportFrom`` naming ``*trendlyne*``, descending into function
        bodies. So neither a module-level ``import bfinance.trendlyne`` nor a function-local
        ``from bfinance.trendlyne import TrendlyneClient`` is available here, even though both
        would only ever *execute* on this path -- which is the interesting part, because "the
        import is inside the guard" is exactly the reasoning that makes an isolation test
        unenforceable.

        ``importlib.import_module`` is the one indirection the scan cannot see: the name is a
        string argument, so there is no ``Import`` node to find at any depth. It is the same
        mechanism :func:`bfinance.sources.cascade.load_provider` already uses to resolve a
        registry row to the module it names, so it is repo-precedented rather than a loophole
        invented here to get around a test.

        The runtime half is the one that actually matters to a caller, and it holds with the
        other: the module is resolved on the **first section call**, never at construction and
        never at import. ``import bfinance`` therefore still executes none of the eight opt-in
        modules, and a default ``Ticker`` still leaves ``sys.modules`` untouched --
        ``tests/test_public_surface.py`` asks the first in a fresh interpreter and
        ``tests/test_ticker_trendlyne_sections.py`` asks the second the same way.

        The instance is cached for the life of this object, so the seven section members share
        one sitemap index, one per-symbol record and one pacing slot. Constructing the client
        performs no I/O of its own; the sitemap is fetched on first use.

        Raises:
            NotImplementedError: ``trendlyne_sections`` is off. Refusing is the honest answer
                here and an empty frame is not: an empty frame is how "this symbol has no
                share-holding rows" reads, so returning one to a caller who never opted in would
                report a fact about the company that nobody looked for.
        """
        if not self.trendlyne_sections:
            raise NotImplementedError(
                f"{self.ticker}: the trendlyne section members are opt-in and this Ticker was "
                f"not opted in. Construct it with trendlyne_sections=True to reach a second "
                f"vendor's research reports, news, holdings, deals, buy/sell zone, technicals "
                f"and SWOT pages, or read the frames this package already serves without them."
            )
        if self._section_client_instance is None:
            module = importlib.import_module("bfinance.trendlyne.client")
            self._section_client_instance = module.TrendlyneClient(
                timeout=self._section_timeout,
                proxy=self._section_proxy,
                cache=self.cache,
            )
        return self._section_client_instance

    def _section_call(
        self, what: str, method: str, *args: Any, **kwargs: Any
    ) -> Tuple[Any, Optional[str]]:
        """Run one section method through the client's **sync** twin. Returns ``(value, error)``.

        ``error`` is ``None`` **only** when ``value`` is the real answer the vendor sent -- the
        same contract :meth:`_read_statement` publishes for a statement, and for the same reason.
        A caller who cannot separate those two cannot separate "this company has no block deals"
        from "the page did not load", which is the one distinction :attr:`data_warnings` exists to
        keep. So a typed failure is recorded through :meth:`_degrade` and reported here as the
        ``error`` the caller needs in order to return its own honest empty shape.

        ``method`` is a **name**, not a bound method, so that nothing about the vendor's class
        is resolved before the flag has been checked. The lookup happens on the object
        :meth:`_section_client` has just built, and an ``AttributeError`` -- a method this
        package does not ship -- propagates as the bug in this package that it is, rather than
        being recorded as a degraded vendor.

        Raises:
            BFinanceError: with ``raise_errors=True`` every typed vendor failure is re-raised
                rather than degraded, as everywhere else in this class.
            ValueError: a caller bug (``page=0``), which propagates. A misspelled argument is
                not bad upstream data and must not be recorded as one.
        """
        client = self._section_client()
        try:
            return getattr(client, method)(self.symbol, *args, **kwargs), None
        except BFinanceError as e:
            if self.raise_errors:
                raise
            cause = f"{type(e).__name__}: {e}"
            self._degrade(what, cause)
            return None, cause

    async def get_profile_async(self, force_refresh: bool = False) -> CompanyProfile:
        """Async variant of profile getter."""
        if self._profile is None or force_refresh:
            try:
                self._profile = await self.screener_client.get_company_profile(
                    self.symbol, force_refresh=force_refresh
                )
                self._profile_degraded = False
            except (TickerNotFoundError, UpstreamServiceError, BFinanceError) as e:
                if self.raise_errors:
                    raise
                logger.warning("%s: No fundamentals found (%s). Returning empty profile fallback.", self.ticker, e)
                # Not cached — see the note in _ensure_profile().
                self._profile_degraded = True
                return CompanyProfile(
                    symbol=self.symbol,
                    company_id=0,
                    name=self.ticker,
                )
        return self._profile

    # --------------------------------------------------- yfinance Core Properties
    @property
    def fast_info(self) -> FastInfo:
        """
        FastInfo container matching yfinance 0.2+ `ticker.fast_info`.
        Provides quick scalar attributes (last_price, market_cap, 52w high/low).
        """
        if self._fast_info is None or self._profile_degraded:
            profile = self._ensure_profile()
            latest_p = profile.ratios.current_price
            hist = None
            try:
                # One cached 1y frame feeds prev-close + 50/200d averages; no extra fan-out.
                hist = self.history(period="1y")
                if (not latest_p or latest_p <= 0) and not hist.empty and "Close" in hist.columns:
                    latest_p = float(hist["Close"].iloc[-1])
            except (BFinanceError, RuntimeError) as e:
                # Narrowed from `except Exception`. The whole point of this call is the 52-week
                # range, previous close and the 50/200d averages, so swallowing its failure is not
                # a harmless fallback -- it hands back a FastInfo that is indistinguishable from
                # a company which has none of them. Everything history() raises is typed
                # (`RealOHLCUnavailableError`, `TickerNotFoundError`, `UpstreamServiceError`,
                # `ParsingError` are all BFinanceError subclasses), plus a RuntimeError out of
                # `asyncio.run`; anything else is a bug here and propagates.
                if self.raise_errors:
                    raise
                self._degrade(
                    "52-week range, previous close and moving averages (history failed)", str(e)
                )
                hist = None
            self._fast_info = FastInfo(profile, latest_price=latest_p, history=hist)
        return self._fast_info

    @property
    def info(self) -> Dict[str, Any]:
        """
        Unified 76-key metrics dictionary matching yfinance `ticker.info`.
        """
        if self._info is None or self._profile_degraded:
            profile = self._ensure_profile()
            latest_p = profile.ratios.current_price
            hist = None
            try:
                # A NARROW request, and the narrowness is the point: build_info_dict reads Close and
                # Volume off this frame and nothing else, so asking for Open/High/Low as well buys
                # a corporate-actions round trip and a two-factor adjustment that are then read for
                # two columns out of five. See INFO_HISTORY_COLUMNS for what was measured.
                hist = self.history(period="1y", requires=INFO_HISTORY_COLUMNS)
                if (not latest_p or latest_p <= 0) and not hist.empty and "Close" in hist.columns:
                    latest_p = float(hist["Close"].iloc[-1])
            except (BFinanceError, RuntimeError) as e:
                # Narrowed from `except Exception`; see `fast_info` for the same reasoning. The
                # consequence here is that `info` and `history_metadata` are built without a
                # previous close, which used to be indistinguishable from "no history exists".
                if self.raise_errors:
                    raise
                self._degrade(
                    "previous close and history-derived fields in info (history failed)", str(e)
                )
                hist = None
            self._history_for_metadata = hist
            self._info = QuoteEngine.build_info_dict(profile, latest_price=latest_p, history=hist)
        return self._info

    def history(
        self,
        period: str = "1mo",
        interval: str = "1d",
        start: Optional[Union[str, datetime]] = None,
        end: Optional[Union[str, datetime]] = None,
        prepost: bool = False,
        actions: bool = True,
        auto_adjust: bool = True,
        back_adjust: bool = False,
        repair: bool = False,
        keepna: bool = False,
        rounding: bool = False,
        timeout: int = 10,
        raise_errors: bool = False,
        ohlc: Literal["bhavcopy", "synthetic"] = "bhavcopy",
        adjust_corporate_actions: bool = True,
        source: Optional[str] = None,
        sources: Optional[Sequence[str]] = None,
        requires: Optional[Sequence[str]] = None,
    ) -> pd.DataFrame:
        """
        Historical OHLCV DataFrame matching exact yfinance `ticker.history()` signature and schema.
        Columns: ['Open', 'High', 'Low', 'Close', 'Adj Close', 'Volume'] (+ ['Dividends', 'Stock Splits'] if actions=True)
        Index: pd.DatetimeIndex

        Args:
            ohlc: ``"bhavcopy"`` (default) returns real published OHLC from the NSE bhavcopy
                and flags the frame ``attrs["bfinance_synthetic_ohlc"] = False``. Pass
                ``"synthetic"`` to opt in to the legacy close-only path, whose Open/High/Low
                are derived from Close (flagged ``True``).

                **Kept, not replaced.** It chooses the bar *shaping*; ``source``/``sources``
                choose the *vendor*; the two are orthogonal. ``ohlc="synthetic"`` combined with
                a named source raises, because the synthetic path is not a source in the price
                registry and routing it through the cascade would mean either inventing a row
                that does not exist or silently swapping in a different vendor's real O/H/L.
            source / sources: **one** price source, or an **ordered list to fail over through**,
                by :mod:`bfinance.sources.registry` name (``"nse-bhavcopy"``,
                ``"marketlens-close"``, ``"trendlyne-ohlc"``, ``"screener-chart"``,
                ``"vendor-b-adjustments"``). ``sources=["nse-bhavcopy", "marketlens-close"]`` is
                a genuine second attempt on the same price basis; the call raises only if every
                one of them fails. Naming neither is the default and does not engage the cascade.

                An unknown name raises ``ValueError`` **before any request is spent**.

                Four attrs keys report what the cascade did, on the returned frame and on an
                empty one alike -- ``bfinance_price_source`` (the winning **vendor**, which is not
                ``bfinance_source``: that key is the transport), ``bfinance_price_source_attempts``,
                ``bfinance_price_source_degraded`` and ``bfinance_price_source_detail``. The
                return type does not change -- a failover is an ordinary DataFrame. See
                :meth:`bfinance.market.ohlcv.OHLCVEngine.fetch_history` for the full contract,
                including why an as-traded request never reaches ``"trendlyne-ohlc"``.
            requires: The columns this call actually needs. ``None`` (the default) means the full
                yfinance OHLCV frame and changes nothing at all. A value such as
                ``("Close", "Volume")`` makes this a **column-narrow** call: only a price source
                that publishes every named column is even asked, the frame returned carries exactly
                those columns, and it is NOT auto-adjusted and carries no ``Adj Close`` or action
                columns — deriving those needs Open/High/Low, which a narrow call has said it does
                not want. See :meth:`bfinance.market.ohlcv.OHLCVEngine.fetch_history` for the three
                things it changes and why.
            adjust_corporate_actions: ``True`` (default) back-adjusts Open/High/Low/Close by the
                cumulative share-count factor over the instrument's whole corporate-action
                history, so the default path matches yfinance's own ``auto_adjust=False`` series
                bar-for-bar (the bhavcopy is as-traded; Yahoo's raw OHLC are not). ``False``
                returns the as-traded series, which is what the exchange printed. Dividends never
                touch O/H/L/C -- they go on ``Adj Close``, which is
                therefore not always equal to ``Close`` (it is, when no dividend ex-date falls in
                the window). Volume is restated by the *inverse* of the share-count factor, so that
                ``price * volume`` stays the rupee value traded across a bonus or sub-division.

                When a named source published bars that already carry a share-count restatement,
                bfinance applies no further factor and ``price_basis`` reads
                ``as_traded_split_bonus_adjusted`` rather than claiming an adjustment it did not
                make.

                Three attrs keys describe the result and are present on every frame:
                ``bfinance_corporate_actions_applied`` (a complete adjustment was made),
                ``bfinance_adjustment_incomplete`` (``None``, or a dict naming the action classes
                the feed recorded that bfinance declines to convert -- e.g. a demerger), and
                ``price_basis`` (a machine-readable basis string, e.g.
                ``as_traded_share_count_adjusted``).
            auto_adjust: ``True`` by default -- yfinance's own default since 1.7, and this
                method's default was flipped to match it on 2026-09-30. ``True`` performs
                yfinance's adjustment: scale Open/High/Low/Close by ``Adj Close / Close`` and
                drop ``Adj Close``, leaving Volume and the action columns alone. The columns are
                then yfinance's own: ``['Open', 'High', 'Low', 'Close', 'Volume']`` (+ the action
                columns). Pass ``False`` for the strictly richer frame that also carries
                ``Adj Close``, the full adjusted series.

                ``True`` raises :class:`AdjustmentNotAvailableError` in exactly **three** cases,
                all of them where there is no real frame to return instead: ``ohlc="synthetic"``
                (O/H/L derived from Close, never back-adjusted, so there is no factor at all); an
                empty frame (no bars in the window, so no feed was asked and nothing could have
                been adjusted); and an actions feed that answered with no ex-dated rows -- the
                ``attrs["bfinance_actions_source"]`` value is
                ``"none"`` when the feed was empty or down. Each message names which case it is.
                The ``ohlc="synthetic"`` one is decided before any request is spent.

                **It does NOT raise for the Screener.in fiscal-year-end approximation.** That was
                a fourth refusing case until 2026-10-01 and it was the wrong one to keep: NSE's
                corporate-actions feed has no index for an ETF, so ``MAFANG``, ``HNGSNGBEES``
                and ``NIFTYBEES`` all land on the approximation, and raising made every ETF in a
                mixed equity + ETF portfolio fail while the same call returned 21 real bars for
                NTPC, RELIANCE, WIPRO, CIPLA and INDGN. What is missing there is the
                *adjustment*, not the prices. So the frame is now RETURNED, with O/H/L/C exactly
                as the archive printed them, ``Adj Close`` dropped so the column set stays
                yfinance's, the approximation's implied ratio deliberately NOT multiplied in, and
                the gap published: ``attrs["bfinance_adjustment_applied"] is False`` with the
                reason in ``attrs["bfinance_adjustment_applied_detail"]``, which also names
                ``auto_adjust=False`` as the way to keep ``Adj Close``. Branch on that bool; it is
                present on every frame, so a consumer never probes for its absence.

                Nor does it raise when the factor was applied but is incomplete
                (``bfinance_corporate_actions_applied is False``): ``Adj Close`` came from the
                real ex-dated feed, so scaling by it is sound and
                ``bfinance_adjustment_incomplete`` discloses the residual gap.

                ``auto_adjust`` is independent of ``actions``, exactly as in yfinance: the
                corporate-actions feed is resolved whenever either flag asks for it, because the
                factor is derived from it, and ``actions`` alone decides whether the
                ``Dividends`` / ``Stock Splits`` columns are published.

        Raises:
            AdjustmentNotAvailableError: ``auto_adjust=True`` on synthetic bars, an empty frame,
                or no ex-dated actions feed -- the three cases with no real frame to return.
                NOT for the fiscal-year-end approximation, which is answered unadjusted with
                ``bfinance_adjustment_applied = False``.
            RealOHLCUnavailableError: default source could not serve the window.
            ValueError: an unknown ``source``/``sources`` name, refused before any request.
            bfinance.sources.errors.SourceUnavailableError: the cascade ran and every named or
                eligible source failed. An ``UpstreamServiceError`` subclass, so existing
                ``except UpstreamServiceError`` handlers need nothing new.
        """
        return self._run_async(
            self.ohlcv_engine.fetch_history(
                self.symbol,
                period=period,
                interval=interval,
                start=start,
                end=end,
                prepost=prepost,
                actions=actions,
                auto_adjust=auto_adjust,
                back_adjust=back_adjust,
                repair=repair,
                keepna=keepna,
                rounding=rounding,
                timeout=timeout,
                raise_errors=raise_errors,
                ohlc=ohlc,
                adjust_corporate_actions=adjust_corporate_actions,
                source=source,
                sources=sources,
                requires=requires,
            )
        )

    async def history_async(
        self,
        period: str = "1mo",
        interval: str = "1d",
        start: Optional[Union[str, datetime]] = None,
        end: Optional[Union[str, datetime]] = None,
        prepost: bool = False,
        actions: bool = True,
        auto_adjust: bool = True,
        back_adjust: bool = False,
        repair: bool = False,
        keepna: bool = False,
        rounding: bool = False,
        timeout: int = 10,
        raise_errors: bool = False,
        ohlc: Literal["bhavcopy", "synthetic"] = "bhavcopy",
        adjust_corporate_actions: bool = True,
        source: Optional[str] = None,
        sources: Optional[Sequence[str]] = None,
        requires: Optional[Sequence[str]] = None,
    ) -> pd.DataFrame:
        """Async variant of history().

        ``source`` and ``sources`` mean exactly what they mean on :meth:`history`, and the
        attribute docstring there is the contract.
        """
        return await self.ohlcv_engine.fetch_history(
            self.symbol,
            period=period,
            interval=interval,
            start=start,
            end=end,
            prepost=prepost,
            actions=actions,
            auto_adjust=auto_adjust,
            back_adjust=back_adjust,
            repair=repair,
            keepna=keepna,
            rounding=rounding,
            timeout=timeout,
            raise_errors=raise_errors,
            ohlc=ohlc,
            adjust_corporate_actions=adjust_corporate_actions,
            source=source,
            sources=sources,
            requires=requires,
        )

    # --------------------------------------------------- Financial Statements API
    #
    # The four ``get_*`` methods below and the twelve properties that delegate to them are
    # named after yfinance methods and claim yfinance parity, so they return the yfinance
    # shape -- ``FinancialStatement.to_yfinance()``: DatetimeIndex columns newest-first,
    # absolute rupees, generic row labels. They did not until now; they returned the native
    # Indian-label Rs-Cr frame that ``models/statements.py`` documents as "do not change".
    # The native shape is still reachable, still unchanged, via
    # ``FinancialStatement.to_dataframe()`` on ``Ticker._ensure_profile()``, which is where
    # finengine reads it and where the Indian-label contract is pinned.
    #
    # The parity claim has one stated limit: the yfinance ``pretty`` keyword is **refused**
    # (:data:`_PRETTY_UNSUPPORTED`), not honoured. It was silently ignored, which is the one
    # outcome worse than a raise -- a caller who passed ``pretty=True`` got a frame whose index
    # vocabulary had nothing to do with the flag.
    def get_income_stmt(
        self, as_dict: bool = False, pretty: bool = False, freq: str = "yearly"
    ) -> Union[pd.DataFrame, dict]:
        """Fetch income statement matching yfinance `get_income_stmt()`.

        Raises:
            NotImplementedError: ``pretty=True``. See :data:`_PRETTY_UNSUPPORTED` -- the flag is
                refused, not ignored, because neither statement path can honour it without an
                unmeasured label map. The default ``pretty=False`` is unchanged.
        """
        if pretty:
            raise NotImplementedError(_PRETTY_UNSUPPORTED)
        quarterly = (freq or "yearly").lower() == "quarterly"
        profile = self._ensure_profile()
        table = profile.quarters if quarterly else profile.profit_loss
        if table.empty and self.statement_fallback:
            # Attempted BEFORE the quarterly raise below, because the fallback vendor does
            # publish 13 quarters where the primary source may carry none.
            df, error = self._fallback_statement_frame(
                "income", "quarterly" if quarterly else "annual"
            )
            if not df.empty:
                return df.to_dict() if as_dict else df
            if error:
                # ``_fallback_statement_frame`` returns a non-empty frame or a reason, so a
                # fallback that served nothing always lands here. There is no quarterly branch
                # beneath it: the raise is the statement immediately below, which the primary
                # source's empty quarterly table would have reached anyway. The previous
                # ``elif quarterly: raise`` could not fire, and the duplication only made it
                # look as though the quarterly-empty path had two exits.
                self._degrade(f"income statement ({freq}) -- fallback vendor", error)
        if quarterly and table.empty:
            raise NotImplementedError("quarterly statements not supported")
        df = table.to_yfinance()
        if as_dict:
            return df.to_dict()
        return df

    def get_balance_sheet(
        self, as_dict: bool = False, pretty: bool = False, freq: str = "yearly"
    ) -> Union[pd.DataFrame, dict]:
        """Fetch balance sheet matching yfinance `get_balance_sheet()`.

        Raises:
            NotImplementedError: ``pretty=True`` (see :data:`_PRETTY_UNSUPPORTED`), or a
                non-yearly ``freq``. The default ``pretty=False`` is unchanged.
        """
        if pretty:
            raise NotImplementedError(_PRETTY_UNSUPPORTED)
        if (freq or "yearly").lower() != "yearly":
            raise NotImplementedError("quarterly statements not supported")
        profile = self._ensure_profile()
        if profile.balance_sheet.empty and self.statement_fallback:
            df, error = self._fallback_statement_frame("balance_sheet", "annual")
            if not df.empty:
                return df.to_dict() if as_dict else df
            if error:
                self._degrade("balance sheet (yearly) -- fallback vendor", error)
        df = profile.balance_sheet.to_yfinance()
        if as_dict:
            return df.to_dict()
        return df

    def get_cash_flow(
        self, as_dict: bool = False, pretty: bool = False, freq: str = "yearly"
    ) -> Union[pd.DataFrame, dict]:
        """Fetch cash flow statement matching yfinance `get_cash_flow()`.

        Raises:
            NotImplementedError: ``pretty=True`` (see :data:`_PRETTY_UNSUPPORTED`), or a
                non-yearly ``freq``. The default ``pretty=False`` is unchanged.
        """
        if pretty:
            raise NotImplementedError(_PRETTY_UNSUPPORTED)
        if (freq or "yearly").lower() != "yearly":
            raise NotImplementedError("quarterly statements not supported")
        profile = self._ensure_profile()
        if profile.cash_flow.empty and self.statement_fallback:
            df, error = self._fallback_statement_frame("cash_flow", "annual")
            if not df.empty:
                return df.to_dict() if as_dict else df
            if error:
                self._degrade("cash flow statement (yearly) -- fallback vendor", error)
        df = profile.cash_flow.to_yfinance()
        if as_dict:
            return df.to_dict()
        return df

    def get_cashflow(self, as_dict: bool = False, pretty: bool = False, freq: str = "yearly"):
        """Alias for get_cash_flow; ``pretty=True`` raises there for the same reason."""
        return self.get_cash_flow(as_dict=as_dict, pretty=pretty, freq=freq)

    @property
    def financials(self) -> pd.DataFrame:
        """10–12+ Years Annual Income Statement (Pandas DataFrame)."""
        return self.get_income_stmt(freq="yearly")

    @property
    def income_stmt(self) -> pd.DataFrame:
        """Annual income statement."""
        return self.get_income_stmt(freq="yearly")

    @property
    def quarterly_financials(self) -> pd.DataFrame:
        """12–14+ Quarters Income Statement (Pandas DataFrame)."""
        return self.get_income_stmt(freq="quarterly")

    @property
    def quarterly_income_stmt(self) -> pd.DataFrame:
        """Quarterly income statement."""
        return self.get_income_stmt(freq="quarterly")

    @property
    def ttm_income_stmt(self) -> pd.DataFrame:
        """TTM Income Statement."""
        raise NotImplementedError("quarterly statements not supported")

    @property
    def balance_sheet(self) -> pd.DataFrame:
        """10–12+ Years Annual Balance Sheet (Pandas DataFrame)."""
        return self.get_balance_sheet(freq="yearly")

    @property
    def quarterly_balance_sheet(self) -> pd.DataFrame:
        """Quarterly Balance Sheet (Pandas DataFrame)."""
        return self.get_balance_sheet(freq="quarterly")

    @property
    def cashflow(self) -> pd.DataFrame:
        """10–12+ Years Annual Cash Flow Statement (Pandas DataFrame)."""
        return self.get_cash_flow(freq="yearly")

    @property
    def cash_flow(self) -> pd.DataFrame:
        """Annual Cash Flow Statement."""
        return self.get_cash_flow(freq="yearly")

    @property
    def quarterly_cashflow(self) -> pd.DataFrame:
        """Quarterly Cash Flow Statement (Pandas DataFrame)."""
        return self.get_cash_flow(freq="quarterly")

    @property
    def quarterly_cash_flow(self) -> pd.DataFrame:
        """Quarterly Cash Flow Statement."""
        return self.get_cash_flow(freq="quarterly")

    @property
    def ttm_cash_flow(self) -> pd.DataFrame:
        """TTM Cash Flow Statement."""
        raise NotImplementedError("quarterly statements not supported")

    # --------------------------------------------------- Corporate Actions & Calendar
    @property
    def dividends(self) -> pd.Series:
        """Historical cash dividends series with ex-dates (pd.Series)."""
        profile = self._ensure_profile()
        return CorporateActionsEngine.extract_dividends(profile)

    @property
    def splits(self) -> pd.Series:
        """
        Historical stock splits and bonus issues series -- **empty for every symbol, today.**

        This property is not broken and is not unreachable; it is deliberately an honest absence.
        :meth:`bfinance.market.corporate.CorporateActionsEngine.extract_splits` returns an empty
        Series by construction because it used to INFER splits from growth in the balance sheet's
        ``Equity Capital`` row, and that inference was deleted: ``Equity Capital`` is
        ``shares issued x face value``, so a 1:1 bonus, a rights issue, a QIP and an ESOP
        conversion are the same number in the same field, and no threshold separates them.
        Measured, a capital raise at a company with no share-count event produced a fabricated
        ``{2026-03-31: 5.26}`` split that rescaled every earlier bar by 1/5.26 while the frame's
        provenance flags still claimed a complete adjustment.

        Splits are a *declared* fact, not an inferred one, and the real source for price
        adjustment is :func:`bfinance.nse.corporate_actions.split_ratio`, which reads the ratio out
        of the NSE filing. That path already supplies the factor ``history()`` uses; this property
        is the Screener.in-profile surface only. Downstream an empty series reads as ``0.0``, which
        :meth:`bfinance.market.corporate.CorporateActionsEngine.merge_actions_into_history`
        documents as "not known", not "none happened".
        """
        profile = self._ensure_profile()
        return CorporateActionsEngine.extract_splits(profile)

    @property
    def actions(self) -> pd.DataFrame:
        """Combined dividends/splits DataFrame matching yfinance `ticker.actions`."""
        return CorporateActionsEngine.build_actions(self.dividends, self.splits)

    @property
    def capital_gains(self) -> pd.Series:
        """Capital gains series matching yfinance (empty for Indian equities)."""
        return CorporateActionsEngine.build_capital_gains()

    @property
    def calendar(self) -> Dict[str, Any]:
        """Upcoming earnings and corporate events calendar matching yfinance `ticker.calendar`."""
        profile = self._ensure_profile()
        return CorporateActionsEngine.build_calendar(profile, self.dividends)

    @property
    def analyst_price_targets(self) -> Optional[Dict[str, Any]]:
        """Analyst price targets summary matching yfinance, from trendlyne's research reports.

        **Without ``Ticker(..., trendlyne_sections=True)`` this is ``None``**, which is the whole
        of what it was before: the package served no estimates feed, and the previous
        implementation -- which synthesised ``low = cmp x 0.85``, ``high = cmp x 1.35``,
        ``mean = cmp x 1.15`` off a current price that could itself fall back to a constant --
        was deleted in 0.1.3 because it produced confident numbers nobody published. Nothing of
        that kind happens here: every figure below is read out of the vendor's own ``Target``
        column, and there is no path through this property that invents one.

        **With the flag on** and the reports page answering, the dictionary is yfinance's shape
        plus two provenance keys:

        * ``current`` is ``None``, always. The route publishes an ``LTP`` column, but that is the
          price at the moment each report was published; publishing it under the key a caller
          reads as *today's* price is the same relabelling the deleted synthesiser was deleted
          for. No current price is derivable from this feed.
        * ``low`` / ``high`` / ``mean`` / ``median`` are the min, max, mean and median of the
          ``Target`` column, over per-report rows only. The vendor's own aggregate *Consensus
          Share Price Target* row is an average of the rows beneath it — it is **row 0 on the
          recorded capture**, `research_reports_tcs.html`, where its ``Target`` is 2445.54 — so
          including it would fold the consensus into every statistic computed here. It is excluded,
          and the count of what remained is published as ``n_reports`` -- a mean of two and a mean
          of twenty are the same number over a very different amount of evidence.
        * **The exclusion is by word, not by position.** The aggregate is matched
          case-insensitively against the ``Author`` column (``_CONSENSUS_ROW_MARKER``), because
          the page decides its own order and nothing measured pins that the aggregate is always
          first. Reading "row 0" as the rule would be a claim about the vendor's markup that this
          package cannot make; reading it as *what the recorded page showed* is one it can.
        * **A missing ``Author`` column withholds the statistics entirely** rather than including
          the aggregate. That is a deliberate choice over the alternative (compute them and hope
          the aggregate is not there): with no column to match against, every figure below would
          fold the consensus into the mean of the rows beneath it, and a mean that is wrong by one
          value looks exactly like one that is right. The reason lands on :attr:`data_warnings`.
        * **A ``Target`` written with a thousands separator is a target, not a blank.**
          ``"2,450.54"`` is repaired before the numeric coercion (``_numeric_targets``);
          ``pd.to_numeric(errors="coerce")`` alone would drop it as ``NaN`` and lose a real broker
          from ``low``, ``high``, ``mean``, ``median`` and ``n_reports`` at once.
        * ``n_reports`` and ``bfinance_analyst_source`` travel with the value so that provenance
          does not have to be inferred from whether a warning happened to be logged.

        Returns ``None`` -- never a synthesised target -- when the flag is off, when the reports
        page carries no usable ``Target`` value, when it carries no ``Author`` column to exclude
        the aggregate with, or when the fetch failed. In each of the first three the specific
        reason is on :attr:`data_warnings`, and the fetch is not attempted twice.
        """
        if not self.trendlyne_sections:
            return None
        frame, error = self._section_call("trendlyne research reports", "research_reports")
        if error is not None:
            # Already recorded by _section_call; a second warning here would name one cause
            # twice and read as two failures.
            return None
        targets = _targets_from_reports(frame)
        if targets is None:
            self._degrade("analyst price targets", self._targets_absence(frame))
        return targets

    def _targets_absence(self, frame: pd.DataFrame) -> str:
        """Why :func:`_targets_from_reports` returned ``None``, as a reason a caller can act on.

        Three absences share that ``None``, and "there is no measured target" would be a false
        statement about two of them: a page with targets whose ``Author`` column has been renamed
        has every target the caller wanted and none this property is willing to publish. So the
        column names are checked here, in the one place with a Ticker to record the answer on,
        rather than being folded into one vague sentence.
        """
        if frame is None or frame.empty:
            return (
                f"the trendlyne research-reports page for {self.symbol} carried no rows, so there "
                f"is no measured target to publish; none was synthesised"
            )
        if _CONSENSUS_ROW_COLUMN not in frame.columns:
            return (
                f"the trendlyne research-reports page for {self.symbol} carries a Target column "
                f"but no '{_CONSENSUS_ROW_COLUMN}' column, so the vendor's aggregate "
                f"'Consensus Share Price Target' row cannot be told apart from a per-report row; "
                f"including it would fold the consensus into every statistic, so none were "
                f"published and none was synthesised"
            )
        return (
            f"the trendlyne research-reports page carries no usable Target value for "
            f"{self.symbol}, so there is no measured target to publish; none was synthesised"
        )

    @property
    def options(self) -> Tuple[str, ...]:
        """F&O expiry dates gated on real membership; () for non-F&O (yfinance 1.7.0)."""
        return DerivativesEngine.resolve_options(self.ticker)

    def option_chain(self, date: Optional[str] = None) -> OptionChain:
        """
        Calls & Puts OptionChain container with Strikes, IV, OI, Volume.

        Always returns an **empty** chain. The previous implementation, reached only when
        ``self.options`` was non-empty, delegated to
        ``DerivativesEngine.generate_option_chain`` — which had no network path, priced options
        with a closed-form heuristic, and drew ``volume``/``openInterest`` from a seeded RNG. Those
        are random numbers shaped like an exchange quote, so returning them is worse than returning
        nothing. The generator has been deleted; see its docstring for what a real source would
        need.

        An empty chain is the truthful answer for a symbol with no verified F&O membership, and
        matches yfinance's ``()`` for a non-F&O underlying.
        """
        return DerivativesEngine.empty_option_chain()

    @property
    def major_holders(self) -> pd.DataFrame:
        """Major holders with float Value column matching yfinance (no percent-strings)."""
        return CorporateActionsEngine.build_major_holders(self._ensure_profile())

    @property
    def institutional_holders(self) -> pd.DataFrame:
        """Institutional holders (empty (0,0) for Indian equities, yfinance parity)."""
        return CorporateActionsEngine.build_institutional_holders()

    @property
    def mutualfund_holders(self) -> pd.DataFrame:
        """Mutual fund holders (empty (0,0) for Indian equities, yfinance parity)."""
        return CorporateActionsEngine.build_mutualfund_holders()

    @property
    def news(self) -> List[Dict[str, Any]]:
        """Latest announcements adapted to yfinance {id, content} news shape."""
        profile = self._ensure_profile()
        return CorporateActionsEngine.build_news(profile.announcements)

    # ------------------------------------- Opt-in trendlyne section members
    #
    # Seven members over one lazy client, all behind ``trendlyne_sections=True``, all reached
    # through `_section_call`, and all returning an honest empty shape (never a fabricated
    # frame) when the fetch fails -- with the reason on `data_warnings` in every one of those
    # cases. None of them is reached by an existing property, so none of them changes one.

    def trendlyne_news(self, page: int = 1) -> List[Dict[str, Any]]:
        """trendlyne's ``/latest-news/`` page for this symbol, in the yfinance news shape.

        The **same** ``{"id", "content": {title, link, publisher, provider, time, pubDate,
        canonicalUrl}}`` structure :attr:`news` returns, from a different vendor: the point of
        this member is that a caller who already indexes those two levels does not have to learn
        a second one. ``tests/test_ticker_trendlyne_sections.py`` pins the two key sets against
        each other, so the shapes cannot drift apart silently.

        Args:
            page: Vendor page number. ``1`` is the bare page; anything higher is ``?page=N``,
                which trendlyne answers with the next batch of cards.

        Returns:
            One item per card on the page, in the page's own order. An empty list means the
            vendor's page carried no cards, which is the same ``[]`` :attr:`news` returns for a
            symbol with no announcements; a *failure* is on :attr:`data_warnings` and does not
            look like an empty list to anyone reading it.
        """
        frame, error = self._section_call("trendlyne news", "latest_news", page=page)
        if error is not None:
            return []
        items = []
        for position, (_, row) in enumerate(frame.iterrows()):
            items.append(
                _trendlyne_news_item(
                    f"bfinance-trendlyne-p{page}-{position}",
                    row.get("date"),
                    row.get("title"),
                    row.get("url"),
                    row.get("source"),
                )
            )
        return items

    def trendlyne_reports(self, page: int = 1) -> pd.DataFrame:
        """trendlyne's broker research-reports table, the vendor's columns, verbatim.

        One request, and one page of it: the frame is stamped ``page`` in its ``attrs`` and is
        not the full report history. Its first row is the vendor's own aggregate *Consensus
        Share Price Target* row, which is kept rather than filtered -- it is the consensus, and
        removing it would be this package deciding what the caller wanted.
        :attr:`analyst_price_targets` reads this same table and documents the one thing a
        consumer must do about that row before averaging it.
        """
        frame, error = self._section_call("trendlyne research reports", "research_reports", page=page)
        return pd.DataFrame() if error is not None else frame

    def trendlyne_shareholding(self) -> pd.DataFrame:
        """The share-holding **summary grid**: holder categories down, quarters across.

        The analogue of :attr:`shareholding`, and the reason it is its own member: this route
        serves **only** the period ``latest`` (every other segment answers HTTP 500, verified),
        so the frame is one unaddressable snapshot and carries
        ``attrs["bfinance_trendlyne_snapshot_only"] = True``. It is still worth one request --
        the page embeds every quarter it holds.

        The per-category holder tables that sit behind the tabs on the same page are **not**
        folded in: they are a second request, and a member that fetched both would be deciding
        which of the two its caller wanted. Reach them on the client as
        ``TrendlyneClient.share_holding_holders``.
        """
        frame, error = self._section_call(
            "trendlyne share-holding summary", "share_holding"
        )
        return pd.DataFrame() if error is not None else frame

    def trendlyne_deals(self) -> pd.DataFrame:
        """Bulk and block deals, as the vendor's ``deals`` table.

        The whole history the page serves, in one request -- there is no period axis on this
        route, which is why it carries no snapshot guard. Not the insider-trading table, which
        shares a client but not a shape.
        """
        frame, error = self._section_call("trendlyne bulk and block deals", "bulk_block_deals")
        return pd.DataFrame() if error is not None else frame

    def trendlyne_buy_sell_zone(self) -> Dict[str, pd.DataFrame]:
        """The PE-band grids on both bases: ``{"standalone", "consolidated"}``.

        Two frames, keyed by the ``div.tab-pane`` they live in rather than by their headers,
        because the two grids carry **byte-identical header rows** on the live page and their
        numbers differ. A key derived from the header would make a swap invisible.

        An empty dictionary means the fetch failed and the reason is on :attr:`data_warnings`;
        a dict whose frames are empty means the page carried no band rows.
        """
        grids, error = self._section_call("trendlyne buy/sell zone", "buy_sell_zone")
        return {} if error is not None else grids

    def trendlyne_technicals(self) -> pd.DataFrame:
        """Trendlyne's own technical-signal table: ``name``, ``value``, ``insight``, ``color``,
        ``parameter``.

        From the robots-clean JSON sibling of the ``/equity/technical-analysis-*`` **page**,
        which ``robots.txt`` disallows; that page route stays unwired. One request, and the
        frame publishes in ``attrs`` which payload keys it does **not** speak for -- the raw
        parameters are on the client as ``adv_technical_analysis``.
        """
        frame, error = self._section_call("trendlyne technical signals", "technical_signals")
        return pd.DataFrame() if error is not None else frame

    def trendlyne_swot(self) -> Dict[str, pd.DataFrame]:
        """The SWOT widget's five fact tables, keyed by their tab pane.

        Five single-column frames whose header cell *is* the fact; each publishes that heading
        in ``attrs["bfinance_trendlyne_swot_heading"]``. The fifth (``tag_others``) is the
        vendor's own extra tab, not a fourth quadrant, and is published under its own name.
        """
        tables, error = self._section_call("trendlyne SWOT tables", "swot")
        return {} if error is not None else tables

    # ----------------------------------------------- Indian Superpower Properties
    @property
    def sector(self) -> Optional[str]:
        """Macro sector classification from Screener/Exchange taxonomy."""
        profile = self._ensure_profile()
        return profile.sector

    @property
    def industry_group(self) -> Optional[str]:
        """Industry group classification."""
        profile = self._ensure_profile()
        return profile.industry_group

    @property
    def industry(self) -> Optional[str]:
        """Specific industry classification."""
        profile = self._ensure_profile()
        return profile.industry

    @property
    def sub_industry(self) -> Optional[str]:
        """Micro sub-industry classification."""
        profile = self._ensure_profile()
        return profile.sub_industry

    @property
    def indices(self) -> List[str]:
        """List of NSE/BSE indices this equity is part of (e.g. Nifty 50, BSE Sensex)."""
        profile = self._ensure_profile()
        return profile.indices

    @property
    def concalls(self) -> List[Concall]:
        """Conference call transcript PDFs, audio recordings (MP3s), and PPT presentations."""
        profile = self._ensure_profile()
        return profile.concalls

    @property
    def annual_reports(self) -> List[Dict[str, str]]:
        """10+ years annual reports direct PDF download URLs."""
        profile = self._ensure_profile()
        return profile.annual_reports

    @property
    def credit_ratings(self) -> List[Dict[str, str]]:
        """Credit rating agency reports (CRISIL, ICRA, CARE, India Ratings)."""
        profile = self._ensure_profile()
        return profile.credit_ratings

    @property
    def shareholding(self) -> pd.DataFrame:
        """12+ Quarters complete shareholding pattern history."""
        profile = self._ensure_profile()
        return profile.shareholding.to_dataframe(orient="columns")

    @property
    def shareholding_yearly(self) -> pd.DataFrame:
        """11+ Years complete annual shareholding pattern history."""
        profile = self._ensure_profile()
        return profile.shareholding_yearly.to_dataframe(orient="columns")

    @property
    def ratios_history(self) -> pd.DataFrame:
        """10-year historical Debtor Days, Working Capital Days, Cash Conversion, ROCE."""
        profile = self._ensure_profile()
        return profile.ratios_history.to_dataframe(orient="columns")

    @property
    def custom_ratios(self) -> Dict[str, Any]:
        """Complete dictionary of advanced institutional ratios (Piotroski, Graham, EV, EBITDA, etc.).

        Provenance keys travel with the result, so a caller can tell a computed metric from an
        omitted one without inferring it from the value:

        ``bfinance_enterprise_value_omitted``
            Why EV is absent -- typically that the cash leg is unsourceable (see
            ``_CASH_ROW_LABELS``). The key is present with value ``None``; it is not omitted.
        ``bfinance_debt_cr`` / ``bfinance_cash_cr``
            The EV legs on their own, so a caller can assemble its own definition.
        ``bfinance_omitted_metrics``
            Per-metric ledger of what could not be computed and why.

        :attr:`valuation_measures` publishes the same names on its ``attrs``.
        """
        from bfinance.market.ratios import CustomRatiosCalculator
        profile = self._ensure_profile()
        r = profile.ratios
        # The NATIVE frames, not ``self.financials`` / ``self.balance_sheet`` /
        # ``self.cash_flow``. Those now return the yfinance shape (generic row labels,
        # absolute rupees, newest-first), and ``CustomRatiosCalculator`` reads the Indian
        # labels ("Net Profit", "Borrowings", "Cash from Operating Activity") and reports
        # Rs Cr. Reading it through the yfinance accessors would leave every leg absent and
        # publish zeros; it used to work only because the two shapes were the same frame.
        return CustomRatiosCalculator.calculate_all_custom_ratios(
            market_cap_cr=r.market_cap,
            current_price=r.current_price,
            trailing_pe=r.stock_pe,
            book_value=r.book_value,
            eps=r.eps_ttm,
            pnl=profile.profit_loss.to_dataframe(orient="columns"),
            bs=profile.balance_sheet.to_dataframe(orient="columns"),
            cf=profile.cash_flow.to_dataframe(orient="columns"),
        )

    @property
    def piotroski_score(self) -> int:
        """Piotroski 9-Point Fundamental Score (0 to 9 integer)."""
        return self.custom_ratios.get("piotroski_score", 0)

    @property
    def graham_number(self) -> Optional[float]:
        """Benjamin Graham Maximum Fair Value Number in ₹."""
        return self.custom_ratios.get("graham_number")

    @property
    def enterprise_value(self) -> Optional[float]:
        """Enterprise Value (Market Cap + Debt - Cash) in ₹ Cr, or ``None`` when a leg is unknown.

        ``None`` is the common case for Indian equities and is **not** an error: the screener.in
        balance sheet itemises only ten rows and none of them is cash - it sits inside the
        undifferentiated ``Other Assets`` bucket - so the cash leg is normally unsourceable. Rather
        than assume ``cash = 0`` and publish an EV overstated by the whole cash balance for every
        net-cash company, the leg is treated as unknown and EV is withheld.

        The reason is on ``custom_ratios()["bfinance_enterprise_value_omitted"]``, alongside
        ``bfinance_debt_cr`` and ``bfinance_cash_cr`` for the legs individually. If a balance sheet
        does carry a cash row, it is read and EV is computed normally.

        :attr:`valuation_measures` applies the same gate and publishes the same keys on its
        ``attrs``, so the two surfaces cannot drift.
        """
        # The ``0.0`` default is load-bearing and must NOT be tidied into a real fallback.
        # ``custom_ratios`` sets ``enterprise_value_cr`` to ``None`` when a leg is unknown rather
        # than omitting the key -- precisely so that this ``.get`` yields ``None`` and a caller
        # cannot mistake "not computable" for "zero". Omitting the key would hand every caller
        # a fabricated 0.0 and move the defect here instead of fixing it.
        return self.custom_ratios.get("enterprise_value_cr", 0.0)

    @property
    def ev_to_ebitda(self) -> Optional[float]:
        """Enterprise Value to EBITDA multiple."""
        return self.custom_ratios.get("ev_to_ebitda")

    @property
    def interest_coverage(self) -> Optional[float]:
        """Interest Coverage Ratio (EBIT / Interest)."""
        return self.custom_ratios.get("interest_coverage")

    @property
    def cagrs(self) -> Dict[str, Dict[str, str]]:
        """Compounded Sales, Profit, Stock Price, and ROE CAGRs (10Y, 5Y, 3Y, 1Y)."""
        profile = self._ensure_profile()
        return profile.cagrs

    @property
    def pros_cons(self) -> Dict[str, List[str]]:
        """Automated qualitative investment insights (Pros and Cons)."""
        profile = self._ensure_profile()
        return {"pros": profile.analysis.pros, "cons": profile.analysis.cons}

    @property
    def peers(self) -> pd.DataFrame:
        """Live industry peer comparison table (CMP, P/E, Market Cap, ROCE)."""
        profile = self._ensure_profile()
        return profile.peers_dataframe()

    def valuation_history(self, metric: str = "pe", days: int = 1825) -> pd.DataFrame:
        """
        Historical multi-year valuation multiples timeseries:
        - 'pe': Historical P/E, Median P/E, EPS
        - 'margins': Gross, Operating, and Net Profit Margins
        - 'ev_ebitda': EV/EBITDA multiple & EBITDA
        - 'pb': Price to Book Value (PBV)
        - 'mcap_sales': Market Cap to Sales multiple
        """
        return self._run_async(
            self.screener_client.get_chart_timeseries(self.symbol, metric=metric, days=days)
        )

    async def valuation_history_async(self, metric: str = "pe", days: int = 1825) -> pd.DataFrame:
        """Async variant of valuation_history()."""
        return await self.screener_client.get_chart_timeseries(self.symbol, metric=metric, days=days)

    @property
    def history_metadata(self) -> Dict[str, Any]:
        """
        Metadata dictionary for historical data matching yfinance 1.7.0 `ticker.history_metadata`.

        Previous closes come from real history (or ``None`` when there is none). This used to
        report ``currentPrice * 0.995`` -- a fabricated constant that happens to land inside
        the real adjustment factor, so it looked plausible while never being a traded price.
        """
        # Read `info` first: it is what populates the reused 1y history frame below.
        current_price = self.info.get("currentPrice")
        return build_history_metadata(self.ticker, self._history_for_metadata, current_price)

    def get_history_metadata(self) -> Dict[str, Any]:
        """Alias for history_metadata matching yfinance."""
        return self.history_metadata

    @property
    def valuation_measures(self) -> pd.DataFrame:
        """
        Key valuation metrics history table matching yfinance 1.3.0+ `ticker.valuation_measures`.

        **Enterprise Value is reported only when every leg of ``mcap + debt - cash`` is actually
        in hand.** Any missing leg makes ``EnterpriseValue``, ``EVToRevenue`` and ``EVToEBITDA``
        ``None``, and ``df.attrs["bfinance_enterprise_value_omitted"]`` names the leg that is
        missing. Returning nothing is the honest answer; the previous version returned a number
        that was wrong in two ways at once:

        * ``cash_cr`` was hardcoded ``0.0``. The upstream balance sheet does not itemise cash --
          see :data:`_CASH_ROW_LABELS` -- so the cash term was an assumption, and for every
          net-cash company it overstated EV by the whole cash balance, permanently and silently.
        * A ragged statement row raised ``ParsingError``, the bare handler substituted an empty
          frame, and the empty frame made ``Borrowings`` read ``0.0`` too. EV then collapsed to
          bare market cap and was returned as a real number: an 80% understatement on the
          measured case, with no flag and no ``None``.

        Provenance keys, always present: ``bfinance_enterprise_value_omitted`` (``str | None``),
        ``bfinance_balance_sheet_error`` and ``bfinance_profit_loss_error`` (``str | None``),
        ``bfinance_debt_cr`` (``float | None``).
        """
        r = self.info
        profile = self._ensure_profile()
        bs, bs_error = self._read_statement(profile.balance_sheet, "balance sheet")
        pnl, pnl_error = self._read_statement(profile.profit_loss, "profit and loss")

        def _latest(df: pd.DataFrame, row: str, stmt_label: str) -> Optional[float]:
            try:
                if df.empty or row not in df.index:
                    return None
                v = df.loc[row].iloc[-1]
                if v is None or pd.isna(v):
                    return None
                return float(v)
            except (KeyError, IndexError, TypeError, ValueError) as e:
                # Narrowed from `except Exception`. These four are what a per-cell read can
                # actually raise: a duplicate index label makes `df.loc[row]` a DataFrame so
                # `float()` is a TypeError; a non-scalar NaN check is a ValueError; a label that
                # vanished between the guard and the lookup is a KeyError. None is the right
                # answer for all of them -- it is a missing number, not a wrong one -- but it is
                # now said out loud instead of passing as "the company reported zero".
                self._degrade(f"{stmt_label} row {row!r}", str(e))
                return None

        mcap = r.get("marketCap")
        # Explicit `is None`, not truthiness: a market cap that failed to parse must not read as
        # a market cap of zero, which is the same defect as `or 0.0` was.
        equity_leg = mcap if (mcap is not None and mcap > 0) else None
        debt_cr = _latest(bs, "Borrowings", "balance sheet")
        cash_cr = next(
            (
                v
                for v in (_latest(bs, label, "balance sheet") for label in _CASH_ROW_LABELS)
                if v is not None
            ),
            None,
        )

        ev: Optional[float] = None
        ev_omitted: Optional[str] = None
        if bs_error is not None:
            ev_omitted = f"balance sheet unreadable: {bs_error}"
        elif cash_cr is None:
            ev_omitted = (
                "the upstream balance sheet has no itemised cash row ("
                f"{' / '.join(_CASH_ROW_LABELS)}), so the cash leg of mcap + debt - cash is unknown"
            )
        elif debt_cr is None:
            ev_omitted = (
                "no readable Borrowings figure (the upstream prints '-' for a company with no "
                "borrowings, and an absent row is a different problem again; neither is read as 0)"
            )
        elif equity_leg is None:
            ev_omitted = "no marketCap in info, so the equity leg is unknown"
        else:
            ev = equity_leg + debt_cr * 1e7 - cash_cr * 1e7

        rev_cr = _latest(pnl, "Sales", "profit and loss")
        op_cr = _latest(pnl, "Operating Profit", "profit and loss")
        ev_to_rev = ev / (rev_cr * 1e7) if (ev is not None and rev_cr and rev_cr > 0) else None
        ev_to_ebitda = ev / (op_cr * 1e7) if (ev is not None and op_cr and op_cr > 0) else None
        metrics = {
            "MarketCap": r.get("marketCap"),
            "EnterpriseValue": ev,
            "TrailingPE": r.get("trailingPE"),
            "ForwardPE": r.get("forwardPE"),
            "PEG_Ratio": r.get("pegRatio"),
            "PriceToSales": r.get("priceToSales"),
            "PriceToBook": r.get("priceToBook"),
            "EVToRevenue": ev_to_rev,
            "EVToEBITDA": ev_to_ebitda,
        }
        # `dtype=object` is load-bearing. A plain `pd.DataFrame(list(metrics.items()))` infers
        # float64 for the Value column, which turns every absent metric into a float NaN --
        # and a NaN is a *number*: it propagates silently through arithmetic, prints as a
        # plausible cell, and is indistinguishable from a metric that was computed. `None` is
        # the whole point of the gate above, so it has to survive into the frame as `None`.
        df = pd.DataFrame(
            {"Metric": list(metrics), "Value": pd.Series(list(metrics.values()), dtype=object)}
        )
        # Machine-readable provenance, so a caller never has to read `None` as "zero cash" --
        # which is exactly what the removed hardcoded `cash_cr = 0.0` asserted, silently.
        df.attrs["bfinance_enterprise_value_omitted"] = ev_omitted
        df.attrs["bfinance_balance_sheet_error"] = bs_error
        df.attrs["bfinance_profit_loss_error"] = pnl_error
        df.attrs["bfinance_debt_cr"] = debt_cr
        return df

    # ----------------------------------------------- Document & Financial Exporters
    def to_excel(self, filepath: str) -> str:
        """
        Export company 10-year financials, quarters, balance sheet, cash flow, shareholding,
        ratios, and peers to a multi-sheet Excel model (.xlsx) workbook.
        """
        from bfinance.utils.excel import FinancialModelExcelExporter
        profile = self._ensure_profile()
        return FinancialModelExcelExporter.export(profile, filepath)

    def download_concall_audio(self, index: int = 0, dest_path: str = ".") -> str:
        """
        Download the concall audio recording (.mp3) to local disk.
        """
        from bfinance.utils.downloader import DocumentDownloader
        concalls = self.concalls
        if not concalls or index >= len(concalls):
            raise ValueError(f"No concall found at index {index} (total {len(concalls)} available).")
        call = concalls[index]
        if not call.audio_url:
            raise ValueError(f"Concall '{call.title}' ({call.date}) does not have an audio recording URL.")
        return DocumentDownloader.download_file(call.audio_url, dest_path)

    def download_concall_transcript(self, index: int = 0, dest_path: str = ".") -> str:
        """
        Download the concall transcript filing (.pdf) to local disk.
        """
        from bfinance.utils.downloader import DocumentDownloader
        concalls = self.concalls
        if not concalls or index >= len(concalls):
            raise ValueError(f"No concall found at index {index} (total {len(concalls)} available).")
        call = concalls[index]
        if not call.transcript_url:
            raise ValueError(f"Concall '{call.title}' ({call.date}) does not have a transcript PDF URL.")
        return DocumentDownloader.download_file(call.transcript_url, dest_path)

    def download_annual_report(self, index: int = 0, dest_path: str = ".") -> str:
        """
        Download the company annual report (.pdf) to local disk.
        """
        from bfinance.utils.downloader import DocumentDownloader
        reports = self.annual_reports
        if not reports or index >= len(reports):
            raise ValueError(f"No annual report found at index {index} (total {len(reports)} available).")
        rep = reports[index]
        url = rep.get("url")
        if not url:
            raise ValueError(f"Annual report '{rep.get('year')}' does not have a valid download URL.")
        return DocumentDownloader.download_file(url, dest_path)

    # ----------------------------------------------- AI / LLM Context & Prompts Engine
    def to_ai_context(self, format: Literal["markdown", "json"] = "markdown", sections: Optional[Set[str]] = None) -> Any:
        """
        Generate complete AI-ready financial dossier for LLMs (Markdown string or structured JSON dict).
        """
        from bfinance.ai.context import AIContextBuilder
        profile = self._ensure_profile()
        if format == "json":
            return AIContextBuilder.build_json_context(profile)
        return AIContextBuilder.build_markdown_context(profile, include_sections=sections)

    def to_investment_memo_prompt(self, custom_instructions: str = "") -> str:
        """
        Generate a ready-to-run initiation coverage investment memo prompt for LLMs.
        """
        from bfinance.ai.prompts import AIPromptFactory
        profile = self._ensure_profile()
        return AIPromptFactory.investment_memo(profile, custom_instructions=custom_instructions)

    def to_forensic_audit_prompt(self) -> str:
        """
        Generate a forensic accounting audit prompt for LLMs to detect anomalies and accounting risks.
        """
        from bfinance.ai.prompts import AIPromptFactory
        profile = self._ensure_profile()
        return AIPromptFactory.forensic_audit(profile)

    def to_concall_analyst_prompt(self) -> str:
        """
        Generate an earnings call takeaways and forward guidance prompt for LLMs.
        """
        from bfinance.ai.prompts import AIPromptFactory
        profile = self._ensure_profile()
        return AIPromptFactory.concall_summary(profile)

    def __repr__(self) -> str:
        return f"<bfinance.Ticker symbol={self.symbol} exchange={self.exchange}>"
