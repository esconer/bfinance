"""
Corporate actions extractor (dividends, stock splits, bonus issues).

Conventions (yfinance parity notes):
- yfinance `ticker.dividends` is indexed by **ex-date**; this engine derives
  annual payouts from Screener.in P&L (`Dividend Payout %` x `EPS in Rs`) and
  dates them at the **fiscal year-end** (`Mar-31`, Asia/Kolkata). Amounts track
  yfinance within ~5%; dates do NOT (ex-dates fall ~4-5 months later).
- yfinance represents NSE 1:1 **bonus issues as a 2.0 split** (e.g. RELIANCE
  2017-09-07, 2024-10-28); this engine does the same (equity-capital ratio).
"""

from typing import Any, Dict, List, Optional
import pandas as pd

from bfinance.models.company import CompanyProfile

YF_TZ = "Asia/Kolkata"
ACTIONS_COLUMNS = ["Dividends", "Stock Splits"]
YF_MAJOR_HOLDERS_INDEX = [
    "insidersPercentHeld",
    "institutionsPercentHeld",
    "institutionsFloatPercentHeld",
    "institutionsCount",
]
YF_CALENDAR_KEYS = [
    "Ex-Dividend Date",
    "Earnings Date",
    "Earnings High",
    "Earnings Low",
    "Earnings Average",
    "Revenue High",
    "Revenue Low",
    "Revenue Average",
]


