"""Local technical indicators: what each one computes, and what it refuses to invent.

``bfinance.market.technical.TechnicalIndicators`` is the local half of operator decision 1 in
``plans/trendlyne-full-e2e.md``: pure pandas arithmetic over an OHLCV frame the caller already
has, with no network anywhere in the module. Everything here is offline and synthetic -- no
fixture, no upstream, no cache.

THE CLAIM UNDER TEST
--------------------
An indicator's failure mode is not an exception, it is a **plausible number**: an SMA-200
computed from ten closes because the caller did not check the length, an RSI of 0.0 for a series
that was never measured, a Bollinger ``%B`` of 0.0 on a window with no variance. Each of those
satisfies ``value is not None``, prints as a cell, and screens as real. So this file asserts
three things for every indicator, not one:

* **the arithmetic is right** -- against numbers written here, or against a first-principles
  reference implementation in this file (Wilder's smoothers are re-derived in plain Python;
  the pivot levels are re-derived from their formulas). The references never call the engine,
  or they would only prove the engine agrees with itself;
* **an insufficient window is NaN or None, never 0.0**;
* **a hole in the input stays a hole** -- no fill, no carry-forward, no skipping.

The "never 0.0" assertions are written as ``value is None`` / ``pd.isna(...)`` rather than as a
``!= 0`` comparison on purpose: the defect is a fabricated zero, and a test that only checked
``not zero`` would pass on any other fabricated value too.
"""

import ast
import math
import pathlib

import pandas as pd
import pytest

from bfinance.market import TechnicalIndicators


# ============================================================ fixtures and references


def _frame(rows, start="2026-01-01"):
    """An OHLCV frame shaped exactly like ``history()``'s: capitalised columns, dated index.

    ``rows`` is a list of ``(open, high, low, close)`` tuples, so a test can write the bar it
    wants to reason about without a generator hiding the arithmetic behind it.
    """
    index = pd.date_range(start, periods=len(rows), freq="B", name="Date")
    columns = ["Open", "High", "Low", "Close", "Volume"]
    values = [tuple(row) + (1000.0,) for row in rows]
    return pd.DataFrame(values, columns=columns, index=index).astype(float)


def _closes_frame(closes):
    """A frame whose closes are the values under test, with a constant 1% band around each."""
    return _frame([(c, c * 1.01, c * 0.99, c) for c in closes])


def _flat_frame(value, periods):
    return _closes_frame([value] * periods)


def _rsi_reference(closes, window):
    """Wilder's RSI re-derived from the definition, in plain Python.

    Seed: the mean of the first ``window`` changes (so the first value is at bar ``window``,
    because a change needs a bar to change from). Then Wilder's ``alpha = 1/window`` recursion.
    The two edges are asserted in the tests, because they are where a second implementation
    usually differs: a flat window is NaN rather than 50, and no loss at all is 100.
    """
    out = [float("nan")] * len(closes)
    avg_gain = avg_loss = float("nan")
    total_gain = total_loss = 0.0
    for index in range(1, len(closes)):
        change = closes[index] - closes[index - 1]
        gain = max(change, 0.0)
        loss = max(-change, 0.0)
        total_gain += gain
        total_loss += loss
        if index < window:
            continue
        if index == window:
            avg_gain, avg_loss = total_gain / window, total_loss / window
        else:
            avg_gain = (avg_gain * (window - 1) + gain) / window
            avg_loss = (avg_loss * (window - 1) + loss) / window
        if avg_loss == 0.0:
            # No downside in the window. 100 only if there was an upside to have.
            out[index] = 100.0 if avg_gain > 0.0 else float("nan")
        else:
            out[index] = 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    return out


def _true_range_reference(rows):
    """``max(H-L, |H - C_prev|, |L - C_prev|)``; the first bar has no prior close, so it is H-L."""
    ranges = []
    for index, (open_, high, low, close) in enumerate(rows):
        if index == 0:
            ranges.append(high - low)
        else:
            previous_close = rows[index - 1][3]
            ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
    return ranges


def _wilder_reference(values, window, start_index):
    """Wilder's smoothing of a list: mean of the first ``window``, then ``alpha = 1/window``."""
    out = [float("nan")] * len(values)
    previous = float("nan")
    seen = 0
    total = 0.0
    for index, value in enumerate(values):
        if index < start_index:
            continue
        if value is None or (isinstance(value, float) and math.isnan(value)):
            seen, total, previous = 0, 0.0, float("nan")
            continue
        seen += 1
        total += value
        if seen < window:
            continue
        previous = total / window if seen == window else (previous * (window - 1) + value) / window
        out[index] = previous
    return out


def _adx_reference(rows, window):
    """ADX from Wilder's definition: +DM/-DM, their DI percentages, DX, then a Wilder DX mean."""
    true_ranges = _true_range_reference(rows)
    up_moves = [None] + [rows[i][1] - rows[i - 1][1] for i in range(1, len(rows))]
    down_moves = [None] + [rows[i - 1][2] - rows[i][2] for i in range(1, len(rows))]
    plus_dm, minus_dm = [], []
    for up, down in zip(up_moves, down_moves):
        if up is None or down is None:
            plus_dm.append(float("nan"))
            minus_dm.append(float("nan"))
            continue
        plus_dm.append(up if (up > down and up > 0) else 0.0)
        minus_dm.append(down if (down > up and down > 0) else 0.0)

    smoothed_tr = _wilder_reference(true_ranges, window, 0)
    smoothed_plus = _wilder_reference(plus_dm, window, 0)
    smoothed_minus = _wilder_reference(minus_dm, window, 0)
    plus_di, minus_di, dx = [], [], []
    for tr, up, down in zip(smoothed_tr, smoothed_plus, smoothed_minus):
        if any(v != v for v in (tr, up, down)) or tr == 0.0:
            plus_di.append(float("nan"))
            minus_di.append(float("nan"))
            dx.append(float("nan"))
            continue
        p, m = 100.0 * up / tr, 100.0 * down / tr
        plus_di.append(p)
        minus_di.append(m)
        dx.append(100.0 * abs(p - m) / (p + m) if (p + m) != 0.0 else float("nan"))
    return _wilder_reference(dx, window, 0)


