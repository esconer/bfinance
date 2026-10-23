"""
C1b -- the primary history call is cached: the immutable prefix is kept, only the tail is fetched.

**The defect this pins.** ``FrameStore`` refuses, correctly, to hold a window whose end is today or
later: today's archive does not exist until NSE publishes it, so a row claiming it would present a
moment of a session as a final day. The price path asked for the cache with the *requested* window,
so ``period="1y"`` -- whose resolved end IS today -- was a permanent miss: two identical calls spent
the whole walk twice, and the plan's measurement of a second ``Ticker("TCS").info`` is 262 archive
files, 233.81 s then 232.89 s. A request whose end was already in the past was cached; the one every
caller of this package is on was not.
C1a wired the cache up only for windows the store may hold, which is what left the primary path
paying full price, and this file is the other half: the window is split where the store's rule draws
its line, the immutable prefix is kept, and only the tail is read live.

**Every test below names the mutation it is red on, and the three that are pins rather than mutant
kills say so.** Each name in this file has been *applied* and the test watched before the docstring
claiming it was written: the boundary spelled "yesterday" (wrong on a Monday, wrong after a holiday),
the split applied to windows that do not reach today, the split skipped altogether, the fetched prefix
stored unsliced (green until the close, when the walk can read today's file -- which is why the
post-close test is the one that pins it), the publish step skipped so the stored row's own cache keys
ride out as this call's, the attrs re-assertion dropped after ``pd.concat``, that empty leg
concatenated anyway, the stored row's missing-dates record carried past the boundary it was written
under *and* the one inside it dropped, the clamped window excluded from the split (the second call
then pays the whole archive walk again -- the regression the clamped-window section pins), and the
prefix fetcher allowed to answer empty like the tail. Three are **pins** instead, saying so in their
own docstrings: a switched-off cache taking the split (a switched-off wrapper is a pass-through, so
both paths spend the same single walk), a caller's mutation of the returned frame (nothing on this
path defers or aliases a write for a mutation to remove), and a stored row whose bars do not answer
its key (refused by ``FrameStore.read`` itself -- measured -- and the refusal's own mutation is
applied to the store's rule, because ``ohlcv`` no longer keeps a copy of it).

**Nothing here touches the network or the user's ``~/.bfinance``.** Every store is a ``tmp_path``
file (or, for the one test that must exercise the default store, ``$BFINANCE_HOME`` pointed at a
temporary directory), every read goes through ``bfinance.market.ohlcv.bhavcopy_range`` monkeypatched
to :class:`_Archive` -- a stand-in that answers *the span it was handed*, one bar per published
candidate day and a recorded 404 for the candidate days whose file does not exist yet, so "how many
archive files did this call spend" is an assertion rather than a claim -- and the calendar everywhere
is the real ``iter_trading_days``, so a count moves with the calendar instead of being typed in.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from typing import List, Optional, Tuple

import pandas as pd
import pytest

import bfinance.market.ohlcv as ohlcv_mod
from bfinance.cache.frame_store import (
    CACHE_COVERAGE_END_ATTR,
    CACHE_FETCHED_ATTR,
    CACHE_REQUESTED_END_ATTR,
    CACHE_STATUS_ATTR,
    IST,
    FrameStore,
)
from bfinance.market.ohlcv import OHLCVEngine, RealOHLCUnavailableError, SOURCE_NETWORK
from bfinance.market.price_cache import CACHE_UNCOVERED_DAYS_ATTR, PriceCache
from bfinance.nse.bhavcopy import MISSING_DATES_ATTR, iter_trading_days

from conftest import StubCorporateActionsClient

#: Every frame here is as-traded on purpose: the adjustment step shapes the frame *after* this read,
#: so auto_adjust=True would drag the corporate-actions feed into a cache test (and a stub cannot
#: supply the share-count factor anyway).
_WINDOW_KWARGS = {"actions": False, "auto_adjust": False}


# --------------------------------------------------------------------------------------------- time


def _today() -> pd.Timestamp:
    """Today in IST, the day both the archive's clock and this path's boundary are judged against."""
    return pd.Timestamp.now(tz=IST).normalize()


def _candidates(start: str, end: str) -> List[str]:
    """Candidate trading days in the span, as ISO days: the archive's own calendar."""
    return [day.isoformat() for day in iter_trading_days(start, end)]


def _boundary(on: Optional[pd.Timestamp] = None) -> str:
    """
    The last day whose archive file is final: the newest candidate strictly before ``on`` (or today).

    Derived here from the calendar rather than read off the engine, deliberately. A test that asked
    the engine for its own answer would agree with "yesterday" instead of failing on it, and
    "yesterday" is wrong on exactly the days this file exists to get right -- a Monday, where
    yesterday is a Sunday, and the day after a holiday, where yesterday has no session at all.
    """
    stamp = _today() if on is None else pd.Timestamp(on)
    days = iter_trading_days(
        (stamp - pd.Timedelta(days=7)).date(), (stamp - pd.Timedelta(days=1)).date()
    )
    return days[-1].isoformat()


def _next_day(day: str) -> str:
    return (pd.Timestamp(day) + pd.Timedelta(days=1)).date().isoformat()


def _year_window(end: str) -> Tuple[str, str]:
    """``start``/``end`` for a year-long request ending on ``end``, spelled out for a fixed key.

    Spelled out rather than passed as ``period="1y"`` so the window -- and therefore the store key a
    test asserts on -- does not move between the two calls of a test that crosses midnight.
    """
    start = (pd.Timestamp(end) - pd.DateOffset(years=1)).date().isoformat()
    return start, end