class CorporateActionsEngine:
    """
    Extracts structured dividends and stock splits series from corporate history.
    """

    @classmethod
    def _tz_dated(cls, records: Dict[Any, float], name: str) -> pd.Series:
        """Build tz-aware (Asia/Kolkata) Series matching yfinance index tz."""
        if not records:
            return pd.Series(dtype=float, name=name)
        s = pd.Series(records).sort_index()
        s.index = pd.DatetimeIndex(pd.to_datetime(s.index))
        if s.index.tz is None:
            s.index = s.index.tz_localize(YF_TZ)
        else:
            s.index = s.index.tz_convert(YF_TZ)
        s.index.name = "Date"
        s.name = name
        return s

    @classmethod
    def extract_dividends(cls, profile: CompanyProfile) -> pd.Series:
        """
        Extract dividend payout series matching yfinance `ticker.dividends`.
        Returns pd.Series indexed by DatetimeIndex with dividend amounts.
        Dated at fiscal year-end (see module docstring for ex-date gap).
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

        return cls._tz_dated(div_records, "Dividends")

    @classmethod
    def extract_splits(cls, profile: CompanyProfile) -> pd.Series:
        """
        Extract stock split / bonus series matching yfinance `ticker.splits`.
        Bonus issues are normalized to split ratios (1:1 bonus -> 2.0).
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

        return cls._tz_dated(splits_records, "Stock Splits")

    @classmethod
    def build_actions(
        cls,
        dividends: Optional[pd.Series] = None,
        splits: Optional[pd.Series] = None,
    ) -> pd.DataFrame:
        """
        Build yfinance `ticker.actions` shape contract: DataFrame indexed by
        Date with float columns ["Dividends", "Stock Splits"] (union of dates).
        """
        frames = []
        for series, col in ((dividends, "Dividends"), (splits, "Stock Splits")):
            if series is not None and not series.empty:
                s = series.dropna()
                if not s.empty:
                    frames.append(s.rename(col).to_frame())
        if not frames:
            return pd.DataFrame(
                {c: pd.Series(dtype=float) for c in ACTIONS_COLUMNS},
                index=pd.DatetimeIndex([], name="Date", tz=YF_TZ),
            )
        df = pd.concat(frames, axis=1).sort_index()
        for col in ACTIONS_COLUMNS:
            if col not in df.columns:
                df[col] = 0.0
        df = df[ACTIONS_COLUMNS].fillna(0.0).astype(float)
        df.index = pd.DatetimeIndex(pd.to_datetime(df.index))
        if df.index.tz is None:
            df.index = df.index.tz_localize(YF_TZ)
        else:
            df.index = df.index.tz_convert(YF_TZ)
        df.index.name = "Date"
        return df

    @classmethod
    def build_capital_gains(cls) -> pd.Series:
        """
        yfinance `ticker.capital_gains` for Indian equities is an empty
        Series (dtype object, name None). Match it; never fabricate.
        """
        return pd.Series([], dtype=object)

    @classmethod
    def build_major_holders(cls, profile: CompanyProfile) -> pd.DataFrame:
        """
        yfinance `ticker.major_holders` shape: single "Value" float column
        indexed by [insidersPercentHeld, institutionsPercentHeld,
        institutionsFloatPercentHeld, institutionsCount]. Fractions, never
        percent-strings. institutionsCount has no screener source (-> NaN).
        """
        empty = pd.DataFrame(
            {"Value": pd.Series(dtype=float)}, index=pd.Index([], dtype=object)
        )
        sh = profile.shareholding
        if not sh.headers:
            return empty
        latest = sh.headers[-1]
        prom = sh.get_metric("Promoters").get(latest)
        fii = sh.get_metric("FIIs").get(latest) or 0.0
        dii = sh.get_metric("DIIs").get(latest) or 0.0
        if prom is None:
            return empty
        insiders = float(prom) / 100.0
        inst = (float(fii) + float(dii)) / 100.0
        denom = 1.0 - insiders
        float_held = (inst / denom) if denom > 0 else float("nan")
        return pd.DataFrame(
            {"Value": [insiders, inst, float_held, float("nan")]},
            index=pd.Index(YF_MAJOR_HOLDERS_INDEX),
        )

    @classmethod
    def build_institutional_holders(cls) -> pd.DataFrame:
        """yfinance returns empty (0,0) DataFrame for RELIANCE.NS; match it."""
        return pd.DataFrame()

    @classmethod
    def build_mutualfund_holders(cls) -> pd.DataFrame:
        """yfinance returns empty (0,0) DataFrame for RELIANCE.NS; match it."""
        return pd.DataFrame()

    @classmethod
    def build_calendar(
        cls,
        profile: CompanyProfile,
        dividends: Optional[pd.Series] = None,
    ) -> Dict[str, Any]:
        """
        yfinance `ticker.calendar` key contract. No analyst-estimates feed
        exists upstream, so forward estimates stay None (never fabricated).
        """
        ex_div = None
        if dividends is not None and not dividends.empty:
            last = dividends.dropna().index.max()
            if isinstance(last, pd.Timestamp):
                ex_div = last.date()
        return {
            "Ex-Dividend Date": ex_div,
            "Earnings Date": ["N/A"],
            "Earnings High": None,
            "Earnings Low": None,
            "Earnings Average": None,
            "Revenue High": None,
            "Revenue Low": None,
            "Revenue Average": None,
        }

    @classmethod
    def build_news(cls, announcements: Optional[List[Dict[str, str]]] = None) -> List[Dict[str, Any]]:
        """
        Adapt screener announcements to the yfinance 1.7 `ticker.news`
        shape: list of {"id": ..., "content": {...}} where content carries
        title/link/publisher/time keys. Empty in -> empty list (never None).
        """
        items: List[Dict[str, Any]] = []
        for i, a in enumerate(announcements or []):
            title = (a or {}).get("title", "")
            url = (a or {}).get("url", "")
            items.append(
                {
                    "id": f"bfinance-{i}",
                    "content": {
                        "title": title,
                        "link": url,
                        "publisher": {"displayName": "Screener.in"},
                        "provider": {"displayName": "Screener.in"},
                        "time": None,
                        "pubDate": None,
                        "canonicalUrl": {"url": url},
                    },
                }
            )
        return items

    @classmethod
    def merge_actions_into_history(
        cls,
        df: pd.DataFrame,
        dividends: Optional[pd.Series] = None,
        splits: Optional[pd.Series] = None,
    ) -> pd.DataFrame:
        """
        Left-join dividend/split series onto a history frame by calendar date.

        Non-action dates stay 0.0; zeros therefore mean genuinely no action
        on that date (or empty/unavailable input series). Tz-aware and
        tz-naive indexes are compared on normalized tz-stripped dates.
        """
        df = df.copy()
        df["Dividends"] = 0.0
        df["Stock Splits"] = 0.0
        if df.empty:
            return df

        def _date_keys(idx) -> pd.DatetimeIndex:
            keys = pd.DatetimeIndex(pd.to_datetime(idx).normalize())
            if getattr(keys, "tz", None) is not None:
                keys = keys.tz_localize(None)
            return keys

        df_keys = _date_keys(df.index)
        for col, series in (("Dividends", dividends), ("Stock Splits", splits)):
            if series is None or series.empty:
                continue
            s = series.dropna()
            if s.empty:
                continue
            action_map = dict(zip(_date_keys(s.index), s.values))
            df[col] = [float(action_map.get(k, 0.0)) for k in df_keys]
        return df
