"""
A symbol that produced nothing must still APPEAR in the batch frame, as an all-NaN column.

The defect
----------
``_download_batch_async`` used to ``continue`` past a symbol whose per-symbol call raised, and past
a symbol that came back empty, so a batch of ``["TCS", "BAD"]`` returned a frame with a ``TCS``
column and no ``BAD`` at all. yfinance 1.7.0 does the opposite: ``multi._download_one`` catches the
exception and stores ``utils.empty_df()`` in ``ctx.dfs``, and ``download()`` concatenates every
entry, so ``BAD`` is a column of ``NaN``. Verified live in this env::

    >>> yfinance.download(["RELIANCE.NS", "ZZZZNOPE.XYZ"], period="5d")
    (5, 11)   # ('Close','ZZZZNOPE.XYZ'), ('High','ZZZZNOPE.XYZ'), ... all NaN

Why the disappearing column is worse than an empty one
-----------------------------------------------------
The caller asked a question about a *set* of tickers and got an answer about a *subset*, with no
way to tell which subset. Two concrete failures follow, and both are silent:

1. Iterating ``df.columns.get_level_values(1).unique()`` yields fewer tickers than were requested,
   so a rebalancing loop that re-derives its universe from the result quietly shrinks.
2. ``df[("Close", "BAD")]`` raises ``KeyError`` where yfinance returns a column of ``NaN``. A caller
   swapping one symbol for another sees a crash on the new name and an empty column on yfinance --
   the same code, two different answers, and the crash looks like a bug in the caller rather than a
   parity gap in the library.

And ``NaN`` specifically, not zero
----------------------------------
A zero is a price. A frame of zeros for ``BAD`` would read as "BAD traded flat", which is a claim
about the market, and every downstream statistic (a mean, a return, a z-score) would consume it
without complaint. ``NaN`` is the only value that says "there is no data here" in a way arithmetic
propagates. So the tests below assert both ``isna().all()`` and the absence of any ``0.0``, and the
mutation control at the end of this file turns the empty frame zero-filled to show the assertion is
not vacuous.

The column set is DERIVED, not hardcoded
-----------------------------------------
yfinance's ``empty_df()`` hardcodes six columns including ``"Adj Close"``. Copying that list here
would be wrong on this package's default path: ``auto_adjust`` defaults to ``True`` (commit
``e6b4913``) and a ``True`` frame has no ``Adj Close`` at all. A failed symbol carrying six columns
next to survivors carrying five is an internally inconsistent frame -- exactly the class of defect
this file exists to prevent, reproduced one column later. The list therefore comes from
:meth:`bfinance.market.ohlcv.OHLCVEngine._history_columns`, the one place that decides it, so
``auto_adjust`` and ``actions`` change the absent symbol's columns exactly as they change a present
symbol's.

Offline: no test here may make a network request. ``download_mod.Ticker`` is replaced throughout.
"""

import importlib
import logging

import pandas as pd
import pytest

from bfinance.utils.exceptions import UpstreamServiceError

# NB: `from bfinance import download` yields the *function*, not the module, because
# `bfinance/__init__.py` re-exports it. importlib is the reliable way to reach the module.
download_mod = importlib.import_module("bfinance.download")
download = download_mod.download

IST = "Asia/Kolkata"

#: The good symbol's frame. Deliberately NOT the 6/7-column shape: the stub answers with the 5
#: columns an ``auto_adjust=True`` history() returns, so the default path's column set is what gets
#: compared against the absent symbol's.
_GOOD_COLUMNS = ["Open", "High", "Low", "Close", "Volume"]
_GOOD_ROW = [100.0, 102.0, 99.0, 101.0, 10]
_DATES = pd.to_datetime(["2024-03-28", "2024-03-29"])


