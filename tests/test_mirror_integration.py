"""
Wiring tests for the local bhavcopy mirror into the OHLCV history path.

The mirror (:mod:`bfinance.mirror`) has been implemented, committed and tested on its own for
some time, and :mod:`bfinance.market.ohlcv` referenced it zero times. A store nothing calls is
not a feature. These tests are about the one property that makes calling it safe, and it is not
the happy path.

THE DEFECT THIS LOCKS DOWN
--------------------------
``Mirror.read()`` returns zero rows for three different situations and cannot tell them apart:

    the security was not listed that day
    the day is before the security existed
    the day has not been downloaded yet

All three are an empty frame. A caller that trusted that frame would be handed a short series
dressed as a complete one -- which is the entire silent-empty-frame defect class this package
exists to eliminate, relocated from the network into a database and given a speedup. So
``coverage()``, not ``read()``, is what decides whether the mirror may answer, and a day it
does not hold is either fetched from NSE or the call raises. Never a short frame.

``test_partial_coverage_is_never_silent`` is the one that matters most, and it is written so a
plausible wrong implementation fails it: the network stub's value for the missing day is a
sentinel (``999.5``) that no stored row carries, so a frame that quietly dropped the day cannot
pass by looking the right length.

Offline by construction. ``bfinance.market.ohlcv.bhavcopy_range`` is monkeypatched throughout and
``StubCorporateActionsClient`` replays an empty recorded answer, so nothing here reaches NSE.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import textwrap

import pandas as pd
import pytest

import bfinance.market.ohlcv as ohlcv_mod
from bfinance.cache.frame_store import FrameStore, default_frame_store_path
from bfinance.market.ohlcv import (
    MIRROR_ENV_VAR,
    SOURCE_MIRROR,
    SOURCE_MIRROR_NETWORK,
    SOURCE_NETWORK,
    OHLCVEngine,
    RealOHLCUnavailableError,
)
from bfinance.mirror import Mirror, default_mirror_path
from bfinance.utils.symbols import resolve_exchange_and_symbol

from conftest import StubCorporateActionsClient

# 2026-09-21 is a Monday, so: 22 Tue, 23 Wed, 24 Thu, 25 Fri, 26 Sat, 27 Sun.
# That gives a five-session week with a real weekend and a real seeded holiday inside one window,
# which is the whole point -- "absent because nobody published a file" and "absent because we
# never downloaded one" are the two things the coverage watermark exists to separate.
DAY_A = "2026-09-22"
DAY_B = "2026-09-23"
DAY_C = "2026-09-24"
DAY_HOLIDAY = "2026-09-25"
DAY_SATURDAY = "2026-09-26"
#: A held day well outside every requested window, for the negative control.
DAY_ELSEWHERE = "2026-09-01"
#: The trading days of the following week. 26 Sat and 27 Sun need no calendar to be excluded.
WIDE_DAYS = ["2026-09-22", "2026-09-23", "2026-09-24", "2026-09-28", "2026-09-29", "2026-09-30"]

#: Two sessions a week apart, with every weekday between them seeded as a holiday. The window is
#: therefore FULLY covered (no gap, so no request at all) and yet the delivered bars are weekly,
#: which is the only way a MIRROR frame can come back coarser than ``interval="1d"``.
SPARSE_DAYS = ["2026-09-22", "2026-10-01", "2026-10-02"]
SPARSE_START = "2026-09-22"
SPARSE_END = "2026-10-03"  # a Saturday: excluded by weekday, needs no payload
SPARSE_HOLIDAYS = ["23-Sep-2026", "24-Sep-2026", "25-Sep-2026",
                   "28-Sep-2026", "29-Sep-2026", "30-Sep-2026"]

#: Close of 2026-09-23 as only the network has it. No stored row carries this value, so a frame
#: that omitted the day cannot be mistaken for one that fetched it.
SENTINEL_CLOSE = 999.5

MIRROR_BARS = {
    DAY_A: {"RELIANCE": (100.0, 110.0, 90.0, 105.0, 1_000),
            "TCS": (3000.0, 3050.0, 2980.0, 3020.0, 500)},
    DAY_B: {"RELIANCE": (150.0, 160.0, 140.0, 155.0, 2_000),
            "TCS": (3080.0, 3100.0, 3060.0, 3090.0, 600)},
    DAY_C: {"RELIANCE": (200.0, 210.0, 190.0, 205.0, 3_000),
            "TCS": (3100.0, 3150.0, 3080.0, 3120.0, 700)},
    DAY_ELSEWHERE: {"RELIANCE": (50.0, 55.0, 45.0, 52.0, 400)},
}

#: What NSE alone knows: the one day the mirror was never backfilled for.
NETWORK_BARS = {
    DAY_B: {"RELIANCE": (900.0, SENTINEL_CLOSE + 1.0, 899.0, SENTINEL_CLOSE, 9_999),
            "TCS": (3090.0, 3110.0, 3070.0, 3100.0, 600)},
}

#: A 2026 CM holiday payload in the shape ``/holiday-master`` returns. One holiday is enough:
#: ``_is_trading_day`` decides weekends with no payload at all, and any other 2026 weekday is
#: then a trading day by elimination.
HOLIDAY_PAYLOAD = {"CM": [{"tradingDate": "25-Sep-2026", "description": "test holiday"}]}


# ------------------------------------------------------------------ stubs and builders

def _market_frame(rows) -> pd.DataFrame:
    """A whole-market bhavcopy frame shaped exactly as ``bhavcopy_range`` returns one."""
    index = pd.MultiIndex.from_arrays(
        [pd.DatetimeIndex([r[0] for r in rows]),
         pd.Index([r[1] for r in rows], dtype=object, name="Symbol")],
        names=["Date", "Symbol"],
    )
    frame = pd.DataFrame(
        [(r[2], r[3], r[4], r[5], r[6]) for r in rows],
        columns=["Open", "High", "Low", "Close", "Volume"],
        index=index,
    )
    frame["Volume"] = frame["Volume"].astype("int64")
    return frame.sort_index()


def _frame_for(bars) -> pd.DataFrame:
    rows = [
        (day, symbol, *values)
        for day, per_symbol in bars.items()
        for symbol, values in per_symbol.items()
    ]
    return _market_frame(rows)


def _simple_bars(days) -> dict:
    """One RELIANCE row per day, with a close that identifies the day, for wide-window tests."""
    return {
        day: {"RELIANCE": (100.0 + i, 110.0 + i, 90.0 + i, 105.0 + i, 1_000 + i)}
        for i, day in enumerate(days)
    }


class _StubScreener:
    """Close-only screener stub. The default path must never reach it, and the tests say so."""

    def __init__(self):
        self.chart_calls = []
        self.profile_calls = []

    async def get_chart_timeseries(self, ticker, metric="price", days=1825):
        self.chart_calls.append((ticker, metric, days))
        return pd.DataFrame()

    async def get_company_profile(self, ticker, force_refresh=False):
        self.profile_calls.append(ticker)
        raise ValueError("no profile")


def _engine() -> OHLCVEngine:
    return OHLCVEngine(
        _StubScreener(), corporate_actions_client=StubCorporateActionsClient()
    )


def _stub_bhavcopy(monkeypatch, bars=None, *, error=None) -> list:
    """Install a whole-market bhavcopy stub and return the list it records calls into."""
    calls: list = []
    payload = NETWORK_BARS if bars is None else bars

    def _bhavcopy_range(start, end, **kwargs):
        calls.append((start, end))
        if error is not None:
            raise error
        in_span = {
            day: per_symbol
            for day, per_symbol in payload.items()
            if str(start) <= day <= str(end)
        }
        return _frame_for(in_span)

    monkeypatch.setattr(ohlcv_mod, "bhavcopy_range", _bhavcopy_range)
    return calls


def _build_mirror(path, days=(), *, calendar=True) -> str:
    """A real on-disk mirror holding ``days`` of :data:`MIRROR_BARS`. Returns the path."""
    store = Mirror(path)
    if calendar:
        assert store.seed_calendar(HOLIDAY_PAYLOAD) == 1
    for day in days:
        store.put_day(day, _frame_for({day: MIRROR_BARS[day]}), "udiff", f"nse:{day}")
    store.close()
    return str(path)


@pytest.fixture(autouse=True)
def _mirror_opted_out(monkeypatch, tmp_path):
    """Every test starts with the mirror OFF and BFINANCE_HOME pointed at a temp dir.

    Autouse so that a test which does not opt in is asserting the default rather than inheriting
    whatever the developer's shell had set, and so "no mirror.db appeared" is a statement about
    this test rather than about the machine.
    """
    monkeypatch.delenv(MIRROR_ENV_VAR, raising=False)
    monkeypatch.setenv("BFINANCE_HOME", str(tmp_path / "home"))
    return tmp_path


# ====================================================================== 1. default is unchanged
#
# ``auto_adjust=False`` is now spelled out on every fetch_history call in this file. auto_adjust
# became True by default on 2026-09-30 (it now matches yfinance 1.7), and True needs a real
# ex-dated corporate-actions feed to scale by -- which none of these tests stubs, because all of
# them are about where the BARS came from, not about the adjustment. Left implicit, the new
# default would raise AdjustmentNotAvailableError out of a test about the mirror, which is not
# evidence about the mirror at all. The explicit False restores exactly the pre-flip call.


async def test_default_path_never_constructs_a_mirror(monkeypatch, tmp_path):
    """The default call path must not so much as name the store.

    The strongest available form of the claim: ``Mirror.__init__`` is replaced with a bomb, so a
    single construction anywhere on the path fails the test rather than passing quietly.
    """
    import bfinance.mirror as mirror_mod

    def _boom(*args, **kwargs):
        raise AssertionError("the default path must never construct a Mirror")

    monkeypatch.setattr(mirror_mod.Mirror, "__init__", _boom)
    calls = _stub_bhavcopy(monkeypatch, {**MIRROR_BARS, **NETWORK_BARS})

    df = await _engine().fetch_history(
        symbol="RELIANCE", start=DAY_A, end=DAY_HOLIDAY, actions=False, auto_adjust=False
    )

    assert calls == [(DAY_A, DAY_HOLIDAY)], "the network answered, exactly as it does today"
    assert df["Close"].tolist() == [105.0, SENTINEL_CLOSE, 205.0]
    assert df.attrs["bfinance_source"] == SOURCE_NETWORK


async def test_default_path_touches_no_disk(monkeypatch, tmp_path):
    """A plain read leaves the frame cache's own file and nothing else -- no mirror database.

    **This assertion changed on purpose when the default-on frame cache landed, and the change is
    the feature rather than a concession to it.** It used to read "no file is created, not even an
    empty one, anywhere under BFINANCE_HOME", which was the right statement while the *mirror* was
    the only on-disk store the price path could reach: the mirror is opt-in, so a plain read had no
    reason to touch a disk at all. The price path now has a second on-disk store, and it is
    default-ON because that is the optimisation: without it, ``history(period="1y")`` pays one
    archive download per weekday of the window on *every* call; with it, the first call pays that
    walk once and the second pays only the days published since (measured, offline, on the 1y
    window: 260 then 0, with the two frames identical). A read of a **closed** window -- one whose
    end is strictly before today, as this one's is -- is exactly the read that store exists to make
    free, and the single row it writes to ``$BFINANCE_HOME/frames.db`` *is* the mechanism that makes
    the second call cost nothing. So "reading history creates no file under BFINANCE_HOME" and "the
    price path is cached" are mutually exclusive claims, and the second one is the feature: the
    assertion moves, not the cache. A test that kept the old claim could only be kept green by
    switching the cache off, i.e. by deleting the win it is here to protect.

    What is *kept* is the property this test was actually written to protect -- the mirror is
    opt-in, so a plain read must not create the mirror's database -- and what is added is the
    stricter half: the directory is enumerated against an explicit expected set, so the one
    permitted artefact is named, and a future third store under ``$BFINANCE_HOME`` still fails here
    instead of arriving as a file no test ever looked for.
    """
    _stub_bhavcopy(monkeypatch, {**MIRROR_BARS, **NETWORK_BARS})

    await _engine().fetch_history(
        symbol="RELIANCE", start=DAY_A, end=DAY_HOLIDAY, actions=False, auto_adjust=False
    )

    home = tmp_path / "home"
    assert default_frame_store_path().parent == home, (
        "premise: the home this module's fixture pointed $BFINANCE_HOME at is the directory "
        "enumerated below"
    )
    created = sorted(path.name for path in home.iterdir()) if home.exists() else []

    # The mirror may not appear. This is the assertion this test was written for, and it is still
    # true: a mirror is only ever there when a test builds one on purpose, as the rest of this file
    # does.
    assert default_mirror_path().name not in created, (
        "reading history must not create a mirror database as a side effect"
    )
    # And the frame cache's file is the only thing that may: one artefact, named, exactly.
    assert created == [default_frame_store_path().name], (
        "a plain read of a closed window may create the frame cache's own file and nothing else; "
        f"found {created}"
    )
    # Which is told apart from an empty file that merely exists: the artefact is this window's row,
    # i.e. the thing the next identical call is served from rather than a database that was opened.
    exchange, _ = resolve_exchange_and_symbol("RELIANCE")
    served = FrameStore().read(
        symbol="RELIANCE", exchange=exchange, series=None, start=DAY_A, end=DAY_HOLIDAY
    )
    assert served.status == "hit", (
        "the one permitted file must hold the cache row this read wrote; if permitting it here "
        "also permitted an empty database, the permission would be for the wrong thing"
    )


# ========================================================================== 2. mirror hit

async def test_mirror_hit_serves_stored_values_exactly(monkeypatch, tmp_path):
    """A mirror that holds the window answers from disk, and says so in attrs."""
    path = _build_mirror(tmp_path / "m.db", days=[DAY_A, DAY_B, DAY_C])
    calls = _stub_bhavcopy(monkeypatch, NETWORK_BARS)

    df = await _engine().fetch_history(
        symbol="RELIANCE", start=DAY_A, end=DAY_HOLIDAY, actions=False, auto_adjust=False, mirror=path
    )

    assert calls == [], "a full-coverage mirror must not spend a request"
    assert df.index.strftime("%Y-%m-%d").tolist() == [DAY_A, DAY_B, DAY_C]
    # Exactly the stored numbers: Open/High/Low/Close/Volume, unshifted, unscaled.
    assert df["Open"].tolist() == [100.0, 150.0, 200.0]
    assert df["High"].tolist() == [110.0, 160.0, 210.0]
    assert df["Low"].tolist() == [90.0, 140.0, 190.0]
    assert df["Close"].tolist() == [105.0, 155.0, 205.0]
    assert df["Volume"].tolist() == [1_000, 2_000, 3_000]
    assert df.attrs["bfinance_source"] == SOURCE_MIRROR
    assert df.attrs["bfinance_mirror"]["used"] is True
    assert df.attrs["bfinance_mirror"]["days_missing"] == []


async def test_mirror_hit_slices_this_symbol_out_of_the_whole_market(monkeypatch, tmp_path):
    """The mirror is keyed by (day, symbol), so one store serves every scrip in the file."""
    path = _build_mirror(tmp_path / "m.db", days=[DAY_A, DAY_B, DAY_C])
    calls = _stub_bhavcopy(monkeypatch, NETWORK_BARS)

    reliance = await _engine().fetch_history(
        symbol="RELIANCE", start=DAY_A, end=DAY_HOLIDAY, actions=False, auto_adjust=False, mirror=path
    )
    tcs = await _engine().fetch_history(
        symbol="TCS", start=DAY_A, end=DAY_HOLIDAY, actions=False, auto_adjust=False, mirror=path
    )

    assert calls == [], "two symbols, one store, still no request"
    assert reliance["Close"].tolist() == [105.0, 155.0, 205.0]
    assert tcs["Close"].tolist() == [3020.0, 3090.0, 3120.0]


async def test_the_env_var_is_the_door_reachable_from_the_public_api(monkeypatch, tmp_path):
    """``Ticker.history()`` cannot pass a new keyword, so the env var is how it is reached.

    Both halves are asserted: set, the store answers; unset, it is not even constructed. A test
    of only the first would pass on a package that read the variable unconditionally.
    """
    path = _build_mirror(tmp_path / "m.db", days=[DAY_A, DAY_B, DAY_C])
    calls = _stub_bhavcopy(monkeypatch, NETWORK_BARS)

    monkeypatch.setenv(MIRROR_ENV_VAR, path)
    df = await _engine().fetch_history(
        symbol="RELIANCE", start=DAY_A, end=DAY_HOLIDAY, actions=False, auto_adjust=False
    )

    assert calls == [], "the env var alone must be enough to reach the mirror"
    assert df.attrs["bfinance_source"] == SOURCE_MIRROR
    assert df["Close"].tolist() == [105.0, 155.0, 205.0]


async def test_a_mirror_never_creates_its_own_database(monkeypatch, tmp_path):
    """Opting in before backfilling gets a named reason, not a 0-byte file and a wrong answer."""
    absent = tmp_path / "not-built-yet.db"
    calls = _stub_bhavcopy(monkeypatch, {**MIRROR_BARS, **NETWORK_BARS})

    df = await _engine().fetch_history(
        symbol="RELIANCE", start=DAY_A, end=DAY_HOLIDAY, actions=False, auto_adjust=False, mirror=str(absent)
    )

    assert not absent.exists(), "a read must not create the store it could not find"
    assert calls == [(DAY_A, DAY_HOLIDAY)]
    assert df.attrs["bfinance_source"] == SOURCE_NETWORK
    assert "sync" in df.attrs["bfinance_mirror"]["reason"]


# ============================================================== 3. partial coverage is NOT silent

async def test_partial_coverage_is_never_silent(monkeypatch, tmp_path):
    """
    The most important test in this file.

    The mirror holds 2026-09-22 and 2026-09-24 and not 2026-09-23, so a request spanning all
    three is answered partly from disk and partly from NSE. The failure this forecloses is a
    two-bar frame that looks like a three-bar answer, and the stub's sentinel close is what
    makes it detectable rather than merely plausible.
    """
    path = _build_mirror(tmp_path / "m.db", days=[DAY_A, DAY_C])
    calls = _stub_bhavcopy(monkeypatch, NETWORK_BARS)

    df = await _engine().fetch_history(
        symbol="RELIANCE", start=DAY_A, end=DAY_HOLIDAY, actions=False, auto_adjust=False, mirror=path
    )

    # All three days are present. Not two.
    assert df.index.strftime("%Y-%m-%d").tolist() == [DAY_A, DAY_B, DAY_C]
    # The gap day carries the network's own number, so provenance is provable from the values.
    assert df["Close"].tolist() == [105.0, SENTINEL_CLOSE, 205.0]
    # Exactly one request, and it asks for the missing day and nothing else.
    assert calls == [(DAY_B, DAY_B)]
    # And the frame is labelled honestly, on both the short key and the full record.
    assert df.attrs["bfinance_source"] == SOURCE_MIRROR_NETWORK
    mirror_state = df.attrs["bfinance_mirror"]
    assert mirror_state["days_missing"] == [DAY_B]
    assert mirror_state["network_days"] == [DAY_B]
    assert mirror_state["used"] is True


async def test_a_stale_mirror_whose_gaps_span_the_window_reads_the_network_once(
    monkeypatch, tmp_path
):
    """
    Topping up a gap costs one request per day of the SPAN asked for, not per missing day.

    Here the mirror holds two days in the middle of a twelve-day window, so closing the gaps
    would fetch the whole window anyway. The engine must notice that and make the single call it
    would have made with no mirror at all -- otherwise enabling the mirror can only ever add
    traffic to a stale store, which is the opposite of what it is for.
    """
    path = _build_mirror(tmp_path / "m.db", days=[DAY_B])
    calls = _stub_bhavcopy(monkeypatch, _simple_bars(WIDE_DAYS))

    df = await _engine().fetch_history(
        symbol="RELIANCE", start=WIDE_DAYS[0], end=WIDE_DAYS[-1], actions=False, auto_adjust=False, mirror=path
    )

    assert calls == [(WIDE_DAYS[0], WIDE_DAYS[-1])], (
        "one whole-window fetch, not a gap fetch plus a fill"
    )
    assert df["Close"].tolist() == [105.0, 106.0, 107.0, 108.0, 109.0]
    assert df.attrs["bfinance_source"] == SOURCE_NETWORK
    state = df.attrs["bfinance_mirror"]
    assert state["used"] is False
    assert state["days_missing"] == ["2026-09-22", "2026-09-24",
                                     "2026-09-28", "2026-09-29", "2026-09-30"]


async def test_a_mirror_whose_calendar_cannot_classify_a_day_never_assumes_absence(
    monkeypatch, tmp_path
):
    """
    No seeded calendar means every weekday is UNKNOWN, and unknown must be treated as a gap.

    2026-09-25 IS a holiday, but this mirror cannot know that, so it must not claim the day is
    covered -- the safe direction is the only one available: fetch, or fail, never assume. The
    Saturday beside it needs no calendar at all and is still free, which is what makes this a
    test of the watermark rather than of a blanket "weekday means gap".
    """
    path = _build_mirror(tmp_path / "m.db", days=[DAY_A], calendar=False)
    calls = _stub_bhavcopy(monkeypatch, _simple_bars([DAY_A, DAY_B, DAY_C]))

    df = await _engine().fetch_history(
        symbol="RELIANCE", start=DAY_A, end=DAY_SATURDAY, actions=False, auto_adjust=False, mirror=path
    )

    state = df.attrs["bfinance_mirror"]
    assert state["calendar_seeded"] is False
    # DAY_HOLIDAY is in the list even though nobody published a file for it, and DAY_SATURDAY is
    # not, even though the calendar could not have said so either.
    assert state["days_missing"] == [DAY_B, DAY_C, DAY_HOLIDAY]
    assert calls == [(DAY_B, DAY_HOLIDAY)]
    assert df.index.strftime("%Y-%m-%d").tolist() == [DAY_A, DAY_B, DAY_C]
    assert df.attrs["bfinance_source"] == SOURCE_MIRROR_NETWORK


# ========================================================================== 4. holiday != gap

async def test_a_holiday_is_not_a_gap(monkeypatch, tmp_path):
    """
    A day the calendar knows nobody published a file for is an ANSWER, not a hole.

    Without this the mirror would demand a file for every exchange holiday forever -- roughly
    twenty 404s a year -- and would then be unable to answer any window that spanned one. The
    seeded holiday and the Saturday are both in the requested window here, and neither may cost
    a request.
    """
    path = _build_mirror(tmp_path / "m.db", days=[DAY_A, DAY_B, DAY_C])
    calls = _stub_bhavcopy(monkeypatch, NETWORK_BARS)

    df = await _engine().fetch_history(
        symbol="RELIANCE", start=DAY_A, end=DAY_SATURDAY, actions=False, auto_adjust=False, mirror=path
    )

    assert calls == [], "neither the seeded holiday nor the weekend may cost a request"
    assert df.attrs["bfinance_source"] == SOURCE_MIRROR
    assert df.index.strftime("%Y-%m-%d").tolist() == [DAY_A, DAY_B, DAY_C]
    state = df.attrs["bfinance_mirror"]
    assert state["days_missing"] == []
    # The Friday is a seeded holiday; the Saturday needs no payload at all. Both are recorded,
    # so "why is there no bar for the 25th?" has an answer on the frame.
    assert state["non_trading_days"] == [DAY_HOLIDAY, DAY_SATURDAY]


async def test_a_holiday_does_not_make_an_uncovered_window_a_hit(monkeypatch, tmp_path):
    """The converse, which is the one a naive "any missing day is fine" check would get wrong."""
    path = _build_mirror(tmp_path / "m.db", days=[DAY_A, DAY_B])
    calls = _stub_bhavcopy(monkeypatch, {**MIRROR_BARS, **NETWORK_BARS})

    df = await _engine().fetch_history(
        symbol="RELIANCE", start=DAY_A, end=DAY_HOLIDAY, actions=False, auto_adjust=False, mirror=path
    )

    # The holiday is free; the trading day the mirror lacks is not. The frame has all of it.
    assert df.index.strftime("%Y-%m-%d").tolist() == [DAY_A, DAY_B, DAY_C]
    assert df.attrs["bfinance_mirror"]["days_missing"] == [DAY_C]
    assert calls == [(DAY_C, DAY_C)]


# ============================================================== 5. network failure is reported

async def test_network_failure_on_a_gap_is_raised_not_degraded(monkeypatch, tmp_path):
    """
    The refusal. The mirror holds most of the window and NSE is down for the rest.

    Returning the mirror's rows here would be a frame missing real trading days, wearing the
    same columns and the same attrs as a complete answer, and the caller would have no way to
    tell. An empty frame is no better -- it is the defect in its purest form. So the call fails,
    names the day, and says where the gap is.
    """
    path = _build_mirror(tmp_path / "m.db", days=[DAY_A, DAY_C])
    _stub_bhavcopy(monkeypatch, error=RuntimeError("upstream 503"))

    with pytest.raises(RealOHLCUnavailableError) as caught:
        await _engine().fetch_history(
            symbol="RELIANCE", start=DAY_A, end=DAY_HOLIDAY, actions=False, auto_adjust=False, mirror=path
        )

    message = str(caught.value)
    assert DAY_B in message, "the failing day must be named, not summarised away"
    assert "NOT returned" in message
    assert "sync" in message, "the message must say how to fix it"


async def test_a_mirror_failure_falls_back_to_the_network_and_says_why(monkeypatch, tmp_path):
    """A corrupt store must not take down a price feed that is otherwise fine."""
    path = _build_mirror(tmp_path / "m.db", days=[DAY_A, DAY_C])
    calls = _stub_bhavcopy(monkeypatch, {**MIRROR_BARS, **NETWORK_BARS})

    def _explode(*args, **kwargs):
        raise sqlite3.DatabaseError("file is not a database")

    import bfinance.mirror as mirror_mod

    monkeypatch.setattr(mirror_mod.Mirror, "coverage", _explode)

    df = await _engine().fetch_history(
        symbol="RELIANCE", start=DAY_A, end=DAY_HOLIDAY, actions=False, auto_adjust=False, mirror=path
    )

    assert df["Close"].tolist() == [105.0, SENTINEL_CLOSE, 205.0]
    assert df.attrs["bfinance_source"] == SOURCE_NETWORK
    assert "coverage query failed" in df.attrs["bfinance_mirror"]["reason"]
    assert calls == [(DAY_A, DAY_HOLIDAY)]


# ==================================================================== 6. provenance is honest

async def test_provenance_is_honest_on_the_non_mirror_path(monkeypatch):
    """``bfinance_source`` reads "network" when nothing local was consulted."""
    _stub_bhavcopy(monkeypatch, {**MIRROR_BARS, **NETWORK_BARS})

    df = await _engine().fetch_history(
        symbol="RELIANCE", start=DAY_A, end=DAY_HOLIDAY, actions=False, auto_adjust=False
    )

    assert df.attrs["bfinance_source"] == SOURCE_NETWORK
    state = df.attrs["bfinance_mirror"]
    assert state["enabled"] is False
    assert state["used"] is False
    assert state["days_missing"] == []
    # Present on every frame, not only on the mirror ones, so a consumer reads one key.
    assert set(state) >= {"enabled", "used", "path", "days_missing", "non_trading_days",
                          "network_days", "calendar_seeded", "source"}


async def test_provenance_is_published_on_an_empty_frame_too(monkeypatch, tmp_path):
    """The empty frame is the one most likely to be trusted, so it is the one that must say so."""
    path = _build_mirror(tmp_path / "m.db", days=[DAY_A, DAY_B, DAY_C])
    _stub_bhavcopy(monkeypatch, NETWORK_BARS)

    df = await _engine().fetch_history(
        symbol="NOSUCHSCRIP", start=DAY_A, end=DAY_HOLIDAY, actions=False, auto_adjust=False, mirror=path
    )

    assert df.empty
    assert df.attrs["bfinance_source"] == SOURCE_MIRROR
    assert df.attrs["bfinance_mirror"]["used"] is True
    assert df.attrs["bfinance_mirror"]["days_missing"] == []


# ============================================================ 7. bar-interval attrs survive

async def test_bar_interval_attributes_survive_a_mirror_served_frame(monkeypatch, tmp_path):
    """The just-committed provenance must not be traded away for the new one.

    Three consecutive sessions is enough for the spacing to be inferable, so this is a real
    check of the value and not just of the key's presence.
    """
    path = _build_mirror(tmp_path / "m.db", days=[DAY_A, DAY_B, DAY_C])
    _stub_bhavcopy(monkeypatch, NETWORK_BARS)

    df = await _engine().fetch_history(
        symbol="RELIANCE", start=DAY_A, end=DAY_HOLIDAY, interval="1d", actions=False, auto_adjust=False, mirror=path
    )

    assert df.attrs["bfinance_source"] == SOURCE_MIRROR
    assert df.attrs["bfinance_bar_interval"] == "1d"
    assert df.attrs["bfinance_bar_interval_mismatch"] is None


async def test_bar_interval_attributes_survive_a_mixed_frame(monkeypatch, tmp_path):
    """The same, on the path where the mirror and the network both contributed."""
    path = _build_mirror(tmp_path / "m.db", days=[DAY_A, DAY_C])
    _stub_bhavcopy(monkeypatch, NETWORK_BARS)

    df = await _engine().fetch_history(
        symbol="RELIANCE", start=DAY_A, end=DAY_HOLIDAY, interval="1d", actions=False, auto_adjust=False, mirror=path
    )

    assert df.attrs["bfinance_source"] == SOURCE_MIRROR_NETWORK
    assert df.attrs["bfinance_bar_interval"] == "1d"
    assert df.attrs["bfinance_bar_interval_mismatch"] is None


async def test_the_interval_mismatch_key_is_still_computed_on_a_mirror_frame(monkeypatch, tmp_path):
    """
    A mirror frame whose delivered spacing is coarser than requested must still be reported.

    The store is a DAILY tape, so a fully-synced mirror cannot produce this on its own -- the only
    feed that coarsens silently is the close-only one. But the watermark can: a mirror whose
    calendar says the intervening weekdays were holidays holds real trading days a week apart,
    and asking for ``interval="1d"`` then yields weekly bars. The interval machinery has to run
    on mirror data, not be skipped because the source changed.
    """
    store = Mirror(tmp_path / "sparse.db")
    assert store.seed_calendar({"CM": [{"tradingDate": d} for d in SPARSE_HOLIDAYS]}) == 6
    bars = _simple_bars(SPARSE_DAYS)
    for day in SPARSE_DAYS:
        store.put_day(day, _frame_for({day: bars[day]}), "udiff", f"nse:{day}")
    store.close()
    calls = _stub_bhavcopy(monkeypatch, NETWORK_BARS)

    df = await _engine().fetch_history(
        symbol="RELIANCE", start=SPARSE_START, end=SPARSE_END, interval="1d",
        actions=False, auto_adjust=False, mirror=str(tmp_path / "sparse.db"),
    )

    assert calls == [], "a fully covered window must not spend a request"
    assert df.attrs["bfinance_source"] == SOURCE_MIRROR
    assert df.attrs["bfinance_mirror"]["days_missing"] == []
    assert df.attrs["bfinance_bar_interval"] == "1wk"
    assert df.attrs["bfinance_bar_interval_mismatch"] is not None
    assert "delivered 1wk" in df.attrs["bfinance_bar_interval_mismatch"]


async def test_a_mirror_frame_resamples_when_a_coarser_interval_is_asked_for(
    monkeypatch, tmp_path
):
    """The resample path is applied to mirror bars exactly as it is to network ones."""
    path = _build_mirror(tmp_path / "m.db", days=[DAY_A, DAY_B, DAY_C])
    _stub_bhavcopy(monkeypatch, NETWORK_BARS)

    df = await _engine().fetch_history(
        symbol="RELIANCE", start=DAY_A, end=DAY_HOLIDAY, interval="1wk", actions=False, auto_adjust=False, mirror=path
    )

    assert df.attrs["bfinance_source"] == SOURCE_MIRROR
    assert df.attrs["bfinance_bar_interval"] == "1wk"
    assert df.attrs["bfinance_bar_interval_mismatch"] is None


# ================================================================ 8. negative control

async def test_a_mirror_with_zero_relevant_days_is_not_a_hit(monkeypatch, tmp_path):
    """
    The negative control. A mirror that exists, is healthy, and holds a day the caller did not
    ask for is not a hit. Treating "the store answered" as "the store answered your question"
    is the error, and it is invisible until a caller gets an empty frame from a populated
    database.
    """
    path = _build_mirror(tmp_path / "m.db", days=[DAY_ELSEWHERE])   # nothing inside the window
    calls = _stub_bhavcopy(monkeypatch, {**MIRROR_BARS, **NETWORK_BARS})

    df = await _engine().fetch_history(
        symbol="RELIANCE", start=DAY_A, end=DAY_HOLIDAY, actions=False, auto_adjust=False, mirror=path
    )

    assert calls == [(DAY_A, DAY_C)], "the network must answer, even though a mirror exists"
    assert df["Close"].tolist() == [105.0, SENTINEL_CLOSE, 205.0]
    assert df.attrs["bfinance_source"] == SOURCE_NETWORK
    state = df.attrs["bfinance_mirror"]
    assert state["enabled"] is True, "the mirror WAS consulted; it simply could not answer"
    assert state["used"] is False, "and not one stored row reached this frame"
    assert state["days_missing"] == [DAY_A, DAY_B, DAY_C]


async def test_a_mirror_holding_the_days_but_not_the_symbol_is_a_real_answer(
    monkeypatch, tmp_path
):
    """
    The converse of the negative control, and the one that is easy to get wrong in the other
    direction: coverage said the file was downloaded, so "no row" means the security did not
    trade, and the mirror may legitimately answer with nothing.
    """
    path = _build_mirror(tmp_path / "m.db", days=[DAY_A, DAY_B, DAY_C])
    calls = _stub_bhavcopy(monkeypatch, NETWORK_BARS)

    df = await _engine().fetch_history(
        symbol="NOSUCHSCRIP", start=DAY_A, end=DAY_HOLIDAY, actions=False, auto_adjust=False, mirror=path
    )

    assert calls == [], "full coverage means the mirror may answer, including with an empty frame"
    assert df.empty
    assert df.attrs["bfinance_source"] == SOURCE_MIRROR


# =========================================================================== isolation

def test_a_bare_import_bfinance_does_not_load_the_mirror():
    """
    ``bfinance.mirror`` is opt-in, and this integration must not quietly opt everyone in.

    Subprocess, because this module imports the mirror itself and an in-process ``sys.modules``
    would be polluted by the very import the check is about -- which is exactly the trap
    ``tests/test_public_surface.py`` documents for the other opt-in modules.
    """
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(
            """
            import sys
            import bfinance
            print("bfinance.mirror" in sys.modules)
            """
        )],
        capture_output=True, text=True, timeout=180, check=False,
    )
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert result.stdout.strip() == "False", (
        "import bfinance executed bfinance.mirror; ohlcv.py must import it lazily, inside the "
        "function, the way it already does for bhavcopy"
    )
