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

Not imported here, on purpose
-----------------------------
Seven capabilities are **opt-in** and are deliberately absent from this module and from
``__all__``:

    bfinance.statements   bfinance.sources   bfinance.costs   bfinance.flows
    bfinance.universe     bfinance.ownership  bfinance.nse.macro

``from bfinance import costs`` already works and is the documented way in, because Python resolves
a name against the package's submodules. **No re-export is needed for that, and adding one would
cost the only thing that matters here:** a module object can only be an attribute of this package
if executing it put it there, and executing any of the seven is exactly what must not happen on
``import bfinance``. The same goes for the entries themselves — a top-level ``resolve`` or
``UniverseArchive`` would have to be bound by an import of its own module, so option (b) is not a
policy question, it is mechanically the same thing as option (a) with the isolation removed.

``bfinance.trendlyne`` is a third-party-source client and is opt-in for the same reason. The other
six opt in for two further ones: :mod:`bfinance.sources` is the *registry* that describes the rest,
and a table of strings that imports its own subjects is not a table; and :mod:`bfinance.statements`
carries a second cascade underneath, so importing it eagerly would put a sub-cascade on every
caller's import path.

So ``import bfinance`` pays for none of them, and a caller who wants one names it. The isolation is
a contract, not a convention: ``tests/test_public_surface.py`` asks the question in a fresh
interpreter, because in-process ``sys.modules`` is already polluted by the very import a test
performs.
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