def _pending(boundary_iso: str, requested_end: str, published_through: str) -> List[str]:
    """Candidate days after the boundary that NSE has not published a file for yet."""
    return [
        day
        for day in _candidates(_next_day(boundary_iso), requested_end)
        if day > published_through
    ]


def _spent(archive: "_Archive") -> int:
    """Archive requests asked for so far: one per candidate day of every span handed over."""
    return len(archive.days_requested)


def _stored_rows(store_path) -> int:
    """How many rows the store holds, counted in SQL so nothing is read through the API."""
    with sqlite3.connect(str(store_path)) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM bfinance_price_frames").fetchone()[0])


# -------------------------------------------------------------------------------------- the archive


class _Archive:
    """
    A range-aware stand-in for ``bfinance.nse.bhavcopy.bhavcopy_range``.

    Range-aware is the whole point. It answers *the span it was handed*: one bar per published
    candidate day inside that span, and a recorded 404 for the candidate days inside it whose file
    does not exist yet. A stub that answered the same frame to every span would make "the second call
    fetched only the tail" unmeasurable -- it would answer a one-day span with a whole year of bars --
    and would hide a walk that asked for the wrong days entirely.

    ``published_through`` is the last day whose file exists, which is what makes the hours either side
    of the close testable: ``yesterday`` is a call made at 15:59, before NSE has published today's
    file; ``today`` is the same call after 18:00, when it has. The default is yesterday, because that
    is the state the primary ``period="1y"`` call is in when a user asks for it during the session.

    ``error`` and ``bare_empty`` model the two failure shapes: an archive that raises (an outage) and
    an archive that answers nothing at all (a client whose every candidate day 404'd).
    """

    def __init__(
        self,
        symbol: str = "TCS",
        published_through: Optional[str] = None,
        *,
        error: Optional[Exception] = None,
        bare_empty: bool = False,
    ) -> None:
        self.symbol = symbol
        self.error = error
        self.bare_empty = bare_empty
        self.published_through = (
            published_through
            if published_through is not None
            else (_today() - pd.Timedelta(days=1)).date().isoformat()
        )
        self.spans: List[Tuple[str, str]] = []
        self.days_requested: List[str] = []
        self.days_served: List[str] = []

    def __call__(self, start, end):
        start, end = str(start)[:10], str(end)[:10]
        self.spans.append((start, end))
        if self.error is not None:
            raise self.error

        days = list(iter_trading_days(start, end))
        self.days_requested.extend(day.isoformat() for day in days)
        served = [
            day
            for day in days
            if day.isoformat() <= self.published_through and day >= date(2024, 1, 1)
        ]
        self.days_served.extend(day.isoformat() for day in served)
        if self.bare_empty:
            # A client that answers nothing at all: no rows and no schema either. An archive that
            # 404'd every candidate day answers an empty frame *with* its columns and its
            # missing-dates record -- both shapes are real, and they take different paths here.
            return pd.DataFrame()
        return self._market_frame(
            served, [day.isoformat() for day in days if day not in served], start, end
        )

    def _market_frame(self, served, missing, start, end) -> pd.DataFrame:
        """Whole-market bhavcopy frame: a (Date, Symbol) MultiIndex and the archive's own columns."""
        rows, dates, symbols = [], [], []
        for day in served:
            close = 100.0 + (day - date(2024, 1, 1)).days
            rows.append((close - 1.0, close + 2.0, close - 2.0, close, 1000 + day.day, 10.0))
            dates.append(pd.Timestamp(day))
            symbols.append(self.symbol)
        index = pd.MultiIndex.from_arrays(
            [pd.DatetimeIndex(dates), pd.Index(symbols, dtype=object, name="Symbol")],
            names=["Date", "Symbol"],
        )
        frame = pd.DataFrame(
            rows,
            columns=["Open", "High", "Low", "Close", "Volume", "Turnover"],
            index=index,
        )
        frame["Volume"] = frame["Volume"].astype("int64")
        frame = frame.sort_index()
        frame.attrs.update(
            {
                "bfinance_provenance": "NSE CM bhavcopy (range-aware test stub)",
                "bfinance_series_included": ("EQ",),
                "bfinance_symbol_count": 1,
                "bfinance_requested_start": start,
                "bfinance_requested_end": end,
                "bfinance_candidate_trading_days": len(list(iter_trading_days(start, end))),
                MISSING_DATES_ATTR: list(missing),
                "bfinance_synthetic_ohlc": False,
            }
        )
        return frame


class _Screener:
    """The bhavcopy path must never fall back to the close-only feed; say so if it ever does."""

    async def get_chart_timeseries(self, *args, **kwargs):
        raise AssertionError("the bhavcopy path must not reach the Screener chart route")

    async def get_company_profile(self, *args, **kwargs):
        raise AssertionError("the bhavcopy path must not reach the Screener profile route")


# -------------------------------------------------------------------------------------- fixtures


@pytest.fixture
def archive(monkeypatch) -> _Archive:
    """Install the range-aware archive stand-in, and take the opt-in mirror out of the way."""
    stub = _Archive()
    monkeypatch.setattr(ohlcv_mod, "bhavcopy_range", stub)
    monkeypatch.delenv(ohlcv_mod.MIRROR_ENV_VAR, raising=False)
    return stub


@pytest.fixture
def store_path(tmp_path):
    """This test's own frame-store file. No test here reads or writes the user's ``~/.bfinance``."""
    return tmp_path / "frames.db"


@pytest.fixture
def store(store_path) -> FrameStore:
    return FrameStore(db_path=store_path)


