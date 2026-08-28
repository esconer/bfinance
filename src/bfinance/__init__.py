"""
bfinance — High-performance Python SDK for Indian Equities (NSE & BSE).
A 1:1 drop-in replacement for yfinance (1.7.0+ compatible) supercharged with 10-year financials, concalls, and live market microstructure.
"""

from .ticker import Ticker
from .tickers import Tickers
from .screens import Screen, screens
from .download import download
from .sector import Sector, Industry
from .screener.client import ScreenerClient
from .market.fast_info import FastInfo
from .market.ratios import ScreenerRatioSearch, CustomRatiosCalculator
from .models.company import CompanyProfile, Concall, PeerStock, TopRatios
from .models.options import OptionChain, OptionContract
from .models.statements import FinancialStatement
from .utils.excel import FinancialModelExcelExporter
from .utils.downloader import DocumentDownloader
from .ai.context import AIContextBuilder
from .ai.prompts import AIPromptFactory
from .ai.tools import BFinanceAITools
from .utils.exceptions import (
    BFinanceError,
    TickerNotFoundError,
    UpstreamServiceError,
    RateLimitExceededError,
)

ratios = ScreenerRatioSearch()

__version__ = "0.1.1"
__author__ = "bfinance contributors"

__all__ = [
    "Ticker",
    "Tickers",
    "Sector",
    "Industry",
    "Screen",
    "screens",
    "ratios",
    "download",
    "FastInfo",
    "ScreenerClient",
    "CompanyProfile",
    "Concall",
    "PeerStock",
    "TopRatios",
    "OptionChain",
    "OptionContract",
    "FinancialStatement",
    "FinancialModelExcelExporter",
    "DocumentDownloader",
    "AIContextBuilder",
    "AIPromptFactory",
    "BFinanceAITools",
    "ScreenerRatioSearch",
    "CustomRatiosCalculator",
    "BFinanceError",
    "TickerNotFoundError",
    "UpstreamServiceError",
    "RateLimitExceededError",
    "__version__",
]