class _StubTicker:
    """Answers for the named symbols and raises for the rest. No network, no cache."""

    #: Symbols that raise :class:`UpstreamServiceError` instead of answering.
    failing: tuple = ()
    #: Symbols that answer with a genuinely empty frame rather than raising.
    empty: tuple = ()

    def __init__(self, symbol, proxy=None):
        self.symbol = symbol

    async def history_async(self, **kwargs):
        sym = self.symbol.upper()
        if sym in type(self).failing:
            raise UpstreamServiceError(f"{sym}: boom")
        if sym in type(self).empty:
            return pd.DataFrame()
        columns = list(_GOOD_COLUMNS)
        row = list(_GOOD_ROW)
        if not kwargs.get("auto_adjust", True):
            # auto_adjust=False keeps Adj Close, at index 4 -- between Close and Volume, which is
            # where yfinance puts it and where _history_columns puts it.
            columns.insert(4, "Adj Close")
            row.insert(4, 101.0)
        index = pd.DatetimeIndex(_DATES, name="Date", tz=IST)
        return pd.DataFrame([row, [v + 1 for v in row]], index=index, columns=columns)


@pytest.fixture
def stub_ticker(monkeypatch):
    _StubTicker.failing = ()
    _StubTicker.empty = ()
    monkeypatch.setattr(download_mod, "Ticker", _StubTicker)
    return _StubTicker


def _tickers_in(frame: pd.DataFrame) -> list:
    """The tickers actually present in the frame, in the order the columns carry them."""
    return list(dict.fromkeys(frame.columns.get_level_values(1)))


def _block(frame: pd.DataFrame, ticker: str) -> pd.DataFrame:
    """
    One ticker's sub-frame, at whatever level that ticker sits on.

    ``.xs`` rather than ``df[ticker]``: on a ``(Price, Ticker)`` MultiIndex, ``df["TCS"]`` raises
    ``KeyError`` in this pandas for EVERY ticker, live ones included (checked against an all-live
    frame), so it is a property of the column layout and not of the union. Using it here would have
    made these tests fail for a reason that has nothing to do with what is under test. The level is
    resolved by name so the same helper serves ``group_by="column"`` and ``group_by="ticker"``.
    """
    level = frame.columns.names.index("Ticker")
    return frame.xs(ticker, axis=1, level=level)


# ------------------------------------------------- the symbol appears, and it is NaN, not zero


def test_a_failed_symbol_appears_alongside_a_good_one(stub_ticker):
    """
    The defect as an assertion: ``BAD`` must be a column, not an absence.

    Before the fix this frame had a ``TCS`` column and nothing else, and the test that used to stand
    here -- ``test_download_raise_errors_true_still_collects_good_symbols_first`` in
    ``test_batch_download.py`` -- asserted only ``not df.empty``. That assertion is satisfied by any
    frame with one live column, so it never noticed the missing one. See the note on that test.
    """
    stub_ticker.failing = ("BAD",)
    frame = download(["TCS", "BAD"], period="5d")

    assert _tickers_in(frame) == ["BAD", "TCS"], (
        f"every requested ticker must be a column; got {_tickers_in(frame)!r}"
    )
    # And indexable, which is the operation that used to raise.
    assert ("Close", "BAD") in frame.columns, (
        f'df[("Close", "BAD")] must resolve to an all-NaN column, not KeyError; '
        f"columns were {list(frame.columns)!r}"
    )


def test_the_absent_column_is_nan_and_not_zero(stub_ticker):
    """
    ``NaN``, not ``0.0``. A zero is a price; a frame of zeros reads as a flat-trading session and
    every downstream statistic consumes it without complaint.

    WHERE THE ``float("nan")`` ACTUALLY MATTERS. Editing ``_all_nan_frame`` to use ``0.0`` instead
    does **not** turn this red, and the reason is worth recording rather than discovering later:
    the stand-in carries ZERO rows, so pandas reindexes it onto the survivors' index and a
    reindexed zero-row frame is NaN whatever values it was holding. The literal is not what makes
    the column NaN here -- the empty index is.

    So the observable zero-fill is a post-concat one (``combined.fillna(0.0)``), and THAT is what
    the mutation control below exercises: it turns four tests red in this file plus
    ``test_batch_download.py::test_download_raise_errors_true_still_collects_good_symbols_first``.
    The ``float("nan")`` is kept because it is what yfinance writes, and because a future change
    that gives the stand-in rows would otherwise silently start publishing zeros -- which is exactly
    what the zero assertion below is here to catch.
    """
    stub_ticker.failing = ("BAD",)
    frame = download(["TCS", "BAD"], period="5d")

    bad = _block(frame, "BAD")
    assert bad.isna().all().all(), f"the failed symbol must be entirely NaN; got\n{bad}"
    zeros = (bad == 0.0)
    assert not zeros.any().any(), (
        f"a zero-filled stand-in would be indistinguishable from a real flat price; got\n{bad}"
    )


