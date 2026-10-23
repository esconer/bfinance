"""
P2 -- one store read and one store write per cold price call, and the same five attrs.

**The cost being removed.** A cold ``Ticker.info`` spent two ``FrameStore.read`` and two
``FrameStore.write`` for one price window. The second pair came from the tail of
:meth:`~bfinance.market.ohlcv.OHLCVEngine._rows_around_the_immutable_boundary`: the merged frame was
handed back to ``PriceCache.get_or_fetch`` for the sole purpose of republishing the five
``bfinance_cache_*`` attrs. That read could only answer ``miss`` -- the window reaches today, and
``FrameStore._may_serve`` refuses those in both directions -- and that write could only refuse the
same window, for the same reason. Neither answer changed the frame; the read and the write were the
cost, and the attrs were the only product.

**What must not change is the observation.** The attrs are now published in the pass that read the
data, by the wrapper's own publish step. Every one of the five is still present and truthful:
``bfinance_cache_status``/``..._fetched_at`` date the live read this call made, while
``..._coverage_end``/``..._requested_end``/``..._uncovered_days`` describe the **caller's request**,
not the immutable prefix the store was allowed to keep. So the tests below compare those values
against the pre-change code path itself -- ``PriceCache.get_or_fetch`` over the same frame and
window, on a scratch store -- rather than against values typed here. "Same as before" is thereby a
measurement, not a restatement.

The request shapes that make the attrs non-trivial are all covered: full coverage (nothing
uncovered), partial coverage (a shortfall that includes days after the boundary), and a warm call
whose whole frame is the stored prefix plus an empty tail -- the case where simply keeping the
prefix's own attrs would publish a replay's age and status for a live read.

Nothing here touches the network or the user's ``~/.bfinance``: the archive is the range-aware stub
below, every store is a ``tmp_path`` file, and the profile route is a typed miss.
"""

from __future__ import annotations

from datetime import date
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
from bfinance.market.ohlcv import OHLCVEngine
from bfinance.market.price_cache import CACHE_UNCOVERED_DAYS_ATTR, PriceCache
from bfinance.nse.bhavcopy import MISSING_DATES_ATTR, iter_trading_days
from bfinance.ticker import Ticker
from bfinance.utils.exceptions import TickerNotFoundError

from conftest import StubCorporateActionsClient

#: The five keys ``PriceCache`` owns, in one tuple so a missing one is a failure rather than a gap
#: a test happens not to look at.
_CACHE_ATTRS = (
    CACHE_STATUS_ATTR,
    CACHE_FETCHED_ATTR,
    CACHE_COVERAGE_END_ATTR,
    CACHE_REQUESTED_END_ATTR,
    CACHE_UNCOVERED_DAYS_ATTR,
)

#: Two distinct instants for the wrapper's clock -- the cold read's, then the warm call's tail.
#: Different on purpose: it is what makes "the composite frame is dated by this call's tail"
#: observable rather than an equality of two identical stamps.
_FIRST_INSTANT = "2026-01-02T09:15:00+05:30"
_TAIL_INSTANT = "2026-01-03T09:16:00+05:30"


# --------------------------------------------------------------------------------------------- time


def _today() -> pd.Timestamp:
    """Today in IST, the day the boundary and the store's own rule are judged against."""
    return pd.Timestamp.now(tz=IST).normalize()


def _candidates(start: str, end: str) -> List[str]:
    """Candidate trading days in the span, as ISO days: the archive's own calendar."""
    return [day.isoformat() for day in iter_trading_days(start, end)]


def _boundary(on: Optional[pd.Timestamp] = None) -> str:
    """The newest day whose archive file is final: the last candidate before ``on`` (or today)."""
    stamp = _today() if on is None else pd.Timestamp(on)
    days = iter_trading_days(
        (stamp - pd.Timedelta(days=7)).date(), (stamp - pd.Timedelta(days=1)).date()
    )
    return days[-1].isoformat()


def _year_window(end: str) -> Tuple[str, str]:
    """``start``/``end`` for a year-long request ending on ``end``, spelled out for a fixed key."""
    start = (pd.Timestamp(end) - pd.DateOffset(years=1)).date().isoformat()
    return start, end


# -------------------------------------------------------------------------------------- the archive