# ================================================================= the module's contract


class TestTheEngineIsReachableAndOpensNoSocket:
    def test_it_is_exported_from_the_market_barrel(self):
        """The barrel is the engine's public door; a class that is not in it is unreachable
        by star import and by ``from bfinance.market import *``."""
        import bfinance.market as market

        assert "TechnicalIndicators" in market.__all__
        assert market.TechnicalIndicators is TechnicalIndicators

    def test_the_module_imports_nothing_that_could_reach_the_network(self):
        """Checked over the AST's import nodes, not the file text.

        This module's own docstring explains that it does no fetching, and a text scan would
        read that prose and fail -- the same trap ``tests/test_no_lying_docs.py`` documents. An
        import node is the only thing that can violate it, and the allowed set is short enough
        to write out: ``pandas`` and the stdlib's ``math``/``typing``.
        """
        path = pathlib.Path(TechnicalIndicators.__module__.replace(".", "/"))
        source = (pathlib.Path(__file__).resolve().parents[1] / "src" / path).with_suffix(".py")
        imported = set()
        for node in ast.walk(ast.parse(source.read_text(encoding="utf-8-sig"))):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])

        assert imported <= {"math", "typing", "pandas"}, f"unexpected imports: {sorted(imported)}"


# ================================================================ SMA / EMA: the windows


class TestTheMovingAverages:
    def test_sma_of_a_constant_series_is_that_constant(self):
        """The arithmetic-exact case: ten bars at 100.0 average to 100.0, and every window in
        the frame must give the same answer. A series that never moves is the one input whose
        mean cannot be argued about."""
        frame = _flat_frame(100.0, 30)

        sma = TechnicalIndicators.sma(frame, 10)

        assert sma.dropna().tolist() == [100.0] * 21
        assert sma.iloc[:9].isna().all(), "a 10-bar window was filled before 10 bars existed"
        assert sma.iloc[9] == pytest.approx(100.0)

    def test_sma_of_a_ramp_equals_the_hand_computed_mean(self):
        """Closes 100..139 over 40 bars; the last 20-bar mean is the sum of 120..139 over 20,
        written out here rather than produced by another call into the engine."""
        closes = [100.0 + i for i in range(40)]
        frame = _closes_frame(closes)

        expected = sum(closes[-20:]) / 20.0

        assert TechnicalIndicators.sma(frame, 20).iloc[-1] == pytest.approx(expected)
        assert expected == pytest.approx(129.5), "the hand-written expectation is itself wrong"

    def test_an_sma_shorter_than_its_window_is_nan_not_zero(self):
        """Five closes, a 200-bar window: the honest answer is "not measured", and the number
        that would look fine downstream is 0.0."""
        frame = _closes_frame([100.0 + i for i in range(5)])

        sma = TechnicalIndicators.sma(frame, 200)

        assert sma.isna().all()
        assert not (sma.dropna() == 0.0).any(), "an empty window produced a zero"

    def test_an_ema_is_seeded_with_its_own_span_sma_then_recurs(self):
        """The seed is ``sma(frame, span)`` -- visible, not incidental -- and the next bar is the
        documented recursion ``y = 2/(span+1)*close + (1 - 2/(span+1))*y_prev``. Both numbers
        here are computed in this test from the closes."""
        closes = [100.0 + i for i in range(30)]
        frame = _closes_frame(closes)
        span = 10

        ema = TechnicalIndicators.ema(frame, span)
        seed = sum(closes[:span]) / span
        alpha = 2.0 / (span + 1.0)

        assert ema.iloc[: span - 1].isna().all()
        assert ema.iloc[span - 1] == pytest.approx(seed)
        assert ema.iloc[span - 1] == pytest.approx(TechnicalIndicators.sma(frame, span).iloc[span - 1])
        assert ema.iloc[span] == pytest.approx(alpha * closes[span] + (1.0 - alpha) * seed)

    def test_an_ema_shorter_than_its_span_is_nan(self):
        frame = _closes_frame([100.0 + i for i in range(4)])

        assert TechnicalIndicators.ema(frame, 20).isna().all()

    def test_both_averages_stay_aligned_to_the_frame(self):
        """Alignment is what makes them usable positionally: the i-th indicator value belongs to
        the i-th bar, including at the bars where it is NaN."""
        frame = _closes_frame([100.0 + i for i in range(30)])

        for series in (
            TechnicalIndicators.sma(frame, 20),
            TechnicalIndicators.ema(frame, 20),
        ):
            assert series.index.equals(frame.index)


# ============================================================================ RSI