def _engine(*, store: Optional[FrameStore] = None, cache=None) -> OHLCVEngine:
    """An engine whose frame cache is this test's own store -- or the wrapper passed instead."""
    return OHLCVEngine(
        _Screener(),
        corporate_actions_client=StubCorporateActionsClient(),
        price_cache=cache if cache is not None else PriceCache(store=store),
    )


async def _history(engine: OHLCVEngine, start: str, end: str, symbol: str = "TCS"):
    return await engine.fetch_history(symbol=symbol, start=start, end=end, **_WINDOW_KWARGS)


# ---------------------------------------------------------------------------- the primary call


async def test_a_second_identical_year_call_pays_only_for_the_tail(store, store_path, archive):
    """
    The headline, on the path the library is actually called on: a year ending *today*.

    Mutations this is red on, both applied and watched: **no split at all** (the defect this file
    exists for: the second call pays the whole walk again) and **a boundary of "yesterday"** (the key
    the first call writes is then not the key the second call looks under, so the row is missed and the
    whole window is walked again -- and this fires on every run whose yesterday is not a trading day,
    which is every Monday and every day after a holiday).

    Storing the fetched prefix *unsliced* is deliberately **not** caught here, and that was measured
    too: on a call made before the close the whole-window frame stops at the boundary anyway, so the
    slice and the whole frame hold the same rows and the second call still pays only for the tail.
    That mutation is what ``test_after_the_close_the_row_kept_still_stops_at_the_boundary`` is for.
    """
    start, end = _year_window(_today().date().isoformat())
    engine = _engine(store=store)
    whole = len(_candidates(start, end))
    boundary = _boundary()

    before = _spent(archive)
    first = await _history(engine, start, end)
    assert _spent(archive) - before == whole, (
        "the first call is the walk that was always spent: one request per candidate day"
    )
    assert archive.spans == [(start, end)], "and it is one read of the whole requested window"
    assert first.attrs[CACHE_STATUS_ATTR] == "miss", "a live fetch happened, so it says so"
    assert first.attrs["bfinance_source"] == SOURCE_NETWORK

    kept = store.read(symbol="TCS", exchange="NSE", series=None, start=start, end=boundary)
    assert kept.status == "hit", "the first call must keep the immutable part of what it read"
    assert str(kept.value.index.max())[:10] == boundary, "nothing after the boundary may be kept"
    assert (
        store.read(symbol="TCS", exchange="NSE", series=None, start=start, end=end).status == "miss"
    ), "the requested window ends today, so the store may not hold it -- and does not"

    before = _spent(archive)
    second = await _history(engine, start, end)
    tail = _candidates(_next_day(boundary), end)
    assert _spent(archive) - before == len(tail), (
        "the second call pays for the tail and nothing else"
    )
    assert len(tail) <= 1, "a window ending today has at most one candidate day left to fetch"
    assert archive.spans[-1] == (_next_day(boundary), end), "and it asked for exactly those days"

    assert second.attrs[CACHE_STATUS_ATTR] == "miss", (
        "a live read happened on this call too, so the frame must not claim to be a replay"
    )
    assert first["Close"].tolist() == second["Close"].tolist(), (
        "the merged frame is the first call's series: the prefix is the same data it read"
    )


async def test_the_warm_frame_is_the_cold_frame(store, store_path, tmp_path, archive):
    """
    Verification 2: the cached answer must be the cold answer -- values, index, dtypes, provenance.

    Mutations this is red on, both applied and watched: **return the merged frame without routing it
    through the wrapper** (the stored row's own cache keys -- status ``hit`` among them -- then ride
    out as this call's, so a frame assembled from a live read claims to be a replay) and
    **concatenate an empty tail anyway** (measured on pandas 3.0.5: the real value columns become
    ``object`` and the index moves from second to nanosecond resolution, so ``equals`` says no).
    Dropping the ``merged.attrs = attrs`` line is red on ``test_publication_...`` instead -- its tail
    is non-empty, and here the tail is whatever today's calendar leaves, which on a weekend is nothing
    to concatenate at all.
    """
    start, end = _year_window(_today().date().isoformat())
    today = _today().date().isoformat()
    boundary = _boundary()
    warm_engine = _engine(store=store)

    await _history(warm_engine, start, end)  # the walk that fills the prefix
    warm = await _history(warm_engine, start, end)  # the merged one, from the prefix + the tail

    cold = await _history(_engine(store=FrameStore(db_path=tmp_path / "cold.db")), start, end)

    assert warm.equals(cold), "the merged frame must be the frame a cold full read produces"
    assert list(warm.columns) == list(cold.columns)
    assert warm.index.equals(cold.index)

    published = (
        CACHE_STATUS_ATTR,
        CACHE_COVERAGE_END_ATTR,
        CACHE_REQUESTED_END_ATTR,
        CACHE_UNCOVERED_DAYS_ATTR,
        "bfinance_source",
        "bfinance_provenance",
        "bfinance_series_included",
        "bfinance_candidate_trading_days",
        MISSING_DATES_ATTR,
        "price_basis",
    )
    for key in published:
        assert warm.attrs.get(key) == cold.attrs.get(key), f"{key} differs from the cold read"

    assert warm.attrs[CACHE_REQUESTED_END_ATTR] == today, "the window asked for is the window named"
    assert warm.attrs[CACHE_COVERAGE_END_ATTR] == _last_bar(warm), (
        "coverage end is what the data actually reaches, not the window's end: the frame must not "
        "read as a claim that today's session is in it"
    )
    assert warm.attrs[CACHE_UNCOVERED_DAYS_ATTR] == _pending(boundary, today, archive.published_through), (
        "and every requested candidate day the frame does not contain is named"
    )

    differing = {k: v for k, v in warm.attrs.items() if k != CACHE_FETCHED_ATTR}
    baseline = {k: v for k, v in cold.attrs.items() if k != CACHE_FETCHED_ATTR}
    assert differing == baseline, (
        "the whole attrs record must survive the merge: only bfinance_cache_fetched_at is allowed "
        "to differ, because the row and the live read were made at two different instants"
    )
    assert warm.attrs[CACHE_FETCHED_ATTR][:10] == today == cold.attrs[CACHE_FETCHED_ATTR][:10], (
        "and both were stamped by a live read on this call, to the day: a replay would carry the "
        "stored row's own older instant"
    )


