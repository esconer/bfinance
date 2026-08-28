"""
Financial Statement & Valuation Excel Exporter.
Generates multi-sheet professional financial model workbooks matching Screener.in's Export to Excel.
"""

from typing import Optional
from pathlib import Path
import pandas as pd

from bfinance.models.company import CompanyProfile


class FinancialModelExcelExporter:
    """
    Exports complete multi-year financial statements, quarters, shareholding, and valuation ratios to .xlsx.
    """

    @classmethod
    def export(cls, profile: CompanyProfile, filepath: str) -> str:
        """
        Export company financial statements to multi-tab Excel file.
        """
        target_path = Path(filepath)
        target_path.parent.mkdir(parents=True, exist_ok=True)

        with pd.ExcelWriter(str(target_path), engine="openpyxl") as writer:
            # 1. Overview & Key Ratios
            r = profile.ratios
            overview_data = [
                {"Metric": "Company Name", "Value": profile.name},
                {"Metric": "NSE Symbol", "Value": profile.nse_symbol or profile.symbol},
                {"Metric": "BSE Code", "Value": profile.bse_code or "N/A"},
                {"Metric": "Current Market Price (₹)", "Value": r.current_price},
                {"Metric": "Market Capitalization (₹ Cr)", "Value": r.market_cap},
                {"Metric": "52-Week High (₹)", "Value": r.high_52w},
                {"Metric": "52-Week Low (₹)", "Value": r.low_52w},
                {"Metric": "Stock P/E", "Value": r.stock_pe},
                {"Metric": "Book Value (₹)", "Value": r.book_value},
                {"Metric": "Dividend Yield (%)", "Value": r.dividend_yield},
                {"Metric": "ROCE (%)", "Value": r.roce},
                {"Metric": "ROE (%)", "Value": r.roe},
                {"Metric": "Face Value (₹)", "Value": r.face_value},
                {"Metric": "Website", "Value": profile.website or "N/A"},
                {"Metric": "Consolidated", "Value": "Yes" if profile.is_consolidated else "No"},
            ]
            pd.DataFrame(overview_data).to_excel(writer, sheet_name="Overview", index=False)

            # 2. Profit & Loss (10+ Years)
            pnl_df = profile.profit_loss.to_dataframe(orient="columns")
            if not pnl_df.empty:
                pnl_df.to_excel(writer, sheet_name="Profit & Loss")

            # 3. Quarters (12+ Quarters)
            q_df = profile.quarters.to_dataframe(orient="columns")
            if not q_df.empty:
                q_df.to_excel(writer, sheet_name="Quarters")

            # 4. Balance Sheet (10+ Years)
            bs_df = profile.balance_sheet.to_dataframe(orient="columns")
            if not bs_df.empty:
                bs_df.to_excel(writer, sheet_name="Balance Sheet")

            # 5. Cash Flow (10+ Years)
            cf_df = profile.cash_flow.to_dataframe(orient="columns")
            if not cf_df.empty:
                cf_df.to_excel(writer, sheet_name="Cash Flow")

            # 6. Shareholding Patterns
            sh_df = profile.shareholding.to_dataframe(orient="columns")
            if not sh_df.empty:
                sh_df.to_excel(writer, sheet_name="Shareholding")

            # 7. Ratios History
            rh_df = profile.ratios_history.to_dataframe(orient="columns")
            if not rh_df.empty:
                rh_df.to_excel(writer, sheet_name="Ratios History")

            # 8. Industry Peers
            peers_df = profile.peers_dataframe()
            if not peers_df.empty:
                peers_df.to_excel(writer, sheet_name="Peers", index=False)

        return str(target_path.resolve())
