"""
NSE Derivatives and Options chain engine matching yfinance's OptionChain API.

Parity note: yfinance 1.7.0 returns NO options data for NSE underlyings
(``options == ()``, ``option_chain()`` empty, a dated expiry raises ValueError) --
even F&O stocks like RELIANCE.NS. bfinance matches that behaviour, and matches it by
*absence*: there is no options feed behind this module.

What that means concretely, since a reader should not have to reconstruct it from a
deleted method:

* :meth:`DerivativesEngine.resolve_options` returns ``()`` unless a real F&O membership
  list is supplied. ``ticker.py`` supplies none, so ``Ticker.options`` is always ``()``.
  The ticker-level gating that used to be an open task is done.
* :meth:`DerivativesEngine.generate_option_chain` raises ``NotImplementedError``. It
  used to synthesise strikes, prices, volumes and open interest from a seeded RNG and
  hand the result back as exchange data. That was removed; its docstring says why.
* :meth:`DerivativesEngine.empty_option_chain` is what a caller actually receives, and
  it is a truthful answer: an unlisted option has no contracts.

No volume, open interest, Greek or implied volatility is produced anywhere in this
module. The one value that looks like a feed is
:meth:`DerivativesEngine.get_upcoming_expiries`, which is a trading *calendar* computed
arithmetically -- a schedule, not market data.
"""

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
    The NSE options surface of bfinance, shaped like yfinance's ``OptionChain``.

    What it does: return an empty chain, and compute the last-Thursday expiry calendar.
    What it does not do: produce a real chain. See :meth:`generate_option_chain` for why
    the synthesis path was deleted, and :meth:`resolve_options` for the gate that keeps
    :attr:`Ticker.options` empty.
    """

    @classmethod
    def resolve_options(
        cls, symbol: str, fo_symbols: Optional[List[str]] = None
    ) -> Tuple[str, ...]:
        """
        Parity helper: return () unless `symbol` is in a real F&O membership
        list (yfinance 1.7.0 returns () for all NSE underlyings probed).

        Supplying a real membership list is still the requirement, but the wiring
        is no longer outstanding: `ticker.py` calls this with no `fo_symbols`, so
        `Ticker.options` is always () today. See the module docstring.
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
        """
        The yfinance-compatible empty chain: a **zero-row** frame on each side.

        Not "a frame of zeros" -- ``calls`` and ``puts`` are both ``(0, 13)`` with every
        cell null, carrying :data:`YF_CHAIN_COLUMNS` so a caller can index them without a
        KeyError. The distinction is load-bearing: a filled frame would be readable as a
        real quote, which is exactly the failure mode this module no longer has.

        Pinned by ``tests/test_no_lying_docs.py``.
        """
        empty_calls = pd.DataFrame({c: pd.Series(dtype=float) for c in YF_CHAIN_COLUMNS})
        empty_puts = pd.DataFrame({c: pd.Series(dtype=float) for c in YF_CHAIN_COLUMNS})
        return OptionChain(calls=empty_calls, puts=empty_puts)

    @classmethod
    def get_upcoming_expiries(cls) -> Tuple[str, ...]:
        """
        The NSE monthly F&O expiry dates: the last Thursday of each upcoming month.

        Returns **3 or 4** dates, never more. Four months are considered, but the current
        month is dropped once its last Thursday is more than a day behind ``today`` (the
        ``last_thursday >= today - 1 day`` filter below). Since a month's last Thursday
        always falls between the 22nd and the 31st, that makes 4 the normal answer and 3
        the answer for the 1-to-6 days *after* the current month's last Thursday. This
        was previously documented as an unconditional "next 4", which was simply wrong
        for those days of every month.

        Purely arithmetic: a trading calendar, not market data, and the only value in
        this module that resembles a feed. No returned date is ever in the past, and all
        of them are Thursdays -- on the 36-or-so days a year that are themselves a last
        Thursday, the first date returned is today.

        The "3 or 4" claim is checked against the actual return value by
        ``tests/test_no_lying_docs.py`` -- keep the two in step.
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

        It had no network path at all. Strikes were synthesised from `cmp` on a price-tiered
        step, prices came from a closed-form heuristic, and ``volume`` and
        ``openInterest`` came from a SHA-256-seeded ``random.Random`` keyed on the contract
        symbol -- i.e. **literally random numbers presented as exchange data**.
        ``change`` and ``percentChange`` were pinned to ``0.0`` so a downstream consumer
        could not even tell the values were synthetic. That generator, and the ``hashlib``
        and ``random`` imports that existed only to serve it, have been deleted.

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
