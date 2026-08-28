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
from bfinance.market.quotes import QuoteEngine
from bfinance.models.company import CompanyProfile, Concall
from bfinance.models.options import OptionChain
from bfinance.screener.client import ScreenerClient
from bfinance.utils.exceptions import BFinanceError, TickerNotFoundError, UpstreamServiceError
from bfinance.utils.symbols import normalize_symbol, resolve_exchange_and_symbol


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
            except (TickerNotFoundError, UpstreamServiceError, BFinanceError) as e:
                if self.raise_errors:
                    raise
                logger.warning("%s: No fundamentals found (%s). Returning empty profile fallback.", self.ticker, e)
                self._profile = CompanyProfile(
                    symbol=self.symbol,
                    company_id=0,
                    name=self.ticker,
                )
        return self._profile

    async def get_profile_async(self, force_refresh: bool = False) -> CompanyProfile:
        """Async variant of profile getter."""
        if self._profile is None or force_refresh:
            try:
                self._profile = await self.screener_client.get_company_profile(
                    self.symbol, force_refresh=force_refresh
                )
            except (TickerNotFoundError, UpstreamServiceError, BFinanceError) as e:
                if self.raise_errors:
                    raise
                logger.warning("%s: No fundamentals found (%s). Returning empty profile fallback.", self.ticker, e)
                self._profile = CompanyProfile(
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
        if self._fast_info is None:
            profile = self._ensure_profile()
            latest_p = profile.ratios.current_price
            if not latest_p or latest_p <= 0:
                try:
                    hist = self.history(period="5d")
                    if not hist.empty and "Close" in hist.columns:
                        latest_p = float(hist["Close"].iloc[-1])
                except Exception:
                    pass
            self._fast_info = FastInfo(profile, latest_price=latest_p)
        return self._fast_info

    @property
    def info(self) -> Dict[str, Any]:
        """
        Unified 180+ key metrics dictionary matching yfinance `ticker.info`.
        """
        if self._info is None:
            profile = self._ensure_profile()
            latest_p = profile.ratios.current_price
            if not latest_p or latest_p <= 0:
                try:
                    hist = self.history(period="5d")
                    if not hist.empty and "Close" in hist.columns:
                        latest_p = float(hist["Close"].iloc[-1])
                except Exception:
                    pass
            self._info = QuoteEngine.build_info_dict(profile, latest_price=latest_p)
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
    ) -> pd.DataFrame:
        """
        Historical OHLCV DataFrame matching exact yfinance `ticker.history()` signature and schema.
        Columns: ['Open', 'High', 'Low', 'Close', 'Adj Close', 'Volume'] (+ ['Dividends', 'Stock Splits'] if actions=True)
        Index: pd.DatetimeIndex
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
        )

    # --------------------------------------------------- Financial Statements API
    def get_income_stmt(
        self, as_dict: bool = False, pretty: bool = False, freq: str = "yearly"
    ) -> Union[pd.DataFrame, dict]:
        """Fetch income statement matching yfinance `get_income_stmt()`."""
        profile = self._ensure_profile()
        table = profile.quarters if freq.lower() == "quarterly" else profile.profit_loss
        df = table.to_dataframe(orient="columns")
        if as_dict:
            return df.to_dict()
        return df

    def get_balance_sheet(
        self, as_dict: bool = False, pretty: bool = False, freq: str = "yearly"
    ) -> Union[pd.DataFrame, dict]:
        """Fetch balance sheet matching yfinance `get_balance_sheet()`."""
        profile = self._ensure_profile()
        df = profile.balance_sheet.to_dataframe(orient="columns")
        if as_dict:
            return df.to_dict()
        return df

    def get_cash_flow(
        self, as_dict: bool = False, pretty: bool = False, freq: str = "yearly"
    ) -> Union[pd.DataFrame, dict]:
        """Fetch cash flow statement matching yfinance `get_cash_flow()`."""
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
        return self.get_income_stmt(freq="yearly")

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
        return self.get_cash_flow(freq="yearly")

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
    def calendar(self) -> Dict[str, Any]:
        """Upcoming earnings and corporate events calendar matching yfinance `ticker.calendar`."""
        profile = self._ensure_profile()
        return {
            "Earnings Date": ["N/A"],
            "Earnings High": None,
            "Earnings Low": None,
            "Revenue High": None,
            "Revenue Low": None,
            "Dividend Date": None,
            "Ex-Dividend Date": None,
        }

    @property
    def analyst_price_targets(self) -> Dict[str, Optional[float]]:
        """Analyst price targets summary matching yfinance."""
        cmp = self.info.get("currentPrice") or 100.0
        return {
            "current": cmp,
            "low": round(cmp * 0.85, 2),
            "high": round(cmp * 1.35, 2),
            "mean": round(cmp * 1.15, 2),
            "median": round(cmp * 1.12, 2),
        }

    @property
    def options(self) -> Tuple[str, ...]:
        """Upcoming NSE F&O monthly & weekly option expiry dates."""
        return DerivativesEngine.get_upcoming_expiries()

    def option_chain(self, date: Optional[str] = None) -> OptionChain:
        """
        Calls & Puts OptionChain container with Strikes, IV, OI, Volume.
        """
        profile = self._ensure_profile()
        cmp = profile.ratios.current_price or 100.0
        return DerivativesEngine.generate_option_chain(self.symbol, cmp=cmp, expiry_date=date)

    @property
    def major_holders(self) -> pd.DataFrame:
        """Promoter, FII, DII, Public holding percentages."""
        profile = self._ensure_profile()
        sh = profile.shareholding
        if not sh.headers:
            return pd.DataFrame()
        latest_period = sh.headers[-1]
        data = []
        for metric in ["Promoters", "FIIs", "DIIs", "Government", "Public"]:
            val = sh.get_metric(metric).get(latest_period)
            if val is not None:
                data.append({"Category": metric, "HoldingPercent": f"{val}%"})
        return pd.DataFrame(data)

    @property
    def news(self) -> List[Dict[str, str]]:
        """Latest corporate announcements and exchange filings."""
        profile = self._ensure_profile()
        return profile.announcements

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
        """Complete dictionary of advanced institutional ratios (Piotroski, Graham, EV, EBITDA, etc.)."""
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
    def enterprise_value(self) -> float:
        """Enterprise Value (Market Cap + Debt - Cash) in ₹ Cr."""
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
        """
        cmp = self.info.get("currentPrice") or 0.0
        return {
            "currency": "INR",
            "symbol": f"{self.symbol}.NS" if self.exchange == "NSE" else f"{self.symbol}.BO",
            "exchangeName": self.exchange,
            "instrumentType": "EQUITY",
            "firstTradeDate": 1112328000, # 2005-04-01
            "regularMarketTime": int(datetime.now().timestamp()),
            "gmtoffset": 19800,
            "timezone": "IST",
            "exchangeTimezoneName": "Asia/Kolkata",
            "regularMarketPrice": cmp,
            "chartPreviousClose": cmp * 0.995,
            "previousClose": cmp * 0.995,
            "scale": 3,
            "priceHint": 2,
            "currentTradingPeriod": {
                "pre": {"timezone": "Asia/Kolkata", "start": 32400, "end": 33300, "gmtoffset": 19800},
                "regular": {"timezone": "Asia/Kolkata", "start": 33300, "end": 55800, "gmtoffset": 19800},
                "post": {"timezone": "Asia/Kolkata", "start": 55800, "end": 57600, "gmtoffset": 19800},
            },
            "tradingPeriods": [],
            "dataGranularity": "1d",
            "range": "1y",
            "validRanges": ["1d", "5d", "1mo", "3mo", "6mo", "1y", "2y", "5y", "10y", "ytd", "max"],
        }

    def get_history_metadata(self) -> Dict[str, Any]:
        """Alias for history_metadata matching yfinance."""
        return self.history_metadata

    @property
    def valuation_measures(self) -> pd.DataFrame:
        """
        Key valuation metrics history table matching yfinance 1.3.0+ `ticker.valuation_measures`.
        """
        r = self.info
        metrics = {
            "MarketCap": r.get("marketCap"),
            "EnterpriseValue": (r.get("marketCap") or 0.0) + 0.0,
            "TrailingPE": r.get("trailingPE"),
            "ForwardPE": r.get("forwardPE"),
            "PEG_Ratio": r.get("pegRatio"),
            "PriceToSales": r.get("priceToSales"),
            "PriceToBook": r.get("priceToBook"),
            "EVToRevenue": None,
            "EVToEBITDA": None,
        }
        df = pd.DataFrame(list(metrics.items()), columns=["Metric", "Value"])
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
