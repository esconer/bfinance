"""
Custom typed exceptions for the bfinance package.
"""

class BFinanceError(Exception):
    """Base exception for all bfinance errors."""
    pass


class TickerNotFoundError(BFinanceError):
    """Raised when a ticker or security cannot be resolved."""
    pass


class UpstreamServiceError(BFinanceError):
    """Raised when an upstream provider (Screener, NSE, BSE) fails or is unreachable."""
    pass


class RateLimitExceededError(UpstreamServiceError):
    """Raised when an upstream provider throttles or rate limits requests (HTTP 429)."""
    pass


class ParsingError(BFinanceError):
    """Raised when HTML/JSON parsing fails due to unexpected upstream schema changes."""
    pass


class NotAnNseSymbolError(BFinanceError):
    """Raised when a symbol cannot be used with an NSE-only source.

    NSE-only endpoints (the MarketLens price history, for example) return an empty result with
    ``error: null`` for BSE tickers, BSE scrip codes, and indices. That silence is
    indistinguishable from a vendor outage, so these inputs are rejected loudly instead.
    """
    pass