class _Archive:
    """
    A range-aware stand-in for ``bfinance.nse.bhavcopy.bhavcopy_range``.

    It answers the span it was handed: one bar per **published** candidate day inside that span, and
    a recorded missing date for the candidate days whose file does not exist yet. ``published_through``
    is the last day with a file, so "the frame stops early" and "the whole frame came from the
    prefix" are staged rather than hoped for -- which is what makes the uncovered-day attrs testable
    where they are non-empty.
    """

    def __init__(self, *, published_through: Optional[str] = None, symbol: str = "TCS") -> None:
        self.symbol = symbol
        self.published_through = (
            published_through
            if published_through is not None
            else (_today() - pd.Timedelta(days=1)).date().isoformat()
        )
        self.spans: List[Tuple[str, str]] = []

    def __call__(self, start, end) -> pd.DataFrame:
        start, end = str(start)[:10], str(end)[:10]
        self.spans.append((start, end))
        days = list(iter_trading_days(start, end))
        served = [
            day
            for day in days
            if day.isoformat() <= self.published_through and day >= date(2024, 1, 1)
        ]
        missing = [day.isoformat() for day in days if day not in served]

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
                "bfinance_candidate_trading_days": len(days),
                MISSING_DATES_ATTR: missing,
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


class _ProfileUnavailable:
    """The profile route is not what this file measures, and a typed miss degrades cleanly."""

    async def get_company_profile(self, symbol, **kwargs):
        raise TickerNotFoundError(f"{symbol}: the profile route is out of scope for this file")


# -------------------------------------------------------------------------------------- fixtures


@pytest.fixture
def archive(monkeypatch) -> _Archive:
    """Install the range-aware archive stand-in, and take the opt-in mirror out of the way."""
    stub = _Archive()
    monkeypatch.setattr(ohlcv_mod, "bhavcopy_range", stub)
    monkeypatch.delenv(ohlcv_mod.MIRROR_ENV_VAR, raising=False)
    return stub


@pytest.fixture
def store(tmp_path) -> FrameStore:
    """This test's own frame-store file. No test here reads or writes ``~/.bfinance``."""
    return FrameStore(db_path=tmp_path / "frames.db")


@pytest.fixture
def now(monkeypatch) -> dict:
    """A settable clock for the wrapper's ``fetched_at``.

    Pointed rather than frozen so a two-call test can give the cold read and the tail two different
    instants -- that is what proves the warm frame publishes the *tail's* age rather than the stored
    prefix's.
    """
    from bfinance.market import price_cache as price_cache_mod

    clock = {"value": _FIRST_INSTANT}
    monkeypatch.setattr(price_cache_mod, "_now_iso", lambda: clock["value"])
    return clock


@pytest.fixture
def store_calls(monkeypatch) -> dict:
    """Count every read and write that reaches the store's own door.

    Both are counted on :class:`FrameStore` rather than on one instance so a test cannot miss a
    second store built somewhere on the path. A test that also builds the reference wrapper resets
    the counters before the call it measures, and compares against the reference afterwards.
    """
    calls = {"read": 0, "write": 0}
    real_read, real_write = FrameStore.read, FrameStore.write

    def counting_read(self, **kwargs):
        calls["read"] += 1
        return real_read(self, **kwargs)

    def counting_write(self, frame, **kwargs):
        calls["write"] += 1
        return real_write(self, frame, **kwargs)

    monkeypatch.setattr(FrameStore, "read", counting_read)
    monkeypatch.setattr(FrameStore, "write", counting_write)
    return calls


# ----------------------------------------------------------------------------------------- helpers


def _engine(store: FrameStore) -> OHLCVEngine:
    return OHLCVEngine(
        _Screener(),
        corporate_actions_client=StubCorporateActionsClient(),
        price_cache=PriceCache(store=store),
    )


async def _price_path(engine: OHLCVEngine, start: str, end: str, symbol: str = "TCS"):
    """The frame the cache labels: the read ``fetch_history`` builds on, before its end-trim.

    ``fetch_history`` drops the requested end day afterwards (yfinance's exclusive end), and the five
    attrs describe the read that happened, not the view after that trim -- the old second pass and
    the new single pass both label this frame. Comparing here is therefore the like-for-like
    before/after; comparing on the returned history frame would compare against a frame the removed
    line never saw.
    """
    rows, _mirror_state, _window_record = await engine._fetch_bhavcopy_bars(
        symbol, symbol, start, end, 0
    )
    return rows


