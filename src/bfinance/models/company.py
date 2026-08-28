"""
Company profile, fundamental ratios, qualitative insights, and concall data models.
"""

from typing import Dict, List, Optional, Any
from pydantic import BaseModel, Field
import pandas as pd
from .statements import FinancialStatement


class TopRatios(BaseModel):
    """Snapshot of primary fundamental valuation & quality ratios."""
    market_cap: Optional[float] = Field(None, description="Market Capitalization in ₹ Cr")
    current_price: Optional[float] = Field(None, description="Current Market Price (CMP) in ₹")
    high_52w: Optional[float] = Field(None, description="52-Week High in ₹")
    low_52w: Optional[float] = Field(None, description="52-Week Low in ₹")
    stock_pe: Optional[float] = Field(None, description="Price to Earnings (TTM P/E)")
    book_value: Optional[float] = Field(None, description="Book Value per Share in ₹")
    dividend_yield: Optional[float] = Field(None, description="Dividend Yield %")
    roce: Optional[float] = Field(None, description="Return on Capital Employed (ROCE %)")
    roe: Optional[float] = Field(None, description="Return on Equity (ROE %)")
    face_value: Optional[float] = Field(None, description="Face Value in ₹")
    debt_to_equity: Optional[float] = Field(None, description="Debt to Equity Ratio")
    peg_ratio: Optional[float] = Field(None, description="PEG Ratio")
    price_to_book: Optional[float] = Field(None, description="Price to Book (P/B)")
    eps_ttm: Optional[float] = Field(None, description="Trailing EPS in ₹")
    promoter_holding: Optional[float] = Field(None, description="Promoter Holding %")
    promoter_pledged: Optional[float] = Field(None, description="Promoter Pledged %")
    free_cash_flow_3y: Optional[float] = Field(None, description="3-Year Free Cash Flow in ₹ Cr")
    custom_ratios: Dict[str, Optional[float]] = Field(default_factory=dict)


class AnalysisInsights(BaseModel):
    """Automated qualitative insights (Pros and Cons)."""
    pros: List[str] = Field(default_factory=list)
    cons: List[str] = Field(default_factory=list)


class Concall(BaseModel):
    """Conference call transcript, audio recording, and investor presentation."""
    date: str
    quarter: Optional[str] = None
    title: str
    transcript_url: Optional[str] = None
    audio_url: Optional[str] = None
    presentation_url: Optional[str] = None


class PeerStock(BaseModel):
    """Industry peer comparison item."""
    rank: int
    name: str
    symbol: Optional[str] = None
    cmp: Optional[float] = None
    pe: Optional[float] = None
    market_cap_cr: Optional[float] = None
    dividend_yield: Optional[float] = None
    net_profit_qtr: Optional[float] = None
    qtr_profit_var: Optional[float] = None
    sales_qtr: Optional[float] = None
    qtr_sales_var: Optional[float] = None
    roce: Optional[float] = None


class CompanyProfile(BaseModel):
    """Complete company profile encapsulating all fundamental, qualitative, and sector data."""
    symbol: str
    company_id: int
    name: str
    about: str = ""
    website: Optional[str] = None
    bse_code: Optional[str] = None
    nse_symbol: Optional[str] = None
    is_consolidated: bool = True
    url: str = ""
    sector: Optional[str] = None
    industry_group: Optional[str] = None
    industry: Optional[str] = None
    sub_industry: Optional[str] = None
    indices: List[str] = Field(default_factory=list)
    ratios: TopRatios = Field(default_factory=TopRatios)
    analysis: AnalysisInsights = Field(default_factory=AnalysisInsights)
    cagrs: Dict[str, Dict[str, str]] = Field(default_factory=dict)
    quarters: FinancialStatement = Field(default_factory=FinancialStatement)
    profit_loss: FinancialStatement = Field(default_factory=FinancialStatement)
    balance_sheet: FinancialStatement = Field(default_factory=FinancialStatement)
    cash_flow: FinancialStatement = Field(default_factory=FinancialStatement)
    ratios_history: FinancialStatement = Field(default_factory=FinancialStatement)
    shareholding: FinancialStatement = Field(default_factory=FinancialStatement)
    shareholding_yearly: FinancialStatement = Field(default_factory=FinancialStatement)
    peers: List[PeerStock] = Field(default_factory=list)
    concalls: List[Concall] = Field(default_factory=list)
    annual_reports: List[Dict[str, str]] = Field(default_factory=list)
    credit_ratings: List[Dict[str, str]] = Field(default_factory=list)
    announcements: List[Dict[str, str]] = Field(default_factory=list)

    def peers_dataframe(self) -> pd.DataFrame:
        """Return peers table as a pandas DataFrame."""
        if not self.peers:
            return pd.DataFrame()
        return pd.DataFrame([p.model_dump() for p in self.peers])

    def concalls_dataframe(self) -> pd.DataFrame:
        """Return concall records as a pandas DataFrame."""
        if not self.concalls:
            return pd.DataFrame()
        return pd.DataFrame([c.model_dump() for c in self.concalls])