class TestRsi:
    def test_a_strictly_rising_series_is_the_ceiling_not_the_floor(self):
        """Every change in the window is a gain, so the average loss is exactly zero and RSI is
        100.0. The companion assertion matters as much as the value: 0.0 would be the same
        arithmetic with the sign flipped, and is the number a ratio-shaped fabricator emits."""
        frame = _closes_frame([100.0 + i for i in range(30)])

        rsi = TechnicalIndicators.rsi(frame, 14)

        assert rsi.iloc[-1] == pytest.approx(100.0)
        assert rsi.dropna().min() > 0.0

    def test_a_strictly_falling_series_is_zero_because_zero_is_measured(self):
        """The mirror of the case above, and the reason ``0.0`` is a legal RSI here: every change
        really was a loss. It is the *unmeasured* zero -- a short window, a flat series -- that
        this module refuses."""
        frame = _closes_frame([200.0 - i for i in range(30)])

        rsi = TechnicalIndicators.rsi(frame, 14)

        assert rsi.iloc[-1] == pytest.approx(0.0)

    def test_a_flat_series_has_no_strength_to_report(self):
        """Average gain and average loss are both zero, so the ratio is 0/0. The conventional
        answer is 50; this engine returns NaN, because 50 would be a number nobody measured."""
        frame = _flat_frame(75.0, 40)

        rsi = TechnicalIndicators.rsi(frame, 14)

        assert rsi.iloc[-1] is not None and math.isnan(rsi.iloc[-1])

    def test_it_matches_a_first_principles_wilder_rsi(self):
        """The oracle is re-derived in this file (``_rsi_reference``) and never calls the engine,
        so agreement is evidence about the recursion and not about a shared helper."""
        closes = [
            100.0, 101.5, 100.2, 102.7, 103.1, 101.0, 99.4, 100.9, 103.3, 104.8,
            103.9, 102.2, 104.1, 106.0, 105.2, 103.7, 104.9, 107.2, 106.6, 105.1,
            107.9, 109.4, 108.3, 106.0, 107.2, 110.1, 111.6, 110.4, 109.0, 112.3,
        ]
        frame = _closes_frame(closes)

        rsi = TechnicalIndicators.rsi(frame, 14)

        for actual, expected in zip(rsi.tolist(), _rsi_reference(closes, 14)):
            if expected != expected:
                assert math.isnan(actual), f"expected NaN, got {actual}"
            else:
                assert actual == pytest.approx(expected, rel=1e-12)

    def test_the_first_value_needs_a_prior_bar_to_have_changed_from(self):
        """``window`` changes require ``window + 1`` closes. A first value one bar earlier would
        mean the very first bar's move was measured against something that does not exist."""
        frame = _closes_frame([100.0 + i for i in range(40)])

        rsi = TechnicalIndicators.rsi(frame, 14)

        assert rsi.iloc[:14].isna().all()
        assert not math.isnan(rsi.iloc[14])

    def test_a_frame_too_short_for_rsi_is_nan(self):
        frame = _closes_frame([100.0 + i for i in range(8)])

        assert TechnicalIndicators.rsi(frame, 14).isna().all()


# ============================================================================ MACD


class TestMacd:
    def test_it_returns_the_three_lines_whatever_the_frame(self):
        """Shape first: a caller indexing ``["histogram"]`` must not hit a ``KeyError`` where a
        real number would have been, so the key set is fixed even on an empty frame."""
        empty = _frame([])

        for frame in (empty, _closes_frame([100.0 + i for i in range(5)]), _closes_frame([100.0 + i for i in range(60)])):
            result = TechnicalIndicators.macd(frame)
            assert sorted(result) == ["histogram", "macd", "signal"]
            for line in result.values():
                assert isinstance(line, pd.Series)
                assert line.index.equals(frame.index)

    def test_the_histogram_is_positive_for_an_accelerating_ramp(self):
        """A quadratic ramp rises faster and faster, so the fast EMA leads the slow one and the
        MACD line sits above its own signal. This is the sign assertion the plan asks for --
        checked on a curve rather than a straight line, because a *linear* ramp has no momentum
        (see the next test) and would make the claim vacuous."""
        frame = _closes_frame([100.0 + i * i for i in range(60)])

        result = TechnicalIndicators.macd(frame)

        assert result["histogram"].iloc[-1] > 0.0
        assert result["macd"].iloc[-1] > result["signal"].iloc[-1]
        # ... and it is the MACD line that is above, not a difference of two NaNs.
        assert not math.isnan(result["macd"].iloc[-1])

    def test_a_constant_slope_has_no_momentum(self):
        """The negative control for the test above, and the reason it is worth having: on a
        straight ramp both EMAs converge to the same line, so the histogram collapses to zero.
        A histogram that stayed positive on a constant slope would be measuring something other
        than the difference between the two averages."""
        frame = _closes_frame([100.0 + i for i in range(60)])

        histogram = TechnicalIndicators.macd(frame)["histogram"].iloc[-1]

        assert histogram == pytest.approx(0.0, abs=1e-9)

    def test_the_macd_line_is_the_difference_of_two_emas(self):
        """The definition, checked against the engine's own EMA at a bar where both are defined
        -- a structural claim that does not depend on any MACD-specific arithmetic."""
        frame = _closes_frame([100.0 + i * i for i in range(60)])
        result = TechnicalIndicators.macd(frame, fast=5, slow=20, signal=4)

        expected = (
            TechnicalIndicators.ema(frame, 5) - TechnicalIndicators.ema(frame, 20)
        )
        assert result["macd"].iloc[-1] == pytest.approx(expected.iloc[-1])
        assert result["histogram"].iloc[-1] == pytest.approx(
            result["macd"].iloc[-1] - result["signal"].iloc[-1]
        )

    def test_a_frame_too_short_to_seed_the_slow_ema_is_all_nan(self):
        frame = _closes_frame([100.0 + i for i in range(10)])

        result = TechnicalIndicators.macd(frame)

        for line in result.values():
            assert line.isna().all(), "a MACD line was published from an unseeded EMA"
            assert not (line.dropna() == 0.0).any()


# ================================================================ ATR and ADX (Wilder)