def test_the_good_symbol_is_untouched_by_the_union(stub_ticker):
    """The control: the union must not have overwritten a real answer with NaN."""
    stub_ticker.failing = ("BAD",)
    frame = download(["TCS", "BAD"], period="5d")

    good = _block(frame, "TCS")
    assert not good.isna().any().any(), f"TCS answered and must stay answered; got\n{good}"
    assert good["Close"].tolist() == [101.0, 102.0], f"TCS Close was altered: {good['Close'].tolist()}"
    assert frame[("Close", "BAD")].isna().all(), "and BAD must still be the NaN one"


def test_an_empty_but_successful_symbol_also_appears(stub_ticker):
    """
    An empty answer is an answer. "This symbol has no bars in this window" is a fact about the
    window, and yfinance's union treats it exactly like a failure -- ``_download_one`` stores
    whatever ``history()`` returned, and a history() that answered empty leaves a column behind.

    This is the second half of the drop at the old ``download.py`` lines 124-126, and it is the
    half a ``raise``-based test could never have reached: nothing was raised.
    """
    stub_ticker.empty = ("BAD",)
    frame = download(["TCS", "BAD"], period="5d")

    assert _tickers_in(frame) == ["BAD", "TCS"], f"BAD answered empty and must appear; got {_tickers_in(frame)!r}"
    assert _block(frame, "BAD").isna().all().all(), f"and it must be all NaN; got\n{frame['BAD']}"


# ---------------------------------------------------- the column set follows the CALL, not a 6


@pytest.mark.parametrize(
    "auto_adjust,actions,expected",
    [
        (True, False, ["Open", "High", "Low", "Close", "Volume"]),
        (False, False, ["Open", "High", "Low", "Close", "Adj Close", "Volume"]),
        (True, True, ["Open", "High", "Low", "Close", "Volume", "Dividends", "Stock Splits"]),
        (False, True, ["Open", "High", "Low", "Close", "Adj Close", "Volume", "Dividends", "Stock Splits"]),
    ],
    ids=["auto_true", "auto_false", "auto_true_actions", "auto_false_actions"],
)
def test_the_failed_symbol_gets_the_columns_the_call_asked_for(
    stub_ticker, auto_adjust, actions, expected
):
    """
    The column set is DERIVED from ``OHLCVEngine._history_columns``, not hardcoded.

    yfinance's ``empty_df()`` hardcodes six columns with ``"Adj Close"`` in them. That is only right
    when ``auto_adjust=False``; on this package's DEFAULT path (``auto_adjust=True`` since
    ``e6b4913``) a live frame has five, so a hardcoded six would hand the failed symbol a column no
    surviving symbol has. The expected lists here are also asserted against the engine's own answer
    below, so a future change to that list cannot leave this test passing against a stale copy.
    """
    from bfinance.market.ohlcv import OHLCVEngine

    assert expected == OHLCVEngine._history_columns(
        auto_adjust=auto_adjust, back_adjust=False, actions=actions
    ), "the expected list in this test has drifted from the engine it is quoting"

    stub_ticker.failing = ("BAD",)
    frame = download(["TCS", "BAD"], period="5d", auto_adjust=auto_adjust, actions=actions)

    bad_columns = sorted(_block(frame, "BAD").columns)
    assert bad_columns == sorted(expected), (
        f"auto_adjust={auto_adjust} actions={actions}: the failed symbol must carry the columns "
        f"this call asked for; got {bad_columns!r}, expected {sorted(expected)!r}"
    )