def _last_bar(frame: pd.DataFrame) -> Optional[str]:
    return None if frame.empty else str(frame.index.max())[:10]


async def test_the_warm_prefix_frame_publishes_the_tails_provenance(
    store, archive, monkeypatch
):
    """
    F2, pinned as a contract: the composite frame's provenance and age are the LIVE TAIL's.

    On the warm-prefix branch the returned frame is ``stored prefix (a replay) + live tail``, and the
    five published keys are built from the window read that fetched that tail -- there is no third
    ``get_or_fetch`` re-publishing them. That same window reaches today. The store refuses to hold
    it and answers ``"miss"``, so the five published keys are the ones a live read publishes --
    and here that is a COMPOSITE claim: ``miss`` and ``fetched_at`` describe the tail that came off
    the wire on this call, not the year of bars behind it. The prefix is not lost and is not
    re-dated: its row, under ``start..boundary``, still carries the instant the walk that wrote it
    ran, which is why the composite is honest rather than a fact that went missing.

    That is deliberate, and it is documented where the frame is composed
    (:meth:`OHLCVEngine._rows_around_the_immutable_boundary`, at the ``return``). The vocabulary has
    no word for "one live tail plus one replay" -- ``hit``/``null``/``miss``/``expired``/``corrupt``
    plus ``disabled`` -- and inventing one would be a second vocabulary for the five keys the wrapper
    owns. What must NOT be composite is the liveness triad, and the assertions below split the two
    claims apart: provenance/age is the tail's, coverage/requested/uncovered is the full frame's.

    Mutation this is red on, applied and watched: **return the merged frame without routing it
    through the wrapper's publish step** (``return rows``). The frame then carries the stored
    prefix's own ``fetched_at`` -- an instant from the earlier call -- which is the wrong-age claim
    this test exists for.
    """
    from bfinance.market import price_cache as price_cache_mod

    start, end = _year_window(_today().date().isoformat())
    boundary = _boundary()
    engine = _engine(store=store)

    first_instant = "2026-01-02T09:15:00+05:30"
    tail_instant = "2026-01-03T09:16:00+05:30"
    monkeypatch.setattr(price_cache_mod, "_now_iso", lambda: first_instant)
    first = await _history(engine, start, end)
    assert first.attrs[CACHE_FETCHED_ATTR] == first_instant

    prefix_before = store.read(symbol="TCS", exchange="NSE", series=None, start=start, end=boundary)
    assert prefix_before.status == "hit"
    prefix_instant = prefix_before.value.attrs[CACHE_FETCHED_ATTR]
    assert prefix_instant != tail_instant, "the row and the tail were dated at two instants"

    monkeypatch.setattr(price_cache_mod, "_now_iso", lambda: tail_instant)
    warm = await _history(engine, start, end)

    assert warm.attrs[CACHE_STATUS_ATTR] == "miss", (
        "a live tail was fetched on this call, so the composite cannot claim to be a replay"
    )
    assert warm.attrs[CACHE_FETCHED_ATTR] == tail_instant, (
        "and its fetched_at is this call's tail, not the earlier prefix's instant"
    )

    prefix_after = store.read(symbol="TCS", exchange="NSE", series=None, start=start, end=boundary)
    assert prefix_after.status == "hit"
    assert prefix_after.value.attrs[CACHE_FETCHED_ATTR] == prefix_instant, (
        "the prefix's own dating is still read under the prefix key after the composite call, so "
        "the tail-dated claim did not overwrite it -- one key, one row, one instant"
    )

    assert warm.attrs[CACHE_COVERAGE_END_ATTR] == _last_bar(warm), (
        "coverage end is the merged frame's last bar, not the tail's alone"
    )
    assert warm.attrs[CACHE_REQUESTED_END_ATTR] == end
    assert warm.attrs[CACHE_UNCOVERED_DAYS_ATTR] == _pending(
        boundary, end, archive.published_through
    ), "and the uncovered days are recomputed for the whole window, so the triad is not composite"


async def test_a_closed_window_is_still_miss_then_hit(store, archive):
    """
    Legs that do not reach today are untouched: one walk, then zero requests and a replay.

    Mutation this is red on, applied and watched: **let the split fire for a window whose end is in
    the past** (drop the ``end_iso < today`` clause). The second call then asks for a tail that ends
    before it starts, so the spans a walk was handed stop being the requested window at all -- and
    ``iter_trading_days`` is handed an inverted span on the way.

    The two legs are the two closed shapes worth pinning: a window ending exactly on the boundary,
    which is the newest window the store may hold at all, and one ending an earlier session still.
    """
    boundary = _boundary()
    previous = _boundary(pd.Timestamp(boundary, tz=IST))
    for end in (boundary, previous):
        engine = _engine(store=store)
        start, _ = _year_window(end)
        whole = len(_candidates(start, end))
        already = len(archive.spans)

        before = _spent(archive)
        first = await _history(engine, start, end)
        assert _spent(archive) - before == whole
        assert first.attrs[CACHE_STATUS_ATTR] == "miss"

        before = _spent(archive)
        second = await _history(engine, start, end)
        assert _spent(archive) - before == 0, "a window the store can hold is answered from disk"
        assert second.attrs[CACHE_STATUS_ATTR] == "hit"
        assert second.equals(first), "and it is the same frame, not a re-derived one"

        assert archive.spans[already:] == [(start, end)], (
            "a closed window is one whole-window read and then nothing: "
            f"{archive.spans[already:]}"
        )


