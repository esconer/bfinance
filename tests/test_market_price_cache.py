"""
C1 — the cache-aside wrapper: answer one window from the frame store, fetch only when it cannot.

**Why this file exists.** ``bhavcopy_range`` spends one archive request per calendar weekday of the
span it is handed -- 261 candidate days for a 1y window on 2026-10-17 -- and the price path remembered
none of it, which is the plan's recorded 184 s for ``Ticker("TCS").info``, paid again in full on the
next identical call. Everything here is about that second call: it must cost **zero** transport
calls, and the frame it returns must be impossible to mistake for one fetched just now.

**Every test below is red on a named mutation, and the name is in its docstring.** The load-bearing
ones are *executed* in :class:`TestTheChecksBite`, in the style ``tests/test_frame_store.py`` uses for
the same reason: an assertion that has never been seen to fail is an assertion nobody has tested. The
mutations are the plausible ones -- the lookup that is never consulted, the shortfall check deleted
because "if there is a row, use it", the uncovered record dropped so every short frame looks clean,
the frame handed to two callers with a store that memoises what it unpickled, the live path left
unlabelled because "it is the caller's own frame", a ``try/except`` around the fetch that answers a
failure with an empty frame, the publish guard removed, the bypass flag ignored, and the attrs rebuilt
from a clean dict.

Nothing here touches the network, and nothing reads or writes the user's ``~/.bfinance``: every store
is built on ``tmp_path`` with a fixed clock, every fetcher is a stand-in whose whole job is to count
itself, and the one test that lets the wrapper build its own default store points ``$BFINANCE_HOME``
at a temporary directory first.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Optional, Sequence

import pandas as pd
import pytest

from bfinance.cache import frame_store
from bfinance.cache.frame_store import (
    CACHE_COVERAGE_END_ATTR,
    CACHE_FETCHED_ATTR,
    CACHE_REQUESTED_END_ATTR,
    CACHE_STATUS_ATTR,
    DISABLED_STATUS,
    IST,
    FrameStore,
)
from bfinance.cache.sqlite_cache import CacheLookup
from bfinance.market import price_cache
from bfinance.market.price_cache import CACHE_UNCOVERED_DAYS_ATTR, PriceCache
from bfinance.nse.bhavcopy import iter_trading_days

#: The day the fixed clock says it is: a **Friday**, so "a window that contains today" contains a real
#: candidate trading day whose archive does not exist yet. A Saturday would have made that window end
#: on a weekend, where the last day is not a trading day at all and the partial-session hazard the
#: publish rule exists for would not be exercised.
TODAY = "2026-10-16"

#: A closed window: every archive file it needs is published and never rewritten.
WINDOW = ("2026-10-01", "2026-10-15")

#: A window that contains ``TODAY`` -- its last day is the day whose file NSE has not published.
OPEN_WINDOW = ("2026-10-12", TODAY)

#: The last day of ``OPEN_WINDOW`` that really has an archive. A live read of an open window ends
#: here, which is why the frame it returns is short by exactly one candidate trading day -- and why
#: that shortfall has to be on the frame rather than left to be discovered.
OPEN_THROUGH = "2026-10-15"

#: The window the headline is measured over: one year, ending the day before ``TODAY``.
ONE_YEAR = ("2025-10-16", "2026-10-15")

#: A window whose walk stopped a fortnight early, and the day it stopped on. Nothing about the frame
#: itself says it is short, which is the point of the two tests that use it.
SHORT_WINDOW = ("2026-09-01", "2026-09-30")
SHORT_THROUGH = "2026-09-15"

#: A window that *extends* ``SHORT_WINDOW``: the request is a different window, so no row written for
#: the narrow one may answer it.
WIDER_END = "2026-10-15"

#: The provenance the fixtures carry, spelled as the fetch layer spells it (``bhavcopy.PROVENANCE``,
#: ``ohlcv.PRICE_BASIS_AS_TRADED``), so a frame that went through the cache is compared against the
#: real vocabulary rather than against a placeholder invented here.
PROVENANCE = "nse_bhavcopy"
BASIS = "as_traded"

#: Every status a frame can publish, all of them :class:`CacheLookup` names: ``"hit"`` from the store,
#: ``"miss"`` from a live fetch, ``"disabled"`` when the caching layer was not in play. ``"null"`` and
#: ``"expired"`` cannot occur from a pickle-backed store that never keeps a null and never lets a
#: window expire; they are in the set so that the check compares against the whole vocabulary rather
#: than against a subset that would also accept a name invented by this module.
STATUSES = frozenset({"hit", "null", "miss", "expired", "corrupt", "disabled"})

#: The statuses this wrapper can publish.
SERVED_STATUSES = frozenset({"hit", "miss", DISABLED_STATUS})


# --------------------------------------------------------------------------- fixtures and helpers


def clock_at(moment: str = TODAY, *, tz: str = IST):
    """A fixed clock, so "today" is a known day rather than the day the suite happens to run on."""
    return lambda: pd.Timestamp(moment, tz=tz)


def store(tmp_path, *, enabled: bool = True, moment: str = TODAY, name: str = "frames.db"):
    """A frame store on its own file under ``tmp_path``, with the clock pinned to ``moment``."""
    return FrameStore(db_path=tmp_path / name, enabled=enabled, clock=clock_at(moment))


def trading_days(window) -> list:
    """Every candidate trading day of a window, as ISO strings. Computed, never counted by hand."""
    return [day.isoformat() for day in iter_trading_days(window[0], window[1])]


def trading_days_after(day: str, end: str) -> list:
    """Every candidate trading day strictly after ``day``, up to and including ``end``."""
    return [
        item.isoformat()
        for item in iter_trading_days(pd.Timestamp(day) + pd.Timedelta(days=1), end)
    ]


def window_frame(window, *, through: Optional[str] = None) -> pd.DataFrame:
    """A per-symbol window frame shaped the way the fetch layer serves one.

    A naive ascending ``DatetimeIndex`` named ``Date`` over the window's candidate trading days, the
    five bhavcopy columns, and the provenance the price layer publishes: ``bfinance_provenance``,
    ``price_basis``, and the two **container** attrs a pickle has to survive --
    ``bfinance_price_source_attempts`` (a list of dicts) and ``bfinance_mirror`` (a dict). Built fresh
    on every call, containers included, because a module-level list shared between tests would let one
    test's mutation show up in another's frame.

    ``through`` models a walk that stopped early: the frame comes back short and says nothing about it.
    """
    days = [day for day in trading_days(window) if through is None or day <= through]
    index = pd.DatetimeIndex(days, name="Date")
    frame = pd.DataFrame(
        {
            "Open": [100.0] * len(index),
            "High": [101.0] * len(index),
            "Low": [99.0] * len(index),
            "Close": [100.5] * len(index),
            "Volume": [1_000_000] * len(index),
        },
        index=index,
    )
    frame.attrs.update(
        {
            "bfinance_provenance": PROVENANCE,
            "bfinance_requested_start": window[0],
            "bfinance_requested_end": window[1],
            "bfinance_bhavcopy_missing_dates": [],
            "bfinance_price_source_attempts": [
                {
                    "source": "trendlyne-ohlc",
                    "outcome": "skipped_columns",
                    "called": False,
                    "reason": "publishes no Open, High or Low.",
                    "basis": BASIS,
                },
                {
                    "source": "nse-bhavcopy",
                    "outcome": "served",
                    "called": True,
                    "reason": "",
                    "basis": BASIS,
                },
            ],
            "bfinance_mirror": {
                "enabled": False,
                "used": False,
                "source": "network",
                "days_missing": [],
                "days_considered": len(index),
            },
            "price_basis": BASIS,
        }
    )
    return frame


def empty_window_frame(window) -> pd.DataFrame:
    """The frame ``bhavcopy`` answers a 404 with: no rows, a date index, and the missing dates."""
    frame = pd.DataFrame(
        {"Open": [], "High": [], "Low": [], "Close": [], "Volume": []},
        index=pd.DatetimeIndex([], name="Date"),
    )
    frame.attrs["bfinance_bhavcopy_missing_dates"] = trading_days(window)
    return frame


def market_frame() -> pd.DataFrame:
    """A whole-market ``(Date, Symbol)`` cross-section, exactly as ``nse.bhavcopy`` returns one.

    Used once, to pin the one shape this wrapper must refuse rather than key: a cross-section is not
    one symbol's time series, so storing it under one symbol's key would file the whole market as TCS.
    """
    index = pd.MultiIndex.from_arrays(
        [pd.DatetimeIndex(["2026-10-12", "2026-10-13"]), pd.Index(["TCS", "ITC"], name="Symbol")],
        names=["Date", "Symbol"],
    )
    return pd.DataFrame({"Close": [100.5, 200.0]}, index=index)


class _ArchiveWalk:
    """A counting stand-in for ``bhavcopy_range``: **one transport call per candidate trading day**.

    ``calls`` is the headline's evidence. The walk a real request performs is one archive request per
    weekday of the span -- weekends are skipped without a request, an exchange holiday costs a 404 that
    is still a request -- so the count is ``len(iter_trading_days(start, end))``, computed rather than
    written down: a hard-coded 262 would be wrong on the next weekday, which is the drift the plan
    warns about.
    """

    def __init__(self, start: str, end: str, *, through: Optional[str] = None):
        self.start, self.end, self.through = start, end, through
        self.calls = 0

    def __call__(self) -> pd.DataFrame:
        self.calls += len(iter_trading_days(self.start, self.end))
        return window_frame((self.start, self.end), through=self.through)


class _Fetcher:
    """A fetcher that answers a scripted sequence and counts its own calls.

    One call per invocation -- the unit this stands in for is "the fetch", not the request inside it,
    which is what :class:`_ArchiveWalk` counts. An answer that is an exception is raised, because that
    is how a fetch layer reports a failure; a frame is copied, because a real fetcher builds a new one
    per call and a shared object would let one call's in-place stamping appear on another's frame.
    """

    def __init__(self, *answers):
        assert answers, "a fetcher with nothing to answer is a test that cannot say what it asserts"
        self.answers = list(answers)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        answer = self.answers[min(self.calls, len(self.answers)) - 1]
        if isinstance(answer, BaseException):
            raise answer
        return answer.copy() if isinstance(answer, pd.DataFrame) else answer


class _UndatedStore:
    """A store that answers a hit without saying when the row was fetched.

    ``PriceCache(store=...)`` takes any object with a ``read``, so "a served frame always carries
    ``CACHE_FETCHED_ATTR``" is a property of the shipped ``FrameStore`` and not of the seam. This wraps
    the real store and strips exactly that one attr off what it serves -- the smallest change that models
    a store-like object which observed the row but not the instant, which is the case the wrapper's own
    docstring forbids an invented date for.
    """

    def __init__(self, inner):
        self._inner = inner

    def read(self, **kwargs):
        lookup = self._inner.read(**kwargs)
        if lookup.status != "hit":
            return lookup
        frame = lookup.value
        frame.attrs.pop(CACHE_FETCHED_ATTR, None)
        return CacheLookup("hit", frame)

    def write(self, *args, **kwargs):
        return self._inner.write(*args, **kwargs)


def row_count(db_path, table: str = "bfinance_price_frames") -> int:
    """How many rows a table holds. Raw SQL on purpose: "was anything written" must not be answered
    by the code under test, which is the whole point of asserting it."""
    with sqlite3.connect(str(db_path), timeout=5.0) as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def through_the_cache(
    cache,
    fetch,
    *,
    window: Sequence[str],
    symbol: str = "TCS",
    exchange: str = "NSE",
    series: Optional[str] = None,
    observed=None,
) -> pd.DataFrame:
    """One call through the wrapper, spelled once so a control drives the identical call."""
    return cache.get_or_fetch(
        fetch,
        symbol=symbol,
        exchange=exchange,
        series=series,
        start=window[0],
        end=window[1],
        observed_coverage_end=observed,
    )


def read_the_store(store_obj, *, window, symbol="TCS", exchange="NSE", series=None):
    """One read of the store at the key the wrapper uses, for the premise checks."""
    return store_obj.read(
        symbol=symbol, exchange=exchange, series=series, start=window[0], end=window[1]
    )


# ------------------------------------------------- the checks the controls below attack


def assert_the_replay_is_free(cache, fetch, *, window=ONE_YEAR, symbol="TCS"):
    """The headline: the first call pays the whole walk, the identical second call pays nothing.

    Both halves are asserted, because either one alone is vacuous. "The second call cost nothing" is
    also true of a wrapper that never fetched; "the first cost the walk" is also false of one that
    never stores. The bars are compared as well, so a stored frame is not merely present but the one
    that was fetched.
    """
    candidates = trading_days(window)
    assert candidates, f"premise: {window} contains no candidate trading day"

    first = through_the_cache(cache, fetch, window=window, symbol=symbol)
    after_first = fetch.calls
    assert after_first == len(candidates), (
        f"the first call cost {after_first} transport call(s); the walk over {window[0]}..{window[1]} "
        f"is {len(candidates)} candidate trading day(s)"
    )
    assert first.attrs[CACHE_STATUS_ATTR] == "miss", (
        "the first call was a live fetch, so that is the status it has to publish"
    )

    second = through_the_cache(cache, fetch, window=window, symbol=symbol)
    assert fetch.calls == after_first, (
        f"the second identical call went back to the archive for {fetch.calls - after_first} more "
        f"day(s), so the replay was not served from the store"
    )
    assert second.attrs[CACHE_STATUS_ATTR] == "hit", (
        f"the replay publishes {second.attrs.get(CACHE_STATUS_ATTR)!r}, not 'hit'"
    )
    assert list(second["Close"]) == list(first["Close"]), (
        "the replay did not return the bars that were fetched"
    )
    assert len(second) == len(candidates), "the stored frame lost rows on the way through"
    assert second.attrs[CACHE_COVERAGE_END_ATTR] == window[1]
    assert second.attrs[CACHE_UNCOVERED_DAYS_ATTR] == [], (
        "a frame that covers the whole window reports uncovered days"
    )
    return first, second


def assert_a_request_beyond_the_coverage_is_not_served(
    cache, fetch, store_obj, *, window=SHORT_WINDOW, through=SHORT_THROUGH, symbol="TCS"
):
    """A row that stops early with nothing saying so is refetched, never replayed.

    The row is put there by :meth:`FrameStore.write` directly, because that is the shape a row written
    by something other than this wrapper takes -- an older build, a copy, a hand edit -- and it is the
    shape the guard exists for. The last clause is the other direction of the same claim: a *wider*
    request is a different window, so the narrow row cannot answer that either.
    """
    missing = trading_days_after(through, window[1])
    assert missing, f"premise: nothing is missing between {through} and {window[1]}"

    planted = window_frame(window, through=through)
    assert (
        store_obj.write(
            planted, symbol=symbol, exchange="NSE", series=None, start=window[0], end=window[1]
        )
        == "hit"
    ), "premise: the store accepts a short row for a closed window"

    before = fetch.calls
    frame = through_the_cache(cache, fetch, window=window, symbol=symbol)

    assert fetch.calls > before, (
        f"a request beyond the cached coverage was answered from the cache: the stored frame stops at "
        f"{through}, the request ends at {window[1]}, and nothing in the store said so"
    )
    assert frame.attrs[CACHE_STATUS_ATTR] == "miss", (
        f"the frame came from a live fetch, so it publishes 'miss', not "
        f"{frame.attrs.get(CACHE_STATUS_ATTR)!r}"
    )
    assert frame.attrs[CACHE_COVERAGE_END_ATTR] == through, (
        "the frame does not say where its data ends"
    )
    assert frame.attrs[CACHE_REQUESTED_END_ATTR] == window[1], (
        "the frame does not say what it was asked for"
    )
    assert frame.attrs[CACHE_COVERAGE_END_ATTR] != frame.attrs[CACHE_REQUESTED_END_ATTR], (
        "the observed end and the requested end are the same value, so the shortfall is invisible"
    )
    assert frame.attrs[CACHE_UNCOVERED_DAYS_ATTR] == missing, (
        f"the shortfall is silent: {len(missing)} candidate trading day(s) of the window "
        f"({missing[0]}..{missing[-1]}) are absent from the frame and recorded nowhere"
    )

    wider = _ArchiveWalk(window[0], WIDER_END)
    through_the_cache(cache, wider, window=(window[0], WIDER_END), symbol=symbol)
    assert wider.calls == len(trading_days((window[0], WIDER_END))), (
        "a wider window was served from the narrower row: the key names both resolved ends of the "
        "window, so an extended request is a different window and has to be fetched"
    )


def assert_the_recorded_shortfall_is_replayed_with_its_record(
    cache, fetch, *, window=SHORT_WINDOW, through=SHORT_THROUGH, symbol="TCS"
):
    """A short window whose shortfall *is* recorded is served, and comes back still saying so.

    Refusing every frame that stops early is the other extreme, and it costs a full walk on every call
    for every symbol with a suspension, every window that ends on an exchange holiday and every scrip
    that listed mid-window. What is never allowed is the third option: a short frame replayed with
    nothing on it that says it is short.
    """
    missing = trading_days_after(through, window[1])

    first = through_the_cache(cache, fetch, window=window, symbol=symbol)
    assert first.attrs[CACHE_UNCOVERED_DAYS_ATTR] == missing, (
        f"the live frame does not record the shortfall ({first.attrs.get(CACHE_UNCOVERED_DAYS_ATTR)!r} "
        f"against {missing!r}), so the next read has no way to tell an explained short frame from a "
        f"silent one"
    )

    before = fetch.calls
    second = through_the_cache(cache, fetch, window=window, symbol=symbol)
    assert fetch.calls == before, (
        "a window whose shortfall is recorded on the frame was refetched anyway: the guard would then "
        "cost a full walk on every call for every symbol with a real gap"
    )
    assert second.attrs[CACHE_STATUS_ATTR] == "hit"
    assert second.attrs[CACHE_UNCOVERED_DAYS_ATTR] == missing, (
        "a short frame was replayed with no record of the shortfall, which makes it indistinguishable "
        "from a complete one"
    )
    assert second.attrs[CACHE_COVERAGE_END_ATTR] == through
    assert second.attrs[CACHE_REQUESTED_END_ATTR] == window[1]


def assert_the_served_frames_are_isolated(cache, fetch, *, window=WINDOW, symbol="TCS"):
    """Two callers never hold one frame -- the bars, and the containers in its attrs, both directions.

    The second call is the replay, which is the path that hands a stored frame out. Everything a shared
    frame leaks is mutated: the numeric block, the list inside ``bfinance_price_source_attempts``, the
    dict inside ``bfinance_mirror``, and this module's own uncovered-days list. The third call must be
    untouched by all of it.
    """
    first = through_the_cache(cache, fetch, window=window, symbol=symbol)
    second = through_the_cache(cache, fetch, window=window, symbol=symbol)
    assert first is not second, "the two calls returned the same object"
    assert second.attrs[CACHE_STATUS_ATTR] == "hit", "premise: the second call was a replay"

    open_before = second["Open"].iloc[0]
    second.iloc[0, 0] = -999.0
    second.attrs["bfinance_price_source_attempts"].append({"source": "MUTANT", "called": False})
    second.attrs["bfinance_mirror"]["days_missing"].append("1999-01-01")
    second.attrs[CACHE_UNCOVERED_DAYS_ATTR].append("1999-01-01")
    assert second.iloc[0, 0] == -999.0, "premise: the edit landed on `second`"

    third = through_the_cache(cache, fetch, window=window, symbol=symbol)
    assert third is not second, "the next read returned the object the previous caller mutated"
    assert third.iloc[0, 0] == open_before, "the previous caller's edit reached the stored bars"
    assert len(third.attrs["bfinance_price_source_attempts"]) == 2, (
        "the previous caller's append reached the stored attrs list"
    )
    assert third.attrs["bfinance_mirror"]["days_missing"] == [], (
        "the previous caller's append reached the stored attrs dict"
    )
    assert "1999-01-01" not in third.attrs[CACHE_UNCOVERED_DAYS_ATTR], (
        "the previous caller's append reached this module's own published list"
    )
    assert first.iloc[0, 0] == open_before, "the live frame was mutated by a later caller"


def assert_every_returned_frame_is_labelled(cache, fetch, *, window=WINDOW, symbol="TCS"):
    """The three statuses, and no path that returns a frame with none at all.

    "A cached frame must never be indistinguishable from a live one" cuts both ways: a live frame
    claiming to be a replay is the same defect seen from the other side, and a frame with no status at
    all leaves the caller inferring liveness from whichever attrs happen to be missing.
    """
    cases = (
        ("a live fetch", through_the_cache(cache, fetch, window=window, symbol=symbol), "miss"),
        ("a replay", through_the_cache(cache, fetch, window=window, symbol=symbol), "hit"),
        (
            "a bypassed cache",
            through_the_cache(
                PriceCache(store=cache.store, enabled=False), fetch, window=window, symbol=symbol
            ),
            DISABLED_STATUS,
        ),
    )
    for where, frame, expected in cases:
        status = frame.attrs.get(CACHE_STATUS_ATTR)
        assert status == expected, (
            f"{where}: the frame publishes {status!r}, not {expected!r}, so a caller cannot tell a "
            f"cached frame from a live one"
        )
        assert status in STATUSES and CacheLookup(status).status == status, (
            f"{where}: {status!r} is not a name from the cache vocabulary"
        )
        for key in (
            CACHE_FETCHED_ATTR,
            CACHE_COVERAGE_END_ATTR,
            CACHE_REQUESTED_END_ATTR,
            CACHE_UNCOVERED_DAYS_ATTR,
        ):
            assert key in frame.attrs, (
                f"{where}: {key} is missing, so this frame cannot be dated or sized"
            )
        stamp = pd.Timestamp(frame.attrs[CACHE_FETCHED_ATTR])
        assert stamp.utcoffset() == pd.Timedelta(hours=5, minutes=30), (
            f"{where}: the fetch time is not the IST instant it claims to be"
        )


def assert_the_answer_is_refused_and_nothing_is_cached(cache, store_obj, answer, *, window=WINDOW):
    """A null, an HTTP status or a response body is refused, and the *next* call still has to fetch.

    The second half is the one that matters: a cached failure is not a wasted row, it is a permanent
    answer -- a frame saying "this symbol has nothing" for a window nobody ever fetched, for the life
    of the file.
    """
    assert row_count(store_obj.db_path) == 0, "premise: the store is empty"

    fetch = _Fetcher(answer)
    with pytest.raises(TypeError, match="not a DataFrame"):
        through_the_cache(cache, fetch, window=window)
    assert fetch.calls == 1, "the fetcher was asked more than once"

    assert row_count(store_obj.db_path) == 0, (
        f"the store holds {row_count(store_obj.db_path)} row(s) after an answer with no frame"
    )
    assert read_the_store(store_obj, window=window).status == "miss"

    good = _ArchiveWalk(*window)
    frame = through_the_cache(cache, good, window=window)
    assert good.calls == len(trading_days(window)), (
        "the failed fetch was cached: the next call was answered without fetching anything"
    )
    assert frame.attrs[CACHE_STATUS_ATTR] == "miss"


def assert_an_empty_answer_is_returned_but_never_cached(cache, *, window=WINDOW):
    """The 404 shape: an empty frame is returned with its record, and never latched.

    An empty frame is how ``bhavcopy`` speaks a 404, and a cached one would make a transient CDN blip
    look like an exchange holiday for the life of the row. Both calls below must therefore cost a
    fetch, and the frame that comes back must say it covered *nothing*: publishing the requested end as
    its coverage would be the exact confusion this module exists to prevent.
    """
    fetch = _Fetcher(empty_window_frame(window))

    for attempt in (1, 2):
        frame = through_the_cache(cache, fetch, window=window)
        assert fetch.calls == attempt, (
            "the empty answer was cached: the second call did not have to fetch"
        )
        assert frame.attrs[CACHE_STATUS_ATTR] == "miss", (
            f"the frame came from a live fetch, so it publishes 'miss', not "
            f"{frame.attrs.get(CACHE_STATUS_ATTR)!r}"
        )
        assert frame.attrs[CACHE_COVERAGE_END_ATTR] is None, (
            f"an empty frame has covered nothing, and it publishes "
            f"{frame.attrs.get(CACHE_COVERAGE_END_ATTR)!r} as its coverage end"
        )
        assert frame.attrs[CACHE_UNCOVERED_DAYS_ATTR] == trading_days(window), (
            "the whole window is uncovered and the frame does not say so"
        )
        assert frame.attrs["bfinance_bhavcopy_missing_dates"] == trading_days(window), (
            "the fetch layer's own record of the missing days was dropped"
        )

    assert row_count(cache.store.db_path) == 0, "an empty answer was written to the store"


def assert_a_raise_is_not_swallowed(cache, store_obj, *, window=WINDOW):
    """A fetch that fails reaches the caller, and nothing is cached for it."""
    assert row_count(store_obj.db_path) == 0, "premise: the store is empty"

    fetch = _Fetcher(RuntimeError("NSE bhavcopy unavailable for 2026-10-01..2026-10-15"))
    try:
        through_the_cache(cache, fetch, window=window)
    except RuntimeError:
        pass
    else:
        raise AssertionError(
            "a fetch that failed was answered with a frame: the failure has to reach the caller's "
            "error handling, and nothing may be cached for it"
        )

    assert row_count(store_obj.db_path) == 0, "a failed fetch was written to the store"
    assert read_the_store(store_obj, window=window).status == "miss"


def assert_a_whole_market_frame_is_refused(cache, *, window=WINDOW):
    """A ``(Date, Symbol)`` cross-section is refused rather than keyed under one symbol."""
    assert row_count(cache.store.db_path) == 0, "premise: the store is empty"

    with pytest.raises(TypeError, match="DatetimeIndex"):
        through_the_cache(cache, _Fetcher(market_frame()), window=window)

    assert row_count(cache.store.db_path) == 0, (
        "a whole-market cross-section was stored under one symbol's key"
    )


def assert_a_window_containing_today_is_never_served(
    cache, fetch, store_obj, *, window=OPEN_WINDOW, through=OPEN_THROUGH
):
    """Today's window is fetched every time, and no row for it is ever written.

    Today's archive does not exist yet, so a stored row for a window containing today could only ever
    hold a partial session -- served tomorrow as if it were final. The rule is the store's
    (``FrameStore._may_serve``); what this asserts is that the wrapper cannot get past it: the frame is
    fetched, labelled, offered and refused, twice.
    """
    assert window[1] == TODAY, "premise: the window ends on the store's today"
    candidates = trading_days(window)
    missing = trading_days_after(through, window[1])

    first = through_the_cache(cache, fetch, window=window)
    assert fetch.calls == len(candidates), (
        f"the open window cost {fetch.calls} transport call(s); its walk is {len(candidates)}"
    )
    assert first.attrs[CACHE_STATUS_ATTR] == "miss"
    assert first.attrs[CACHE_UNCOVERED_DAYS_ATTR] == missing, (
        f"the frame ends at {through} and the window ends at {window[1]}, and the frame does not say so"
    )

    second = through_the_cache(cache, fetch, window=window)
    assert fetch.calls == len(candidates) * 2, (
        "a window containing today was served from the cache: today's archive does not exist yet, so "
        "the row could only be a partial session presented as a final one"
    )
    assert second.attrs[CACHE_STATUS_ATTR] == "miss"
    assert row_count(store_obj.db_path) == 0, "a row was written for a window containing today"


def assert_the_cache_is_bypassed(cache, fetch, *, window=WINDOW):
    """The negative control: with the caching layer off, every call is a live fetch that says so.

    Two identical calls, and the second must cost the fetch again. If it does not, the bypass was
    reading the store anyway -- which is the one thing a caller who has been told to take the live path
    cannot check for themselves.
    """
    frames = [through_the_cache(cache, fetch, window=window) for _ in (1, 2)]

    assert fetch.calls == 2, (
        "a disabled wrapper answered from a cache: with the caching layer switched off, every call is "
        "a live fetch"
    )
    for frame in frames:
        assert frame.attrs.get(CACHE_STATUS_ATTR) == DISABLED_STATUS, (
            f"a bypassed frame publishes {frame.attrs.get(CACHE_STATUS_ATTR)!r}, not "
            f"{DISABLED_STATUS!r}: 'no cache decision was made' is a different fact from 'the lookup "
            f"ran and found nothing'"
        )
        assert frame.attrs[CACHE_COVERAGE_END_ATTR] == window[1]
        assert frame.attrs[CACHE_REQUESTED_END_ATTR] == window[1]
        assert frame.attrs[CACHE_UNCOVERED_DAYS_ATTR] == []


def assert_provenance_survives_the_cache(cache, fetch, *, window=WINDOW):
    """A frame that went through the cache still carries its provenance and its price basis.

    The attrs the price layer publishes, checked by value and by container type on **both** paths: the
    live frame, stamped and then offered to the store, and the replay, pickled and read back. A frame
    whose basis cannot be read off it is a frame whose numbers cannot be used: as-traded and
    share-count-adjusted series look identical in the bars.
    """
    live = through_the_cache(cache, fetch, window=window)
    cached = through_the_cache(cache, fetch, window=window)
    assert cached.attrs[CACHE_STATUS_ATTR] == "hit", (
        "premise: the second call was served from the store"
    )

    for where, frame in (("the live frame", live), ("the replayed frame", cached)):
        assert frame.attrs.get("bfinance_provenance") == PROVENANCE, (
            f"{where}: provenance is {frame.attrs.get('bfinance_provenance')!r}, not {PROVENANCE!r}"
        )
        assert frame.attrs.get("price_basis") == BASIS, (
            f"{where}: price_basis is {frame.attrs.get('price_basis')!r}, not {BASIS!r}, and the basis "
            f"is the only thing that says which series these numbers are"
        )
        attempts = frame.attrs.get("bfinance_price_source_attempts")
        assert isinstance(attempts, list) and len(attempts) == 2, (
            f"{where}: the source-attempt ledger came back as {attempts!r}"
        )
        assert attempts[-1]["source"] == "nse-bhavcopy" and attempts[-1]["called"] is True
        mirror = frame.attrs.get("bfinance_mirror")
        assert isinstance(mirror, dict) and mirror["source"] == "network", (
            f"{where}: the mirror record came back as {mirror!r}"
        )
        assert frame.attrs.get("bfinance_requested_start") == window[0], (
            f"{where}: the fetch layer's own attrs were dropped on the way through"
        )


def frame_or_empty(fetch):
    """The lenient answer the swallow-the-failure mutation stands in for: a failure becomes a frame."""
    try:
        return fetch()
    except Exception:  # noqa: BLE001 - this is the mutation, not a policy
        return pd.DataFrame(
            {"Open": [], "High": [], "Low": [], "Close": [], "Volume": []},
            index=pd.DatetimeIndex([], name="Date"),
        )


def served_frame_with_the_now_fallback(frame, *, start, end):
    """The pre-fix ``_served_frame``, kept as an executable mutation.

    It is that function with the one expression this task deleted -- ``or _now_iso()`` -- put back: a
    replay whose frame carries no fetch time is dated "now". Kept rather than described, in the style
    ``test_frame_store.py`` uses for its own mutations, because an assertion that has never been seen to
    fail is an assertion nobody has tested.
    """
    served = price_cache._isolated(frame)
    coverage_end = served.attrs.get(CACHE_COVERAGE_END_ATTR)
    return price_cache._publish(
        served,
        "hit",
        fetched_at=served.attrs.get(CACHE_FETCHED_ATTR) or price_cache._now_iso(),
        coverage_end=coverage_end,
        requested_end=end,
        uncovered=price_cache._uncovered_days(
            start=start, coverage_end=coverage_end, requested_end=end
        ),
    )


def replay_from_an_undated_store(tmp_path, *, name: str = "frames.db") -> pd.DataFrame:
    """A replay served by a store that does not date its rows, built the same way in both halves of the
    control that mutates the fallback.

    Two calls through the wrapper: the first fetches and stores (that is how a row gets behind the key),
    the second is the replay. The premises are asserted rather than assumed -- without them, a test whose
    second call had gone live would be asserting about a frame the fetcher just built.
    """
    store_obj = store(tmp_path, name=name)
    cache = PriceCache(store=_UndatedStore(store_obj))
    fetch = _Fetcher(window_frame(WINDOW))

    first = through_the_cache(cache, fetch, window=WINDOW)
    replay = through_the_cache(cache, fetch, window=WINDOW)

    assert first.attrs[CACHE_STATUS_ATTR] == "miss" and fetch.calls == 1, (
        "premise: the first call fetched exactly once and stored what it fetched"
    )
    assert replay.attrs[CACHE_STATUS_ATTR] == "hit", (
        "premise: the second call was answered from the store"
    )
    return replay


def assert_the_replay_is_undated(replay: pd.DataFrame) -> None:
    """The whole of "a store that did not say when it fetched produces a frame that says so too".

    The key is published and its value is ``None``: this module's convention is one key a consumer reads
    without branching, and ``None`` is the value that means "not observed" -- the same distinction the
    wrapper draws between ``"disabled"`` and ``"miss"``, from the side of a fact nobody recorded.
    """
    assert CACHE_FETCHED_ATTR in replay.attrs, (
        "the fetch-time key is missing altogether, so a consumer has to branch on its presence instead "
        "of reading it"
    )
    assert replay.attrs[CACHE_FETCHED_ATTR] is None, (
        f"the store never said when the row was fetched and the replay is dated "
        f"{replay.attrs[CACHE_FETCHED_ATTR]!r}: a replay stamped 'now' claims the data was just fetched, "
        f"which is a claim about a fetch that did not happen on this call"
    )


def plant_a_row_of_other_dates(store_obj, *, window=WINDOW, symbol="TCS") -> pd.DataFrame:
    """A row of 2027 dates filed under the 2026 window's key, written through the store's own writer.

    Nothing exotic is needed to make it exist: ``write`` stores the frame it was handed for the window it
    was handed, and never looks at the bars, so this is what the writer does with a frame that arrived
    from the wrong place. Returned, so a caller can name the dates it holds in a failure message.
    """
    wrong = window_frame(window)
    wrong.index = pd.DatetimeIndex(pd.bdate_range("2027-01-04", periods=len(wrong), name="Date"))
    assert (
        store_obj.write(
            wrong, symbol=symbol, exchange="NSE", series=None, start=window[0], end=window[1]
        )
        == "hit"
    ), "premise: the writer cannot tell a frame that does not answer its key"
    return wrong


def assert_a_row_of_other_dates_is_not_replayed(cache, fetch, store_obj, *, window=WINDOW) -> None:
    """The store's read check seen from the consumer's side: a mis-filed row costs a live fetch.

    Two things are asserted and both are the consumer's: the fetch happened, and the frame the caller
    gets is the window's own series rather than the row's dates. What the store answers for that row is
    its own file's assertion and is checked by the test that plants the row, not here -- this helper is
    driven by that test and by the control that deletes the store's check, and a premise that the
    mutation itself breaks would fail the control before its claim could be measured.
    """
    frame = through_the_cache(cache, fetch, window=window)

    assert fetch.calls == 1, (
        f"a row filed under {window[0]}..{window[1]} with bars dated 2027 was replayed: the caller "
        f"asked the window's question and got another window's answer"
    )
    assert frame.attrs[CACHE_STATUS_ATTR] == "miss", (
        f"the frame came from a live fetch, so it publishes 'miss', not "
        f"{frame.attrs.get(CACHE_STATUS_ATTR)!r}"
    )
    assert [str(day)[:10] for day in frame.index] == trading_days(window), (
        "the frame handed to the caller is not the live window's own series"
    )


# ===================================================================== 1: the headline number


class TestTheHeadlineReplay:
    """Red on: **the store's lookup never being consulted** -- a wrapper that fetches first and stores
    afterwards, or one comparing the lookup's status to a name the store does not answer with. The
    second identical call then costs the whole walk again, which is the 184 s this task exists to
    remove. Executed in
    :meth:`TestTheChecksBite.test_a_lookup_that_never_hits_would_walk_the_archive_again`.
    """

    def test_a_second_identical_call_makes_zero_transport_calls(self, tmp_path):
        cache = PriceCache(store=store(tmp_path))
        fetch = _ArchiveWalk(*ONE_YEAR)
        walked = len(trading_days(ONE_YEAR))

        first, second = assert_the_replay_is_free(cache, fetch, window=ONE_YEAR)

        # The number, printed as well as asserted: it is the deliverable's headline.
        print(
            f"headline: {ONE_YEAR[0]}..{ONE_YEAR[1]} is {walked} candidate trading day(s); first call "
            f"{walked} transport call(s), second call {fetch.calls - walked}"
        )
        assert pd.Timestamp(second.attrs[CACHE_FETCHED_ATTR]) <= pd.Timestamp.now(tz=IST), (
            "the replay's fetch time is in the future"
        )
        assert first.attrs[CACHE_STATUS_ATTR] != second.attrs[CACHE_STATUS_ATTR], (
            "a replay and a live fetch publish the same status, so nothing says which one this is"
        )


# ============================================================== 2: never quietly short, either way


class TestTheWindowIsNeverQuietlyShort:
    """Red on: **the shortfall check deleted** (``_unexplained_days`` answering ``[]`` -- "if there is
    a row, use it") and on **the uncovered record dropped** (``_uncovered_days`` answering ``[]``,
    which makes every short frame look like a clean one). Executed in
    :meth:`TestTheChecksBite.test_the_serve_rule_deleted_would_answer_a_short_request_from_the_cache`
    and :meth:`TestTheChecksBite.test_the_gap_record_deleted_would_replay_a_clean_short_frame`.

    The frame used here stops a fortnight before the window ends and says nothing about it, which is
    the worst available failure mode for this work: at every point a caller could look, it is
    indistinguishable from a complete one.
    """

    def test_a_request_beyond_the_coverage_is_not_served_and_the_gap_is_recorded(
        self, tmp_path, caplog
    ):
        cache = PriceCache(store=store(tmp_path))
        fetch = _ArchiveWalk(*SHORT_WINDOW, through=SHORT_THROUGH)

        with caplog.at_level(logging.WARNING, logger="bfinance"):
            assert_a_request_beyond_the_coverage_is_not_served(
                cache, fetch, cache.store, window=SHORT_WINDOW, through=SHORT_THROUGH
            )

        assert any("not serving" in message and "TCS" in message for message in caplog.messages), (
            "the refusal is silent in the log as well as on the frame"
        )

    def test_a_recorded_shortfall_is_replayed_with_its_record(self, tmp_path):
        cache = PriceCache(store=store(tmp_path))
        fetch = _ArchiveWalk(*SHORT_WINDOW, through=SHORT_THROUGH)

        assert_the_recorded_shortfall_is_replayed_with_its_record(
            cache, fetch, window=SHORT_WINDOW, through=SHORT_THROUGH
        )


# ======================================================================= 3: one caller, one object


class TestIsolation:
    """Red on: **the replay handing out the object the store returned**, with a store that memoises
    what it unpickled -- the "unpickling is expensive, hold on to the loaded frame" mutation, which is
    exactly the optimisation a process-level memo invites. Executed in
    :meth:`TestTheChecksBite.test_handing_out_the_stored_object_would_let_one_caller_corrupt_the_next`.
    """

    def test_mutating_a_served_frame_does_not_reach_the_stored_one(self, tmp_path):
        cache = PriceCache(store=store(tmp_path))

        assert_the_served_frames_are_isolated(cache, _Fetcher(window_frame(WINDOW)), window=WINDOW)


# ============================================================== 4: every frame says what it is


class TestTheStatusIsAlwaysTheRealOne:
    """Red on: **stamping only the frames that came out of the store** -- "the live frame is the
    caller's own frame, do not touch it" -- which leaves the live path with no status, no coverage and
    no date at all. Executed in
    :meth:`TestTheChecksBite.test_stamping_only_replays_would_leave_live_frames_unlabelled`.
    """

    def test_a_hit_a_live_fetch_and_a_bypass_each_say_what_they_are(self, tmp_path):
        cache = PriceCache(store=store(tmp_path))

        assert_every_returned_frame_is_labelled(
            cache, _Fetcher(window_frame(WINDOW)), window=WINDOW
        )

    def test_the_statuses_this_wrapper_publishes_are_names_from_the_vocabulary(self):
        assert SERVED_STATUSES <= STATUSES, (
            f"{sorted(SERVED_STATUSES - STATUSES)} is invented here rather than taken from CacheLookup"
        )


# ================================================================== 5: a failure is never data


class TestFailuresAreNeverCached:
    """Red on: **a ``try/except`` around the fetch that answers a failure with an empty frame** ("so
    the caller always gets a frame") and on **the fetched answer being offered to the store
    unchecked**. Executed in
    :meth:`TestTheChecksBite.test_swallowing_a_failed_fetch_would_turn_it_into_data`.

    Every shape here is one a failed fetch really takes, and each ends the same way: nothing is
    written, and the next call still has to fetch.
    """

    @pytest.mark.parametrize(
        "answer",
        [None, 404, b"<html>Not Found</html>"],
        ids=["null", "http-404", "response-body"],
    )
    def test_a_null_or_an_http_status_is_refused_rather_than_cached(self, tmp_path, answer):
        store_obj = store(tmp_path)

        assert_the_answer_is_refused_and_nothing_is_cached(
            PriceCache(store=store_obj), store_obj, answer
        )

    def test_an_empty_frame_is_returned_with_its_record_and_never_cached(self, tmp_path):
        assert_an_empty_answer_is_returned_but_never_cached(PriceCache(store=store(tmp_path)))

    def test_a_raise_is_not_swallowed_and_nothing_is_cached(self, tmp_path):
        store_obj = store(tmp_path)

        assert_a_raise_is_not_swallowed(PriceCache(store=store_obj), store_obj)

    def test_a_whole_market_cross_section_is_refused_rather_than_keyed(self, tmp_path):
        assert_a_whole_market_frame_is_refused(PriceCache(store=store(tmp_path)))


# ======================================================== 6: today is never a cached session


class TestAWindowContainingToday:
    """Red on: **the store's publish guard** (``FrameStore._may_serve``) removed -- the rule belongs to
    the store and is not re-implemented here, so the mutation that turns this red is the guard's
    deletion. Executed in
    :meth:`TestTheChecksBite.test_deleting_the_publish_guard_would_let_todays_partial_session_through`.

    A stored row for a window that contains today could only ever hold a partial session, and tomorrow
    it would look like a complete one. What this file asserts is that the wrapper cannot get around it:
    the row is offered twice and written never, and every call fetches.
    """

    def test_todays_window_is_fetched_every_time_and_never_stored(self, tmp_path):
        store_obj = store(tmp_path)
        cache = PriceCache(store=store_obj)
        fetch = _ArchiveWalk(*OPEN_WINDOW, through=OPEN_THROUGH)

        assert_a_window_containing_today_is_never_served(cache, fetch, store_obj)


# ====================================================================== 7: the negative control


class TestTheBypass:
    """Red on: **the ``enabled`` flag no longer consulted** -- the wrapper reading and writing the
    store anyway, which is the one thing a caller who has been told to take the live path cannot detect
    for themselves. Executed in
    :meth:`TestTheChecksBite.test_ignoring_the_flag_would_serve_a_bypassed_caller_a_stale_frame`.

    Both routes to a live path are covered: the wrapper switched off, and the store switched off
    underneath an enabled wrapper.
    """

    def test_with_the_wrapper_disabled_every_call_is_a_live_fetch_that_says_so(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("BFINANCE_HOME", str(tmp_path / "home"))

        assert_the_cache_is_bypassed(PriceCache(enabled=False), _Fetcher(window_frame(WINDOW)))

        assert not (tmp_path / "home" / "frames.db").exists(), (
            "a wrapper that is switched off built itself a store: the default file is created on first "
            "use, and a bypassed cache has no use for one"
        )

    def test_a_disabled_store_under_an_enabled_wrapper_is_a_live_path_too(self, tmp_path):
        store_obj = store(tmp_path, enabled=False)

        assert_the_cache_is_bypassed(PriceCache(store=store_obj), _Fetcher(window_frame(WINDOW)))

        assert not (tmp_path / "frames.db").exists(), "a switched-off store left a file behind"


# ======================================================================== 8: attrs integrity


class TestProvenanceSurvivesTheCache:
    """Red on: **the replay's attrs rebuilt from the cache keys alone** ("start the frame's attrs from
    a clean dict") -- the mutation that turns a labelled frame into an unlabelled one while every bar
    is still correct. Executed in
    :meth:`TestTheChecksBite.test_rebuilding_the_attrs_would_erase_the_provenance`.

    This is the whole reason the storage layer is a pickle store rather than ``SQLiteCache``: measured,
    ``pd.read_json(df.to_json()).attrs == {}``.
    """

    def test_a_frame_that_went_through_the_cache_keeps_its_provenance_and_its_basis(self, tmp_path):
        cache = PriceCache(store=store(tmp_path))

        assert_provenance_survives_the_cache(cache, _Fetcher(window_frame(WINDOW)), window=WINDOW)

    def test_the_coverage_a_caller_states_is_the_coverage_that_is_published(self, tmp_path):
        """A fetch layer that knows the archive's watermark better than the frame's index says so."""
        cache = PriceCache(store=store(tmp_path))
        fetch = _Fetcher(window_frame(WINDOW, through=WINDOW[0]))

        frame = through_the_cache(cache, fetch, window=WINDOW, observed=WINDOW[1])

        assert frame.attrs[CACHE_COVERAGE_END_ATTR] == WINDOW[1], (
            "the caller's observed coverage end was ignored"
        )
        assert frame.attrs[CACHE_UNCOVERED_DAYS_ATTR] == [], (
            "a frame whose coverage the caller certified as complete reports uncovered days"
        )
        assert (
            read_the_store(cache.store, window=WINDOW).value.attrs[CACHE_COVERAGE_END_ATTR]
            == WINDOW[1]
        ), "the row was stored with a different coverage end from the one published on the frame"


# ============================================ a stored row that answers a different question


class TestARowThatDoesNotAnswerItsKey:
    """Red on: **the store's window check deleted** -- a row of other dates served as an ordinary
    replay and passed straight to the caller.

    The store's own file asserts the status it answers for such a row; what this asserts is the half a
    consumer owes its caller: the frame it gets is the window it asked for, or a live fetch. The row is
    written through the store's own writer, because that is the honest shape of the hazard -- ``write``
    stores any frame for any window it is handed -- so nothing exotic is required for one to exist.
    """

    def test_a_row_of_other_dates_is_not_replayed(self, tmp_path, caplog):
        store_obj = store(tmp_path)
        wrong = plant_a_row_of_other_dates(store_obj)
        assert read_the_store(store_obj, window=WINDOW).status == "corrupt", (
            f"premise: the read refuses the row holding "
            f"{str(wrong.index.min())[:10]}..{str(wrong.index.max())[:10]}"
        )
        fetch = _Fetcher(window_frame(WINDOW))

        with caplog.at_level(logging.WARNING, logger="bfinance"):
            assert_a_row_of_other_dates_is_not_replayed(
                PriceCache(store=store_obj), fetch, store_obj
            )

        assert any("do not lie inside the window" in message for message in caplog.messages), (
            "the refusal is silent in the log as well as on the frame"
        )


# =================================================== a replay is never dated "now"


class TestAReplayIsNeverDatedNow:
    """Red on: **the ``or _now_iso()`` fallback in ``_served_frame`` restored** -- a store-like object
    that answers a hit without a fetch time gets its frame stamped "now".

    Unreachable with the shipped :class:`FrameStore`, which publishes the instant it read off the row,
    but ``PriceCache(store=...)`` is a public injection point, and the module's own liveness rule says
    what a fetched-at value is: an observation. "A replay that stamped 'now' would claim the data was
    just fetched" -- and a frame that claims a fetch which did not happen on this call, while holding a
    stored row's bars, is the one lie this module exists to prevent. So a store that did not say
    produces a frame that does not say either. Executed in
    :meth:`TestTheChecksBite.test_restoring_the_now_fallback_would_date_a_replay_today`.
    """

    def test_a_store_that_does_not_date_its_rows_produces_an_undated_replay(self, tmp_path):
        assert_the_replay_is_undated(replay_from_an_undated_store(tmp_path))


# ================================================================ the controls: the checks bite


class TestTheChecksBite:
    """The named mutations, executed.

    Each control patches the seam the mutation would change, drives the *same helper* the test above
    drives, and requires that helper to fail -- so "this test would catch it" is a measurement rather
    than a claim. The seams are module-level functions on purpose: a mutation that cannot be pointed at
    is a mutation nobody has tried.
    """

    def test_a_lookup_that_never_hits_would_walk_the_archive_again(self, tmp_path, monkeypatch):
        """Mutation: the store's ``read`` answering ``"miss"`` for every key."""
        store_obj = store(tmp_path)
        monkeypatch.setattr(store_obj, "read", lambda **kwargs: CacheLookup("miss"))
        cache = PriceCache(store=store_obj)

        with pytest.raises(AssertionError, match="went back to the archive"):
            assert_the_replay_is_free(cache, _ArchiveWalk(*ONE_YEAR), window=ONE_YEAR)

    def test_the_serve_rule_deleted_would_answer_a_short_request_from_the_cache(
        self, tmp_path, monkeypatch
    ):
        """Mutation: ``_unexplained_days`` reduced to ``[]`` -- "if there is a row, use it"."""
        monkeypatch.setattr(price_cache, "_unexplained_days", lambda *args, **kwargs: [])
        cache = PriceCache(store=store(tmp_path))

        with pytest.raises(AssertionError, match="answered from the cache"):
            assert_a_request_beyond_the_coverage_is_not_served(
                cache,
                _ArchiveWalk(*SHORT_WINDOW, through=SHORT_THROUGH),
                cache.store,
                window=SHORT_WINDOW,
                through=SHORT_THROUGH,
            )

    def test_the_gap_record_deleted_would_replay_a_clean_short_frame(self, tmp_path, monkeypatch):
        """Mutation: ``_uncovered_days`` reduced to ``[]`` -- a short frame with nothing on it.

        Both halves of the claim go with it: the live frame stops recording the shortfall, and the
        guard that used that record stops objecting to the short row.
        """
        monkeypatch.setattr(price_cache, "_uncovered_days", lambda *args, **kwargs: [])
        cache = PriceCache(store=store(tmp_path))

        with pytest.raises(AssertionError, match="does not record the shortfall"):
            assert_the_recorded_shortfall_is_replayed_with_its_record(
                cache,
                _ArchiveWalk(*SHORT_WINDOW, through=SHORT_THROUGH),
                window=SHORT_WINDOW,
                through=SHORT_THROUGH,
            )

    def test_handing_out_the_stored_object_would_let_one_caller_corrupt_the_next(
        self, tmp_path, monkeypatch
    ):
        """Mutation: ``_isolated`` returning its argument, with a store that memoises what it loaded."""
        load = frame_store._frame_from_blob  # the real loader, captured before the patch
        loaded = {}

        def memoised(blob):
            if "frame" not in loaded:
                loaded["frame"] = load(blob)
            return loaded["frame"]

        monkeypatch.setattr(frame_store, "_frame_from_blob", memoised)
        monkeypatch.setattr(frame_store, "_isolated", lambda frame: frame)
        monkeypatch.setattr(price_cache, "_isolated", lambda frame: frame)
        cache = PriceCache(store=store(tmp_path))

        # Either identity check catches it: the object handed to the second caller is already the one
        # the store loaded, and the third call is handed the very object the second caller edited.
        with pytest.raises(
            AssertionError, match="returned the same object|previous caller mutated"
        ):
            assert_the_served_frames_are_isolated(cache, _Fetcher(window_frame(WINDOW)))

    def test_stamping_only_replays_would_leave_live_frames_unlabelled(self, tmp_path, monkeypatch):
        """Mutation: ``_publish`` stamping only the frames that came out of the store."""
        stamp = price_cache._publish

        def only_replays(frame, status, **kwargs):
            return stamp(frame, status, **kwargs) if status == "hit" else frame

        monkeypatch.setattr(price_cache, "_publish", only_replays)
        cache = PriceCache(store=store(tmp_path))

        with pytest.raises(AssertionError, match="a live fetch: the frame publishes"):
            assert_every_returned_frame_is_labelled(cache, _Fetcher(window_frame(WINDOW)))

    def test_swallowing_a_failed_fetch_would_turn_it_into_data(self, tmp_path, monkeypatch):
        """Mutation: ``_fetched`` answering a raise with an empty frame."""
        monkeypatch.setattr(price_cache, "_fetched", frame_or_empty)
        cache = PriceCache(store=store(tmp_path))

        with pytest.raises(AssertionError, match="answered with a frame"):
            assert_a_raise_is_not_swallowed(cache, cache.store)

    def test_deleting_the_publish_guard_would_let_todays_partial_session_through(
        self, tmp_path, monkeypatch
    ):
        """Mutation: ``FrameStore._may_serve`` answering ``True`` -- the deleted guard.

        The row is the same row and the clock is the same clock in both halves; the only thing that
        changes is whether the guard looks at them.
        """
        store_obj = store(tmp_path)
        cache = PriceCache(store=store_obj)
        assert_a_window_containing_today_is_never_served(
            cache, _ArchiveWalk(*OPEN_WINDOW, through=OPEN_THROUGH), store_obj
        )

        monkeypatch.setattr(
            frame_store.FrameStore, "_may_serve", lambda self, window_end, fetched_at: True
        )
        with pytest.raises(AssertionError, match="today's archive does not exist yet"):
            assert_a_window_containing_today_is_never_served(
                cache, _ArchiveWalk(*OPEN_WINDOW, through=OPEN_THROUGH), store_obj
            )

    def test_ignoring_the_flag_would_serve_a_bypassed_caller_a_stale_frame(
        self, tmp_path, monkeypatch
    ):
        """Mutation: the ``enabled`` flag no longer consulted -- the bypass reads the store anyway."""
        bypassed = PriceCache(store=store(tmp_path), enabled=False)
        assert_the_cache_is_bypassed(bypassed, _Fetcher(window_frame(WINDOW)))

        monkeypatch.setattr(bypassed, "enabled", True)
        with pytest.raises(AssertionError, match="disabled wrapper answered from a cache"):
            assert_the_cache_is_bypassed(bypassed, _Fetcher(window_frame(WINDOW)))

    def test_rebuilding_the_attrs_would_erase_the_provenance(self, tmp_path, monkeypatch):
        """Mutation: ``_publish`` starting the frame's attrs from a clean dict."""

        def rebuilt(frame, status, *, fetched_at, coverage_end, requested_end, uncovered):
            frame.attrs = {
                CACHE_STATUS_ATTR: status,
                CACHE_FETCHED_ATTR: fetched_at,
                CACHE_COVERAGE_END_ATTR: coverage_end,
                CACHE_REQUESTED_END_ATTR: requested_end,
                CACHE_UNCOVERED_DAYS_ATTR: list(uncovered),
            }
            return frame

        monkeypatch.setattr(price_cache, "_publish", rebuilt)
        cache = PriceCache(store=store(tmp_path))

        with pytest.raises(AssertionError, match="provenance"):
            assert_provenance_survives_the_cache(cache, _Fetcher(window_frame(WINDOW)))

    def test_deleting_the_store_check_would_replay_another_windows_bars(self, tmp_path, monkeypatch):
        """Mutation: ``frame_store._bars_lie_inside_the_window`` answering ``True`` -- the deleted
        window check, measured end to end through the consumer rather than at the store alone.

        The row is the same row in both halves; the only thing that changes is whether anything inspects
        the bars against the key they are filed under.
        """
        store_obj = store(tmp_path)
        plant_a_row_of_other_dates(store_obj)

        monkeypatch.setattr(frame_store, "_bars_lie_inside_the_window", lambda *args, **kwargs: True)
        with pytest.raises(AssertionError, match="was replayed"):
            assert_a_row_of_other_dates_is_not_replayed(
                PriceCache(store=store_obj), _Fetcher(window_frame(WINDOW)), store_obj
            )

    def test_restoring_the_now_fallback_would_date_a_replay_today(self, tmp_path, monkeypatch):
        """Mutation: ``_served_frame`` falling back to ``_now_iso()`` where the frame carries no fetch
        time -- the expression this task deleted, put back and run.

        The undated store is the same one in both halves; the only thing that changes is whether the
        wrapper invents a date for a row nobody dated.
        """
        monkeypatch.setattr(price_cache, "_served_frame", served_frame_with_the_now_fallback)

        with pytest.raises(AssertionError, match="claims the data was just fetched"):
            assert_the_replay_is_undated(replay_from_an_undated_store(tmp_path))