class TestAtr:
    def test_it_matches_a_first_principles_true_range_and_wilder_mean(self):
        """``_true_range_reference`` and ``_wilder_reference`` in this file re-derive both legs of
        the definition; the engine is compared against them bar by bar."""
        rows = [
            (100.0, 105.0, 99.0, 104.0),
            (104.0, 110.0, 103.0, 103.5),
            (103.5, 108.0, 102.0, 107.5),
            (107.5, 109.0, 100.0, 101.0),
            (101.0, 103.0, 99.5, 102.5),
            (102.5, 112.0, 102.0, 111.0),
            (111.0, 113.0, 109.0, 110.0),
            (110.0, 115.0, 109.5, 114.5),
            (114.5, 116.0, 112.0, 113.0),
            (113.0, 118.0, 112.5, 117.0),
            (117.0, 119.0, 115.0, 116.0),
            (116.0, 120.0, 114.0, 119.5),
            (119.5, 121.0, 118.0, 118.5),
            (118.5, 122.0, 117.0, 121.5),
        ]
        frame = _frame(rows)
        expected = _wilder_reference(_true_range_reference(rows), 5, 0)

        atr = TechnicalIndicators.atr(frame, 5)

        for index, (actual, want) in enumerate(zip(atr.tolist(), expected)):
            if want != want:
                assert math.isnan(actual), f"bar {index}: expected NaN, got {actual}"
            else:
                assert actual == pytest.approx(want, rel=1e-12), f"bar {index}"

    def test_the_first_true_range_is_the_first_bars_own_range(self):
        """There is no previous close on bar 0, so Wilder defines its true range as ``H - L``.
        Written out as its own test because it is the one bar where the three-leg maximum is
        undefined, and a silent ``skipna`` would paper over exactly that bar."""
        frame = _frame([(100.0, 107.0, 96.0, 101.0), (101.0, 108.0, 100.0, 107.5)])

        atr = TechnicalIndicators.atr(frame, 1)

        assert atr.iloc[0] == pytest.approx(11.0)  # 107 - 96

    def test_an_atr_shorter_than_its_window_is_nan(self):
        frame = _closes_frame([100.0 + i for i in range(6)])

        atr = TechnicalIndicators.atr(frame, 14)

        assert atr.isna().all()


class TestAdx:
    def test_it_matches_a_first_principles_wilder_adx(self):
        """Every leg -- +DM, -DM, both DI percentages, DX and the final smoothing -- is
        re-derived in ``_adx_reference``. A zig-zag frame is used because a straight trend makes
        DX constant and would hide any error in the smoothing."""
        rows = [
            (100.0, 104.0, 99.0, 103.0),
            (103.0, 106.0, 102.0, 105.0),
            (105.0, 107.0, 101.0, 102.0),
            (102.0, 108.0, 101.5, 107.0),
            (107.0, 109.0, 106.0, 108.5),
            (108.5, 112.0, 107.0, 108.0),
            (108.0, 111.0, 103.0, 104.0),
            (104.0, 115.0, 103.5, 114.0),
            (114.0, 118.0, 113.0, 117.0),
            (117.0, 119.0, 112.0, 113.5),
            (113.5, 120.0, 112.5, 119.5),
            (119.5, 124.0, 118.0, 123.0),
            (123.0, 125.0, 121.0, 122.0),
            (122.0, 128.0, 121.5, 127.0),
            (127.0, 129.0, 126.0, 128.5),
            (128.5, 133.0, 127.5, 132.0),
            (132.0, 134.0, 130.0, 131.0),
            (131.0, 138.0, 130.5, 137.0),
            (137.0, 140.0, 136.0, 139.5),
            (139.5, 142.0, 138.0, 141.0),
            (141.0, 143.0, 139.0, 140.0),
            (140.0, 146.0, 139.5, 145.0),
            (145.0, 148.0, 144.0, 147.0),
            (147.0, 149.0, 145.0, 146.0),
            (146.0, 152.0, 145.5, 151.0),
            (151.0, 154.0, 150.0, 153.0),
            (153.0, 155.0, 151.0, 152.0),
            (152.0, 158.0, 151.5, 157.0),
            (157.0, 161.0, 156.0, 160.0),
            (160.0, 162.0, 159.0, 161.5),
            (161.5, 160.0, 157.5, 158.0),
            (158.0, 159.0, 153.0, 154.0),
            (154.0, 165.0, 153.5, 164.0),
            (164.0, 168.0, 163.0, 167.0),
            (167.0, 170.0, 166.0, 168.5),
            (168.5, 169.0, 164.0, 165.0),
            (165.0, 172.0, 164.5, 171.0),
            (171.0, 176.0, 170.0, 175.0),
            (175.0, 177.0, 173.0, 174.0),
            (174.0, 181.0, 173.5, 180.0),
            (180.0, 184.0, 179.0, 183.0),
            (183.0, 185.0, 181.0, 182.0),
            (182.0, 190.0, 181.5, 189.0),
            (189.0, 192.0, 188.0, 191.5),
            (191.5, 193.0, 190.0, 192.5),
        ]
        frame = _frame(rows)
        expected = _adx_reference(rows, 14)

        adx = TechnicalIndicators.adx(frame, 14)

        defined = 0
        for index, (actual, want) in enumerate(zip(adx.tolist(), expected)):
            if want != want:
                assert math.isnan(actual), f"bar {index}: expected NaN, got {actual}"
            else:
                defined += 1
                assert actual == pytest.approx(want, rel=1e-12), f"bar {index}"
        assert defined >= 5, "the comparison covered too few bars to be evidence"

    def test_a_trend_with_no_countertrend_bars_is_at_the_ceiling(self):
        """Every bar's up-move dominates and the true range is the day's range, so ``-DI`` is 0
        and ADX is 100.0 -- the maximum, measured."""
        frame = _closes_frame([100.0 + i for i in range(60)])

        adx = TechnicalIndicators.adx(frame, 14)

        assert adx.iloc[-1] == pytest.approx(100.0, abs=1e-9)

    def test_a_flat_series_has_no_direction(self):
        """+DI and -DI are both 0, so DX is 0/0. Not 0.0, and not 20 (the conventional floor)."""
        frame = _flat_frame(90.0, 60)

        assert TechnicalIndicators.adx(frame, 14).isna().all()

    def test_an_adx_shorter_than_twice_its_window_is_nan(self):
        """The bar count, pinned from both sides: 2*window - 1 bars has no ADX, 2*window bars
        has one. Wilder's chain is a prior bar, then ``window`` directional movements, then
        ``window`` DX values -- an off-by-one here publishes a number from a window that had not
        filled, which is exactly the defect this module is written against."""
        one_short = _closes_frame([100.0 + i for i in range(27)])
        just_enough = _closes_frame([100.0 + i for i in range(28)])

        assert TechnicalIndicators.adx(one_short, 14).isna().all()
        assert not math.isnan(TechnicalIndicators.adx(just_enough, 14).iloc[-1])