async def test_a_window_clamped_to_the_floor_is_split_under_the_resolved_key(
    store, store_path, archive
):
    """
    A clamped request is split like any other, and the key is the RESOLVED window -- one key.

    ``period="max"``, ``"5y"`` and the default 1825-day lookback all resolve to the archive floor,
    and they used to be excluded from the split on the argument that they would share a row and a
    frame. They do share a key, and sharing it is the cache working rather than a collision: the
    clamp happens *before* the key is built (:meth:`_fetch_bhavcopy_bars` resolves the window and
    then calls the reader), so ``start_iso`` is :data:`BHAVCOPY_FIRST_DATE` for every clamped
    request and the row is filed under the window its bars actually answer. The one thing that
    differs between two clamped spellings is the truncation sentence, and that is never read off
    the row -- ``fetch_history`` republishes it from the current call's own request -- which the
    ``2019``/``2020`` pair below pins. Keying on the *requested* start instead would file the same
    floor-to-boundary data once per spelling and answer none of them from another's row, which is a
    second defect rather than a fix for this one.

    Mutations this is red on, both applied and watched: **exclude the clamped window from the split**
    (the second call then walks the whole archive again and no prefix row exists) and **file the
    prefix under the requested start** (the second spelling's read is then a key nothing writes, so
    it misses and pays the whole walk).
    """
    end = _today().date().isoformat()
    start = ohlcv_mod.BHAVCOPY_FIRST_DATE  # the clamped window's resolved start: the floor itself
    boundary = _boundary()
    tail = _candidates(_next_day(boundary), end)
    whole = len(_candidates(start, end))
    engine = _engine(store=store)

    before = _spent(archive)
    first = await _history(engine, "2019-01-01", end)
    assert _spent(archive) - before == whole, (
        "the first call is the walk that was always spent, in full"
    )
    assert archive.spans[-1] == (start, end), (
        "and it spans the clamped window in ONE range fetch, not one per half: "
        f"{archive.spans[-1]}"
    )
    assert first.attrs[CACHE_STATUS_ATTR] == "miss"
    assert first.attrs["bfinance_window_truncated"] is True, (
        "the clamp is still reported -- this test is about the split, not about the clamp"
    )
    assert "2019-01-01" in first.attrs["bfinance_window_truncated_detail"]

    kept = store.read(symbol="TCS", exchange="NSE", series=None, start=start, end=boundary)
    assert kept.status == "hit", "the resolved window's immutable part is filed under its own key"
    assert _last_bar(kept.value) == boundary, "nothing after the boundary may be kept"
    assert (
        store.read(symbol="TCS", exchange="NSE", series=None, start=start, end=end).status == "miss"
    ), "and the open window it resolves to is not filed at all"
    assert _stored_rows(store_path) == 1, "one row: the prefix, and only the prefix"

    before = _spent(archive)
    second = await _history(engine, "2020-01-01", end)  # different request, SAME resolved key
    assert _spent(archive) - before == len(tail), (
        "a different spelling of the same clamped window pays only the tail"
    )
    assert archive.spans[-1] == (_next_day(boundary), end), "and asks for exactly those days"
    assert second.attrs[CACHE_STATUS_ATTR] == "miss", "a live read happened on this call too"
    assert "2020-01-01" in second.attrs["bfinance_window_truncated_detail"], (
        "the sentence is the CURRENT request's: the row was written by the 2019 request and must "
        "not put its start on a 2020 caller's frame"
    )
    assert first["Close"].tolist() == second["Close"].tolist(), (
        "and the bars are the resolved window's own series, unchanged by the second spelling"
    )
    assert _stored_rows(store_path) == 1, "the repeat call adds nothing: the tail is never stored"


async def test_a_switched_off_cache_reads_the_whole_window_again(tmp_path, monkeypatch, archive):
    """
    ``PriceCache(enabled=False)`` must be exactly the pre-cache path: no split, no tail, no store.

    This one is a **pin, not a mutant kill**, and the mutation was applied and measured before this
    sentence was written: dropping the ``enabled`` guard is **green** here, because a switched-off
    wrapper is a pass-through -- it never stores and never serves, so its fetcher runs once per lookup
    and the split spends the same single whole-window walk the un-split path spends. What the guard
    buys is that the path does not take the split at all; what this test pins is the *outcome* that
    has to hold either way: one whole-window walk per call, ``disabled`` published rather than a
    misleading ``miss``, and no store file created -- which is the half a guard could break.
    """
    home = tmp_path / "home"
    monkeypatch.setenv("BFINANCE_HOME", str(home))
    start, end = _year_window(_today().date().isoformat())
    whole = len(_candidates(start, end))
    engine = _engine(cache=PriceCache(enabled=False))

    frames = []
    for _ in range(2):
        before = _spent(archive)
        frames.append(await _history(engine, start, end))
        assert _spent(archive) - before == whole, (
            "with no cache there is nothing to split a window around, so every call is the walk"
        )

    assert [frame.attrs[CACHE_STATUS_ATTR] for frame in frames] == ["disabled", "disabled"], (
        "the frame must say the cache was off rather than label a live read as a miss"
    )
    assert archive.spans == [(start, end), (start, end)]
    assert not (home / "frames.db").exists(), (
        "a switched-off cache must not create a store -- not even an empty one"
    )


