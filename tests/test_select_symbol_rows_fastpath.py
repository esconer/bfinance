"""
The row-select fast path: probe first, normalise only when the probe finds nothing.

``_select_symbol_rows`` filters a whole-market bhavcopy range frame down to one symbol on every
uncached history request, and that frame is large: 939,861 rows for a ~1-year, ~2,500-symbol
range. It used to run

    market.loc[market[key].astype(str).str.strip().str.upper().eq(symbol)]

unconditionally, folding all 939,861 rows twice for a column that an NSE bhavcopy publishes
upper-case and unpadded. It now probes the raw column with cheap equality first and falls through
to that same normalisation only on a miss.

Measured on this machine (Windows, CPython 3.13.15, pandas 3.0.5) on a 939,861-row frame of 2,500
symbols, min of six alternating runs of the pre-change body and the shipped function:

    symbol present    515.7 ms  ->   61.6 ms   (8.4x)
    symbol absent     507.9 ms  ->  643.0 ms

The 250,000-row frame the timing test below builds measures 140.1 ms -> 16.9 ms (8.3x), miss
142.3 ms -> 173.3 ms. The slower miss is the cost of probing twice before the work that answers
it, and it is only paid where the answer is a miss anyway.

Two properties this module pins, because a fast path is only worth having if it is
indistinguishable from the slow one:

* **A hit is exact.** A raw value equal to an already-normalised symbol needs neither stripping
  nor case folding, so the probe's hit set is a subset of the normalisation's and returning early
  cannot change which rows come back. Asserted row-for-row against a local copy of the pre-change
  body (``_pre_change_select``), including the empty frame and its index.
* **A miss is not an answer.** Padding and mixed case are exactly what the probe is blind to, so
  a miss must fall through. ``test_padded_or_mixed_case_still_selects_its_rows`` is the negative
  control: delete the fall-through and it goes red with an empty frame.

``.BO``/``.NS`` suffixes are *not* this function's job -- ``bfinance.utils.symbols.normalize_symbol``
strips them before the symbol reaches here -- so the cases below pin that the selector leaves them
alone, which is what it did before the change too.
"""

import time

import numpy as np
import pandas as pd
import pytest

from bfinance.market import ohlcv as ohlcv_mod

#: Rows in the timing frame. Big enough that folding the column costs hundreds of milliseconds, so
#: the assertion catches "normalisation on every call" rather than policing individual milliseconds.
TIMING_ROWS = 250_000

#: Symbols per day in the timing frame, so the range frame is a realistic whole-market shape.
TIMING_SYMBOLS = 2_500


# ------------------------------------------------------------------------ oracle and frame builder