# ====================================================================== Bollinger bands


class TestBollinger:
    def test_the_bands_are_the_mean_plus_and_minus_n_standard_deviations(self):
        """``sigma`` is the **population** standard deviation (``ddof=0``). Recomputed here as
        ``sqrt(mean((x - mean)^2))`` over the same window, which is the definition rather than
        another call into the engine."""
        closes = [10.0, 12.0, 14.0, 16.0, 18.0, 20.0, 22.0, 24.0, 26.0, 28.0,
                  30.0, 32.0, 34.0, 36.0, 38.0, 40.0, 42.0, 44.0, 46.0, 48.0]
        frame = _closes_frame(closes)
        window = 20

        bands = TechnicalIndicators.bollinger(frame, window, 2.0)
        last = closes[-window:]
        mean = sum(last) / window
        sigma = math.sqrt(sum((value - mean) ** 2 for value in last) / window)

        assert bands["mid"].iloc[-1] == pytest.approx(mean)
        assert bands["upper"].iloc[-1] == pytest.approx(mean + 2.0 * sigma)
        assert bands["lower"].iloc[-1] == pytest.approx(mean - 2.0 * sigma)

    def test_the_mid_band_is_the_sma_of_the_same_window(self):
        frame = _closes_frame([100.0 + i * i for i in range(60)])

        bands = TechnicalIndicators.bollinger(frame, 20, 2.0)

        assert bands["mid"].iloc[-1] == pytest.approx(TechnicalIndicators.sma(frame, 20).iloc[-1])

    def test_pct_b_is_zero_at_the_lower_band_and_one_at_the_upper(self):
        """The two ends of the ratio, on windows whose mean and sigma are known exactly.

        Setting the last close to a band value computed from the *same* window would be
        circular -- replacing the close moves the mean and the sigma, so the band moves too.
        Instead each window is constructed backwards from the answer it should give: the nine
        earlier closes sum to ``-2`` with a sum of squares of ``6``, and the tenth is ``+2``, so
        the 10-bar mean is ``0``, the population sigma is ``sqrt(10/10) = 1``, the bands are
        ``-2`` and ``+2``, and a close sitting on either one must read exactly ``0.0``/``1.0``.
        """
        root = math.sqrt(2.0)
        at_upper = _closes_frame([root - 1.0, -1.0 - root, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 2.0])
        at_lower = _closes_frame([1.0 - root, 1.0 + root, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, -2.0])

        upper_bands = TechnicalIndicators.bollinger(at_upper, 10, 2.0)
        lower_bands = TechnicalIndicators.bollinger(at_lower, 10, 2.0)

        # The construction really does have the mean and spread it claims.
        assert upper_bands["mid"].iloc[-1] == pytest.approx(0.0, abs=1e-12)
        assert upper_bands["upper"].iloc[-1] == pytest.approx(2.0, abs=1e-12)
        assert lower_bands["lower"].iloc[-1] == pytest.approx(-2.0, abs=1e-12)
        # ... and the ratio lands on the ends.
        assert upper_bands["pct_b"].iloc[-1] == pytest.approx(1.0, abs=1e-9)
        assert lower_bands["pct_b"].iloc[-1] == pytest.approx(0.0, abs=1e-9)

    def test_pct_b_is_the_position_within_the_bands(self):
        """The definition, on a frame whose close sits inside the bands rather than on them."""
        frame = _closes_frame([100.0 + i for i in range(40)])

        bands = TechnicalIndicators.bollinger(frame, 20, 2.0)
        close = frame["Close"].iloc[-1]

        expected = (close - bands["lower"].iloc[-1]) / (bands["upper"].iloc[-1] - bands["lower"].iloc[-1])

        assert bands["pct_b"].iloc[-1] == pytest.approx(expected)
        assert 0.0 < bands["pct_b"].iloc[-1] < 1.0, "a close inside the bands is not outside them"

    def test_a_zero_width_band_has_no_percent_b(self):
        """No variance in the window means ``upper == lower``, and every close is simultaneously
        on both bands. 0.0 would claim it sits on the lower one; NaN says the ratio has no
        meaning here."""
        frame = _flat_frame(42.0, 40)

        bands = TechnicalIndicators.bollinger(frame, 20, 2.0)

        assert bands["upper"].iloc[-1] == pytest.approx(bands["lower"].iloc[-1])
        assert math.isnan(bands["pct_b"].iloc[-1])

    def test_the_key_set_is_complete_on_an_empty_frame(self):
        empty = _frame([])

        bands = TechnicalIndicators.bollinger(empty)

        assert sorted(bands) == ["lower", "mid", "pct_b", "upper"]
        assert all(line.empty for line in bands.values())


# ============================================================================== pivots


