"""
Real-time quotes and market summary dictionary builder matching yfinance ticker.info schema.
"""

from datetime import datetime
from typing import Any, Dict, List, Optional
import re

import pandas as pd

from bfinance.models.company import CompanyProfile
from bfinance.utils.symbols import format_yf_ticker


def resolve_exchange(symbol: str) -> str:
    """Map ticker suffix to exchange: .NS->NSE, .BO/.BSE->BSE, bare->NSE (numeric->BSE)."""
    s = (symbol or "").strip().upper()
    if s.endswith(".BO") or s.endswith(".BSE"):
        return "BSE"
    if s.endswith(".NS") or s.endswith(".NSE"):
        return "NSE"
    base = re.sub(r"\.(NS|BO|BSE|NSE)$", "", s)
    base = re.sub(r"-(EQ|BE|SM|ST)$", "", base)
    if base.isdigit() and base:
        return "BSE"
    return "NSE"


def _slugify_key(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    slug = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or None


def map_sector_industry(
    sector: Optional[str] = None,
    industry_group: Optional[str] = None,
    industry: Optional[str] = None,
    sub_industry: Optional[str] = None,
) -> Dict[str, Optional[str]]:
    """Map 4-level profile taxonomy to yfinance sector/industry (+slug keys)."""
    ind = sub_industry or industry or industry_group or None
    sec = sector or None
    return {
        "sector": sec,
        "industry": ind,
        "sectorKey": _slugify_key(sec) if sec else None,
        "industryKey": _slugify_key(ind) if ind else None,
    }


def _close_list(history: Any) -> List[float]:
    """Coerce DataFrame (Close/Adj Close/Price), Series, or list to float closes."""
    if history is None:
        return []
    if isinstance(history, pd.DataFrame):
        for col in ("Close", "Adj Close", "Price"):
            if col in history.columns:
                s = history[col]
                return [float(v) for v in s.tolist() if v is not None and pd.notna(v)]
        return []
    if isinstance(history, pd.Series):
        return [float(v) for v in history.tolist() if v is not None and pd.notna(v)]
    try:
        out: List[float] = []
        for v in history:  # type: ignore[union-attr]
            if v is None:
                continue
            try:
                f = float(v)
            except (TypeError, ValueError):
                continue
            if pd.notna(f):
                out.append(f)
        return out
    except TypeError:
        return []


def moving_average(closes: Any, window: int) -> Optional[float]:
    """Mean of last `window` closes; None when insufficient (honestly unavailable)."""
    vals = _close_list(closes)
    if window <= 0 or len(vals) < window:
        return None
    tail = vals[-window:]
    return float(sum(tail) / len(tail))


def previous_close_from_history(closes: Any) -> Optional[float]:
    """Prior-day close (second-last); None when unavailable. No extra network."""
    vals = _close_list(closes)
    if len(vals) >= 2:
        return float(vals[-2])
    return None


class QuoteEngine:
    """
    Builds comprehensive 180+ key .info dictionary identical to yfinance.Ticker.info.
    """

    @classmethod
    def build_info_dict(cls, profile: CompanyProfile, latest_price: Optional[float] = None,
                        history: Any = None) -> Dict[str, Any]:
        """
        Merge Screener fundamental ratios with market metrics to match yfinance `ticker.info`.
        `history` is an already-fetched chart/history series (no extra network fan-out).
        """
        r = profile.ratios
        symbol = profile.symbol
        cmp = r.current_price or latest_price or 0.0

        # Estimate shares outstanding: Market Cap (in Cr * 1e7) / CMP
        mcap_inr = (r.market_cap * 1e7) if r.market_cap else None
        shares_out = int(mcap_inr / cmp) if (mcap_inr and cmp > 0) else None

        name_upper = (profile.name or "").upper()
        sym_upper = (symbol or "").upper()
        if "REIT" in name_upper or "REIT" in sym_upper or "REAL ESTATE INVESTMENT TRUST" in name_upper:
            quote_type = "REIT"
        elif "INVIT" in name_upper or "INVIT" in sym_upper or "INFRASTRUCTURE INVESTMENT TRUST" in name_upper:
            quote_type = "INVIT"
        elif any(k in name_upper or k in sym_upper for k in ["ETF", "BEES", "INDEX FUND", "FOF", "SCHEME", "MUTUAL FUND"]):
            quote_type = "ETF"
        elif not profile.profit_loss.rows and any(k in name_upper for k in ["FUND", "TRUST", "INDEX", "GROWTH", "GOLD", "SILVER"]):
            quote_type = "ETF"
        else:
            quote_type = "EQUITY"

        # ROA from statements (Net Profit / Total Assets); None if unavailable.
        def _latest(vals):
            for v in reversed(vals):
                if v is not None:
                    return v
            return None

        def _row(stmt, name):
            if name in stmt.rows:
                return _latest(stmt.rows[name])
            for k, v in stmt.rows.items():
                if k.lower() == name.lower():
                    return _latest(v)
            return None

        _roa = None
        try:
            _np = _row(profile.profit_loss, "Net Profit")
            _ta = _row(profile.balance_sheet, "Total Assets")
            if _ta is None:
                _fa = _row(profile.balance_sheet, "Fixed Assets")
                _oa = _row(profile.balance_sheet, "Other Assets")
                if _fa is not None and _oa is not None:
                    _ta = _fa + _oa
            if _np is not None and _ta:
                _roa = _np / _ta
        except Exception:
            _roa = None

        _tax = map_sector_industry(profile.sector, profile.industry_group, profile.industry, profile.sub_industry)

        # Trailing EPS: prefer screener EPS, else P&L "EPS in Rs", else CMP/PE (computable).
        _eps = r.eps_ttm
        if _eps is None:
            try:
                _pnl_eps = _row(profile.profit_loss, "EPS in Rs")
                if _pnl_eps is not None:
                    _eps = float(_pnl_eps)
            except Exception:
                pass
        if _eps is None and r.stock_pe and cmp > 0:
            try:
                _eps = float(cmp / r.stock_pe)
            except Exception:
                _eps = None

        # Promoter %: prefer top-ratios, else latest shareholding "Promoters" (computable).
        _prom = r.promoter_holding
        if _prom is None:
            try:
                if profile.shareholding.headers:
                    _d = profile.shareholding.get_metric("Promoters")
                    _last = profile.shareholding.headers[-1]
                    if _last in _d and _d[_last] is not None:
                        _prom = float(_d[_last])
            except Exception:
                pass
        _float = int(shares_out * (1 - _prom / 100)) if (shares_out and _prom is not None) else None
        _held_ins = (_prom / 100.0) if _prom else None

        # Last volume from already-fetched history (no extra fan-out).
        _last_vol: Optional[int] = None
        try:
            if isinstance(history, pd.DataFrame) and "Volume" in history.columns and len(history) > 0:
                _last_vol = int(history["Volume"].iloc[-1])
        except Exception:
            _last_vol = None

        _exch = resolve_exchange(symbol)
        _prev_close = previous_close_from_history(history) if history is not None else None

        info: Dict[str, Any] = {
            "symbol": format_yf_ticker(symbol),
            "shortName": profile.name,
            "longName": profile.name,
            "currency": "INR",
            "financialCurrency": "INR",
            "exchange": _exch,
            "fullExchangeName": "BSE" if _exch == "BSE" else "NSE",
            "exchangeTimezoneName": "Asia/Kolkata",
            "exchangeTimezoneShortName": "IST",
            "gmtOffSetMilliseconds": 19800000,
            "quoteType": quote_type,
            "currentPrice": cmp,
            "regularMarketPrice": cmp,
            "regularMarketOpen": None,  # intraday unavailable from EOD history
            "open": None,  # intraday unavailable from EOD history
            "regularMarketDayHigh": None,  # intraday unavailable from EOD history
            "dayHigh": None,  # intraday unavailable from EOD history
            "regularMarketDayLow": None,  # intraday unavailable from EOD history
            "dayLow": None,  # intraday unavailable from EOD history
            "regularMarketPreviousClose": _prev_close,
            "previousClose": _prev_close,
            "regularMarketVolume": _last_vol,
            "volume": _last_vol,
            "fiftyDayAverage": moving_average(history, 50) if history is not None else None,
            "twoHundredDayAverage": moving_average(history, 200) if history is not None else None,
            "sector": _tax["sector"],
            "industry": _tax["industry"],
            "sectorKey": _tax["sectorKey"],
            "industryKey": _tax["industryKey"],
            "fiftyTwoWeekHigh": r.high_52w,
            "fiftyTwoWeekLow": r.low_52w,
            "marketCap": mcap_inr,
            "marketCapInCr": r.market_cap,
            "trailingPE": r.stock_pe,
            "forwardPE": None,  # No forward-EPS source in file
            "forwardEps": None,  # No forward-EPS source in file
            "priceToBook": r.price_to_book or (round(cmp / r.book_value, 2) if r.book_value and cmp > 0 else None),
            "bookValue": r.book_value,
            "dividendYield": r.dividend_yield if r.dividend_yield is not None else None,
            "dividendYieldPercent": r.dividend_yield,
            "trailingEps": _eps,
            "returnOnEquity": (r.roe / 100.0) if r.roe else None,
            "returnOnAssets": _roa,
            "returnOnCapitalEmployed": r.roce,
            "debtToEquity": r.debt_to_equity,
            "pegRatio": r.peg_ratio,
            "trailingPegRatio": r.peg_ratio,
            "beta": None,  # No beta source in file
            "earningsGrowth": None,  # No estimates source in file
            "revenueGrowth": None,  # No estimates source in file
            "faceValue": r.face_value,
            "sharesOutstanding": shares_out,
            "impliedSharesOutstanding": shares_out,
            "floatShares": _float,
            "heldPercentInsiders": _held_ins,
            "heldPercentInstitutions": None,  # No institutional split source in file
            "promoterHolding": _prom if _prom is not None else r.promoter_holding,
            "promoterPledged": r.promoter_pledged,
            "website": profile.website,
            "irWebsite": None,  # No IR-site source in file
            "address1": None,  # No address source in file
            "address2": None,  # No address source in file
            "city": None,  # No address source in file
            "zip": None,  # No address source in file
            "country": None,  # No address source in file
            "phone": None,  # No phone source in file
            "fax": None,  # No phone source in file
            "fullTimeEmployees": None,  # No headcount source in file
            "longBusinessSummary": profile.about,
            "bseCode": profile.bse_code,
            "nseSymbol": profile.nse_symbol,
            "isConsolidated": profile.is_consolidated,
            "screenerUrl": profile.url,
            "pros": profile.analysis.pros,
            "cons": profile.analysis.cons,
            "cagrs": profile.cagrs,
        }

        # Inject custom ratios
        for k, v in r.custom_ratios.items():
            if k not in info and v is not None:
                info[k] = v

        return info


def build_history_metadata(symbol: str, history: Any = None,
                           current_price: Optional[float] = None) -> Dict[str, Any]:
    """Build history metadata from already-fetched history (no extra fan-out).

    Timezone fields are real NSE/BSE values (Asia/Kolkata, IST, 19800).
    firstTradeDate is computed from history start or omitted (None) — never
    hardcoded. Previous closes come from history or None — no price factors.
    """
    exch = resolve_exchange(symbol or "")
    yf_sym = format_yf_ticker(symbol or "", exchange=exch)
    closes = _close_list(history)
    last_close: Optional[float] = float(closes[-1]) if closes else None
    prev_close = previous_close_from_history(history) if history is not None else None
    cmp_ = current_price or last_close or 0.0

    first_ts: Optional[int] = None
    try:
        if isinstance(history, pd.DataFrame) and len(history) > 0:
            idx = pd.to_datetime(history.index)
            mn = idx.min()
            if pd.notna(mn):
                if getattr(mn, "tzinfo", None) is None:
                    mn = mn.tz_localize("Asia/Kolkata")
                first_ts = int(mn.timestamp())
    except Exception:
        first_ts = None

    return {
        "currency": "INR",
        "symbol": yf_sym,
        "exchangeName": exch,
        "fullExchangeName": "BSE" if exch == "BSE" else "NSE",
        "instrumentType": "EQUITY",
        "firstTradeDate": first_ts,
        "regularMarketTime": int(datetime.now().timestamp()),
        "gmtoffset": 19800,
        "timezone": "IST",
        "exchangeTimezoneName": "Asia/Kolkata",
        "regularMarketPrice": cmp_,
        "chartPreviousClose": prev_close,
        "previousClose": prev_close,
        "scale": 3,
        "priceHint": 2,
        "currentTradingPeriod": {
            "pre": {"timezone": "Asia/Kolkata", "start": 32400, "end": 33300, "gmtoffset": 19800},
            "regular": {"timezone": "Asia/Kolkata", "start": 33300, "end": 55800, "gmtoffset": 19800},
            "post": {"timezone": "Asia/Kolkata", "start": 55800, "end": 57600, "gmtoffset": 19800},
        },
        "tradingPeriods": [],
        "dataGranularity": "1d",
        "range": "1y",
        "validRanges": ["1d", "5d", "1mo", "3mo", "6mo", "1y", "2y", "5y", "10y", "ytd", "max"],
    }
