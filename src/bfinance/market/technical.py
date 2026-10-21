"""
Local technical indicators for Indian equities: SMA, EMA, RSI, MACD, ADX, ATR, Bollinger bands
and pivot levels, computed in pure pandas from a frame the caller already has.

No network, no client, no cache
-------------------------------
Every method takes the OHLCV frame produced by ``Ticker.history`` / ``OHLCVEngine.fetch_history``
-- ``Open``, ``High``, ``Low``, ``Close``, ``Volume``, indexed by timestamp -- and returns
arithmetic over it. Nothing here fetches, retries, paces or degrades, because there is nothing
to degrade: either the caller has the bars or the indicator cannot be computed, and the second
answer is what this module exists to give correctly. That is the local half of operator decision
1 in ``plans/trendlyne-full-e2e.md``; the remote half (the robots-clean
``/equity/api/stock/adv-technical-analysis/`` route) is the trendlyne client's job.

The honesty contract
--------------------
The defect this module is written against is a *plausible* number: ``0.0`` where no measurement
exists. Every ``market/`` engine here already carries that rule -- ``quotes.moving_average``
returns ``None`` when the window is short rather than dividing an empty list -- and a rolling
indicator makes it worse rather than better, because a rolling window is undefined exactly where
a caller most wants a number. So:

* **"Insufficient" means a named quantity of bars.** An SMA needs ``window`` closes. An EMA needs
  ``span`` (its seed is the SMA of the first ``span`` closes). RSI, ATR and ADX need Wilder's
  window plus the prior bar each depends on. Bollinger needs ``window`` closes. Nothing is
  computed from a partial window and nothing is extrapolated backwards.
* **A missing bar is never filled.** ``rolling(..., min_periods=window)`` leaves a bar NaN when
  any close inside its window is missing, and the recursive smoothers below *reset* on a gap and
  re-seed once ``window`` consecutive real values are in hand. Nothing is carried across a gap,
  because carrying it is an interpolation this module was not asked to perform and would look
  exactly like data.
* **Zero and absent are different facts and stay different.** RSI is ``0.0`` when every change
  in the window was a loss (measured, and bearish) and ``NaN`` when the window contains no
  change at all (not measured). ``%B`` is ``NaN`` on a zero-width band rather than 0.0.
* **Shapes are stable so a caller can index them.** The Series methods always return a Series --
  possibly empty, possibly all-NaN -- never ``None``, so a positional lookup cannot raise where a
  real number would have sat. ``macd``/``bollinger`` always return their full key set. The two
  refusal-shaped calls are ``pivots`` (``None`` when the frame is too short) and ``snapshot``
  (always a dict, ``None`` in every slot it cannot measure).

Units
-----
Prices are rupees per share. ``rsi``/``adx``/``pct_b`` are dimensionless (0-100, 0-100, 0-1);
``atr`` is rupees; ``macd``/``signal``/``histogram`` are rupees; ``n_std`` is standard
deviations. Read them here rather than inferring them from a caller.
"""

from typing import Any, Dict, Optional
import math

import pandas as pd


def _positive_int(value: Any, label: str) -> int:
    """Window/span arguments are counts of bars, so a non-positive one is a caller error.

    Raised rather than clamped: ``sma(frame, 0)`` has no honest reading (an empty window has no
    mean), and silently turning it into ``sma(frame, 1)`` would publish a number the caller did
    not ask for under a name they did choose. Same shape as the argument refusals in
    ``CorporateActionsEngine.resolve_actions_async``.

    ``bool`` is refused explicitly, ahead of the integer check. ``bool`` is a subclass of ``int``
    and ``int(True) == 1``, while ``1 != True`` is ``False`` -- so every other part of this guard
    would wave ``sma(frame, window=True)`` through and answer a one-bar average under a name that
    reads like a flag. A window is a count; ``True`` is not one. ``numpy.bool_`` is refused on the
    same grounds and by the same predicate: ``series.notna().all()`` returns one, and it is the
    most natural thing a caller has to hand.
    """
    if pd.api.types.is_bool(value):
        raise ValueError(
            f"{label} must be a positive integer number of bars, got {value!r}. bool is a "
            f"subclass of int and True == 1, so it would otherwise be read as a one-bar window."
        )
    try:
        count = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a positive integer number of bars, got {value!r}") from exc
    if count < 1 or count != value:
        raise ValueError(f"{label} must be a positive integer number of bars, got {value!r}")
    return count


