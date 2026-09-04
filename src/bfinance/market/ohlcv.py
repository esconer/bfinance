import logging
from datetime import datetime
from typing import Optional, Union
import numpy as np
import pandas as pd

logger = logging.getLogger("bfinance")

from bfinance.market.corporate import CorporateActionsEngine
from bfinance.screener.client import ScreenerClient
from bfinance.utils.exceptions import BFinanceError, TickerNotFoundError
from bfinance.utils.symbols import normalize_symbol


def _resample_ohlcv(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Resample daily bars to weekly (Monday-labeled) or monthly bars."""
    if rule == "weekly":
        keys = df.index.normalize() - pd.to_timedelta(df.index.weekday, unit="D")
    else:
        keys = df.index.normalize().to_period("M").to_timestamp()
        if getattr(df.index, "tz", None) is not None:
            keys = keys.tz_localize("Asia/Kolkata")
    agg = {}
    for col in df.columns:
        if col == "Open":
            agg[col] = "first"
        elif col == "High":
            agg[col] = "max"
        elif col == "Low":
            agg[col] = "min"
        elif col in ("Close", "Adj Close"):
            agg[col] = "last"
        elif col == "Volume":
            agg[col] = "sum"
        elif col == "Dividends":
            agg[col] = "sum"
        else:
            agg[col] = "max"
    out = df.groupby(keys).agg(agg)
    out.index.name = "Date"
    out.sort_index(inplace=True)
    if "Volume" in out.columns:
        out["Volume"] = out["Volume"].fillna(0).astype(np.int64)
    return out


class OHLCVEngine:
    """
    Historical OHLCV data builder converting deep price feeds into yfinance-identical DataFrames.

    Provenance: Close + Volume are real values from the Screener chart API
    (chart API is close-only); Open/High/Low are synthesized from Close;
    Adj Close == Close (unadjusted).
    """

    PERIOD_DAYS_MAP = {
        "1d": 2,
        "5d": 7,
        "1mo": 30,
        "3mo": 90,
        "6mo": 180,
        "1y": 365,
        "2y": 730,
        "3y": 1095,
        "5y": 1825,
        "10y": 3652,
        "ytd": 365,
        "max": 10000,
    }

    def __init__(self, screener_client: ScreenerClient):
        self.screener = screener_client

    async def fetch_history(
        self,
        symbol: str,
        period: str = "1mo",
        interval: str = "1d",
        start: Optional[Union[str, datetime]] = None,
        end: Optional[Union[str, datetime]] = None,
        prepost: bool = False,
        actions: bool = True,
        auto_adjust: bool = True,
        back_adjust: bool = False,
        repair: bool = False,
        keepna: bool = False,
        rounding: bool = False,
        timeout: int = 10,
        raise_errors: bool = False,
    ) -> pd.DataFrame:
        """
        Fetch historical OHLCV DataFrame matching exact yfinance schema:
        Columns: ['Open', 'High', 'Low', 'Close', 'Adj Close', 'Volume'] (+ ['Dividends', 'Stock Splits'] if actions=True)
        Index: pd.DatetimeIndex localized to Asia/Kolkata (yfinance NSE convention).

        Provenance: Close + Volume are real values from the Screener chart API
        (chart API is close-only); Open/High/Low are synthesized from Close;
        Adj Close == Close (unadjusted).

        yfinance 1.7.0 parity notes (verified live vs RELIANCE.NS):
        - auto_adjust=True (default) drops "Adj Close"; OHLC are the
          adjusted values. Because this feed is unadjusted (Adj == Close),
          adjustment is a no-op scale of 1.0 and only the column drop applies.
        - back_adjust=True drops "Adj Close" (no-op scale, same reason).
        - end dates are exclusive (start inclusive), matching yfinance.
        - interval="1wk" resamples daily bars into Monday-labeled weekly
          bars (Open=first, High=max, Low=min, Close=last, Volume=sum,
          Dividends=sum, Stock Splits=max). interval="1mo" resamples to
          month-start bars with the same aggregation.
        - repair=True is not implemented (yfinance itself needs scipy for it,
          missing in this env) -> NotImplementedError instead of silently
          returning unrepaired data. Unsupported intraday intervals also raise.
        - prepost/keepna/back_adjust are honored as explicit no-ops for NSE
          daily bars (yfinance returns identical frames for them here).
        """
        if repair:
            raise NotImplementedError("repair=True is not supported by bfinance")
        norm_interval = (interval or "1d").lower()
        if norm_interval in ("1d", "1day", "daily"):
            resample = None
        elif norm_interval in ("1wk", "1w", "wk", "weekly"):
            resample = "weekly"
        elif norm_interval in ("1mo", "1m", "monthly"):
            resample = "monthly"
        else:
            raise NotImplementedError(f"interval={interval!r} is not supported by bfinance")
        if prepost:
            logger.debug("%s: prepost=True is a no-op for NSE daily bars.", symbol)
        if back_adjust:
            logger.debug("%s: back_adjust=True applied as no-op (feed is unadjusted).", symbol)

        clean_symbol = normalize_symbol(symbol)

        # Calculate lookback days
        days = 1825
        if period and period.lower() in self.PERIOD_DAYS_MAP:
            days = self.PERIOD_DAYS_MAP[period.lower()]

        # If start date provided, calculate days from today
        if start:
            start_dt = pd.to_datetime(start)
            now_dt = pd.to_datetime(datetime.now())
            delta_days = (now_dt - start_dt).days + 10
            days = max(days, delta_days)

        # Fetch from Screener Chart API (provides adjusted daily Close & Volume over 20+ years)
        try:
            chart_df = await self.screener.get_chart_timeseries(clean_symbol, metric="price", days=days)
        except Exception as e:
            if raise_errors:
                raise
            logger.warning("%s: No price data found (%s). Returning empty DataFrame.", symbol, e)
            chart_df = pd.DataFrame()

        if chart_df.empty or "Price" not in chart_df.columns:
            cols = ["Open", "High", "Low", "Close", "Volume"]
            if not auto_adjust and not back_adjust:
                cols.insert(4, "Adj Close")
            if actions:
                cols.extend(["Dividends", "Stock Splits"])
            empty = pd.DataFrame(
                columns=cols,
                index=pd.DatetimeIndex([], name="Date", tz="Asia/Kolkata"),
            )
            empty.attrs["bfinance_synthetic_ohlc"] = True
            return empty

        # Localize to Asia/Kolkata (yfinance NSE convention)
        chart_df = chart_df.copy()
        chart_df.index = pd.to_datetime(chart_df.index)
        if chart_df.index.tz is None:
            chart_df.index = chart_df.index.tz_localize("Asia/Kolkata")
        else:
            chart_df.index = chart_df.index.tz_convert("Asia/Kolkata")
        chart_df.sort_index(inplace=True)

        close_series = chart_df["Price"].astype(float)
        volume_series = (
            chart_df["Volume"].astype(float)
            if "Volume" in chart_df.columns
            else pd.Series(0.0, index=chart_df.index)
        )

        df = pd.DataFrame(index=chart_df.index)
        df["Close"] = close_series
        df["Adj Close"] = close_series

        # Previous close for daily open reference
        prev_close = close_series.shift(1).bfill()
        df["Open"] = prev_close

        # High and Low envelope
        df["High"] = np.maximum(df["Open"], df["Close"]) * 1.002
        df["Low"] = np.minimum(df["Open"], df["Close"]) * 0.998

        df["Volume"] = volume_series.fillna(0).astype(np.int64)

        if actions:
            # Merge real dividends/splits; zeros only when genuinely unavailable
            try:
                get_profile = getattr(self.screener, "get_company_profile", None)
                if get_profile is None:
                    raise AttributeError("screener has no get_company_profile")
                profile = await get_profile(clean_symbol)
                dividends = CorporateActionsEngine.extract_dividends(profile)
                splits = CorporateActionsEngine.extract_splits(profile)
                df = CorporateActionsEngine.merge_actions_into_history(
                    df, dividends=dividends, splits=splits
                )
            except Exception as e:
                logger.debug("%s: corporate actions unavailable (%s); using zeros.", symbol, e)
                df["Dividends"] = 0.0
                df["Stock Splits"] = 0.0

        if rounding:
            df["Open"] = df["Open"].round(2)
            df["High"] = df["High"].round(2)
            df["Low"] = df["Low"].round(2)
            df["Close"] = df["Close"].round(2)
            df["Adj Close"] = df["Adj Close"].round(2)

        cols = ["Open", "High", "Low", "Close", "Volume"]
        if not auto_adjust and not back_adjust:
            cols.insert(4, "Adj Close")
        if actions:
            cols.extend(["Dividends", "Stock Splits"])

        df = df[cols]
        df.index.name = "Date"

        # Filter by start (inclusive) / end (exclusive) matching yfinance
        if start:
            start_ts = pd.to_datetime(start)
            if df.index.tz is not None and start_ts.tzinfo is None:
                start_ts = start_ts.tz_localize("Asia/Kolkata")
            df = df[df.index >= start_ts]
        if end:
            end_ts = pd.to_datetime(end)
            if df.index.tz is not None and end_ts.tzinfo is None:
                end_ts = end_ts.tz_localize("Asia/Kolkata")
            df = df[df.index < end_ts]

        if not keepna:
            df.dropna(how="all", inplace=True)

        if resample is not None and not df.empty:
            df = _resample_ohlcv(df, resample)

        df.attrs["bfinance_synthetic_ohlc"] = True
        return df
