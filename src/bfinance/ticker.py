"""
Master Ticker facade providing 100% yfinance drop-in compatibility + Indian market superpowers.
"""

import asyncio
import logging
from typing import Any, Dict, List, Literal, Optional, Set, Tuple, Union
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
    ):
        if not isinstance(ticker, str) or not ticker.strip():
            raise ValueError("ticker must be a non-empty string")
        self.ticker = ticker.strip()
        self.exchange, self.symbol = resolve_exchange_and_symbol(self.ticker)
        self.raise_errors = raise_errors
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
        Unified 180+ key metrics dictionary matching yfinance `ticker.info`.
        """
        if self._info is None or self._profile_degraded:
            profile = self._ensure_profile()
            latest_p = profile.ratios.current_price
            hist = None
            try:
                hist = self.history(period="1y")
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
        auto_adjust: bool = False,
        back_adjust: bool = False,
        repair: bool = False,
        keepna: bool = False,
        rounding: bool = False,
        timeout: int = 10,
        raise_errors: bool = False,
        ohlc: Literal["bhavcopy", "synthetic"] = "bhavcopy",
        adjust_corporate_actions: bool = True,
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
            adjust_corporate_actions: ``True`` (default) back-adjusts Open/High/Low/Close by the
                cumulative share-count factor over the instrument's whole corporate-action
                history, so the default path matches yfinance's own ``auto_adjust=False`` series
                bar-for-bar (the bhavcopy is as-traded; Yahoo's raw OHLC are not). ``False``
                returns the as-traded series, which is what the exchange printed. Dividends never
                touch O/H/L/C -- they go on ``Adj Close``, which is
                therefore not always equal to ``Close`` (it is, when no dividend ex-date falls in
                the window). Volume is restated by the *inverse* of the share-count factor, so that
                ``price * volume`` stays the rupee value traded across a bonus or sub-division.

                Three attrs keys describe the result and are present on every frame:
                ``bfinance_corporate_actions_applied`` (a complete adjustment was made),
                ``bfinance_adjustment_incomplete`` (``None``, or a dict naming the action classes
                the feed recorded that bfinance declines to convert -- e.g. a demerger), and
                ``price_basis`` (a machine-readable basis string, e.g.
                ``as_traded_share_count_adjusted``).
            auto_adjust: must be False. bfinance's default path already returns yfinance's
                adjusted basis, and yfinance's ``auto_adjust=True`` differs only in the column
                set (no ``Adj Close``), so True raises rather than silently changing which
                columns come back.

        Raises:
            AdjustmentNotAvailableError: ``auto_adjust=True``.
            RealOHLCUnavailableError: default source could not serve the window.
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
        auto_adjust: bool = False,
        back_adjust: bool = False,
        repair: bool = False,
        keepna: bool = False,
        rounding: bool = False,
        timeout: int = 10,
        raise_errors: bool = False,
        ohlc: Literal["bhavcopy", "synthetic"] = "bhavcopy",
        adjust_corporate_actions: bool = True,
    ) -> pd.DataFrame:
        """Async variant of history()."""
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
        )

    # --------------------------------------------------- Financial Statements API
    def get_income_stmt(
        self, as_dict: bool = False, pretty: bool = False, freq: str = "yearly"
    ) -> Union[pd.DataFrame, dict]:
        """Fetch income statement matching yfinance `get_income_stmt()`."""
        profile = self._ensure_profile()
        table = profile.quarters if freq.lower() == "quarterly" else profile.profit_loss
        if freq.lower() == "quarterly" and table.empty:
            raise NotImplementedError("quarterly statements not supported")
        df = table.to_dataframe(orient="columns")
        if as_dict:
            return df.to_dict()
        return df

    def get_balance_sheet(
        self, as_dict: bool = False, pretty: bool = False, freq: str = "yearly"
    ) -> Union[pd.DataFrame, dict]:
        """Fetch balance sheet matching yfinance `get_balance_sheet()`."""
        if (freq or "yearly").lower() != "yearly":
            raise NotImplementedError("quarterly statements not supported")
        profile = self._ensure_profile()
        df = profile.balance_sheet.to_dataframe(orient="columns")
        if as_dict:
            return df.to_dict()
        return df

    def get_cash_flow(
        self, as_dict: bool = False, pretty: bool = False, freq: str = "yearly"
    ) -> Union[pd.DataFrame, dict]:
        """Fetch cash flow statement matching yfinance `get_cash_flow()`."""
        if (freq or "yearly").lower() != "yearly":
            raise NotImplementedError("quarterly statements not supported")
        profile = self._ensure_profile()
        df = profile.cash_flow.to_dataframe(orient="columns")
        if as_dict:
            return df.to_dict()
        return df

    def get_cashflow(self, as_dict: bool = False, pretty: bool = False, freq: str = "yearly"):
        """Alias for get_cash_flow."""
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
        """Historical stock splits and bonus issues series (pd.Series)."""
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
    def analyst_price_targets(self) -> Optional[Dict[str, Optional[float]]]:
        """Analyst price targets summary matching yfinance.

        Upstream provides no analyst-estimates feed, so there is no honest
        value to return. Previously this synthesized low/high/mean/median
        from fixed multipliers off the current price — removed in 0.1.3.
        Returns None until a real estimates source is integrated.
        """
        return None

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
        return CustomRatiosCalculator.calculate_all_custom_ratios(
            market_cap_cr=r.market_cap,
            current_price=r.current_price,
            trailing_pe=r.stock_pe,
            book_value=r.book_value,
            eps=r.eps_ttm,
            pnl=self.financials,
            bs=self.balance_sheet,
            cf=self.cash_flow,
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