class TechnicalIndicators:
    """
    Pure-pandas technical indicators over an already-fetched OHLCV frame.

    Classmethod-only, like every other engine in ``bfinance.market``: nothing here holds state,
    because the only state a technical indicator has is the window it was handed, and a caller
    who wants a different window should get a different window rather than a mutated engine.
    """

    # ------------------------------------------------------------------ column plumbing

    @classmethod
    def _column(cls, frame: Any, field: str) -> pd.Series:
        """One OHLCV column as float, NaN preserved, matched case-insensitively.

        ``history()`` prints ``Open``/``Close`` while most third-party frames print
        ``open``/``close``, and a technical engine that raises ``KeyError`` on the second
        spelling is a nuisance rather than a guard, so both resolve to the same series.

        Two deliberate choices inside:

        * A column that is absent, or present but wholly unreadable, yields an **all-NaN series
          aligned to the frame**, not an exception and not zeros. Every indicator above it then
          reports "not measured" across its whole length, which is the truth: there is no close
          and no average of nothing.
        * ``Adj Close`` is never silently substituted for ``Close``. Which one an indicator reads
          is a real modelling choice -- the two disagree across any dividend or split -- and
          making it here, invisibly, from which column happens to be present would change every
          number in this module based on a flag the caller never passed to it.
        """
        if not isinstance(frame, pd.DataFrame):
            return pd.Series(dtype=float, name=field)
        index = frame.index
        for column in frame.columns:
            if str(column).strip().lower() == field:
                series = pd.to_numeric(frame[column], errors="coerce").astype(float)
                return series.rename(field)
        return pd.Series(float("nan"), index=index, name=field, dtype=float)

    # ------------------------------------------------------------ the recursive smoothers

    @classmethod
    def _recursive_mean(
        cls, series: pd.Series, alpha: float, min_periods: int, name: str
    ) -> pd.Series:
        """Alpha-weighted exponential mean, seeded with the simple mean of the first
        ``min_periods`` real values.

        Both smoothers this module needs are this one function: Wilder's smoothing is it with
        ``alpha = 1 / window``, an EMA is it with ``alpha = 2 / (span + 1)``. Writing it once is
        what keeps the two definitions from drifting apart, and it makes the seed *stated* rather
        than inherited from whichever pandas call was used -- after the first ``min_periods``
        values the seed equals ``sma(frame, min_periods)``, which is the convention charting
        platforms use and the one a hand calculation can reproduce.

        A gap resets the accumulator. A bar whose input is NaN produces NaN, and the values after
        it are NaN until ``min_periods`` consecutive real bars have been seen again -- the
        alternative, carrying the previous smoothed value through, is an interpolation dressed
        as an indicator, and it is what ``ewm`` does by default (measured on this pandas: with
        ``adjust=False`` a NaN bar reproduces the previous value verbatim).
        """
        values: list[float] = []
        seen = 0
        total = 0.0
        previous = float("nan")
        for raw in series.tolist():
            value = float(raw)
            if value != value:  # NaN: a bar that was not measured, not a bar worth zero
                seen, total, previous = 0, 0.0, float("nan")
                values.append(float("nan"))
                continue
            seen += 1
            total += value
            if seen < min_periods:
                values.append(float("nan"))
                continue
            previous = (total / min_periods) if seen == min_periods else (
                alpha * value + (1.0 - alpha) * previous
            )
            values.append(previous)
        return pd.Series(values, index=series.index, name=name, dtype=float)

    @classmethod
    def _ema_of(cls, series: pd.Series, span: int, name: str) -> pd.Series:
        """The EMA recursion over an arbitrary series (``Close``, or a MACD line)."""
        return cls._recursive_mean(series, 2.0 / (span + 1.0), span, name)

    @classmethod
    def _wilder_of(cls, series: pd.Series, window: int, name: str) -> pd.Series:
        """Wilder's smoothing over an arbitrary series (gains, losses, true range, +DM)."""
        return cls._recursive_mean(series, 1.0 / window, window, name)

    @classmethod
    def _true_range(cls, frame: Any) -> pd.Series:
        """Wilder's true range: the widest of today's range, up-gap and down-gap.

        ``max(high - low, |high - prev_close|, |low - prev_close|)``. The maximum is taken with
        ``skipna=False`` so a bar missing a high or a low yields NaN rather than the widest of
        whatever legs happen to be readable -- with ``skipna=True`` a missing ``low`` silently
        falls back to the close-gap leg, which is a smaller number that looks measured.

        The one exception is the first bar, which has no previous close at all; there Wilder's
        own definition is ``high - low``, and it is written out here rather than left to a
        skipna default that would also have quietly repaired every later gap.
        """
        high = cls._column(frame, "high")
        low = cls._column(frame, "low")
        close = cls._column(frame, "close")
        previous = close.shift(1)
        legs = pd.concat(
            [high - low, (high - previous).abs(), (low - previous).abs()], axis=1
        )
        true_range = legs.max(axis=1, skipna=False)
        if len(true_range):
            true_range = true_range.copy()
            true_range.iloc[0] = (high - low).iloc[0]
        return true_range.rename("true_range")

    # ------------------------------------------------------------------ trend indicators

    @classmethod
    def sma(cls, frame: Any, window: int = 20) -> pd.Series:
        """
        Arithmetic mean of the close over ``window`` bars, aligned to ``frame``'s index.

        Insufficient means fewer than ``window`` closes have been measured, so the first
        ``window - 1`` bars are NaN. A NaN anywhere inside the window makes that bar NaN as
        well (``min_periods=window``): the mean of nine prices and a hole is not a mean of ten
        prices, and writing it as one is the fill this module refuses.
        """
        count = _positive_int(window, "window")
        return cls._column(frame, "close").rolling(count, min_periods=count).mean().rename("sma")

    @classmethod
    def ema(cls, frame: Any, span: int = 20) -> pd.Series:
        """
        Exponential moving average of the close, seeded with the SMA of the first ``span`` closes.

        Insufficient means fewer than ``span`` closes, so the first ``span - 1`` bars are NaN
        and the ``span``-th is exactly ``sma(frame, span)``; from there it recurs as
        ``y = 2/(span+1) * close + (1 - 2/(span+1)) * y_prev``. See :meth:`_recursive_mean` for
        what happens across a gap.
        """
        span = _positive_int(span, "span")
        return cls._ema_of(cls._column(frame, "close"), span, "ema")

    @classmethod
    def rsi(cls, frame: Any, window: int = 14) -> pd.Series:
        """
        Wilder's Relative Strength Index, on a 0-100 scale.

        Seeded with the mean of the first ``window`` changes and smoothed with Wilder's
        ``alpha = 1/window``, so the first value needs ``window + 1`` closes -- ``window``
        changes need a prior bar to be a change *from*. The three outcomes are kept distinct:

        * every real change in the window was a gain -> ``100.0`` (measured, at the ceiling);
        * every real change was a loss -> ``0.0`` (measured, at the floor);
        * the window holds no change at all (a flat series) -> NaN. ``0.0`` and ``100.0`` would
          both be false, and the conventional "50 = neutral" is a number this module has no
          measurement for.
        """
        count = _positive_int(window, "window")
        change = cls._column(frame, "close").diff()
        average_gain = cls._wilder_of(change.clip(lower=0.0), count, "avg_gain")
        average_loss = cls._wilder_of((-change).clip(lower=0.0), count, "avg_loss")
        rsi = pd.Series(float("nan"), index=change.index, name="rsi", dtype=float)
        for position in range(len(change)):
            gain = average_gain.iat[position]
            loss = average_loss.iat[position]
            if gain != gain or loss != loss:
                continue
            if loss == 0.0:
                # No downside in the window. 100 only if there was an upside to have.
                rsi.iat[position] = 100.0 if gain > 0.0 else float("nan")
            else:
                rsi.iat[position] = 100.0 - (100.0 / (1.0 + (gain / loss)))
        return rsi

    @classmethod
    def macd(
        cls, frame: Any, fast: int = 12, slow: int = 26, signal: int = 9
    ) -> Dict[str, pd.Series]:
        """
        MACD lines: ``{"macd": ..., "signal": ..., "histogram": ...}``, all rupees per share.

        ``macd`` is the fast EMA minus the slow EMA and the ``signal`` is an EMA of that line,
        so the first defined ``macd`` is at the slow span and the first defined ``signal`` is
        ``signal`` bars after it. Every key is always present; a frame too short to seed either
        EMA yields NaN series rather than a shorter dict, so a caller indexing
        ``result["histogram"]`` cannot land on a ``KeyError`` where a real number would have been.
        """
        fast_span = _positive_int(fast, "fast")
        slow_span = _positive_int(slow, "slow")
        signal_span = _positive_int(signal, "signal")
        close = cls._column(frame, "close")
        macd_line = cls._ema_of(close, fast_span, "macd") - cls._ema_of(close, slow_span, "macd")
        signal_line = cls._ema_of(macd_line, signal_span, "signal")
        return {
            "macd": macd_line,
            "signal": signal_line,
            "histogram": (macd_line - signal_line).rename("histogram"),
        }

    # ------------------------------------------------------------- volatility indicators

    @classmethod
    def adx(cls, frame: Any, window: int = 14) -> pd.Series:
        """
        Wilder's Average Directional Index, on a 0-100 scale.

        Built from Wilder's +DM/-DM, their smoothed percentages of true range, the directional
        index, and a final Wilder smoothing of that. Bar counts, all from :meth:`_true_range` and
        the DM definitions below: true range starts on bar 0 (Wilder's ``High - Low`` there), the
        first directional movement needs a bar to move from so it starts on bar 1, the smoothed
        DI percentages therefore start on bar ``window``, and the final smoothing of DX needs
        ``window`` more -- so the first defined ADX is the ``2 * window``-th bar, and a frame
        with fewer than ``2 * window`` bars has none at all.

        +DM is ``NaN`` on the first bar (there is no prior bar to move from) and ``0.0`` where an
        up-move failed to dominate the down-move, which is a measured zero and not a hole. DX is
        NaN when ``+DI + -DI`` is zero -- a window with no directional movement has no strength
        to report, and dividing by it would print a number that means nothing.
        """
        count = _positive_int(window, "window")
        high = cls._column(frame, "high")
        low = cls._column(frame, "low")
        up_move = high.diff()
        down_move = -low.diff()
        measured = up_move.notna() & down_move.notna()
        plus_dm = up_move.where((up_move > down_move) & (up_move > 0.0), 0.0).where(measured)
        minus_dm = down_move.where((down_move > up_move) & (down_move > 0.0), 0.0).where(measured)

        smoothed_range = cls._wilder_of(cls._true_range(frame), count, "smoothed_tr")
        plus_di = 100.0 * cls._wilder_of(plus_dm, count, "plus_dm") / smoothed_range
        minus_di = 100.0 * cls._wilder_of(minus_dm, count, "minus_dm") / smoothed_range
        total = plus_di + minus_di
        directional_index = (100.0 * (plus_di - minus_di).abs() / total.where(total != 0.0))
        return cls._wilder_of(directional_index, count, "adx")

    @classmethod
    def atr(cls, frame: Any, window: int = 14) -> pd.Series:
        """
        Wilder's Average True Range, in rupees per share.

        The mean of the first ``window`` true ranges and then Wilder's smoothing of the rest, so
        the first value is at bar ``window - 1``. The ``high``/``low``/``Close`` columns it reads
        are exactly the ones ATR is defined over; ``volume`` plays no part in any indicator here.
        """
        count = _positive_int(window, "window")
        return cls._wilder_of(cls._true_range(frame), count, "atr")

    @classmethod
    def bollinger(
        cls, frame: Any, window: int = 20, n_std: float = 2.0
    ) -> Dict[str, pd.Series]:
        """
        Bollinger bands: ``{"mid", "upper", "lower", "pct_b"}`` -- three in rupees, one a ratio.

        ``mid`` is the SMA, the bands are ``mid +/- n_std * sigma`` with ``sigma`` the
        **population** standard deviation (``ddof=0``, the charting-platform convention), and
        ``pct_b`` is ``(close - lower) / (upper - lower)``: 0 at the lower band, 1 at the upper,
        and outside 0..1 at the bands themselves.

        ``pct_b`` is NaN on a zero-width band -- a window with no variance has no position
        within the band, and ``0.0`` there would say "at the lower band" for a price that is
        equally at the upper one.
        """
        count = _positive_int(window, "window")
        try:
            deviation = float(n_std)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"n_std must be a number of standard deviations, got {n_std!r}") from exc
        if not math.isfinite(deviation) or deviation < 0.0:
            raise ValueError(f"n_std must be a finite, non-negative number, got {n_std!r}")

        close = cls._column(frame, "close")
        mid = close.rolling(count, min_periods=count).mean()
        spread = close.rolling(count, min_periods=count).std(ddof=0)
        upper = (mid + deviation * spread).rename("upper")
        lower = (mid - deviation * spread).rename("lower")
        width = upper - lower
        return {
            "mid": mid.rename("mid"),
            "upper": upper,
            "lower": lower,
            "pct_b": ((close - lower) / width.where(width != 0.0)).rename("pct_b"),
        }

    # --------------------------------------------------------------- pivot point levels

    #: The four families, each producing ``pp`` plus three supports and three resistances.
    PIVOT_FAMILIES = ("classic", "woodie", "camarilla", "fibonacci")

    #: The levels every family publishes, in the order a charting platform prints them.
    PIVOT_LEVELS = ("pp", "s1", "s2", "s3", "r1", "r2", "r3")

    @classmethod
    def pivots(cls, frame: Any) -> Optional[Dict[str, Dict[str, Optional[float]]]]:
        """
        Pivot, support and resistance levels for all four families, in rupees per share.

        ``{"classic": {...}, "woodie": {...}, "camarilla": {...}, "fibonacci": {...}}``, each
        carrying exactly :data:`PIVOT_LEVELS`, computed from the **last completed bar** -- the
        final row's ``High``/``Low``/``Close``. Those levels therefore describe the session that
        *follows* that bar, which is what a pivot is: the only bar whose range is known before
        the next one opens. A caller comparing them against the same frame's last close is
        comparing across sessions, and that is the arithmetic, not a bug in it.

        A refusal, not a partial dict, when the frame holds fewer than **two** rows: one bar
        cannot establish that the frame is a completed session series at all, and a dict with
        seven levels in it is indistinguishable from a real answer to a caller who only checks
        for keys. Fewer than two rows returns ``None``. So does a non-frame. When the final
        bar's ``High``/``Low``/``Close`` is NaN, the key set is still returned but every level in
        it is ``None``: one unreadable leg makes all seven levels undefined, and publishing six
        good ones and a hole would be a partial answer to a question that has none.

        The formulas, with ``range = high - low``:

        * ``pp`` is ``(high + low + close) / 3`` for classic, Camarilla and Fibonacci --
          Camarilla defines no pivot of its own, so the classic average is what is labelled
          ``pp`` beside its levels -- and ``(high + low + 2*close) / 4`` for Woodie.
        * classic and Woodie then share six levels, which is the only thing that differs between
          them: ``r1 = 2*pp - low``, ``s1 = 2*pp - high``, ``r2 = pp + range``,
          ``s2 = pp - range``, ``r3 = high + 2*(pp - low)``, ``s3 = low - 2*(high - pp)``.
        * Fibonacci: ``pp +/- 0.382 * range``, ``pp +/- 0.618 * range`` and ``pp +/- range``.
        * Camarilla: ``close +/- range * 1.1/12``, ``+/- * 1.1/6`` and ``+/- * 1.1/4`` -- that
          is, 1x, 2x and 3x its own first offset off the **close**, not off the pivot. The 1.1
          is Camarilla's own multiplier and not a typo; its fourth level, which this engine does
          not publish, is 6x.
        """
        if not isinstance(frame, pd.DataFrame) or len(frame) < 2:
            return None

        high = cls._column(frame, "high")
        low = cls._column(frame, "low")
        close = cls._column(frame, "close")
        h, l, c = high.iloc[-1], low.iloc[-1], close.iloc[-1]
        if any(value != value for value in (h, l, c)):  # NaN leg: no level, not half of one
            return {family: {level: None for level in cls.PIVOT_LEVELS} for family in cls.PIVOT_FAMILIES}

        h, l, c = float(h), float(l), float(c)
        spread = h - l
        pivots: Dict[str, Dict[str, Optional[float]]] = {}
        for family in cls.PIVOT_FAMILIES:
            if family == "woodie":
                pp = (h + l + 2.0 * c) / 4.0
                levels = cls._range_levels(pp, h, l, spread)
            elif family == "fibonacci":
                pp = (h + l + c) / 3.0
                levels = {
                    "pp": pp,
                    "s3": pp - spread,
                    "s2": pp - 0.618 * spread,
                    "s1": pp - 0.382 * spread,
                    "r1": pp + 0.382 * spread,
                    "r2": pp + 0.618 * spread,
                    "r3": pp + spread,
                }
            elif family == "camarilla":
                # Off the close, at 1x/2x/3x the range*1.1/12 offset. See the docstring.
                pp = (h + l + c) / 3.0
                quarter = spread * 1.1 / 12.0
                levels = {
                    "pp": pp,
                    "s3": c - 3.0 * quarter,
                    "s2": c - 2.0 * quarter,
                    "s1": c - quarter,
                    "r1": c + quarter,
                    "r2": c + 2.0 * quarter,
                    "r3": c + 3.0 * quarter,
                }
            else:
                pp = (h + l + c) / 3.0
                levels = cls._range_levels(pp, h, l, spread)
            pivots[family] = {level: float(levels[level]) for level in cls.PIVOT_LEVELS}
        return pivots

    @staticmethod
    def _range_levels(pp: float, high: float, low: float, spread: float) -> Dict[str, float]:
        """The shared six levels; only the pivot point differs between classic and Woodie."""
        return {
            "pp": pp,
            "s3": low - 2.0 * (high - pp),
            "s2": pp - spread,
            "s1": 2.0 * pp - high,
            "r1": 2.0 * pp - low,
            "r2": pp + spread,
            "r3": high + 2.0 * (pp - low),
        }

    # -------------------------------------------------------------------- one-row summary

    #: The snapshot's keys, in publication order. Every one is ``None`` when its window is short.
    SNAPSHOT_KEYS = (
        "close",
        "sma_20",
        "sma_50",
        "sma_200",
        "rsi_14",
        "macd_hist",
        "atr_14",
        "adx_14",
        "bollinger_pct_b",
        "pivots",
    )

    @staticmethod
    def _last_number(series: pd.Series) -> Optional[float]:
        """The final bar's value, or ``None`` when it is NaN.

        The final bar specifically, not the last non-NaN value: a stale average read as though it
        were today's is a fabrication with a date on it, and "the latest" has to mean the latest
        bar to be worth anything.
        """
        if len(series) == 0:
            return None
        value = series.iloc[-1]
        value = float(value)
        return None if value != value else value

    @classmethod
    def snapshot(cls, frame: Any) -> Dict[str, Any]:
        """
        The latest value of every headline indicator, as one dict.

        Keys are :data:`SNAPSHOT_KEYS`; prices and ``atr_14`` are rupees, ``rsi_14``/``adx_14``
        are 0-100, ``bollinger_pct_b`` is 0-1, ``macd_hist`` is rupees, and ``pivots`` is the
        :meth:`pivots` dict or ``None``.

        Always a dict, always every key present. A 5-bar frame has no SMA-20, no MACD, no RSI,
        no ATR, no ADX and no Bollinger bands, so those read ``None`` -- not ``0.0``, which
        would say "the 200-day average is zero" and screen as a real value -- and a caller can
        render the whole row without branching on which windows happened to fit. ``pivots`` is
        the exception to "no windows to fill": it needs two bars, not twenty, so it is measured
        on frames where every indicator beside it is ``None``.
        """
        summary: Dict[str, Any] = {key: None for key in cls.SNAPSHOT_KEYS}
        if not isinstance(frame, pd.DataFrame) or frame.empty:
            return summary

        macd = cls.macd(frame)
        summary["close"] = cls._last_number(cls._column(frame, "close"))
        summary["sma_20"] = cls._last_number(cls.sma(frame, 20))
        summary["sma_50"] = cls._last_number(cls.sma(frame, 50))
        summary["sma_200"] = cls._last_number(cls.sma(frame, 200))
        summary["rsi_14"] = cls._last_number(cls.rsi(frame, 14))
        summary["macd_hist"] = cls._last_number(macd["histogram"])
        summary["atr_14"] = cls._last_number(cls.atr(frame, 14))
        summary["adx_14"] = cls._last_number(cls.adx(frame, 14))
        summary["bollinger_pct_b"] = cls._last_number(cls.bollinger(frame, 20, 2.0)["pct_b"])
        summary["pivots"] = cls.pivots(frame)
        return summary