class TestPivots:
    @staticmethod
    def _expected(high, low, close):
        """The four families' levels, written out from their published formulas.

        Deliberately *not* the engine's helper: these are the formulas as the charting platforms
        state them, so this function and ``TechnicalIndicators._range_levels`` agreeing is
        evidence and not a tautology.
        """
        span = high - low
        classic_pp = (high + low + close) / 3.0
        woodie_pp = (high + low + 2.0 * close) / 4.0

        def common(pp):
            return {
                "pp": pp,
                "s1": 2.0 * pp - high,
                "s2": pp - span,
                "s3": low - 2.0 * (high - pp),
                "r1": 2.0 * pp - low,
                "r2": pp + span,
                "r3": high + 2.0 * (pp - low),
            }

        return {
            "classic": common(classic_pp),
            "woodie": common(woodie_pp),
            "fibonacci": {
                "pp": classic_pp,
                "s1": classic_pp - 0.382 * span,
                "s2": classic_pp - 0.618 * span,
                "s3": classic_pp - span,
                "r1": classic_pp + 0.382 * span,
                "r2": classic_pp + 0.618 * span,
                "r3": classic_pp + span,
            },
            "camarilla": {
                "pp": classic_pp,
                "s1": close - span * 1.1 / 12.0,
                "s2": close - span * 1.1 / 6.0,
                "s3": close - span * 1.1 / 4.0,
                "r1": close + span * 1.1 / 12.0,
                "r2": close + span * 1.1 / 6.0,
                "r3": close + span * 1.1 / 4.0,
            },
        }

    def test_every_family_matches_the_formula_written_out_here(self):
        """The arithmetic the plan asks for: a known OHLC day, checked against first principles.

        The last bar is ``high=110, low=95, close=104``, so ``pp`` is ``103`` for classic and
        Fibonacci, ``103.25`` for Woodie, and Camarilla's levels come off the close. Those are
        spelled out at the bottom so the expectation is readable without running anything.
        """
        high, low, close = 110.0, 95.0, 104.0
        frame = _frame([(99.0, 101.0, 98.0, 100.0), (100.0, high, low, close)])

        pivots = TechnicalIndicators.pivots(frame)
        expected = self._expected(high, low, close)

        assert sorted(pivots) == ["camarilla", "classic", "fibonacci", "woodie"]
        for family, levels in expected.items():
            assert sorted(pivots[family]) == ["pp", "r1", "r2", "r3", "s1", "s2", "s3"]
            for level, value in levels.items():
                assert pivots[family][level] == pytest.approx(value), f"{family}.{level}"

        # The hand-written numbers, so the expectation itself is pinned and not merely derived.
        assert pivots["classic"]["pp"] == pytest.approx(103.0)
        assert pivots["woodie"]["pp"] == pytest.approx(103.25)
        assert pivots["classic"]["r1"] == pytest.approx(2 * 103.0 - 95.0)
        assert pivots["classic"]["s1"] == pytest.approx(2 * 103.0 - 110.0)
        assert pivots["classic"]["r3"] == pytest.approx(110.0 + 2 * (103.0 - 95.0))
        assert pivots["classic"]["s3"] == pytest.approx(95.0 - 2 * (110.0 - 103.0))

    def test_the_levels_come_from_the_last_completed_bar(self):
        """Two frames identical except for their final bar must publish different levels, and
        the levels must equal what the *final* bar's H/L/C produce -- asserted by rebuilding the
        expected dict from that bar and checking an earlier bar's own levels do not appear."""
        first = _frame([(100.0, 105.0, 99.0, 104.0), (104.0, 110.0, 103.0, 107.0)])
        second = _frame([(100.0, 105.0, 99.0, 104.0), (107.0, 108.0, 106.0, 107.5)])

        from_first = TechnicalIndicators.pivots(first)["classic"]
        from_second = TechnicalIndicators.pivots(second)["classic"]

        assert from_first != from_second
        assert from_second["pp"] == pytest.approx((108.0 + 106.0 + 107.5) / 3.0)
        assert from_first["pp"] == pytest.approx((110.0 + 103.0 + 107.0) / 3.0)

    def test_classic_and_woodie_agree_only_when_the_close_is_the_midpoint(self):
        """The structural claim behind the arithmetic test: the two families differ *only* in
        the pivot point, so they coincide on a day whose close is the midpoint of its range --
        which is why the arithmetic test above uses a day whose close is not.
        """
        midpoint = _frame([(100.0, 105.0, 99.0, 104.0), (102.0, 110.0, 100.0, 105.0)])
        off_midpoint = _frame([(100.0, 105.0, 99.0, 104.0), (102.0, 110.0, 100.0, 102.0)])
        levels = ("pp", "s1", "s2", "s3", "r1", "r2", "r3")

        agreeing = TechnicalIndicators.pivots(midpoint)
        differing = TechnicalIndicators.pivots(off_midpoint)

        assert all(agreeing["classic"][level] == pytest.approx(agreeing["woodie"][level]) for level in levels)
        assert any(
            differing["classic"][level] != pytest.approx(differing["woodie"][level])
            for level in ("pp", "s1", "r1")
        )

    def test_a_frame_shorter_than_two_rows_is_refused_not_partly_answered(self):
        """One row cannot establish a completed session series, and a dict full of ``None`` reads
        to a caller that only checks for keys exactly like a real answer. So it is ``None``."""
        assert TechnicalIndicators.pivots(_frame([])) is None
        assert TechnicalIndicators.pivots(_frame([(100.0, 105.0, 99.0, 104.0)])) is None

    def test_an_unreadable_leg_withholds_every_level_rather_than_half_of_them(self):
        """A NaN ``Close`` on the last bar makes every formula undefined. Publishing six good
        levels and one hole would be a partial answer to a question with none."""
        frame = _frame([(100.0, 105.0, 99.0, 104.0), (104.0, 110.0, 103.0, 107.0)])
        frame.iloc[-1, frame.columns.get_loc("Close")] = float("nan")

        pivots = TechnicalIndicators.pivots(frame)

        assert sorted(pivots) == ["camarilla", "classic", "fibonacci", "woodie"]
        for family, levels in pivots.items():
            assert sorted(levels) == ["pp", "r1", "r2", "r3", "s1", "s2", "s3"]
            assert all(value is None for value in levels.values()), f"{family} published levels"

    def test_a_non_frame_is_refused_rather_than_exploded_on(self):
        """``pivots`` is the one method whose contract is a refusal, so it takes the refusal
        seriously: None in, None out -- never a ``TypeError`` from ``len(None)``."""
        assert TechnicalIndicators.pivots(None) is None
        assert TechnicalIndicators.pivots([1.0, 2.0, 3.0]) is None


# ======================================================================== the snapshot


