"""
Custom Ratios Engine: Screener.in Ratio Search, Piotroski 9-Point Score,
Altman Z-Score, Graham Number, Enterprise Value, and Quantitative Ratio Calculations.
"""

from typing import Any, Dict, List, Optional, Tuple
import math
import httpx
import pandas as pd

from bfinance.utils.exceptions import UpstreamServiceError

#: Row labels that would carry cash if the upstream balance sheet itemised it. It does not.
#: Measured on all three committed screener.in fixtures (``company_itc.html``,
#: ``company_tcs.html`` and ``company_itc_full_columns.html``, each read through
#: ``ScreenerHTMLParser._parse_table_section``), the balance-sheet table has exactly ten rows:
#:
#:     Equity Capital, Reserves, Borrowings, Other Liabilities, Total Liabilities,
#:     Fixed Assets, CWIP, Investments, Other Assets, Total Assets
#:
#: Cash is inside the undifferentiated "Other Assets" bucket, and
#: ``models.statements.INDIAN_TO_YFINANCE_LABELS`` has no cash entry either. So this is a *hook*,
#: not a lookup that works today: no real response ever matches it.
#:
#: It is still read, and it used to be *branched on* at module scope with a ``0.0`` fallback --
#: which is why the cash leg of every Enterprise Value this engine produced was an assumption and
#: not a measurement, and why the code read as though it handled a case the data never produces.
#: When no label matches, the cash leg is now reported as unknown and Enterprise Value is omitted;
#: when one does match, it is subtracted. See :func:`_cell`.
#:
#: This mirrors ``ticker._CASH_ROW_LABELS``. The duplicate is deliberate: ``bfinance.ticker`` is the
#: facade over this engine and must not be a dependency of it, and the two tuples are pinned equal
#: by ``tests/test_ratios_honesty.py::test_the_cash_hook_is_the_same_hook_in_both_modules``.
_CASH_ROW_LABELS: Tuple[str, ...] = ("Cash Equivalents", "Cash and Bank Balance")

#: The exceptions a single-cell statement read can actually raise. Narrowed from
#: ``except Exception`` -- see :func:`_cell` for which is which. A ``BFinanceError`` cannot appear
#: here: statement frames are materialised by the caller (see
#: :meth:`bfinance.models.statements.FinancialStatement.to_dataframe`, which raises
#: ``ParsingError`` on a ragged row) and a failure there propagates into the caller of
#: :meth:`CustomRatiosCalculator.calculate_all_custom_ratios` rather than reaching a cell read.
_CELL_READ_ERRORS: Tuple[type, ...] = (KeyError, IndexError, TypeError, ValueError)


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