async def test_a_stored_row_cannot_supply_a_day_that_is_not_final(store, store_path, archive):
    """
    A row claiming today -- planted under the prefix key -- is refused, not served to the merge.

    The store's *write* rule is about the window a row is filed under, not about its payload: a row
    written for a window ending on the boundary can carry a bar dated today and still be accepted,
    because nothing on the way in inspects the frame's dates against its key. That is the
    publish-boundary failure arriving through the cache, and the *read* is where it is caught:
    ``FrameStore.read`` refuses a row whose bars do not answer its key (``_bars_lie_inside_the_window``,
    in the store rather than here) and answers ``corrupt``, which the wrapper turns into the live read
    the row stood in for. Measured, that is where the refusal comes from -- the store logs the planted
    key and the bar outside it, and this path never sees the row at all. ``ohlcv`` kept its own copy of
    that check until this was measured; the copy was unreachable and was removed rather than kept as a
    second rule to hold in step.

    Mutation this is red on, applied and watched: ``frame_store._bars_lie_inside_the_window``
    monkeypatched to accept anything. The store then serves the planted row, the merge carries
    today's bar, and the frame advertises a coverage end on a session that is not over -- which is the
    failure this test exists for, whichever module owns the refusal. It patches a peer's private
    helper on purpose: the point is that *some* layer refuses it, and the one that does is named here.
    """
    start, end = _year_window(_today().date().isoformat())
    today = _today().date().isoformat()
    boundary = _boundary()

    planted = pd.DataFrame(
        {"Open": [1.0], "High": [2.0], "Low": [0.5], "Close": [1.5], "Volume": [10]},
        index=pd.DatetimeIndex([today], name="Date", tz=IST),
    )
    planted.attrs["bfinance_provenance"] = "planted"
    assert store.write(
        planted, symbol="TCS", exchange="NSE", series=None, start=start, end=boundary
    ) == "hit", "the store accepts it, which is why the read side has to check the payload"
    assert (
        store.write(planted, symbol="TCS", exchange="NSE", series=None, start=start, end=today)
        == "miss"
    ), "while a window that reaches today is refused outright -- the guard is not being deleted"

    engine = _engine(store=store)
    before = _spent(archive)
    frame = await _history(engine, start, end)

    assert today not in [str(day)[:10] for day in frame.index], (
        "no bar dated today may reach the caller, from the store or anywhere else"
    )
    assert frame.attrs[CACHE_COVERAGE_END_ATTR] < today
    assert _spent(archive) - before == len(_candidates(start, end)), (
        "a refused row costs the live read it was standing in for -- the safe direction"
    )


async def test_a_failed_or_empty_read_is_never_stored_or_answered(store_path, archive):
    """
    The failure shapes stay failures, and nothing about them reaches the store.

    Mutation this is red on, applied and watched: **let the whole-window fetcher answer empty** (pass
    ``allow_empty=True`` where the tail passes it). "NSE answered nothing at all for the window" is
    the outage signal the caller relies on; turning it into an empty frame makes an outage
    indistinguishable from a symbol that does not trade, and that frame is then the frame a caller
    acts on.

    Both halves are asserted at the store as well as at the exception, because "the store refuses it"
    is a promise this path must not break on its way past -- and a refused write is a *status*, not an
    exception, so nothing else in the suite would notice if one started being accepted.
    """
    start, end = _year_window(_today().date().isoformat())

    archive.error = OSError("connection reset")
    with pytest.raises(RealOHLCUnavailableError) as outage:
        await _history(_engine(store=FrameStore(db_path=store_path)), start, end)
    assert "connection reset" in str(outage.value)
    assert _stored_rows(store_path) == 0

    archive.error = None
    archive.bare_empty = True
    with pytest.raises(RealOHLCUnavailableError) as empty:
        await _history(_engine(store=FrameStore(db_path=store_path)), start, end)
    assert "no data" in str(empty.value)
    assert _stored_rows(store_path) == 0


async def test_the_boundary_carries_a_weekend_into_monday(tmp_path, monkeypatch, archive):
    """
    The boundary is a trading day, so Saturday and the Monday after it share one key.

    Sunday and the Monday after it resolve to the *same* Friday, so a prefix stored on the Sunday
    answers the Monday call with no walk at all. "Yesterday" moves the key every single day, which
    on a weekend means the Monday call misses a row that was written two days earlier -- the cost the
    trading-day boundary removes, and the reason the calendar is asked instead of the date arithmetic.

    Mutation this is red on: **boundary = today - 1 day.** The Sunday call then files its prefix under
    Saturday, the Monday call looks under Sunday, and the whole window is walked again.

    The clock is injected the way ``tests/test_market_price_cache.py`` injects one into the store:
    a rule that cannot be pointed at a fixed instant cannot be tested at all, and this rule's whole
    content is which day it is. The store is pointed at the same instant so that the *other* date rule
    -- "a window that contains today is neither stored nor served" -- is judged against the same day
    as the boundary is, which is what happens in production.
    """
    sunday = (_today() - pd.Timedelta(days=_today().weekday() + 1)).normalize()
    monday = sunday + pd.Timedelta(days=1)
    start, _ = _year_window(sunday.date().isoformat())

    async def _run(day: pd.Timestamp, store: FrameStore):
        monkeypatch.setattr(ohlcv_mod, "_ist_today", lambda: day)
        engine = _engine(store=store)
        before = _spent(archive)
        frame = await _history(engine, start, day.date().isoformat())
        return frame, _spent(archive) - before

    store = FrameStore(
        db_path=tmp_path / "frames.db",
        clock=lambda: sunday,
    )
    frame, sunday_cost = await _run(sunday, store)
    assert sunday_cost == len(_candidates(start, sunday.date().isoformat())), (
        "the Sunday call is a cold prefix: it reads the whole window once"
    )
    assert frame.attrs[CACHE_COVERAGE_END_ATTR] == _boundary(sunday)

    boundary = _boundary(monday)
    assert boundary == _boundary(sunday), "the weekend and the Monday after it share a boundary"
    monday_store = FrameStore(db_path=store.db_path, clock=lambda: monday)
    archive.published_through = monday.date().isoformat()
    frame, monday_cost = await _run(monday, monday_store)
    assert monday_cost == len(_candidates(_next_day(boundary), monday.date().isoformat())) <= 1, (
        "the Monday call must find the prefix and pay for the tail alone"
    )
    assert frame.attrs[CACHE_COVERAGE_END_ATTR] == monday.date().isoformat(), (
        "and the tail is the day it now covers"
    )