def _five(frame: pd.DataFrame) -> dict:
    return {key: frame.attrs.get(key) for key in _CACHE_ATTRS}


def _pre_change_attrs(
    frame: pd.DataFrame, *, start: str, end: str, tmp_path, name: str, store_enabled: bool = True
) -> dict:
    """The five attrs the removed line produced, from the same frame and the same request.

    That line was ``cache.get_or_fetch(lambda: rows, symbol=..., exchange=..., series=None,
    start=start_iso, end=end_iso)``. The store's rule is unchanged, so a scratch store answers its
    read with ``miss`` and refuses its write -- the call is the old publish step and nothing else.
    ``store_enabled=False`` reproduces the same step for a store that is switched off, where the
    old call answered ``disabled`` instead. The five keys are stripped from the frame first, so the
    comparison says "the reference derives these from a frame and a request", never "the reference
    echoes what the new code wrote".
    """
    stripped = frame.copy()
    for key in _CACHE_ATTRS:
        stripped.attrs.pop(key, None)
    scratch = PriceCache(
        store=FrameStore(db_path=tmp_path / f"reference-{name}.db", enabled=store_enabled)
    )
    reference = scratch.get_or_fetch(
        lambda: stripped,
        symbol="TCS",
        exchange="NSE",
        series=None,
        start=start,
        end=end,
    )
    return reference.attrs


# ------------------------------------------------------------------------------- the primary call


def test_a_cold_info_pays_one_store_read_and_one_store_write(
    store, archive, store_calls, now, tmp_path
):
    """
    The headline, on the call the plan measured: a cold ``Ticker.info``.

    ``info`` fetches exactly one price frame (``period="1y"``, ``requires=("Close", "Volume")``) and
    keeps it as ``_history_for_metadata``, so the count below is the price path's own. Before the
    change the count was 2/2; the second pair was the re-wrap, whose read could only miss (the
    window reaches today) and whose write could only be refused (same rule).

    The attrs are compared on the frame ``_fetch_bhavcopy_bars`` returned -- the one the removed
    line labelled, before ``fetch_history`` applies its exclusive-end trim -- and then the narrowed
    frame ``info`` holds must carry the same five values.

    Mutation this is red on: **restore the second ``cache.get_or_fetch``** at the end of
    ``_rows_around_the_immutable_boundary``. The write count becomes two, because the REFUSED write
    is still a call to the store's door -- which is the cost this removes.
    """
    engine = _engine(store)
    ticker = Ticker("TCS", cache_ttl_hours=0, screener_client=_ProfileUnavailable())
    ticker.ohlcv_engine = engine

    # The old second pass labelled the frame *this* call returns, not the narrowed view built from
    # it; capture it so the reference below is applied to the same object the removed line saw.
    captured = {}
    real_fetch = engine._fetch_bhavcopy_bars

    async def capturing_fetch(*args, **kwargs):
        result = await real_fetch(*args, **kwargs)
        captured["rows"] = result[0]
        return result

    engine._fetch_bhavcopy_bars = capturing_fetch

    store_calls["read"] = store_calls["write"] = 0

    info = ticker.info

    assert info, "info returned no dictionary"

    assert (store_calls["read"], store_calls["write"]) == (1, 1), (
        "one cold info must open the store's read door once and its write door once; measured "
        f"{store_calls}"
    )

    labelled = captured["rows"]
    frame = ticker._history_for_metadata
    assert frame is not None and not frame.empty, "the price frame info was built from is missing"
    for source, where in ((labelled, "the price read"), (frame, "info's narrowed frame")):
        for key in _CACHE_ATTRS:
            assert key in source.attrs, f"the single pass dropped {key} from {where}"

    end = labelled.attrs[CACHE_REQUESTED_END_ATTR]
    start = labelled.attrs["bfinance_requested_start"]
    assert end == _today().date().isoformat(), "requested_end is the caller's request, spelled ISO"
    assert start < end

    reference = _pre_change_attrs(
        labelled, start=start, end=end, tmp_path=tmp_path, name="cold-info"
    )
    assert _five(labelled) == {key: reference[key] for key in _CACHE_ATTRS}, (
        "the single pass must publish exactly what the removed re-wrap published, including the "
        "fetched_at the wrapper's clock gave it"
    )
    assert _five(frame) == _five(labelled), (
        "and the five keys must survive the narrowing into info's frame unchanged"
    )


