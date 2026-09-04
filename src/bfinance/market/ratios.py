"""
Custom Ratios Engine: Screener.in Ratio Search, Piotroski 9-Point Score,
Altman Z-Score, Graham Number, Enterprise Value, and Quantitative Ratio Calculations.
"""

from typing import Any, Dict, List, Optional
import math
import httpx
import pandas as pd


def _safe_float(v: Any, default: float = 0.0) -> float:
    """Coerce to float; None, NaN, inf and non-numeric strings map to default."""
    try:
        if v is None:
            return default
        try:
            if pd.isna(v):
                return default
        except Exception:
            return default
        if isinstance(v, str):
            s = v.strip().replace(",", "")
            if s in ("", "\u2014", "\u2013", "-", "nan", "NaN", "None", "null", "N/A", "NA", "nan%"):
                return default
            try:
                f = float(s)
            except Exception:
                return default
            if isinstance(f, float) and (math.isnan(f) or math.isinf(f)):
                return default
            return f
        try:
            f = float(v)
        except Exception:
            return default
        if isinstance(f, float) and (math.isnan(f) or math.isinf(f)):
            return default
        return f
    except Exception:
        return default


def has(df: pd.DataFrame, row: str, col: str) -> bool:
    """True when row/col exist and value is present and not NaN."""
    try:
        if df is None or getattr(df, "empty", True):
            return False
        if row not in df.index:
            return False
        if col not in df.columns:
            return False
        v = df.loc[row, col]
        try:
            if pd.isna(v):
                return False
        except Exception:
            return False
        if v is None:
            return False
        return True
    except Exception:
        return False