class TestSnapshot:
    def test_it_always_has_the_same_keys(self):
        """Every slot exists whatever the frame, because the point of a summary row is that a
        caller can render it without asking which windows happened to fit."""
        empty = _frame([])
        short = _closes_frame([100.0 + i for i in range(5)])
        long_enough = _closes_frame([100.0 + i * i for i in range(260)])

        for frame in (empty, short, long_enough):
            summary = TechnicalIndicators.snapshot(frame)
            assert tuple(summary) == TechnicalIndicators.SNAPSHOT_KEYS

    def test_an_empty_frame_is_all_none(self):
        summary = TechnicalIndicators.snapshot(_frame([]))

        assert all(value is None for value in summary.values())

    def test_a_frame_too_short_for_the_long_windows_reads_none_not_zero(self):
        """Five closes: there is a last close, and there is no 200-bar average, no RSI, no ADX
        and no ATR. The two that *are* measurable must be real numbers and the rest ``None`` --
        the whole point of the test is that no slot reads 0.0."""
        frame = _closes_frame([100.0 + i for i in range(5)])

        summary = TechnicalIndicators.snapshot(frame)

        assert summary["close"] == pytest.approx(104.0)
        for key in ("sma_20", "sma_50", "sma_200", "rsi_14", "macd_hist", "atr_14", "adx_14", "bollinger_pct_b"):
            assert summary[key] is None, f"{key} published {summary[key]!r} from five closes"
        # Pivots need two rows and five is five: that one is a real answer.
        assert summary["pivots"] is not None
        assert summary["pivots"]["classic"]["pp"] == pytest.approx(
            (frame["High"].iloc[-1] + frame["Low"].iloc[-1] + frame["Close"].iloc[-1]) / 3.0
        )

    def test_a_frame_long_enough_measures_everything(self):
        frame = _closes_frame([100.0 + i for i in range(300)])

        summary = TechnicalIndicators.snapshot(frame)

        for key in ("close", "sma_20", "sma_50", "sma_200", "rsi_14", "macd_hist", "atr_14", "adx_14", "bollinger_pct_b"):
            assert summary[key] is not None, f"{key} was withheld from a 300-bar frame"
            assert not isinstance(summary[key], bool)
        assert summary["pivots"] is not None
        assert summary["sma_200"] == pytest.approx(TechnicalIndicators.sma(frame, 200).iloc[-1])

    def test_no_value_in_a_summary_is_ever_a_float_nan(self):
        """``float('nan')`` passes ``is not None`` and prints as a plausible cell, which is why
        the engine converts it to ``None`` at the boundary. Asserted across three frames, since
        a NaN reaching a caller is invisible to every other assertion here."""
        for closes in ([100.0 + i for i in range(3)], [100.0] * 40, [100.0 + i * i for i in range(300)]):
            summary = TechnicalIndicators.snapshot(_closes_frame(closes))
            for key, value in summary.items():
                if key == "pivots":
                    continue
                assert not (isinstance(value, float) and math.isnan(value)), f"{key} is NaN"


# ================================================================ holes are never filled


class TestNothingIsFilledAcrossAHole:
    @staticmethod
    def _frame_with_a_hole():
        """A whole bar goes missing, not just its close.

        ATR and ADX read ``High``/``Low`` (and true range reads the prior close), so a hole in
        ``Close`` alone would leave them perfectly measurable -- and a test that asserted they
        go NaN would then be asserting nothing at all. All four price columns go.
        """
        frame = _closes_frame([100.0 + i for i in range(40)])
        frame.iloc[25, [0, 1, 2, 3]] = float("nan")
        return frame

    def test_a_rolling_mean_is_nan_while_the_hole_is_inside_its_window(self):
        """Twenty bars, one of them unmeasured: the mean of nineteen prices and a hole is not
        the mean of twenty. The window is not shortened and the bar is not skipped."""
        frame = self._frame_with_a_hole()

        sma = TechnicalIndicators.sma(frame, 20)

        assert not math.isnan(sma.iloc[24]), "the window before the hole is measurable"
        assert sma.iloc[25:].isna().all(), "a hole was skipped and the window closed over it"

    def test_the_recursive_indicators_reset_at_the_hole_rather_than_carrying_through(self):
        """``ewm`` by default reproduces the previous smoothed value on a NaN bar -- an
        interpolation that looks exactly like data. These must not: NaN at the hole, and NaN
        again until the window has refilled with real bars."""
        frame = self._frame_with_a_hole()

        for series in (
            TechnicalIndicators.ema(frame, 20),
            TechnicalIndicators.rsi(frame, 14),
            TechnicalIndicators.atr(frame, 14),
            TechnicalIndicators.macd(frame)["histogram"],
            TechnicalIndicators.bollinger(frame)["pct_b"],
        ):
            assert math.isnan(series.iloc[25]), "the hole itself reported a value"
            assert series.iloc[-1] is not None and math.isnan(series.iloc[-1]), (
                "the tail was carried across a hole instead of waiting for real bars again"
            )

    def test_adx_refuses_to_read_a_direction_through_a_hole(self):
        frame = self._frame_with_a_hole()

        adx = TechnicalIndicators.adx(frame, 14)

        assert adx.iloc[25:].isna().all()

    def test_the_snapshot_reports_the_hole_rather_than_a_stale_number(self):
        """The failure this whole section exists to prevent, in the shape a caller sees it: a
        20-bar average whose window contains an unmeasured close must read ``None``, not the
        last average that was computed before the hole."""
        frame = self._frame_with_a_hole()

        summary = TechnicalIndicators.snapshot(frame)

        assert summary["sma_20"] is None
        assert summary["rsi_14"] is None
        assert summary["adx_14"] is None
        # The last close itself is real and is published -- the hole is 14 bars back.
        assert summary["close"] == pytest.approx(139.0)

    def test_a_hole_in_the_last_close_is_published_as_no_close(self):
        frame = self._frame_with_a_hole()
        frame.iloc[-1, frame.columns.get_loc("Close")] = float("nan")

        assert TechnicalIndicators.snapshot(frame)["close"] is None