async def test_the_warm_call_reads_once_and_never_writes(
    store, archive, store_calls, now, tmp_path
):
    """
    The warm call: one prefix read, no write at all, and the live tail's own age on the frame.

    The prefix is a replay and is never rewritten; the tail reaches today and is never stored. So
    the store's write door is used zero times, where the removed re-wrap used it once per call to
    be refused.

    Mutation this is red on, both applied and watched: **return the merged frame without routing it
    through the publish step** (the prefix's ``hit`` and its older instant ride out as this call's)
    and **compute the uncovered days from the prefix window** (the days after the boundary vanish
    from the list, which the reference comparison catches).
    """
    start, end = _year_window(_today().date().isoformat())
    engine = _engine(store)
    now["value"] = _FIRST_INSTANT
    await _price_path(engine, start, end)

    now["value"] = _TAIL_INSTANT
    store_calls["read"] = store_calls["write"] = 0
    warm = await _price_path(engine, start, end)

    assert (store_calls["read"], store_calls["write"]) == (1, 0), (
        "the warm call reads the prefix once and writes nothing -- the tail is never stored, and "
        f"the refused re-wrap write is gone; measured {store_calls}"
    )
    assert warm.attrs[CACHE_STATUS_ATTR] == "miss", "a live tail was fetched, so it is not a hit"
    assert warm.attrs[CACHE_FETCHED_ATTR] == _TAIL_INSTANT, (
        "the composite frame is dated by this call's tail, not by the stored prefix's instant"
    )

    reference = _pre_change_attrs(warm, start=start, end=end, tmp_path=tmp_path, name="warm")
    assert _five(warm) == {key: reference[key] for key in _CACHE_ATTRS}


async def test_uncovered_days_describe_the_request_not_the_prefix(
    store, archive, now, tmp_path
):
    """
    The request stops well before its own end: the shortfall must name days after the boundary.

    ``uncovered_days`` is not a property of the immutable prefix. The prefix ends at the boundary;
    the caller asked through today, so a list computed from the prefix window alone would omit every
    day after it -- a present attr describing the wrong window, which is the failure mode this test
    exists for. The same is true of ``requested_end``.

    Mutation this is red on: **compute the attrs from the prefix frame's own attrs** (or from
    ``start_iso..boundary_iso``). The list then stops at the boundary, and ``requested_end`` reads
    the boundary rather than the day the caller asked for.
    """
    start, end = _year_window(_today().date().isoformat())
    boundary = _boundary()
    archive.published_through = (
        pd.Timestamp(boundary) - pd.Timedelta(days=7)
    ).date().isoformat()

    frame = await _price_path(_engine(store), start, end)

    uncovered = frame.attrs[CACHE_UNCOVERED_DAYS_ATTR]
    coverage_end = str(frame.index.max())[:10]
    assert uncovered, "this request holds less than it asked for, so its shortfall must be named"
    # The mutation this catches is "compute the attrs from the prefix window", whose uncovered
    # list stops at the immutable boundary. That claim is only testable when the calendar puts a
    # trading day after the boundary: on a Saturday (or any day whose boundary is the last
    # trading day) the request itself contains none, so there is nothing a correct answer could
    # name. Guard the premise rather than the assertion -- the equality below still runs and is
    # what proves the list describes the request.
    if any(day > boundary for day in _candidates(start, end)):
        assert any(day > boundary for day in uncovered), (
            "days after the immutable boundary are part of the caller's request: a list computed "
            f"from the prefix window alone would stop at {boundary}; got {uncovered}"
        )
    assert uncovered == [day for day in _candidates(start, end) if day > coverage_end], (
        "every candidate day after the data ends, and only those -- no weekend is a shortfall"
    )
    assert frame.attrs[CACHE_REQUESTED_END_ATTR] == end, (
        "requested_end is the caller's window end, not the prefix's end"
    )
    assert frame.attrs[CACHE_COVERAGE_END_ATTR] == coverage_end, (
        "coverage_end is where the frame's own data stops, not where the request stops"
    )

    reference = _pre_change_attrs(frame, start=start, end=end, tmp_path=tmp_path, name="short")
    assert _five(frame) == {key: reference[key] for key in _CACHE_ATTRS}