def _pre_change_select(market: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """``_select_symbol_rows`` as it stood before P1, verbatim, as the oracle.

    Deliberately a local copy rather than a git checkout or an import: an oracle that changes with
    the code under test cannot catch a change in the code under test. If this function and the
    shipped one ever disagree, the shipped one is wrong.
    """
    if isinstance(market.index, pd.MultiIndex):
        for level in market.index.names:
            if level is not None and str(level).strip().upper() in ohlcv_mod._SYMBOL_KEYS:
                market = market.reset_index(level=level)
                break
    key = next(
        (c for c in market.columns if str(c).strip().upper() in ohlcv_mod._SYMBOL_KEYS), None
    )
    if key is None:
        return market
    return market.loc[market[key].astype(str).str.strip().str.upper().eq(symbol)]


def _market_frame(symbols, *, symbol_column: str = "SYMBOL") -> pd.DataFrame:
    """One bar per entry, in whole-market shape: a Date index and a symbol column.

    Values are distinct per row so a test can tell *which* row came back, and the index is a
    named ``DatetimeIndex`` because that is what the bhavcopy parser produces on the real path.
    """
    rows = len(symbols)
    # Seconds, not days: 250,000 days lands in the year 2708 and overflows pandas' nanosecond
    # range on the older pandas that Python 3.10 resolves. Only the index's name, dtype and
    # uniqueness are ever asserted here, never its dates.
    index = pd.DatetimeIndex(pd.date_range("2024-01-01", periods=rows, freq="s"), name="Date")
    frame = pd.DataFrame(
        {
            symbol_column: list(symbols),
            "Open": np.arange(rows, dtype=float) + 100.0,
            "High": np.arange(rows, dtype=float) + 101.0,
            "Low": np.arange(rows, dtype=float) + 99.0,
            "Close": np.arange(rows, dtype=float) + 100.5,
            "Volume": np.arange(rows, dtype=np.int64) + 1_000,
        },
        index=index,
    )
    frame.attrs["bfinance_provenance"] = "test"
    return frame


# ------------------------------------------------------------------- correctness on messy inputs

@pytest.mark.parametrize(
    ("column_value", "requested", "expected_rows"),
    [
        pytest.param("RELIANCE", "RELIANCE", 1, id="as-published"),
        pytest.param("reliance", "RELIANCE", 1, id="lowercase-column"),
        pytest.param("Reliance", "RELIANCE", 1, id="mixed-case-column"),
        pytest.param("  RELIANCE  ", "RELIANCE", 1, id="padded-column"),
        pytest.param("RELIANCE.NS", "RELIANCE", 0, id="ns-suffix-column"),
        pytest.param("RELIANCE.BO", "RELIANCE", 0, id="bo-suffix-column"),
        pytest.param("RELIANCE", "RELIANCE.NS", 0, id="ns-suffix-request"),
        pytest.param("RELIANCE", "RELIANCE.BO", 0, id="bo-suffix-request"),
        pytest.param("RELIANCE", "reliance", 0, id="lowercase-request"),
        pytest.param("RELIANCE", "  RELIANCE  ", 0, id="padded-request"),
    ],
)
def test_messy_symbols_match_the_pre_change_behaviour(column_value, requested, expected_rows):
    """Every messy shape returns exactly the rows the old body returned -- and the right count.

    The count is asserted separately from the equality because "both implementations agree on an
    empty frame" is a pass the equality alone would happily grant.
    """
    frame = _market_frame([column_value, "TCS", "INFY"])

    got = ohlcv_mod._select_symbol_rows(frame, requested)
    want = _pre_change_select(frame, requested)

    pd.testing.assert_frame_equal(got, want)
    assert len(got) == expected_rows
    assert got.attrs == want.attrs


def test_bo_suffix_is_removed_upstream_by_normalize_symbol():
    """The suffix is ``normalize_symbol``'s job; by the time it reaches here it is gone.

    This is the real ``history()`` path for ``Ticker("reliance.bo")``: the suffix never reaches the
    selector, which is why the selector does not (and did not) strip it.
    """
    from bfinance.utils.symbols import normalize_symbol

    frame = _market_frame(["RELIANCE", "TCS"])

    assert ohlcv_mod._select_symbol_rows(frame, "RELIANCE.BO").empty
    assert normalize_symbol("reliance.bo") == "RELIANCE"
    assert len(ohlcv_mod._select_symbol_rows(frame, normalize_symbol("reliance.bo"))) == 1


# -------------------------------------------------------- equality with the pre-change behaviour

def test_whole_market_frame_returns_the_same_rows_as_the_pre_change_body():
    """2,500 symbols with one duplicated, compared row-for-row, index included.

    The duplicate is the case a positional-only implementation would get wrong: both of RELIANCE's
    rows must come back, with their original labels.
    """
    symbols = [f"SYM{i:04d}" for i in range(TIMING_SYMBOLS)]
    symbols[7] = "RELIANCE"
    symbols[1_500] = "RELIANCE"
    frame = _market_frame(symbols)

    for symbol in ("RELIANCE", "SYM0001", "SYM2499", "NOT_IN_FRAME"):
        got = ohlcv_mod._select_symbol_rows(frame, symbol)
        want = _pre_change_select(frame, symbol)
        pd.testing.assert_frame_equal(got, want)
        assert got.attrs == want.attrs, symbol

    duplicated = ohlcv_mod._select_symbol_rows(frame, "RELIANCE")
    assert len(duplicated) == 2
    assert duplicated.index.tolist() == [frame.index[7], frame.index[1_500]]


def test_symbol_level_on_a_multi_index_is_reset_before_the_probe():
    """The real bhavcopy parser indexes by (Date, Symbol); equality must survive that path."""
    index = pd.MultiIndex.from_product(
        [pd.date_range("2024-01-01", periods=3, freq="D"), ["RELIANCE", "TCS"]],
        names=["Date", "Symbol"],
    )
    frame = pd.DataFrame(
        {"Open": np.arange(6, dtype=float) + 100.0, "Close": np.arange(6, dtype=float) + 100.5},
        index=index,
    )

    got = ohlcv_mod._select_symbol_rows(frame, "RELIANCE")
    want = _pre_change_select(frame, "RELIANCE")

    pd.testing.assert_frame_equal(got, want)
    assert len(got) == 3
    assert got.index.name == "Date"


def test_frame_without_a_symbol_key_is_returned_unchanged():
    """A frame already scoped to one instrument is passed through, not copied or filtered."""
    frame = _market_frame(["RELIANCE"], symbol_column="Series")

    assert ohlcv_mod._select_symbol_rows(frame, "RELIANCE") is frame


# ------------------------------------------------------------------------- determinism, no mutation

def test_repeated_calls_agree_and_leave_the_input_untouched():
    frame = _market_frame(["RELIANCE", "TCS", "RELIANCE"])
    snapshot = frame.copy(deep=True)
    attrs_before = dict(frame.attrs)

    first = ohlcv_mod._select_symbol_rows(frame, "RELIANCE")
    second = ohlcv_mod._select_symbol_rows(frame, "RELIANCE")

    pd.testing.assert_frame_equal(first, second)
    pd.testing.assert_frame_equal(frame, snapshot)
    assert frame.attrs == attrs_before


# ------------------------------------------------------------------------------- negative control

@pytest.mark.parametrize(
    "column_value",
    [
        pytest.param("  RELIANCE  ", id="padded"),
        pytest.param("Reliance", id="mixed-case"),
        pytest.param("reliance", id="lowercase"),
    ],
)
def test_padded_or_mixed_case_still_selects_its_rows(column_value):
    """Negative control for the fall-through: the probe is blind to these, so it must fall through.

    Remove the normalisation from ``_select_symbol_rows`` and this test returns an empty frame and
    goes red. That is the one regression this whole change is allowed to be judged on.
    """
    frame = _market_frame([column_value, "TCS"])

    # The oracle first, so the control cannot pass by both implementations being wrong together.
    assert len(_pre_change_select(frame, "RELIANCE")) == 1

    selected = ohlcv_mod._select_symbol_rows(frame, "RELIANCE")

    assert not selected.empty
    assert len(selected) == 1
    pd.testing.assert_frame_equal(selected, _pre_change_select(frame, "RELIANCE"))


# ------------------------------------------------------------------------ empty-frame contract

def test_absent_symbol_keeps_the_empty_frame_contract():
    """A miss that stays a miss returns the old empty frame: same columns, index, dtype, attrs."""
    frame = _market_frame(["RELIANCE", "TCS"])

    got = ohlcv_mod._select_symbol_rows(frame, "INFY")
    want = _pre_change_select(frame, "INFY")

    assert got.empty
    pd.testing.assert_frame_equal(got, want)
    assert list(got.columns) == list(frame.columns)
    assert isinstance(got.index, pd.DatetimeIndex)
    assert got.index.name == "Date"
    assert got.index.dtype == frame.index.dtype
    assert got.attrs == frame.attrs


# ---------------------------------------------------------------------------------- timing gate

def _best_of(fn, reps: int) -> float:
    """Best-of-N wall time in ms -- the minimum is the least noisy estimator of the CPU cost."""
    best = float("inf")
    for _ in range(reps):
        start = time.perf_counter()
        fn()
        best = min(best, (time.perf_counter() - start) * 1_000.0)
    return best


def test_hit_path_does_not_normalise_the_whole_column():
    """A present symbol must cost far less than the strip+upper fold it replaced.

    The bound is deliberately loose -- 0.5x the pre-change body, where this machine measures 0.12x
    (16.9 ms against 140.1 ms on 250,000 rows). It exists to catch a regression back to
    ``.str.upper()`` on every call, not to police milliseconds; a slower CI box moves both timings
    together, which is why the assertion is a ratio and the absolute ceiling is only a backstop.
    """
    symbols = [f"SYM{i:04d}" for i in range(TIMING_SYMBOLS)]
    symbols[0] = "RELIANCE"
    frame = _market_frame(np.resize(np.array(symbols, dtype=object), TIMING_ROWS))

    # Warm-up: the first call pays pandas' dtype and index caches, which neither body owns.
    ohlcv_mod._select_symbol_rows(frame, "RELIANCE")
    _pre_change_select(frame, "RELIANCE")

    fast = _best_of(lambda: ohlcv_mod._select_symbol_rows(frame, "RELIANCE"), reps=3)
    slow = _best_of(lambda: _pre_change_select(frame, "RELIANCE"), reps=3)

    assert fast < slow * 0.5, (
        f"row-select hit path took {fast:.1f} ms against {slow:.1f} ms for the pre-change body "
        f"on {TIMING_ROWS:,} rows -- the cheap probe looks to have been removed"
    )
    # Backstop only: 16.9 ms measured here, so a 10x slower machine still passes.
    assert fast < 150.0, f"row-select hit path took {fast:.1f} ms on {TIMING_ROWS:,} rows"
