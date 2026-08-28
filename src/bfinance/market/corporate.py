"""
Corporate actions extractor (dividends, stock splits, bonus issues).
"""

from typing import List, Optional
import pandas as pd

from bfinance.models.company import CompanyProfile
from bfinance.utils.formatting import parse_indian_number


class CorporateActionsEngine:
    """
    Extracts structured dividends and stock splits series from corporate history.
    """

    @classmethod
    def extract_dividends(cls, profile: CompanyProfile) -> pd.Series:
        """
        Extract dividend payout series matching yfinance `ticker.dividends`.
        Returns pd.Series indexed by DatetimeIndex with dividend amounts.
        """
        pnl = profile.profit_loss
        if not pnl.headers:
            return pd.Series(dtype=float, name="Dividends")

        payout_map = pnl.get_metric("Dividend Payout %")
        eps_map = pnl.get_metric("EPS in Rs") or pnl.get_metric("EPS")

        div_records = {}
        for period in pnl.headers:
            if period.upper() == "TTM":
                continue
            payout_pct = payout_map.get(period)
            eps_val = eps_map.get(period)

            if payout_pct is not None and eps_val is not None and payout_pct > 0:
                div_per_share = (payout_pct / 100.0) * eps_val
                # Parse period like 'Mar 2024' -> date '2024-03-31'
                try:
                    dt = pd.to_datetime(f"01 {period}", format="%d %b %Y") + pd.offsets.MonthEnd(1)
                    div_records[dt] = round(div_per_share, 2)
                except Exception:
                    pass

        if not div_records:
            return pd.Series(dtype=float, name="Dividends")

        s = pd.Series(div_records).sort_index()
        s.index.name = "Date"
        s.name = "Dividends"
        return s

    @classmethod
    def extract_splits(cls, profile: CompanyProfile) -> pd.Series:
        """
        Extract stock split / bonus series matching yfinance `ticker.splits`.
        """
        bs = profile.balance_sheet
        if not bs.headers:
            return pd.Series(dtype=float, name="Stock Splits")

        equity_map = bs.get_metric("Equity Capital")
        splits_records = {}

        prev_cap = None
        for period in bs.headers:
            curr_cap = equity_map.get(period)
            if curr_cap and prev_cap and prev_cap > 0:
                ratio = curr_cap / prev_cap
                # If equity capital doubled or grew by integer factor, mark as bonus/split
                if ratio >= 1.5:
                    try:
                        dt = pd.to_datetime(f"01 {period}", format="%d %b %Y") + pd.offsets.MonthEnd(1)
                        splits_records[dt] = round(ratio, 2)
                    except Exception:
                        pass
            if curr_cap:
                prev_cap = curr_cap

        if not splits_records:
            return pd.Series(dtype=float, name="Stock Splits")

        s = pd.Series(splits_records).sort_index()
        s.index.name = "Date"
        s.name = "Stock Splits"
        return s
