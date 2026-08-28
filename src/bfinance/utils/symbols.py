"""
Symbol normalization and exchange resolution utilities for Indian equities.
"""

import re
from typing import Tuple, Optional


def normalize_symbol(symbol: str) -> str:
    """
    Clean and normalize any ticker symbol (e.g. 'RELIANCE.NS' -> 'RELIANCE', '  tcs  ' -> 'TCS').
    """
    if not symbol:
        return ""
    cleaned = symbol.strip().upper()
    # Strip .NS, .BO, .BSE, .NSE suffixes
    cleaned = re.sub(r"\.(NS|BO|BSE|NSE)$", "", cleaned, flags=re.IGNORECASE)
    # Strip trailing series suffixes like -EQ, -BE if present at end
    cleaned = re.sub(r"-(EQ|BE|SM|ST)$", "", cleaned, flags=re.IGNORECASE)
    return cleaned


def resolve_exchange_and_symbol(ticker: str) -> Tuple[str, str]:
    """
    Resolve exchange ('NSE' or 'BSE') and clean base symbol.
    Defaults to 'NSE' unless explicitly marked .BO / .BSE or if ticker is numeric (BSE scrip).
    """
    raw = ticker.strip().upper()
    if raw.endswith(".BO") or raw.endswith(".BSE"):
        return "BSE", normalize_symbol(raw)
    if raw.endswith(".NS") or raw.endswith(".NSE"):
        return "NSE", normalize_symbol(raw)
    if raw.isdigit():
        return "BSE", raw
    return "NSE", normalize_symbol(raw)


def format_yf_ticker(symbol: str, exchange: str = "NSE") -> str:
    """
    Format standard yfinance ticker with suffix (e.g. 'RELIANCE.NS', '500112.BO').
    """
    clean = normalize_symbol(symbol)
    if exchange.upper() == "BSE" or clean.isdigit():
        return f"{clean}.BO"
    return f"{clean}.NS"
