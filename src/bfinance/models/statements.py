"""
Financial statement models and DataFrame serializers.

Native shape (downstream finengine reads this — do not change):
  orientation: metric rows x period-string columns (e.g. "Mar 2024").
  row labels: TitleCase Indian labels ("Sales", "Net Profit", "Borrowings").
  units: values in Rs Cr (1 Cr = 1e7 Rs).

YFinance shape via ``FinancialStatement.to_yfinance()``:
  same orientation (metric rows x period columns), columns parsed to
  DatetimeIndex (month-end dates), values scaled x1e7 to absolute Rs,
  Indian labels renamed to generic yfinance labels where a mapping exists
  (unmapped rows kept as-is).
"""

from typing import Dict, List, Optional
from pydantic import BaseModel, Field
import pandas as pd


#: Indian (Screener.in) row label -> generic yfinance-style row label.
INDIAN_TO_YFINANCE_LABELS: Dict[str, str] = {
    "Sales": "Total Revenue",
    "Expenses": "Total Expenses",
    "Operating Profit": "Operating Income",
    "Other Income": "Other Income Non Operating",
    "Interest": "Interest Expense",
    "Depreciation": "Depreciation And Amortization",
    "Profit before tax": "Pretax Income",
    "Tax %": "Tax Rate",
    "Net Profit": "Net Income",
    "EPS in Rs": "Diluted EPS",
    "EPS": "Diluted EPS",
    "Equity Capital": "Common Stock",
    "Reserves": "Retained Earnings",
    "Borrowings": "Total Debt",
    "Other Liabilities": "Other Liabilities",
    "Total Liabilities": "Total Liabilities",
    "Fixed Assets": "Net PPE",
    "CWIP": "Capital Work In Progress",
    "Investments": "Investments And Advances",
    "Other Assets": "Other Assets",
    "Total Assets": "Total Assets",
    "Cash from Operating Activity": "Operating Cash Flow",
    "Cash from Investing Activity": "Investing Cash Flow",
    "Cash from Financing Activity": "Financing Cash Flow",
    "Net Cash Flow": "Net Cash Flow",
}


class FinancialStatement(BaseModel):
    """
    Generic financial table model (P&L, Balance Sheet, Cash Flow, Quarters, Ratios).
    """
    headers: List[str] = Field(default_factory=list)
    rows: Dict[str, List[Optional[float]]] = Field(default_factory=dict)

    @property
    def empty(self) -> bool:
        """True if the financial statement contains no data."""
        return not self.headers or not self.rows

    def to_dataframe(self, orient: str = "columns") -> pd.DataFrame:
        """
        Convert financial statement to pandas DataFrame.
        orient='columns' (default, matches yfinance): rows are metrics, columns are period dates.
        orient='index': rows are periods, columns are metrics.
        """
        if not self.headers or not self.rows:
            return pd.DataFrame()

        # Build raw table
        df = pd.DataFrame(self.rows, index=self.headers).T
        if orient == "index":
            return df.T
        return df

    def get_metric(self, metric_name: str) -> Dict[str, Optional[float]]:
        """Get metric values mapped by period."""
        if metric_name not in self.rows:
            # Case-insensitive match
            for k, v in self.rows.items():
                if k.lower() == metric_name.lower():
                    return dict(zip(self.headers, v))
            return {}
        return dict(zip(self.headers, self.rows[metric_name]))

    def to_yfinance(self) -> pd.DataFrame:
        """Return yfinance-shaped copy: DatetimeIndex cols, absolute Rs, generic labels."""
        if not self.headers or not self.rows:
            return pd.DataFrame()
        df = self.to_dataframe(orient="columns")
        df = df.rename(index=INDIAN_TO_YFINANCE_LABELS)
        dates, keep = [], []
        for col in df.columns:
            try:
                dt = pd.to_datetime(f"01 {col}", format="%d %b %Y") + pd.offsets.MonthEnd(1)
            except Exception:
                continue
            dates.append(dt)
            keep.append(col)
        if not keep:
            return pd.DataFrame()
        df = df[keep].copy()
        df.columns = pd.DatetimeIndex(dates)
        df = df.apply(pd.to_numeric, errors="coerce").dropna(how="all")
        df = df * 1e7
        for alias in ("Net Income Including Noncontrolling Interests", "Net Income Continuous Operations"):
            if alias not in df.index and "Net Income" in df.index:
                df.loc[alias] = df.loc["Net Income"]
        return df

    def __repr__(self) -> str:
        return f"<FinancialStatement periods={len(self.headers)} metrics={len(self.rows)}>"