def _cell(df: Any, row: str) -> Tuple[Optional[float], Optional[str]]:
    """
    Read the latest value of one statement row. Returns ``(value, reason)``.

    ``value`` is ``None`` **if and only if** ``reason`` is not ``None``. The pair is kept
    together for the reason the whole EV block exists: ``0.0`` and ``None`` are different
    facts, and a single return slot cannot tell them apart.

    * ``0.0`` -- the company reported zero. A debt-free company really is debt-free.
    * ``None`` -- the number is not in hand, and ``reason`` says why: the frame is empty, the
      row is absent, or the cell is not a number.

    That last case matters. The upstream prints ``-`` for a company with no borrowings, and
    ``_safe_float(..., default=0.0)`` turned it into ``0.0`` -- so "no borrowings" and "the
    borrowings figure could not be read" produced the same number, and a broken statement was
    indistinguishable from a clean one.

    The handler is narrowed from ``except Exception`` to :data:`_CELL_READ_ERRORS`: a label that
    vanished between the guard and the lookup is a ``KeyError``; an empty column selection is an
    ``IndexError``; a duplicate index label makes ``df.loc[row]`` a DataFrame rather than a
    Series, which is a ``TypeError`` at the coercion; a non-scalar ``pd.isna`` inside
    :func:`_safe_float` is a ``ValueError``. All four mean *this number is missing*, which is
    ``None`` plus a reason. Anything else is a bug in this package, and it propagates -- a
    swallowed ``AttributeError`` is how a wrong number once outlived the code that caused it.
    """
    if df is None:
        return None, f"no {row!r} row: the statement frame is None"
    if not isinstance(df, pd.DataFrame) or df.empty:
        return None, f"no {row!r} row: the statement frame is empty"
    if row not in df.index:
        return None, f"no {row!r} row in the statement"
    try:
        raw = df.loc[row].iloc[-1]
    except _CELL_READ_ERRORS as exc:
        return None, f"{row!r} row could not be read: {exc}"
    value = _safe_float(raw, default=float("nan"))
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None, f"{row!r} latest value {raw!r} is not a number"
    return float(value), None


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

        Raises:
            UpstreamServiceError: any non-200, a non-JSON body, or a transport failure.

        ⚠ **This endpoint is disallowed by screener.in's robots.txt** (``Disallow: /*?q=``) and
        therefore has no compliant route. The method is kept because it is public API, but it is
        no longer reachable from any internal code path, and it now **fails loudly** instead of
        returning an empty list.

        It previously swallowed every failure::

            except Exception:
                pass
            return []

        so a blocked request, a 404, and a genuine "no such ratio" were indistinguishable, and the
        caller received an empty catalog with no error and no log. A custom-ratio builder that
        silently offers zero ratios is worse than one that says it cannot reach the dictionary.
        """
        if not query:
            return []
        try:
            with httpx.Client(headers=cls.HEADERS, timeout=timeout, follow_redirects=True) as client:
                r = client.get(cls.BASE_URL, params={"q": query})
        except Exception as exc:
            raise UpstreamServiceError(
                f"Could not reach Screener.in's ratio dictionary ({cls.BASE_URL}): {exc}"
            ) from exc

        if r.status_code != 200:
            raise UpstreamServiceError(
                f"Screener.in ratio search returned HTTP {r.status_code} for {query!r}. "
                "This is an upstream failure, not an empty result set."
            )
        try:
            payload = r.json()
        except ValueError as exc:
            raise UpstreamServiceError(
                f"Screener.in ratio search returned a non-JSON body for {query!r} "
                f"({len(r.text)} bytes, content-type {r.headers.get('content-type')!r})."
            ) from exc
        return payload if isinstance(payload, list) else []


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

        **Enterprise Value is reported only when every leg of ``mcap + debt - cash`` is actually
        in hand.** A missing leg makes ``enterprise_value_cr`` and ``ev_to_ebitda`` ``None``, and
        ``bfinance_enterprise_value_omitted`` names the leg that is missing. The key is present
        with the value ``None`` rather than absent, because ``Ticker.enterprise_value`` reads it
        with ``.get("enterprise_value_cr", 0.0)`` -- omitting the key would hand that caller a
        fabricated ``0.0``, which is the defect in a different place.

        Returning nothing is the honest answer. The previous version returned ``mcap + debt``,
        with the cash leg hardcoded ``0.0``, and that number was wrong in two ways at once:

        * the cash leg was an assumption. The upstream balance sheet does not itemise cash (see
          :data:`_CASH_ROW_LABELS`), so for every net-cash company EV came back overstated by the
          entire cash balance, permanently and with no exception ever raised; and
        * the debt leg used ``_safe_float(..., default=0.0)``, so the upstream's ``-`` for a
          debt-free company and an unreadable borrowings row were the same ``0.0``.

        Wording and key names are matched to :attr:`bfinance.ticker.Ticker.valuation_measures`, so
        a caller reads one behaviour across the two surfaces rather than two.

        Provenance keys, always present: ``bfinance_enterprise_value_omitted`` (``str | None``),
        ``bfinance_debt_cr`` (``float | None``), ``bfinance_cash_cr`` (``float | None``) and
        ``bfinance_omitted_metrics`` (``List[str]``) -- one line per metric that was left out and
        why, so a key that is absent is never a mystery.

        A statement that failed to parse is *not* reported here. It raises
        :class:`~bfinance.utils.exceptions.ParsingError` out of ``Ticker.custom_ratios`` before
        this method is entered, because the frames arrive already materialised; that channel is
        :attr:`bfinance.ticker.Ticker.valuation_measures`'s ``bfinance_balance_sheet_error``.
        """
        ratios: Dict[str, Any] = {}
        # One line per metric that was left out and why, published as `bfinance_omitted_metrics`.
        # Collected from the first block onward so a reader of any ratio can find out why the
        # ones that are missing are missing.
        omitted: List[str] = []

        # 1. Piotroski F-Score
        piot = cls.calculate_piotroski_score(pnl, bs, cf)
        ratios["piotroski_score"] = piot["score"]
        ratios["piotroski_breakdown"] = piot["breakdown"]
        ratios["piotroski_max_score"] = piot.get("max_score", 9)
        ratios["piotroski_notes"] = piot.get("notes", [])

        # 2. Graham Number
        effective_eps = _safe_float(eps, default=0.0) if eps is not None else None
        # `effective_eps` is a float or None by construction (`_safe_float` returns a float or the
        # default it was given), so the NaN test is a plain `isinstance` check. It used to be a
        # `pd.isna` inside `try/except Exception: pass` guarding against a failure that cannot
        # happen -- a bare handler over an impossible case is still a bare handler.
        if effective_eps is not None and math.isnan(float(effective_eps)):
            effective_eps = None
        if (effective_eps is None or effective_eps <= 0) and "EPS in Rs" in pnl.index:
            raw_eps, eps_reason = _cell(pnl, "EPS in Rs")
            if raw_eps is not None and raw_eps > 0:
                effective_eps = raw_eps
            else:
                omitted.append(f"graham_number: EPS in Rs {eps_reason or 'is not positive'}")
        cp_safe = _safe_float(current_price, default=0.0) if current_price is not None else 0.0
        tp_safe = _safe_float(trailing_pe, default=0.0) if trailing_pe is not None else 0.0
        if (effective_eps is None or effective_eps <= 0) and cp_safe > 0 and tp_safe > 0:
            effective_eps = cp_safe / tp_safe

        ratios["graham_number"] = cls.calculate_graham_number(effective_eps, book_value)
        if ratios["graham_number"] and cp_safe > 0:
            ratios["graham_upside_%"] = round(((ratios["graham_number"] - cp_safe) / cp_safe) * 100, 2)

        # 3. Enterprise Value
        #
        # Every leg is read through `_cell`, which returns a value only when the number is
        # actually in hand. No leg is defaulted to 0.0: an unmeasured leg is not a zero leg.
        # Each `_cell` reason is collected and published, so a caller can see *which* input was
        # missing instead of receiving a plausible number.

        debt_cr, debt_reason = _cell(bs, "Borrowings")
        if debt_reason is not None:
            omitted.append(f"debt leg: {debt_reason}")

        # Cash: the first label in the hook that is actually present. One note for all of them,
        # because "the source itemises no cash" is a single fact, not one per label tried.
        cash_cr: Optional[float] = None
        cash_reason: Optional[str] = (
            "cash leg: the upstream balance sheet has no itemised cash row (tried "
            f"{' / '.join(_CASH_ROW_LABELS)}); cash sits inside 'Other Assets', so the cash leg "
            "of mcap + debt - cash is unknown"
        )
        for label in _CASH_ROW_LABELS:
            value, _label_reason = _cell(bs, label)
            if value is not None:
                cash_cr, cash_reason = value, None
                break
        if cash_reason is not None:
            omitted.append(cash_reason)

        # Explicit `is not None and > 0`, not truthiness: a market cap that failed to parse must
        # not read as a market cap of zero, which is the same defect as `or 0.0` was.
        equity_leg: Optional[float] = None
        mcap = _safe_float(market_cap_cr, default=float("nan")) if market_cap_cr is not None else float("nan")
        if not (isinstance(mcap, float) and math.isnan(mcap)) and mcap > 0:
            equity_leg = float(mcap)
        else:
            omitted.append(f"equity leg: no usable market cap (got {market_cap_cr!r})")

        ev: Optional[float] = None
        ev_omitted: Optional[str] = None
        if cash_cr is None:
            ev_omitted = cash_reason
        elif debt_cr is None:
            ev_omitted = f"debt leg: no readable Borrowings figure ({debt_reason})"
        elif equity_leg is None:
            ev_omitted = "equity leg: no market cap in the profile, so the equity leg is unknown"
        else:
            ev = equity_leg + debt_cr - cash_cr

        ratios["enterprise_value_cr"] = round(ev, 2) if ev is not None else None
        ratios["cash_and_equivalents_cr"] = round(cash_cr, 2) if cash_cr is not None else None

        # 4. EV / EBITDA. Follows the same gate as EV: a ratio built on an omitted EV is omitted
        # too, rather than being computed from a value this function just declined to publish.
        op_val, op_reason = _cell(pnl, "Operating Profit")
        if op_val is None:
            omitted.append(f"ev_to_ebitda: {op_reason}")
        else:
            dep_val, dep_reason = _cell(pnl, "Depreciation")
            if dep_val is None:
                # add back depreciation; when absent use operating profit alone, and say so
                dep_val = 0.0
                omitted.append(f"ev_to_ebitda: {dep_reason}; using operating profit alone")
            ebitda = op_val + dep_val
            if ebitda <= 0:
                omitted.append(
                    f"ev_to_ebitda: operating profit plus depreciation is {ebitda}, not a positive "
                    f"denominator"
                )
            elif ev is None:
                omitted.append(f"ev_to_ebitda: {ev_omitted}")
            else:
                ratios["ev_to_ebitda"] = round(ev / ebitda, 2)

        # 5. Interest Coverage Ratio
        op2, ic_op_reason = _cell(pnl, "Operating Profit")
        interest, interest_reason = _cell(pnl, "Interest")
        if op2 is None:
            omitted.append(f"interest_coverage: {ic_op_reason}")
        elif interest is None:
            omitted.append(f"interest_coverage: {interest_reason}")
        elif interest <= 0:
            # omit key when interest is zero or negative -- and say so, which the old
            # `except Exception: pass` never did.
            omitted.append(
                f"interest_coverage: interest expense is {interest}, so EBIT / Interest has no "
                f"meaning (a company with no debt has no coverage ratio, not an infinite one)"
            )
        else:
            # Other Income is optional: absent means "no other income", which is a modelling
            # choice rather than a missing input, so it does not withhold the ratio -- but it is
            # still recorded, because the reader deserves to know EBIT excluded it.
            oi2, oi_reason = _cell(pnl, "Other Income")
            if oi2 is None:
                oi2 = 0.0
                omitted.append(f"interest_coverage: {oi_reason}; using operating profit alone")
            # EBIT adds other income to operating profit
            ebit = op2 + oi2
            ratios["interest_coverage"] = round(ebit / interest, 2)

        # 6. Debt to Equity
        de_debt, de_debt_reason = _cell(bs, "Borrowings")
        de_res, de_res_reason = _cell(bs, "Reserves")
        de_eqc, de_eqc_reason = _cell(bs, "Equity Capital")
        if de_debt is None or de_res is None or de_eqc is None:
            omitted.append(
                f"debt_to_equity: {de_debt_reason or de_res_reason or de_eqc_reason}"
            )
        else:
            net_worth = de_res + de_eqc
            if net_worth > 0:
                ratios["debt_to_equity"] = round(de_debt / net_worth, 2)
            else:
                omitted.append(f"debt_to_equity: net worth is {net_worth}, not a positive base")

        # 7. Quality of Earnings (CFO / PAT)
        cfo, cfo_reason = _cell(cf, "Cash from Operating Activity")
        pat, pat_reason = _cell(pnl, "Net Profit")
        if cfo is None:
            omitted.append(f"cfo_to_pat: {cfo_reason}")
        elif pat is None:
            omitted.append(f"cfo_to_pat: {pat_reason}")
        elif pat <= 0:
            omitted.append(
                f"cfo_to_pat: net profit is {pat}, so a quality-of-earnings ratio is meaningless"
            )
        else:
            ratios["cfo_to_pat"] = round(cfo / pat, 2)

        # 8. Free Cash Flow (CFO - Capex)
        cfo2, fcf_cfo_reason = _cell(cf, "Cash from Operating Activity")
        cfi2, fcf_cfi_reason = _cell(cf, "Cash from Investing Activity")
        if cfo2 is None or cfi2 is None:
            omitted.append(f"free_cash_flow_cr: {fcf_cfo_reason or fcf_cfi_reason}")
        else:
            # keep only outflows; positive inflows such as asset sales are ignored
            fcf = cfo2 + min(cfi2, 0.0)
            ratios["free_cash_flow_cr"] = round(fcf, 2)

        ratios["bfinance_enterprise_value_omitted"] = ev_omitted
        ratios["bfinance_debt_cr"] = debt_cr
        ratios["bfinance_cash_cr"] = cash_cr
        ratios["bfinance_omitted_metrics"] = omitted

        # Nothing leaves here as NaN, provenance keys included. This is the dict-side twin of
        # `dtype=object` in `Ticker.valuation_measures`: there, an inferred float64 column turned
        # every absent metric into a float NaN; here a stray NaN from any arithmetic would do the
        # same thing, because NaN is a *number* -- it propagates silently through arithmetic,
        # prints as a plausible cell, and satisfies `is not None`. None is the answer the rest of
        # this function is built to give, so it is enforced here rather than trusted.
        #
        # This runs last, over the whole dict. An earlier version swept only the ratios and left
        # the provenance keys behind, so `bfinance_cash_cr` could still come back as NaN -- the
        # tests in `tests/test_ratios_honesty.py` caught exactly that.
        for key, value in ratios.items():
            if isinstance(value, float) and math.isnan(value):
                ratios[key] = None
                omitted.append(f"{key}: computed to NaN and is reported as None")

        return ratios