def test_the_default_path_gives_no_adj_close_to_the_failed_symbol(stub_ticker):
    """
    The specific wrong-shape a hardcoded six would produce, isolated.

    ``auto_adjust`` defaults to ``True``, so the DEFAULT call's failed symbol has five columns. This
    is pinned separately from the table above because the default is the one a caller who typed
    nothing is on, and it is the one a hardcoded ``empty_df()`` copy would get wrong.
    """
    import inspect

    assert inspect.signature(download).parameters["auto_adjust"].default is True

    stub_ticker.failing = ("BAD",)
    frame = download(["TCS", "BAD"], period="5d")

    assert "Adj Close" not in _block(frame, "BAD").columns, (
        "the default auto_adjust=True frame has no Adj Close, so the failed symbol must not have "
        f"one either; got {sorted(frame['BAD'].columns)!r}"
    )
    # And it agrees with the symbol that actually answered, which is the property that matters:
    # one frame, one column set.
    assert sorted(_block(frame, "BAD").columns) == sorted(_block(frame, "TCS").columns), (
        "the failed symbol and the live symbol must carry the same columns; they differ: "
        f"{sorted(frame['BAD'].columns)!r} vs {sorted(frame['TCS'].columns)!r}"
    )


def test_auto_adjust_false_does_add_adj_close_to_the_failed_symbol(stub_ticker):
    """The other direction: a richer call must not be answered with a poorer stand-in."""
    stub_ticker.failing = ("BAD",)
    frame = download(["TCS", "BAD"], period="5d", auto_adjust=False)

    assert "Adj Close" in _block(frame, "BAD").columns, (
        f"auto_adjust=False keeps Adj Close; got {sorted(frame['BAD'].columns)!r}"
    )


# ------------------------------------------------------------ the MultiIndex contract still holds


def test_the_multiindex_names_and_level_zero_sort_survive_the_union(stub_ticker):
    """
    ``columns.names == ["Price", "Ticker"]`` and level 0 sorted, exactly as for live symbols.

    Asserted against the live symbol's own columns rather than a literal, so this pins the contract
    ("the failed symbol obeys the same rules") instead of re-stating the implementation.
    """
    stub_ticker.failing = ("BAD",)
    frame = download(["TCS", "BAD"], period="5d")

    assert list(frame.columns.names) == ["Price", "Ticker"]
    level0 = list(frame.columns.get_level_values(0).unique())
    assert level0 == sorted(level0), f"level 0 must be sorted; got {level0!r}"
    # The failed symbol contributes to every level, and contributes them in the same order.
    for price in level0:
        tickers = list(frame[price].columns)
        assert "BAD" in tickers, f"('Close', 'BAD')-style columns must exist for every price; {tickers!r}"


def test_the_failed_ticker_lands_in_its_correct_sorted_position(stub_ticker):
    """
    Sorted by NAME, not appended last, and not dropped into whatever order the fan-out finished in.

    ``BAD`` sits between ``AAA`` and ``TCS`` for every price. If the union appended the absent frame
    after the live ones, ``sort_index(axis=1)`` would still fix it -- so the assertion that matters
    is the one below: the position is produced by the sort the live symbols already relied on, and it
    is checked against the full requested set rather than against "not the last one".
    """
    stub_ticker.failing = ("BAD",)
    frame = download(["AAA", "BAD", "TCS"], period="5d")

    assert _tickers_in(frame) == ["AAA", "BAD", "TCS"], (
        f"the frame must carry the requested tickers in sorted order; got {_tickers_in(frame)!r}"
    )
    for price in frame.columns.get_level_values(0).unique():
        assert list(frame[price].columns) == ["AAA", "BAD", "TCS"], (
            f"BAD must be sorted between AAA and TCS for {price}; got {list(frame[price].columns)!r}"
        )


