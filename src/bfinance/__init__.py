"""
bfinance — High-performance Python SDK for Indian Equities (NSE & BSE).
A 1:1 drop-in replacement for yfinance (1.7.0+ compatible) supercharged with 10-year financials, concalls, and live market microstructure.

Removed in this working tree:

* ``Sector`` / ``Industry`` — deleted. The implementation was a hardcoded four-sector,
  twenty-company table of invented prices and P/Es with no HTTP anywhere, and an **offline** test
  asserted those fabricated values were correct, so CI enforced the fiction. There is no live path
  to restore because there was never a fetch. For real sector/industry classification use
  ``Ticker.sector``, ``Ticker.industry`` and ``Ticker.sub_industry``, which read the genuine
  four-level taxonomy off the company profile.
* ``DerivativesEngine.generate_option_chain`` — deleted. It priced options with a closed-form
  heuristic and drew volume/open interest from a seeded RNG. ``Ticker.option_chain()`` now always
  returns an empty chain.
"""

from .ticker import Ticker
from .tickers import Tickers
from .screens import Screen, screens
from .download import download
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
    NotAnNseSymbolError,
    TickerNotFoundError,
    UpstreamServiceError,
    RateLimitExceededError,
)
from .nse import marketlens

ratios = ScreenerRatioSearch()

__version__ = "0.1.3"
__author__ = "bfinance contributors"

__all__ = [
    "Ticker",
    "Tickers",
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
    "NotAnNseSymbolError",
    "marketlens",
    "BFinanceError",
    "TickerNotFoundError",
    "UpstreamServiceError",
    "RateLimitExceededError",
    "__version__",
]
