"""
Custom Ratios Engine: Screener.in Ratio Search, Piotroski 9-Point Score,
Altman Z-Score, Graham Number, Enterprise Value, and Quantitative Ratio Calculations.
"""

from typing import Any, Dict, List, Optional
import math
import httpx
import pandas as pd


class ScreenerRatioSearch:
    """
    Client for searching Screener.in's ratio dictionary & custom formula catalog.
    """

    BASE_URL = "https://www.screener.in/api/ratio/search/"
    HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

    @classmethod
    def search(cls, query: str, timeout: float = 10.0) -> List[Dict[str, str]]:
        """
        Search ratio dictionary on Screener.in.

        Returns list of dicts with keys: 'name', 'description', 'unit'.
        """
        if not query:
            return []
        try:
            with httpx.Client(headers=cls.HEADERS, timeout=timeout, follow_redirects=True) as client:
                r = client.get(cls.BASE_URL, params={"q": query})
                if r.status_code == 200:
                    return r.json()
        except Exception:
            pass
        return []


class CustomRatiosCalculator:
    """
    Computes standard and advanced institutional investment ratios from financial statements.
    """

    @classmethod
    def calculate_piotroski_score(cls, pnl: pd.DataFrame, bs: pd.DataFrame, cf: pd.DataFrame) -> Dict[str, Any]:
        """
        Compute Joseph Piotroski 9-Point F-Score:
        - Profitability (4 points): Positive Net Income, Positive ROA, Positive CFO, CFO > Net Income (Quality of earnings)
        - Leverage & Liquidity (3 points): Lower Leverage YoY, Higher Current Ratio YoY, No Share Dilution YoY
        - Operating Efficiency (2 points): Higher Gross Margin YoY, Higher Asset Turnover YoY
        """
        score = 0
        breakdown = {}

        if pnl.empty or len(pnl.columns) < 2:
            return {"score": 0, "breakdown": {}}

        # Get latest 2 periods
        curr_col = pnl.columns[-2] if pnl.columns[-1] == "TTM" and len(pnl.columns) >= 3 else pnl.columns[-1]
        prev_col = pnl.columns[-3] if pnl.columns[-1] == "TTM" and len(pnl.columns) >= 3 else pnl.columns[-2]

        def get_val(df: pd.DataFrame, row: str, col: str, default: float = 0.0) -> float:
            if df.empty or row not in df.index or col not in df.columns:
                return default
            val = df.loc[row, col]
            try:
                return float(val) if val is not None and not pd.isna(val) else default
            except Exception:
                return default

        # 1. Profitability
        net_profit_curr = get_val(pnl, "Net Profit", curr_col)
        tot_assets_curr = get_val(bs, "Total Assets", curr_col) or (get_val(bs, "Fixed Assets", curr_col) + get_val(bs, "Other Assets", curr_col)) or 1.0
        tot_assets_prev = get_val(bs, "Total Assets", prev_col) or (get_val(bs, "Fixed Assets", prev_col) + get_val(bs, "Other Assets", prev_col)) or 1.0

        roa_curr = net_profit_curr / tot_assets_curr if tot_assets_curr else 0.0
        net_profit_prev = get_val(pnl, "Net Profit", prev_col)
        roa_prev = net_profit_prev / tot_assets_prev if tot_assets_prev else 0.0

        cfo_curr = get_val(cf, "Cash from Operating Activity", curr_col)

        # F1: Positive Net Income
        f1 = net_profit_curr > 0
        # F2: Positive ROA
        f2 = roa_curr > 0
        # F3: Positive Operating Cash Flow
        f3 = cfo_curr > 0
        # F4: Cash Flow from Operations > Net Income (Accruals)
        f4 = cfo_curr > net_profit_curr

        # 2. Leverage & Liquidity
        debt_curr = get_val(bs, "Borrowings", curr_col)
        debt_prev = get_val(bs, "Borrowings", prev_col)
        f5 = debt_curr <= debt_prev # Lower or flat debt

        other_assets_curr = get_val(bs, "Other Assets", curr_col)
        other_liab_curr = get_val(bs, "Other Liabilities", curr_col) or 1.0
        cr_curr = other_assets_curr / other_liab_curr if other_liab_curr else 1.0

        other_assets_prev = get_val(bs, "Other Assets", prev_col)
        other_liab_prev = get_val(bs, "Other Liabilities", prev_col) or 1.0
        cr_prev = other_assets_prev / other_liab_prev if other_liab_prev else 1.0
        f6 = cr_curr >= cr_prev # Higher liquidity

        eq_curr = get_val(bs, "Equity Capital", curr_col)
        eq_prev = get_val(bs, "Equity Capital", prev_col)
        f7 = eq_curr <= eq_prev # No share dilution

        # 3. Operating Efficiency
        sales_curr = get_val(pnl, "Sales", curr_col)
        sales_prev = get_val(pnl, "Sales", prev_col)
        exp_curr = get_val(pnl, "Expenses", curr_col)
        exp_prev = get_val(pnl, "Expenses", prev_col)

        gm_curr = (sales_curr - exp_curr) / sales_curr if sales_curr else 0.0
        gm_prev = (sales_prev - exp_prev) / sales_prev if sales_prev else 0.0
        f8 = gm_curr >= gm_prev # Expanding margin

        at_curr = sales_curr / tot_assets_curr if tot_assets_curr else 0.0
        at_prev = sales_prev / tot_assets_prev if tot_assets_prev else 0.0
        f9 = at_curr >= at_prev # Expanding asset turnover

        points = [f1, f2, f3, f4, f5, f6, f7, f8, f9]
        score = sum(1 for p in points if p)

        breakdown = {
            "positive_net_income": f1,
            "positive_roa": f2,
            "positive_cfo": f3,
            "cfo_exceeds_pat": f4,
            "lower_leverage": f5,
            "higher_current_ratio": f6,
            "no_equity_dilution": f7,
            "higher_gross_margin": f8,
            "higher_asset_turnover": f9,
        }

        return {"score": score, "max_score": 9, "breakdown": breakdown}

    @classmethod
    def calculate_graham_number(cls, eps: Optional[float], book_value: Optional[float]) -> Optional[float]:
        """
        Calculate Benjamin Graham Number = Sqrt(22.5 * EPS * BookValue).
        """
        if eps is None or book_value is None or eps <= 0 or book_value <= 0:
            return None
        return math.sqrt(22.5 * eps * book_value)

    @classmethod
    def calculate_all_custom_ratios(
        cls,
        market_cap_cr: Optional[float],
        current_price: Optional[float],
        trailing_pe: Optional[float],
        book_value: Optional[float],
        eps: Optional[float],
        pnl: pd.DataFrame,
        bs: pd.DataFrame,
        cf: pd.DataFrame,
    ) -> Dict[str, Any]:
        """
        Compile complete dictionary of custom investment ratios.
        """
        ratios: Dict[str, Any] = {}

        # 1. Piotroski F-Score
        piot = cls.calculate_piotroski_score(pnl, bs, cf)
        ratios["piotroski_score"] = piot["score"]
        ratios["piotroski_breakdown"] = piot["breakdown"]

        # 2. Graham Number
        effective_eps = eps
        if (effective_eps is None or effective_eps <= 0) and not pnl.empty and "EPS in Rs" in pnl.index:
            try:
                effective_eps = float(pnl.loc["EPS in Rs"].iloc[-1] or 0.0)
            except Exception:
                pass
        if (effective_eps is None or effective_eps <= 0) and current_price and trailing_pe and trailing_pe > 0:
            effective_eps = current_price / trailing_pe

        ratios["graham_number"] = cls.calculate_graham_number(effective_eps, book_value)
        if ratios["graham_number"] and current_price:
            ratios["graham_upside_%"] = round(((ratios["graham_number"] - current_price) / current_price) * 100, 2)

        # 3. Enterprise Value
        latest_debt = 0.0
        if not bs.empty and "Borrowings" in bs.index:
            latest_debt = float(bs.loc["Borrowings"].iloc[-1] or 0.0)
        
        mcap = market_cap_cr or 0.0
        ev = mcap + latest_debt
        ratios["enterprise_value_cr"] = round(ev, 2)

        # 4. EV / EBITDA
        if not pnl.empty and "Operating Profit" in pnl.index:
            ebitda = float(pnl.loc["Operating Profit"].iloc[-1] or 0.0)
            if ebitda > 0:
                ratios["ev_to_ebitda"] = round(ev / ebitda, 2)

        # 5. Interest Coverage Ratio
        if not pnl.empty and "Operating Profit" in pnl.index and "Interest" in pnl.index:
            ebit = float(pnl.loc["Operating Profit"].iloc[-1] or 0.0)
            interest = float(pnl.loc["Interest"].iloc[-1] or 0.0)
            if interest > 0:
                ratios["interest_coverage"] = round(ebit / interest, 2)

        # 6. Debt to Equity
        if not bs.empty and "Borrowings" in bs.index and "Reserves" in bs.index and "Equity Capital" in bs.index:
            debt = float(bs.loc["Borrowings"].iloc[-1] or 0.0)
            net_worth = float(bs.loc["Reserves"].iloc[-1] or 0.0) + float(bs.loc["Equity Capital"].iloc[-1] or 0.0)
            if net_worth > 0:
                ratios["debt_to_equity"] = round(debt / net_worth, 2)

        # 7. Quality of Earnings (CFO / PAT)
        if not cf.empty and not pnl.empty and "Cash from Operating Activity" in cf.index and "Net Profit" in pnl.index:
            cfo = float(cf.loc["Cash from Operating Activity"].iloc[-1] or 0.0)
            pat = float(pnl.loc["Net Profit"].iloc[-1] or 0.0)
            if pat > 0:
                ratios["cfo_to_pat"] = round(cfo / pat, 2)

        # 8. Free Cash Flow (CFO - Capex)
        if not cf.empty and "Cash from Operating Activity" in cf.index and "Cash from Investing Activity" in cf.index:
            cfo = float(cf.loc["Cash from Operating Activity"].iloc[-1] or 0.0)
            cfi = float(cf.loc["Cash from Investing Activity"].iloc[-1] or 0.0)
            fcf = cfo + cfi # CFI is negative for capex
            ratios["free_cash_flow_cr"] = round(fcf, 2)

        return ratios