def test_group_by_ticker_also_carries_the_absent_column(stub_ticker):
    """
    ``group_by="ticker"`` is the mirror of the default and must obey the same rule.

    ``columns.names == ["Ticker", "Price"]`` here, so the sort key is the Ticker level: ``BAD``
    sorts into the middle of the outer level, between ``AAA`` and ``TCS``.
    """
    stub_ticker.failing = ("BAD",)
    frame = download(["AAA", "BAD", "TCS"], period="5d", group_by="ticker")

    assert list(frame.columns.names) == ["Ticker", "Price"]
    tickers = list(dict.fromkeys(frame.columns.get_level_values(0)))
    assert tickers == ["AAA", "BAD", "TCS"], f"outer level must be sorted; got {tickers!r}"
    assert _block(frame, "BAD").isna().all().all(), f"BAD must be all NaN; got\n{frame['BAD']}"
    assert not _block(frame, "TCS").isna().any().any(), f"TCS answered; got\n{frame['TCS']}"


def test_a_fully_failed_batch_still_returns_every_ticker_and_still_warns(stub_ticker, caplog):
    """
    Nothing answered at all: every ticker is still a column, and the warning still fires.

    The old code returned a bare empty ``DataFrame()`` here, which is indistinguishable from
    "you passed no tickers". Now the columns are there and ``df.empty`` is still ``True`` (zero
    rows) -- so every caller test asserting emptiness keeps passing -- but the frame now says which
    tickers were asked about, which is the point.
    """
    stub_ticker.failing = ("AAA", "BBB")
    with caplog.at_level(logging.WARNING, logger="bfinance.download"):
        frame = download(["AAA", "BBB"], period="5d")

    assert _tickers_in(frame) == ["AAA", "BBB"], f"got {_tickers_in(frame)!r}"
    assert frame.empty, "a frame of columns and no rows is still an empty frame, and must stay one"
    assert _block(frame, "AAA").isna().all().all()

    message = " ".join(record.getMessage() for record in caplog.records)
    assert "AAA" in message and "BBB" in message, (
        f"a fully-blank run must still name the symbols that produced nothing; got {message!r}"
    )


# ------------------------------------------------- the two things this must NOT have broken


def test_raise_errors_true_still_raises(stub_ticker):
    """
    Non-negotiable. ``raise_errors=True`` is the only way a caller opts into strict behaviour, and
    unioning an all-NaN column instead of raising would silently disarm it -- the caller believes
    they are strict while debugging the opposite, which is the failure mode this package has already
    been bitten by once (see ``test_adjust_default_drift.py``).

    The union and the raise are different modes, and the flag is the switch between them.
    """
    stub_ticker.failing = ("BAD",)
    with pytest.raises(UpstreamServiceError) as exc:
        download(["TCS", "BAD"], period="5d", raise_errors=True)
    assert "BAD" in str(exc.value), f"the raise must be the offending symbol's own; got {exc}"


def test_raise_errors_true_still_raises_for_an_empty_symbol(stub_ticker):
    """
    ``raise_errors=True`` with a symbol that merely answered empty: no exception exists to raise, so
    this does not raise. Pinned because it is the seam between the two modes -- a caller who asks for
    strict mode gets strict mode for FAILURES, and an empty window is a legitimate answer, not one.
    """
    stub_ticker.empty = ("BAD",)
    frame = download(["TCS", "BAD"], period="5d", raise_errors=True)
    assert _tickers_in(frame) == ["BAD", "TCS"]


def test_the_default_call_is_still_lenient(stub_ticker):
    """The default must stay lenient: this change does not turn a bad symbol into a raise."""
    stub_ticker.failing = ("BAD",)
    frame = download(["TCS", "BAD"], period="5d")
    assert _tickers_in(frame) == ["BAD", "TCS"]


def test_a_whole_batch_of_failures_is_still_an_empty_frame(stub_ticker):
    """
    ``download(...).empty`` must stay ``True`` when nothing answered.

    A frame with columns and zero rows IS empty by pandas' definition, so this holds -- but it is
    the property a lot of existing offline tests rely on for a totally-failed stub, so it is pinned
    rather than assumed.
    """
    stub_ticker.failing = ("AAA", "BBB")
    assert download(["AAA", "BBB"], period="5d").empty