async def test_a_switched_off_store_stays_disabled_rather_than_miss(archive, now, tmp_path):
    """
    A store that is switched off is not a store that was asked and had nothing.

    ``PriceCache(enabled=False)`` never reaches this path: the wrapper is a pass-through there. The
    other shape is a wrapper that is on around a *store* that is off -- the split still runs (there
    is a boundary to split around), but the prefix's own label says ``disabled``, which is the
    vocabulary's word for "there was no cache decision to report". A ``miss`` would claim the store
    was consulted and found empty, a different and false statement.

    Mutation this is red on: **hard-code ``status="miss"``** in the single pass. The old second pass
    read the same store and would have copied its own ``disabled`` answer out.
    """
    start, end = _year_window(_today().date().isoformat())
    frame = await _price_path(
        _engine(FrameStore(db_path=tmp_path / "frames.db", enabled=False)), start, end
    )

    assert frame.attrs[CACHE_STATUS_ATTR] == "disabled", (
        "the store was switched off, so the frame must not claim a miss"
    )
    reference = _pre_change_attrs(
        frame, start=start, end=end, tmp_path=tmp_path, name="disabled-store", store_enabled=False
    )
    assert _five(frame) == {key: reference[key] for key in _CACHE_ATTRS}


# ------------------------------------------------------------------------- the same five, by shape


@pytest.mark.parametrize(
    "shape", ["full coverage", "partial coverage", "whole frame cached"]
)
async def test_the_single_pass_publishes_the_pre_change_attrs_for_every_shape(
    shape, store, archive, now, tmp_path
):
    """
    The before/after contract: for each request shape, the five attrs are the old code's values.

    The three shapes are the ones the five keys can say different things about:

    * **full coverage** -- every candidate day in the request is held; ``uncovered_days`` is empty.
    * **partial coverage** -- the archive stops early; the list is non-empty and reaches past the
      boundary.
    * **whole frame cached** -- the prefix answers the window and the tail has no bars, so the merged
      frame *is* the stored prefix. The publish step has to relabel it ``miss`` with this call's
      instant; keeping the prefix's ``hit``/older instant would describe a replay as a live read.

    The reference is the removed code path itself (see :func:`_pre_change_attrs`), not a copy of the
    values, so this test does not drift with the calendar.
    """
    start, end = _year_window(_today().date().isoformat())
    boundary = _boundary()
    engine = _engine(store)
    now["value"] = _FIRST_INSTANT

    if shape == "full coverage":
        archive.published_through = end
        frame = await _price_path(engine, start, end)
        assert frame.attrs[CACHE_UNCOVERED_DAYS_ATTR] == [], (
            "a frame holding every candidate day has nothing to explain"
        )
    elif shape == "partial coverage":
        archive.published_through = (
            pd.Timestamp(boundary) - pd.Timedelta(days=7)
        ).date().isoformat()
        frame = await _price_path(engine, start, end)
        assert frame.attrs[CACHE_UNCOVERED_DAYS_ATTR], "the shortfall must be named"
    else:
        archive.published_through = boundary
        await _price_path(engine, start, end)
        now["value"] = _TAIL_INSTANT
        frame = await _price_path(engine, start, end)
        assert archive.spans[-1] == (
            (pd.Timestamp(boundary) + pd.Timedelta(days=1)).date().isoformat(),
            end,
        ), "the warm call asked only for the tail"
        assert frame.attrs[CACHE_STATUS_ATTR] == "miss", (
            "the frame is the prefix plus an empty tail, and a live tail read happened: a replay's "
            "status would be the stored row's, not this call's"
        )
        assert frame.attrs[CACHE_FETCHED_ATTR] == _TAIL_INSTANT, (
            "and its age is this call's tail, not the earlier walk that wrote the prefix"
        )

    reference = _pre_change_attrs(
        frame, start=start, end=end, tmp_path=tmp_path, name=shape.replace(" ", "-")
    )
    assert _five(frame) == {key: reference[key] for key in _CACHE_ATTRS}, (
        f"{shape}: the single pass must publish the removed re-wrap's five values"
    )