# ============================================================ inputs the engine must absorb


class TestInputTolerance:
    def test_a_frame_without_a_close_column_measures_nothing_and_raises_nothing(self):
        """Missing input is an absence, not a crash and not a zero. The columns are there and
        carry no prices, so every close-derived indicator is NaN across its whole length."""
        frame = pd.DataFrame(
            {"Open": [1.0, 2.0], "High": [2.0, 3.0], "Low": [0.5, 1.5], "Volume": [10.0, 20.0]},
            index=pd.date_range("2026-01-01", periods=2, freq="B"),
        )

        assert TechnicalIndicators.sma(frame, 20).isna().all()
        assert TechnicalIndicators.ema(frame, 20).isna().all()
        assert TechnicalIndicators.rsi(frame, 14).isna().all()
        assert TechnicalIndicators.atr(frame, 14).isna().all()
        assert TechnicalIndicators.adx(frame, 14).isna().all()
        assert TechnicalIndicators.macd(frame)["macd"].isna().all()
        assert TechnicalIndicators.bollinger(frame)["mid"].isna().all()
        assert TechnicalIndicators.snapshot(frame)["close"] is None

    def test_lower_case_columns_resolve_to_the_same_indicator(self):
        """``history()`` prints ``Close``; most third-party frames print ``close``. The engine
        resolves both rather than raising a ``KeyError`` at a caller."""
        closes = [100.0 + i for i in range(30)]
        title_case = _closes_frame(closes)
        lower_case = title_case.rename(columns=str.lower)

        assert TechnicalIndicators.sma(lower_case, 20).iloc[-1] == pytest.approx(
            TechnicalIndicators.sma(title_case, 20).iloc[-1]
        )
        # `pytest.approx` does not walk nested dicts, so the pivot levels are compared one by one
        # (comparing the dicts with `==` would pass on any object with the right keys).
        from_lower, from_title = (
            TechnicalIndicators.pivots(lower_case),
            TechnicalIndicators.pivots(title_case),
        )
        for family in from_title:
            for level, value in from_title[family].items():
                assert from_lower[family][level] == pytest.approx(value), f"{family}.{level}"

    def test_adj_close_is_not_silently_substituted_for_close(self):
        """Both columns present, disagreeing -- which is what ``auto_adjust=False`` hands a
        caller across any dividend. Picking either one silently would make every indicator's
        value depend on a flag that was never passed to this engine."""
        frame = _closes_frame([100.0 + i for i in range(30)])
        frame["Adj Close"] = frame["Close"] - 10.0

        summary = TechnicalIndicators.snapshot(frame)

        assert summary["close"] == pytest.approx(129.0)

    @pytest.mark.parametrize(
        "call,kwargs",
        [
            (TechnicalIndicators.sma, {"window": 0}),
            (TechnicalIndicators.sma, {"window": -5}),
            (TechnicalIndicators.ema, {"span": 0}),
            (TechnicalIndicators.rsi, {"window": 0}),
            (TechnicalIndicators.macd, {"fast": 0}),
            (TechnicalIndicators.atr, {"window": -1}),
            (TechnicalIndicators.adx, {"window": 0}),
            (TechnicalIndicators.bollinger, {"window": 0}),
            (TechnicalIndicators.bollinger, {"n_std": -1.0}),
        ],
    )
    def test_an_impossible_window_is_refused_at_the_argument(self, call, kwargs):
        """A count of bars has no honest reading at zero, so the call raises instead of
        publishing an empty-window average under a name the caller chose. The alternative --
        clamping to 1 -- would silently answer a different question."""
        frame = _closes_frame([100.0 + i for i in range(30)])

        with pytest.raises(ValueError):
            call(frame, **kwargs)

    @pytest.mark.parametrize(
        "call,kwargs",
        [
            (TechnicalIndicators.sma, {"window": True}),
            (TechnicalIndicators.ema, {"span": False}),
            (TechnicalIndicators.rsi, {"window": True}),
            (TechnicalIndicators.bollinger, {"window": True}),
        ],
    )
    def test_a_bool_is_not_a_window(self, call, kwargs):
        """``bool`` is a subclass of ``int``: ``int(True) == 1``, and ``1 != True`` is ``False``,
        so every other half of the guard waves a ``True`` through and answers a **one-bar** average
        under a name that reads like a flag. ``sma(frame, window=True)`` is a caller mistake, and
        it has to be refused the same way ``window=0`` is rather than quietly answered."""
        frame = _closes_frame([100.0 + i for i in range(30)])

        with pytest.raises(ValueError, match="positive integer"):
            call(frame, **kwargs)

    def test_control_a_one_bar_window_is_still_allowed(self):
        """The other half of the control: the fix rejects ``bool``, not ``1``. A one-bar SMA is a
        real (if useless) measurement, and refusing it would be the clamping the guard exists to
        avoid."""
        frame = _closes_frame([100.0 + i for i in range(30)])

        assert TechnicalIndicators.sma(frame, window=1).notna().all()

    def test_control_the_bool_rejection_is_what_stops_it_and_not_the_count_check(self):
        """Isolated so the test above cannot pass for the wrong reason. ``int(True) == 1`` and
        ``1 != True`` is ``False``, so with no explicit ``bool`` check every other part of the
        guard accepts a ``True``; ``numpy``'s ``bool_`` is the same shape."""
        import numpy as np

        frame = _closes_frame([100.0 + i for i in range(30)])

        assert int(True) == 1 and (1 != True) is False, (
            "the premise of this test: if bool were not an int subclass, the guard would already "
            "refuse it and this finding would be about nothing"
        )
        with pytest.raises(ValueError):
            TechnicalIndicators.sma(frame, window=np.bool_(True))
        with pytest.raises(ValueError):
            TechnicalIndicators.sma(frame, window=True)