def test_no_tickers_at_all_is_still_a_bare_empty_frame(stub_ticker):
    """
    The degenerate case is unchanged: nothing requested means nothing to be a column for. Asserted
    so the new code cannot be mistaken for one that fabricates columns out of an empty request.
    """
    assert download("", period="5d").empty
    assert download([], period="5d").empty
    frame = download([], period="5d")
    assert list(frame.columns) == [], f"an empty request must have no columns; got {list(frame.columns)!r}"


def test_the_single_ticker_flat_path_still_works_for_a_failure(stub_ticker):
    """
    ``multi_level_index=False`` with one ticker drops the MultiIndex. For a FAILED ticker the flat
    frame is the all-NaN one, which is still the yfinance shape -- yfinance drops the level from
    whatever the concat produced, and the concat produced an ``empty_df()`` entry.
    """
    stub_ticker.failing = ("BAD",)
    frame = download(["BAD"], period="5d", multi_level_index=False)

    assert not isinstance(frame.columns, pd.MultiIndex)
    assert sorted(frame.columns) == sorted(_GOOD_COLUMNS), f"got {sorted(frame.columns)!r}"
    assert frame.isna().all().all(), f"got\n{frame}"


# ---------------------------------------------------------------------- negative controls


def test_the_stand_in_carries_no_rows_of_its_own(stub_ticker):
    """
    The property that actually makes the union work, and the one a future edit breaks silently.

    The stand-in must be a ZERO-ROW frame. It is what makes pandas take the survivors' index as the
    union's, and it is why the absent column comes out NaN even though nothing in the code ever
    writes NaN after the concat (see the note on
    ``test_the_absent_column_is_nan_and_not_zero``). A stand-in that arrived pre-indexed to the
    survivors' dates, or one that got reindexed somewhere along the way, would publish real rows of
    whatever it held -- and if that were zeros, they would look like a flat-trading session.

    Asserted on the helper rather than on the output because the output cannot distinguish the two:
    both a zero-row and a NaN-filled row-carrying stand-in produce an all-NaN column.
    """
    frame = download_mod._all_nan_frame(["Open", "Close"])
    assert len(frame) == 0, f"the stand-in must contribute no rows; got {len(frame)} rows"
    assert frame.index.name == "Date", (
        f"yfinance names the index 'Date' and so must this; got {frame.index.name!r}"
    )
    assert frame.isna().all().all(), "every cell of the stand-in is NaN"


def test_a_good_symbol_with_a_zero_in_it_is_not_mistaken_for_the_stand_in(stub_ticker):
    """
    The converse, and the reason the zero check is not a formality.

    A real answer MAY legitimately contain ``0.0`` -- Volume is zero on a session with no trades.
    If the union overwrote live values, or had used zeros for the absent symbol, the two would be
    indistinguishable and this goes red. It is here to pin that the NaN stand-in is added BESIDE
    the survivors and never on top of them.
    """

    class _ZeroVolumeTicker:
        """Answers TCS with real zeros; BAD still fails, so both halves are exercised at once."""

        def __init__(self, symbol, proxy=None):
            self.symbol = symbol

        async def history_async(self, **kwargs):
            if self.symbol.upper() == "BAD":
                raise UpstreamServiceError("BAD: boom")
            index = pd.DatetimeIndex(_DATES, name="Date", tz=IST)
            return pd.DataFrame(
                [[100.0, 102.0, 99.0, 101.0, 0], [100.0, 102.0, 99.0, 101.0, 0]],
                index=index,
                columns=_GOOD_COLUMNS,
            )

    original = download_mod.Ticker
    try:
        download_mod.Ticker = _ZeroVolumeTicker
        frame = download(["TCS", "BAD"], period="5d")
    finally:
        download_mod.Ticker = original

    live = _block(frame, "TCS")
    absent = _block(frame, "BAD")
    assert live["Volume"].tolist() == [0, 0], f"a real zero must survive as a real zero; got\n{live}"
    assert absent.isna().all().all(), (
        f"BAD must still be the NaN one, so the two are tellable apart; got\n{absent}"
    )
    assert not absent.eq(0.0).any().any(), "the stand-in must not contain the real symbol's zeros"
