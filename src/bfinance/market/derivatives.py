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
        **Removed — this used to fabricate an option chain, and that was worse than nothing.**

        It had no network path at all. Strikes were synthesised from `cmp` on a price-tiered step
        (:115-128), prices came from a closed-form heuristic (:142-145), and ``volume`` and
        ``openInterest`` came from :meth:`_contract_rng` — a seeded ``numpy`` generator, i.e.
        **literally random numbers presented as exchange data**. ``change`` and ``percentChange``
        were pinned to ``0.0`` so a downstream consumer could not even tell the values were
        synthetic.

        The only reason it never reached a user is that :meth:`resolve_options` returns ``()``
        unless real F&O membership is supplied, and nothing supplied it. That was containment by
        accident: the moment someone wired up ``fo_symbols``, the package would have started
        returning invented open interest. The trap is now removed rather than left armed.

        Returns:
            Never returns.

        Raises:
            NotImplementedError: always. The real chain is derivable from the NSE FO bhavcopy
                (``bfinance.nse.bhavcopy`` with ``exchange="FO"``, verified 200 with 37,165 rows)
                plus a per-expiry contract master for strikes and IV — neither is implemented.
                Until then :meth:`Ticker.option_chain` returns an **empty** chain, which is
                truthful: an unlisted option has no contracts.
        """
        raise NotImplementedError(
            "Option chains are not available. This method previously returned a fabricated chain "
            "with randomly generated volume and open interest, which has been removed. An empty "
            "OptionChain is returned instead until a real F&O source is wired in."
        )
