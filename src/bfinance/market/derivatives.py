"""
NSE Derivatives and Options chain engine matching yfinance OptionChain API.

Parity note: yfinance 1.7.0 returns NO options data for NSE underlyings
(`options == ()`, `option_chain()` empty, dated expiry raises ValueError) —
even F&O stocks like RELIANCE.NS. This engine has no real NSE options feed,
so all volumes/OI are deterministic placeholders seeded by contract symbol
(identical across calls; no RNG state). Ticker-level gating onto a real F&O
membership list must happen in ticker.py (NEEDS-MAIN).
"""

import hashlib
import random
from datetime import datetime, timedelta
from typing import List, Optional, Tuple
import numpy as np
import pandas as pd

from bfinance.models.options import OptionChain
from bfinance.utils.symbols import normalize_symbol

YF_CHAIN_COLUMNS = [
    "contractSymbol",
    "strike",
    "lastPrice",
    "bid",
    "ask",
    "change",
    "percentChange",
    "volume",
    "openInterest",
    "impliedVolatility",
    "inTheMoney",
    "contractSize",
    "currency",
]


class DerivativesEngine:
    """
    NSE Options Chain engine generating strike matrices, Greeks, and IV metrics.
    """

    @classmethod
    def resolve_options(
        cls, symbol: str, fo_symbols: Optional[List[str]] = None
    ) -> Tuple[str, ...]:
        """
        Parity helper: return () unless `symbol` is in a real F&O membership
        list (yfinance 1.7.0 returns () for all NSE underlyings probed).
        Ticker wiring must supply `fo_symbols` from a real source.
        """
        if not fo_symbols:
            return ()
        clean = normalize_symbol(symbol)
        members = {normalize_symbol(s) for s in fo_symbols}
        if clean not in members:
            return ()
        return cls.get_upcoming_expiries()

    @classmethod
    def empty_option_chain(cls) -> OptionChain:
        """yfinance-compatible empty chain (tz of no data): empty calls/puts."""
        empty_calls = pd.DataFrame({c: pd.Series(dtype=float) for c in YF_CHAIN_COLUMNS})
        empty_puts = pd.DataFrame({c: pd.Series(dtype=float) for c in YF_CHAIN_COLUMNS})
        return OptionChain(calls=empty_calls, puts=empty_puts)

    @classmethod
    def _contract_rng(cls, symbol: str, expiry_date: str, strike: float) -> random.Random:
        """Deterministic per-contract RNG (no global random state)."""
        seed = int(hashlib.sha256(f"{symbol}|{expiry_date}|{strike}".encode()).hexdigest(), 16) % (2 ** 32)
        return random.Random(seed)

    @classmethod
    def get_upcoming_expiries(cls) -> Tuple[str, ...]:
        """
        Calculate next 4 NSE monthly F&O expiry dates (last Thursday of each month).
        """
        expiries = []
        today = datetime.today()

        for m in range(4):
            # Calculate month
            target_month = (today.month + m - 1) % 12 + 1
            target_year = today.year + ((today.month + m - 1) // 12)

            # Find last day of target month
            if target_month == 12:
                next_month_first = datetime(target_year + 1, 1, 1)
            else:
                next_month_first = datetime(target_year, target_month + 1, 1)
            last_day = next_month_first - timedelta(days=1)

            # Find last Thursday (weekday 3)
            offset = (last_day.weekday() - 3) % 7
            last_thursday = last_day - timedelta(days=offset)

            if last_thursday >= today - timedelta(days=1):
                expiries.append(last_thursday.strftime("%Y-%m-%d"))

        return tuple(expiries)

    @classmethod
    def generate_option_chain(
        cls, symbol: str, cmp: float, expiry_date: Optional[str] = None
    ) -> OptionChain:
        """
        Build calls and puts DataFrames for the specified expiry date.
        """
        clean_symbol = normalize_symbol(symbol)
        if not expiry_date:
            expiries = cls.get_upcoming_expiries()
            expiry_date = expiries[0] if expiries else datetime.today().strftime("%Y-%m-%d")

        # Determine strike step based on CMP
        if cmp > 10000:
            step = 500
        elif cmp > 2000:
            step = 50
        elif cmp > 500:
            step = 20
        elif cmp > 100:
            step = 10
        else:
            step = 2.5

        atm_strike = round(cmp / step) * step
        strikes = [atm_strike + (i * step) for i in range(-15, 16)]

        expiry_tag = expiry_date.replace("-", "")[2:]

        calls_data = []
        puts_data = []

        for s in strikes:
            moneyness_call = cmp - s
            moneyness_put = s - cmp

            call_itm = moneyness_call > 0
            put_itm = moneyness_put > 0

            # Approximate theoretical pricing for realistic schema parity
            iv = 0.18 + abs(s - cmp) / cmp * 0.05
            call_price = max(0.5, moneyness_call + 15.0 if call_itm else max(0.5, 20.0 * np.exp(-abs(moneyness_call) / 100)))
            put_price = max(0.5, moneyness_put + 15.0 if put_itm else max(0.5, 20.0 * np.exp(-abs(moneyness_put) / 100)))

            # Call contract
            rng = cls._contract_rng(clean_symbol, expiry_date, float(s))
            calls_data.append({
                "contractSymbol": f"{clean_symbol}{expiry_tag}C{int(s)}",
                "strike": float(s),
                "lastPrice": round(float(call_price), 2),
                "bid": round(float(call_price * 0.99), 2),
                "ask": round(float(call_price * 1.01), 2),
                "change": 0.0,
                "percentChange": 0.0,
                "volume": int(rng.randint(50, 5000)),
                "openInterest": int(rng.randint(500, 50000)),
                "impliedVolatility": round(float(iv), 4),
                "inTheMoney": bool(call_itm),
                "contractSize": "REGULAR",
                "currency": "INR",
            })

            # Put contract
            puts_data.append({
                "contractSymbol": f"{clean_symbol}{expiry_tag}P{int(s)}",
                "strike": float(s),
                "lastPrice": round(float(put_price), 2),
                "bid": round(float(put_price * 0.99), 2),
                "ask": round(float(put_price * 1.01), 2),
                "change": 0.0,
                "percentChange": 0.0,
                "volume": int(rng.randint(50, 5000)),
                "openInterest": int(rng.randint(500, 50000)),
                "impliedVolatility": round(float(iv), 4),
                "inTheMoney": bool(put_itm),
                "contractSize": "REGULAR",
                "currency": "INR",
            })

        df_calls = pd.DataFrame(calls_data)
        df_puts = pd.DataFrame(puts_data)
        return OptionChain(calls=df_calls, puts=df_puts)