async def test_publication_moves_the_tail_and_never_the_window_that_was_stored(
    tmp_path, monkeypatch, archive
):
    """
    The same call either side of the close: 15:59 has no file, 18:00 has one, the store has neither.

    This is also the one place a stored record is provably stale. The prefix is written by a walk that
    saw the *whole* window, so its missing-dates record names the day that was still unpublished --
    a day *after* the boundary it is filed under. Re-reading it after publication must not carry that
    claim: the tail re-observed the day, found a file, and the merged record is empty again.

    Mutations this is red on, both applied and watched: **drop the ``<= boundary`` filter** when
    merging the records (the merged frame then claims a shortfall on the day it just fetched and is
    holding, which is precisely the "frame that cannot say what it has" this package refuses
    everywhere else) and **skip the publish step** (the stored row's own keys -- ``hit`` among them --
    ride out as this call's). The last two assertions are a *pin* rather than a mutant: today's window
    never being stored is the store's own refusal, and this path must not be the door around it, so
    what is asserted is that it is not -- a count in SQL, and a read that comes back ``miss``.

    The clock is injected so the scenario is a session day's hours whatever day of the week the suite
    runs on: today is the last session that closed, and the publication flip is that session's own
    file appearing. A weekend run would otherwise make "the day that is not published yet" impossible
    to stage, because a weekend day has no file to wait for in the first place.
    """
    today = pd.Timestamp(_boundary(), tz=IST)  # the last session that closed
    boundary = _boundary(today)
    monkeypatch.setattr(ohlcv_mod, "_ist_today", lambda: today)
    start, end = _year_window(today.date().isoformat())
    store = FrameStore(db_path=tmp_path / "frames.db", clock=lambda: today)
    archive.published_through = boundary  # 15:59: today's file does not exist yet

    engine = _engine(store=store)
    before = _spent(archive)
    awaiting = await _history(engine, start, end)
    assert _spent(archive) - before == len(_candidates(start, end)), "a cold prefix reads the window"
    assert _last_bar(awaiting) == boundary, "and it reaches the last final day, not the session's"
    assert awaiting.attrs[MISSING_DATES_ATTR] == [today.date().isoformat()], (
        "with the day that has no file yet named as the archive's own missing-dates record"
    )
    assert awaiting.attrs[CACHE_UNCOVERED_DAYS_ATTR] == [today.date().isoformat()]
    assert _stored_rows(tmp_path / "frames.db") == 1, "one row: the prefix, and only the prefix"

    archive.published_through = today.date().isoformat()  # 18:00: the file has appeared
    before = _spent(archive)
    published = await _history(engine, start, end)
    assert _spent(archive) - before == 1, "the tail is the one day that appeared"
    assert published.attrs[CACHE_COVERAGE_END_ATTR] == today.date().isoformat(), (
        "the frame being read now reaches today -- and the prefix alone cannot do that, so this is "
        "the tail's bar arriving, not a claim inherited from the stored row"
    )
    assert published.attrs[MISSING_DATES_ATTR] == [], (
        "the stored row's claim about today must not survive a call that fetched today"
    )
    assert published.attrs[CACHE_UNCOVERED_DAYS_ATTR] == []
    assert _last_bar(published) == boundary, (
        "the bars handed to the caller still stop before the requested end, which is yfinance's "
        "exclusive-end contract on every path and not something the cache may change"
    )

    assert _stored_rows(tmp_path / "frames.db") == 1, (
        "today's window is never stored, however the frame that reaches it was assembled"
    )
    assert (
        store.read(symbol="TCS", exchange="NSE", series=None, start=start, end=end).status == "miss"
    )


