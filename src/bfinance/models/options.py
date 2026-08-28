"""
Options chain and derivatives data models matching yfinance.
"""

from typing import NamedTuple, Optional, List
from pydantic import BaseModel, Field
import pandas as pd


class OptionContract(BaseModel):
    """Single call/put option contract record."""
    contractSymbol: str
    strike: float
    lastPrice: float
    bid: Optional[float] = None
    ask: Optional[float] = None
    change: Optional[float] = None
    percentChange: Optional[float] = None
    volume: Optional[int] = None
    openInterest: Optional[int] = None
    impliedVolatility: Optional[float] = None
    inTheMoney: Optional[bool] = None
    contractSize: str = "REGULAR"
    currency: str = "INR"


class OptionChain(NamedTuple):
    """Option chain container matching yfinance OptionChain (calls, puts)."""
    calls: pd.DataFrame
    puts: pd.DataFrame