def latest_pair(df: pd.DataFrame):
    """Return (curr, prev) columns, TTM-aware per statement frame."""
    cols = list(df.columns)
    if len(cols) >= 3 and cols[-1] == "TTM":
        return (cols[-2], cols[-3])
    return (cols[-1], cols[-2])


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
    def _total_assets(cls, bs: pd.DataFrame, col: str) -> Optional[float]:
        """Total Assets with Fixed plus Other fallback, else None."""
        try:
            if has(bs, "Total Assets", col):
                v = _safe_float(bs.loc["Total Assets", col], default=float("nan"))
                if v is not None and not (isinstance(v, float) and math.isnan(v)):
                    return float(v)
            if has(bs, "Fixed Assets", col) and has(bs, "Other Assets", col):
                fa = _safe_float(bs.loc["Fixed Assets", col], default=float("nan"))
                oa = _safe_float(bs.loc["Other Assets", col], default=float("nan"))
                if fa is None or oa is None:
                    return None
                try:
                    if math.isnan(float(fa)) or math.isnan(float(oa)):
                        return None
                except Exception:
                    return None
                return float(fa) + float(oa)
        except Exception:
            return None
        return None

    @classmethod
    def calculate_piotroski_score(cls, pnl: pd.DataFrame, bs: pd.DataFrame, cf: pd.DataFrame) -> Dict[str, Any]:
        """
        Compute Joseph Piotroski 9-Point F-Score:
        - Profitability (4 points): Positive Net Income, Positive ROA, Positive CFO, CFO > Net Income (Quality of earnings)
        - Leverage & Liquidity (3 points): Lower Leverage YoY, Higher Current Ratio YoY, No Share Dilution YoY
        - Operating Efficiency (2 points): Higher Gross Margin YoY, Higher Asset Turnover YoY
        """
        notes: List[str] = []

        def _cols_ok(df: pd.DataFrame) -> bool:
            try:
                if df is None or getattr(df, "empty", True):
                    return False
                cols = list(df.columns)
                if len(cols) < 2:
                    return False
                if cols[-1] == "TTM" and len(cols) < 3:
                    return False
                return True
            except Exception:
                return False

        if not (_cols_ok(pnl) and _cols_ok(bs) and _cols_ok(cf)):
            if not _cols_ok(pnl):
                notes.append("pnl needs at least 2 comparable columns")
            if not _cols_ok(bs):
                notes.append("bs needs at least 2 comparable columns")
            if not _cols_ok(cf):
                notes.append("cf needs at least 2 comparable columns")
            return {"score": 0, "max_score": 9, "breakdown": {}, "notes": notes}

        curr_p, prev_p = latest_pair(pnl)
        curr_b, prev_b = latest_pair(bs)
        curr_c, prev_c = latest_pair(cf)

        # Total assets per balance-sheet frame (no fabrication)
        ta_curr = cls._total_assets(bs, curr_b)
        ta_prev = cls._total_assets(bs, prev_b)
        assets_ok = (
            ta_curr is not None
            and ta_prev is not None
            and ta_curr > 0
            and ta_prev > 0
        )
        if not assets_ok:
            notes.append("assets unavailable: Total Assets missing and Fixed plus Other fallback failed")

        # F1: Positive Net Income
        if has(pnl, "Net Profit", curr_p):
            net_curr = _safe_float(pnl.loc["Net Profit", curr_p], default=0.0)
            f1 = bool(net_curr > 0)
        else:
            net_curr = 0.0
            f1 = False
            notes.append("Net Profit curr missing")

        # F2: Positive ROA on beginning assets
        if has(pnl, "Net Profit", curr_p) and assets_ok:
            try:
                f2 = bool((_safe_float(pnl.loc["Net Profit", curr_p], default=0.0) / float(ta_prev)) > 0)
            except Exception:
                f2 = False
                notes.append("ROA computation failed")
        else:
            f2 = False
            notes.append("ROA needs beginning Total Assets")

        # F3: Positive Operating Cash Flow
        if has(cf, "Cash from Operating Activity", curr_c):
            cfo_curr = _safe_float(cf.loc["Cash from Operating Activity", curr_c], default=0.0)
            f3 = bool(cfo_curr > 0)
        else:
            cfo_curr = 0.0
            f3 = False
            notes.append("CFO curr missing")

        # F4: CFO exceeds PAT
        if has(cf, "Cash from Operating Activity", curr_c) and has(pnl, "Net Profit", curr_p):
            f4 = bool(cfo_curr > net_curr)
        else:
            f4 = False
            notes.append("accruals check needs CFO and Net Profit")

        # F5: Lower leverage via Borrowings over Assets ratio
        if assets_ok and has(bs, "Borrowings", curr_b) and has(bs, "Borrowings", prev_b):
            try:
                d_curr = _safe_float(bs.loc["Borrowings", curr_b], default=0.0)
                d_prev = _safe_float(bs.loc["Borrowings", prev_b], default=0.0)
                lev_curr = float(d_curr) / float(ta_curr) if float(ta_curr) else float("inf")
                lev_prev = float(d_prev) / float(ta_prev) if float(ta_prev) else float("inf")
                f5 = bool(lev_curr <= lev_prev)
            except Exception:
                f5 = False
                notes.append("leverage ratio computation failed")
        else:
            f5 = False
            notes.append("leverage needs Borrowings and assets for both periods")

        # F6: Higher current ratio from real Current rows
        if (
            has(bs, "Current Assets", curr_b)
            and has(bs, "Current Assets", prev_b)
            and has(bs, "Current Liabilities", curr_b)
            and has(bs, "Current Liabilities", prev_b)
        ):
            try:
                ca_c = _safe_float(bs.loc["Current Assets", curr_b], default=0.0)
                cl_c = _safe_float(bs.loc["Current Liabilities", curr_b], default=0.0)
                ca_p = _safe_float(bs.loc["Current Assets", prev_b], default=0.0)
                cl_p = _safe_float(bs.loc["Current Liabilities", prev_b], default=0.0)
                if cl_c and cl_p:
                    f6 = bool((float(ca_c) / float(cl_c)) >= (float(ca_p) / float(cl_p)))
                else:
                    f6 = False
                    notes.append("current liabilities zero")
            except Exception:
                f6 = False
                notes.append("current ratio computation failed")
        else:
            f6 = False
            notes.append("current ratio needs Current Assets and Current Liabilities rows")

        # F7: No equity dilution
        if has(bs, "Equity Capital", curr_b) and has(bs, "Equity Capital", prev_b):
            eq_curr = _safe_float(bs.loc["Equity Capital", curr_b], default=0.0)
            eq_prev = _safe_float(bs.loc["Equity Capital", prev_b], default=0.0)
            f7 = bool(eq_curr <= eq_prev)
        else:
            f7 = False
            notes.append("Equity Capital missing")

        # F8: Higher margin, OP over Sales when available else Sales minus Expenses over Sales
        sales_ok = (
            has(pnl, "Sales", curr_p)
            and has(pnl, "Sales", prev_p)
        )
        s_curr = _safe_float(pnl.loc["Sales", curr_p], default=0.0) if has(pnl, "Sales", curr_p) else 0.0
        s_prev = _safe_float(pnl.loc["Sales", prev_p], default=0.0) if has(pnl, "Sales", prev_p) else 0.0
        if not sales_ok or not (s_curr > 0 and s_prev > 0):
            f8 = False
            notes.append("margin needs positive Sales for both periods")
        elif has(pnl, "Operating Profit", curr_p) and has(pnl, "Operating Profit", prev_p):
            op_c = _safe_float(pnl.loc["Operating Profit", curr_p], default=0.0)
            op_p = _safe_float(pnl.loc["Operating Profit", prev_p], default=0.0)
            f8 = bool((float(op_c) / float(s_curr)) >= (float(op_p) / float(s_prev)))
        elif (
            has(pnl, "Sales", curr_p)
            and has(pnl, "Sales", prev_p)
            and has(pnl, "Expenses", curr_p)
            and has(pnl, "Expenses", prev_p)
        ):
            e_c = _safe_float(pnl.loc["Expenses", curr_p], default=0.0)
            e_p = _safe_float(pnl.loc["Expenses", prev_p], default=0.0)
            gm_c = (float(s_curr) - float(e_c)) / float(s_curr) if float(s_curr) else float("-inf")
            gm_p = (float(s_prev) - float(e_p)) / float(s_prev) if float(s_prev) else float("-inf")
            f8 = bool(gm_c >= gm_p)
        else:
            f8 = False
            notes.append("margin needs Operating Profit with Sales with Expenses fallback")

        # F9: Higher asset turnover, needs assets and sales
        if assets_ok and has(pnl, "Sales", curr_p) and has(pnl, "Sales", prev_p):
            try:
                at_curr = float(s_curr) / float(ta_curr) if float(ta_curr) else float("-inf")
                at_prev = float(s_prev) / float(ta_prev) if float(ta_prev) else float("-inf")
                f9 = bool(at_curr >= at_prev)
            except Exception:
                f9 = False
                notes.append("turnover computation failed")
        else:
            f9 = False
            notes.append("turnover needs Sales and assets for both periods")

        points = [f1, f2, f3, f4, f5, f6, f7, f8, f9]
        score = sum(1 for p in points if p)

        breakdown = {
            "positive_net_income": bool(f1),
            "positive_roa": bool(f2),
            "positive_cfo": bool(f3),
            "cfo_exceeds_pat": bool(f4),
            "lower_leverage": bool(f5),
            "higher_current_ratio": bool(f6),
            "no_equity_dilution": bool(f7),
            "higher_gross_margin": bool(f8),
            "higher_asset_turnover": bool(f9),
            "notes": list(notes),
        }

        return {"score": score, "max_score": 9, "breakdown": breakdown, "notes": list(notes)}

    @classmethod
    def calculate_graham_number(cls, eps: Optional[float], book_value: Optional[float]) -> Optional[float]:
        """
        Calculate Benjamin Graham Number = Sqrt(22.5 * EPS * BookValue).
        """
        try:
            if eps is None or book_value is None:
                return None
            try:
                if pd.isna(eps) or pd.isna(book_value):
                    return None
            except Exception:
                pass
            ef = _safe_float(eps, default=float("nan"))
            bf = _safe_float(book_value, default=float("nan"))
            try:
                if math.isnan(float(ef)) or math.isnan(float(bf)):
                    return None
            except Exception:
                return None
            if float(ef) <= 0 or float(bf) <= 0:
                return None
            return math.sqrt(22.5 * float(ef) * float(bf))
        except Exception:
            return None

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
        ratios["piotroski_max_score"] = piot.get("max_score", 9)
        ratios["piotroski_notes"] = piot.get("notes", [])

        # 2. Graham Number
        effective_eps = _safe_float(eps, default=0.0) if eps is not None else None
        try:
            if pd.isna(effective_eps):
                effective_eps = None
        except Exception:
            pass
        if (effective_eps is None or effective_eps <= 0) and not pnl.empty and "EPS in Rs" in pnl.index:
            try:
                raw_eps = pnl.loc["EPS in Rs"].iloc[-1]
                v = _safe_float(raw_eps, default=0.0)
                if v > 0:
                    effective_eps = v
            except Exception:
                pass
        cp_safe = _safe_float(current_price, default=0.0) if current_price is not None else 0.0
        tp_safe = _safe_float(trailing_pe, default=0.0) if trailing_pe is not None else 0.0
        if (effective_eps is None or effective_eps <= 0) and cp_safe > 0 and tp_safe > 0:
            effective_eps = cp_safe / tp_safe

        ratios["graham_number"] = cls.calculate_graham_number(effective_eps, book_value)
        if ratios["graham_number"] and cp_safe > 0:
            ratios["graham_upside_%"] = round(((ratios["graham_number"] - cp_safe) / cp_safe) * 100, 2)

        # 3. Enterprise Value
        latest_debt = 0.0
        if not bs.empty and "Borrowings" in bs.index:
            try:
                latest_debt = _safe_float(bs.loc["Borrowings"].iloc[-1], default=0.0)
            except Exception:
                latest_debt = 0.0

        cash_hold = 0.0
        if not bs.empty:
            if "Cash Equivalents" in bs.index:
                try:
                    cash_hold = _safe_float(bs.loc["Cash Equivalents"].iloc[-1], default=0.0)
                except Exception:
                    cash_hold = 0.0
            elif "Cash and Bank Balance" in bs.index:
                try:
                    cash_hold = _safe_float(bs.loc["Cash and Bank Balance"].iloc[-1], default=0.0)
                except Exception:
                    cash_hold = 0.0
            # cash stays zero when neither cash row is present

        mcap = _safe_float(market_cap_cr, default=0.0) if market_cap_cr is not None else 0.0
        ev = mcap + latest_debt - cash_hold
        ratios["enterprise_value_cr"] = round(ev, 2)
        ratios["cash_and_equivalents_cr"] = round(cash_hold, 2)

        # 4. EV / EBITDA
        if not pnl.empty and "Operating Profit" in pnl.index:
            try:
                op_val = _safe_float(pnl.loc["Operating Profit"].iloc[-1], default=0.0)
                dep_val = 0.0
                if "Depreciation" in pnl.index:
                    dep_val = _safe_float(pnl.loc["Depreciation"].iloc[-1], default=0.0)
                # add back depreciation; when absent use operating profit alone
                ebitda = op_val + dep_val
                if ebitda > 0:
                    ratios["ev_to_ebitda"] = round(ev / ebitda, 2)
            except Exception:
                pass

        # 5. Interest Coverage Ratio
        if not pnl.empty and "Operating Profit" in pnl.index and "Interest" in pnl.index:
            try:
                op2 = _safe_float(pnl.loc["Operating Profit"].iloc[-1], default=0.0)
                oi2 = _safe_float(pnl.loc["Other Income"].iloc[-1], default=0.0) if "Other Income" in pnl.index else 0.0
                # EBIT adds other income to operating profit
                ebit = op2 + oi2
                interest = _safe_float(pnl.loc["Interest"].iloc[-1], default=0.0)
                if interest > 0:
                    ratios["interest_coverage"] = round(ebit / interest, 2)
                # omit key when interest is zero or negative
            except Exception:
                pass

        # 6. Debt to Equity
        if not bs.empty and "Borrowings" in bs.index and "Reserves" in bs.index and "Equity Capital" in bs.index:
            try:
                debt = _safe_float(bs.loc["Borrowings"].iloc[-1], default=0.0)
                res = _safe_float(bs.loc["Reserves"].iloc[-1], default=0.0)
                eqc = _safe_float(bs.loc["Equity Capital"].iloc[-1], default=0.0)
                net_worth = res + eqc
                if net_worth > 0:
                    ratios["debt_to_equity"] = round(debt / net_worth, 2)
            except Exception:
                pass

        # 7. Quality of Earnings (CFO / PAT)
        if not cf.empty and not pnl.empty and "Cash from Operating Activity" in cf.index and "Net Profit" in pnl.index:
            try:
                cfo = _safe_float(cf.loc["Cash from Operating Activity"].iloc[-1], default=0.0)
                pat = _safe_float(pnl.loc["Net Profit"].iloc[-1], default=0.0)
                if pat > 0:
                    ratios["cfo_to_pat"] = round(cfo / pat, 2)
            except Exception:
                pass

        # 8. Free Cash Flow (CFO - Capex)
        if not cf.empty and "Cash from Operating Activity" in cf.index and "Cash from Investing Activity" in cf.index:
            try:
                cfo2 = _safe_float(cf.loc["Cash from Operating Activity"].iloc[-1], default=0.0)
                cfi2 = _safe_float(cf.loc["Cash from Investing Activity"].iloc[-1], default=0.0)
                # keep only outflows; positive inflows such as asset sales are ignored
                fcf = cfo2 + min(cfi2, 0.0)
                ratios["free_cash_flow_cr"] = round(fcf, 2)
            except Exception:
                pass

        return ratios
