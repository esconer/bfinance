"""
Financial statement models and DataFrame serializers.
"""

from typing import Dict, List, Optional, Any
from pydantic import BaseModel, Field
import pandas as pd


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

    def __repr__(self) -> str:
        return f"<FinancialStatement periods={len(self.headers)} metrics={len(self.rows)}>"