async def test_the_gap_the_stored_row_recorded_is_still_named_after_the_merge(store, archive):
    """
    A hole the archive recorded *inside* the stored window must survive the merge.

    A stored row is a walk's own slice, holes included: the walk that wrote it asked for a candidate
    day whose file NSE had not published, recorded it in ``bfinance_bhavcopy_missing_dates``, and was
    still written -- a short frame that says so is servable, which is the contract the store and
    ``PriceCache`` both document. The tail can never re-observe that day (its own span starts the day
    after the boundary), so a merge that kept only the tail's record would drop the hole from the one
    place it was named, and a frame with a missing bar in it would read as a complete one.

    Mutations this is red on, both applied and watched: **keep only the tail's record** (the merged
    frame then reports a clean window with a bar missing from it) and **filter one day too tightly**
    (``<`` where the code says ``<=``, which drops the boundary session's own entry -- the off-by-one
    a boundary is exactly the place to make). The scenario is the ordinary shape of a real gap -- the
    boundary's own file was missing when the row was written -- so the day under test is the one
    immediately before today, the day a one-day staleness bug would also get wrong.

    The first call is where the other half lives, and it is a *pin*: the frame covers up to the last
    day it actually has data for, its coverage end says so, and ``bfinance_cache_uncovered_days``
    names the difference to the window that was asked for. Nothing may shorten that record because the
    data came from a cache.
    """
    today = _today().date().isoformat()
    boundary = _boundary()
    behind = _boundary(pd.Timestamp(boundary, tz=IST))
    start, end = _year_window(today)
    archive.published_through = behind  # the boundary's file is missing when the walk runs
    engine = _engine(store=store)

    first = await _history(engine, start, end)
    assert boundary in first.attrs[MISSING_DATES_ATTR]
    assert first.attrs[CACHE_COVERAGE_END_ATTR] == behind, (
        "the coverage end is the last day with data, not the window's end"
    )
    assert boundary in first.attrs[CACHE_UNCOVERED_DAYS_ATTR], (
        "and the requested day the frame does not have is named as uncovered"
    )

    second = await _history(engine, start, end)
    assert second.attrs[CACHE_STATUS_ATTR] == "miss", "the tail was read live on this call"
    assert boundary in second.attrs[MISSING_DATES_ATTR], (
        "the day the stored row recorded as unpublished must still be named: the tail's walk starts "
        "after the boundary and can never see it again"
    )
    assert boundary in second.attrs[CACHE_UNCOVERED_DAYS_ATTR]
    assert _last_bar(second) == behind, "and the frame still has no bar for it"


async def test_after_the_close_the_row_kept_still_stops_at_the_boundary(
    tmp_path, monkeypatch, archive
):
    """
    Whatever the walk saw, the row the store keeps is the immutable part -- and no more.

    This is the write side of the same boundary. A call made after the close reads today's file --
    that is what the tail is for -- so the frame in hand reaches today; handing *that* to the store
    under the prefix key would file a final-looking row containing a day the store's own rule says can
    never be final. It would still be *accepted*: that rule is about the window a row is filed under,
    not about the dates inside its payload. Every later call would then have to refuse it, which is a
    whole archive walk where a one-day tail was due.

    Mutation this is red on: **hand the store the frame unsliced** (``return frame`` instead of
    ``frame.loc[frame.index <= boundary]``). The stored row then carries today's bar, the next call
    refuses it, and the tail's single request becomes the whole window's.
    """
    today = pd.Timestamp(_boundary(), tz=IST)  # the last session that closed
    boundary = _boundary(today)
    monkeypatch.setattr(ohlcv_mod, "_ist_today", lambda: today)
    start, end = _year_window(today.date().isoformat())
    store = FrameStore(db_path=tmp_path / "frames.db", clock=lambda: today)
    archive.published_through = today.date().isoformat()  # the file exists: the walk read it

    engine = _engine(store=store)
    await _history(engine, start, end)

    kept = store.read(symbol="TCS", exchange="NSE", series=None, start=start, end=boundary)
    assert kept.status == "hit"
    assert _last_bar(kept.value) == boundary, (
        "the stored row stops where the store's rule draws the line, whatever the read reached"
    )

    before = _spent(archive)
    again = await _history(engine, start, end)
    assert _spent(archive) - before == 1, "so the next call pays the tail -- one day -- and no more"
    assert again.attrs[CACHE_COVERAGE_END_ATTR] == today.date().isoformat(), (
        "and the frame it returns is the prefix's bars plus that one day"
    )


async def test_a_caller_mutation_of_the_frame_reaches_nothing(store, store_path, archive):
    """
    Mutating ``iloc[0, 0]`` and appending to a list inside ``attrs`` is the pair the store names as
    the reason it copies on the way out (and the reason ``PriceCache`` copies again). The split adds
    a merge to that path, so this pins that the merge introduces no second alias: the frame handed
    over is not the object the store holds, and the row's payload is written before the caller can
    see the frame at all.

    This one is a pin rather than a mutation-kill: the aliasing it would catch is a *deferred or
    aliased write*, and this implementation has none to remove -- the store pickles inside its own
    call, and the merged frame is a fresh object. It is here so that a future change that keeps the
    frame it stored, or hands the caller the object it loaded, cannot pass unnoticed.
    """
    start, end = _year_window(_today().date().isoformat())
    engine = _engine(store=store)

    await _history(engine, start, end)
    frame = await _history(engine, start, end)
    boundary = _boundary()

    frame.iloc[0, 0] = -999.0
    frame.attrs[CACHE_UNCOVERED_DAYS_ATTR].append("1999-01-01")
    frame.attrs[MISSING_DATES_ATTR].append("1999-01-01")

    again = await _history(engine, start, end)
    assert again.iloc[0, 0] != -999.0, "the merged frame is a fresh object, not a shared one"
    assert "1999-01-01" not in again.attrs[CACHE_UNCOVERED_DAYS_ATTR]
    assert "1999-01-01" not in again.attrs[MISSING_DATES_ATTR]

    kept = store.read(symbol="TCS", exchange="NSE", series=None, start=start, end=boundary).value
    assert kept.iloc[0, 0] != -999.0, "and the stored payload was written before the caller saw it"
    assert "1999-01-01" not in kept.attrs[MISSING_DATES_ATTR]
