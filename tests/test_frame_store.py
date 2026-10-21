"""
B1 — the price-frame store: storage that survives ``attrs``, and the rules that keep it honest.

**Why this file exists.** ``SQLiteCache`` cannot hold a price frame, and it fails silently when asked
to: ``set`` JSON-encodes its payload and the column is ``payload TEXT NOT NULL``, so every frame
write lands in the ``TypeError`` handler at ``sqlite_cache.py:229-231``, which logs at WARNING and
returns. Measured here as the counter-factual half of
:meth:`TestItsOwnStorage.test_the_payload_column_is_a_blob_because_text_would_refuse_it`. Everything
in Wave 3 of the plan -- caching the bhavcopy range, memoising the 1y frame -- is blocked on a store
that can actually hold a frame, which is why this file is the enabler's evidence rather than a
detail of it.

**Every test below is red on a named mutation, and the name is in its docstring.** The mutations are
the plausible ones -- a refactor that reaches for JSON because "a cache is a cache", a loader that
memoises because unpickling is expensive, a guard deleted because it "cannot happen", a refusal that
logs and stores anyway -- and the load-bearing ones are *executed* in
:class:`TestTheChecksBite` rather than described, in the style ``tests/test_cache_status_provenance.py``
uses for the same reason: an assertion that has never been seen to fail is an assertion nobody has
tested.

Nothing here touches the network, and nothing reads or writes the user's ``~/.bfinance``: every
store is built on ``tmp_path`` with a fixed clock, and the one test that exercises the default path
points ``$BFINANCE_HOME`` at a temporary directory first.
"""

from __future__ import annotations

import io
import logging
import pickle
import sqlite3
import time

import pandas as pd
import pytest

from bfinance.cache import frame_store
from bfinance.cache.frame_store import FrameStore, frame_key
from bfinance.cache.sqlite_cache import CacheLookup, SQLiteCache

#: The day the fixed clock says it is. A test that used the real clock would behave differently
#: before and after IST midnight -- the drift the store's own clock seam exists to make impossible --
#: and the day matters here more than in most files, because the publish rule *is* a date comparison.
#: 2026-10-17 is a Saturday, so the weekday window below ends on a Friday.
TODAY = "2026-10-17"

#: A closed window: it ends the day before ``TODAY``, so its archive files exist and never change.
WINDOW = ("2026-10-01", "2026-10-16")

#: A window that contains ``TODAY``, and the one the publish rule refuses.
OPEN_WINDOW = ("2026-10-12", TODAY)

#: The last bar of the fixture frame, and therefore the coverage the store must report.
COVERAGE_END = "2026-10-14"

#: Every status a caller can ever observe from this store, in the vocabulary it already has.
#: ``CacheLookup`` names five of them; ``"disabled"`` is ``ownership.py:1288``'s name for "no request
#: was made", which a store that is switched off has to be able to say.
STATUSES = frozenset({"hit", "null", "miss", "expired", "corrupt", "disabled"})


# --------------------------------------------------------------------------- fixtures and helpers


def clock_at(moment: str, *, tz: str = frame_store.IST):
    """A fixed clock, so "today" is a known day rather than the day the suite happens to run on."""
    return lambda: pd.Timestamp(moment, tz=tz)


def store(tmp_path, *, enabled: bool = True, moment: str = TODAY, name: str = "frames.db"):
    """A store on its own file under ``tmp_path``, with the clock pinned to ``moment``."""
    return FrameStore(db_path=tmp_path / name, enabled=enabled, clock=clock_at(moment))


def price_frame(
    periods: int = 3,
    start: str = "2026-10-12",
    *,
    source: str = "nse-bhavcopy",
    transport: str = "network",
    attempts: list | None = None,
):
    """A frame shaped the way the price layer really serves one.

    Six yfinance columns on a naive ``DatetimeIndex`` named ``Date``, carrying ``price_basis`` and the
    four provenance keys -- including the two **container** values the storage format has to survive:
    ``bfinance_price_source_attempts`` is a list of dicts and ``bfinance_mirror`` is a dict, and each
    is built fresh here because a module-level list shared between tests would let one test's
    mutation appear in another's frame (which is itself the isolation defect this file tests for).
    """
    index = pd.bdate_range(start, periods=periods, name="Date")
    frame = pd.DataFrame(
        {
            "Open": [100.0 + i for i in range(periods)],
            "High": [101.0 + i for i in range(periods)],
            "Low": [99.0 + i for i in range(periods)],
            "Close": [100.5 + i for i in range(periods)],
            "Adj Close": [100.5 + i for i in range(periods)],
            "Volume": [1_000_000 + i for i in range(periods)],
        },
        index=index,
    )
    frame.attrs.update(
        {
            "bfinance_price_source": source,
            "bfinance_source": transport,
            "bfinance_price_source_attempts": (
                list(attempts)
                if attempts is not None
                else [
                    {
                        "source": "trendlyne-ohlc",
                        "outcome": "skipped_columns",
                        "called": False,
                        "reason": "publishes no Open, High or Low.",
                        "basis": "as_traded_split_bonus_adjusted",
                    },
                    {
                        "source": "nse-bhavcopy",
                        "outcome": "served",
                        "called": True,
                        "reason": "",
                        "basis": "as_traded",
                    },
                ]
            ),
            "bfinance_mirror": {"transport": transport, "days_missing": [], "days_considered": 3},
            "price_basis": "as_traded",
        }
    )
    return frame


def failure_frame(shape: str):
    """The three shapes a *failed* fetch really takes, rather than three invented ones.

    ``no rows`` is the shape ``is_empty_payload`` already calls a failure; ``all NaN`` is the shape
    ``OHLCVEngine.download`` answers a ticker that failed with, deliberately, so the failed ticker
    still appears; ``no columns`` is the frame that is not a price frame under any reading.
    """
    if shape == "no rows":
        return price_frame(periods=0)
    if shape == "no columns":
        return pd.DataFrame(index=pd.bdate_range("2026-10-12", periods=3, name="Date"))
    if shape == "all NaN":
        frame = price_frame()
        for column in frame.columns:
            frame[column] = float("nan")
        return frame
    raise AssertionError(f"{shape!r} is not one of the three failure shapes")


def row_count(db_path, table: str = "bfinance_price_frames") -> int:
    """How many rows a table holds. Raw SQL on purpose: "did anything get written" must not be
    answered by the code under test, which is the whole point of asserting it."""
    with sqlite3.connect(str(db_path), timeout=5.0) as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def table_names(db_path) -> set:
    with sqlite3.connect(str(db_path), timeout=5.0) as conn:
        return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def write_frame(store_obj, frame, window=WINDOW, *, symbol="TCS", series="EQ"):
    """One place the store is written through, so a control can drive the identical call."""
    return store_obj.write(
        frame, symbol=symbol, exchange="NSE", series=series, start=window[0], end=window[1]
    )


def read_frame(store_obj, window=WINDOW, *, symbol="TCS", series="EQ"):
    """... and one place it is read through, for the same reason."""
    return store_obj.read(
        symbol=symbol, exchange="NSE", series=series, start=window[0], end=window[1]
    )


def plant_row(store_obj, *, written_window, under_window, fetched_at, symbol="TCS", series="EQ"):
    """A row for a window the store would refuse, put on disk without :meth:`FrameStore.write`.

    It is moved there by re-keying a row the store really wrote, rather than by assembling one out of
    columns: the payload and the coverage are then genuine, and the only things untrue about the row
    are the window it claims to cover and the instant it claims to have been fetched in -- which is
    exactly the shape an older build's row, a copied file or a hand edit takes.

    ``fetched_at`` is planted rather than read off the real clock because the row being modelled was
    written **while its own window was open**, and that is an instant inside the window the caller
    names -- a test that took "now" instead would be asserting a different thing on every day the
    suite ran, and would quietly stop asserting anything once the real date passed the fixed one.
    """
    assert (
        write_frame(store_obj, price_frame(), written_window, symbol=symbol, series=series) == "hit"
    )
    with sqlite3.connect(str(store_obj.db_path), timeout=5.0) as conn:
        conn.execute(
            "UPDATE bfinance_price_frames SET frame_key = ?, fetched_at = ? WHERE frame_key = ?",
            (
                frame_key(symbol, "NSE", series, under_window[0], under_window[1]),
                pd.Timestamp(fetched_at, tz=frame_store.IST).timestamp(),
                frame_key(symbol, "NSE", series, written_window[0], written_window[1]),
            ),
        )
        conn.commit()


def plant_empty_row(store_obj, window=WINDOW, *, symbol="TCS", series="EQ") -> None:
    """A row holding an empty frame, put on disk without :meth:`FrameStore.write`.

    It has to be planted: ``write`` refuses an empty frame by :func:`_refusal`, so no row of this shape
    can exist unless something other than the writer put it there -- an older build, a copy, or an edit
    by hand, the same provenance the other planted rows in this file have. The payload is a real frame
    pickled by the store's own ``_payload_blob``, so only the *shape* is unusual (no bars, and therefore
    no coverage for the column to state) -- which is exactly the shape the read half must handle without
    crashing on ``index.min()``, and without inventing a refusal of its own for it.
    """
    assert write_frame(store_obj, price_frame(), window, symbol=symbol, series=series) == "hit"
    with sqlite3.connect(str(store_obj.db_path), timeout=5.0) as conn:
        conn.execute(
            "UPDATE bfinance_price_frames SET payload = ?, n_rows = 0, coverage_end = '' "
            "WHERE frame_key = ?",
            (
                frame_store._payload_blob(price_frame(periods=0)),
                frame_key(symbol, "NSE", series, window[0], window[1]),
            ),
        )
        conn.commit()


# ------------------------------------------------------- the checks the controls below attack


def assert_provenance_survived(served, expected, *, where: str) -> None:
    """Every attr the fetched frame carried is still on the frame that came back, values included.

    Missing keys are reported as a set so the message names every key that went, which is what makes
    the JSON control below legible.
    """
    missing = sorted(set(expected.attrs) - set(served.attrs))
    assert not missing, f"{where}: the store dropped {missing}"
    changed = {
        name: (expected.attrs[name], served.attrs[name])
        for name in expected.attrs
        if served.attrs[name] != expected.attrs[name]
    }
    assert not changed, f"{where}: the store changed these attrs: {changed}"


def assert_not_cached(store_obj, frame, window=WINDOW, *, where: str = "") -> None:
    """The whole of "never cache a failure, a null or a 404" in one check.

    Read as a sentence: nothing is written, the write says so, and the next read is a miss. That last
    clause is the one that matters most -- a cached failure is not a wasted row, it is a permanent
    answer. A cached 404 makes a transient CDN blip look like a market holiday for the life of the
    file, and no later call can tell the difference.
    """
    assert row_count(store_obj.db_path) == 0, f"{where}: the store was not empty to begin with"
    status = write_frame(store_obj, frame, window)
    assert row_count(store_obj.db_path) == 0, (
        f"{where}: {row_count(store_obj.db_path)} row(s) were written for a failed fetch"
    )
    assert status == "miss", f"{where}: write answered {status!r}, and nothing was stored"
    assert read_frame(store_obj, window).status == "miss", f"{where}: the next read did not miss"


def assert_reads_are_isolated(store_obj, window=WINDOW) -> None:
    """Two reads never hand out one object.

    Both directions are mutated, because they are two different defects with one cause: the numeric
    block and the attrs *containers*. Measured on pandas 3.0.5, a shared frame leaks all three -- the
    data, the list inside ``bfinance_price_source_attempts`` and the dict inside ``bfinance_mirror``.
    """
    first = read_frame(store_obj, window).value
    assert first is not None
    first_close = first["Close"].iloc[0]
    first.iloc[0, 0] = -999.0
    first.attrs["bfinance_price_source_attempts"].append({"source": "MUTANT", "called": False})
    first.attrs["bfinance_mirror"]["days_missing"].append("1999-01-01")

    second = read_frame(store_obj, window).value
    assert second is not first, "the second read returned the same object as the first"
    assert second["Close"].iloc[0] == first_close, "the first caller's edit reached the stored bars"
    assert len(second.attrs["bfinance_price_source_attempts"]) == 2, (
        "the first caller's append reached the stored attrs list"
    )
    assert second.attrs["bfinance_mirror"]["days_missing"] == [], (
        "the first caller's append reached the stored attrs dict"
    )


def assert_the_row_does_not_answer_its_key(store_obj, frame, window=WINDOW) -> None:
    """The whole of "a row is served only when its bars lie inside the window the key names".

    Read as a sentence: the row is on disk and it holds bars outside the window it is filed under, so
    the read is ``"corrupt"`` and hands out nothing. The second clause is the one that matters -- a
    caller that gets a frame believes the question it asked has been answered, and bars from another
    window under this key are a wrong answer that nothing in the frame contradicts. ``frame`` is the
    planted row, used only to *say* in the failure message what the row holds.

    Driven by the tests and by the control that neuters the check, on purpose: a check nobody has
    watched fail is a check nobody has tested.
    """
    span = (
        "no bars"
        if len(frame) == 0
        else f"{str(frame.index.min())[:10]}..{str(frame.index.max())[:10]}"
    )
    lookup = read_frame(store_obj, window)
    assert lookup.status == "corrupt", (
        f"a row holding {span} was answered as {lookup.status!r} for the window "
        f"{window[0]}..{window[1]}: a frame whose bars lie outside the key it is filed under only ever "
        f"answers a different question"
    )
    assert lookup.value is None, f"a frame holding {span} was handed to the caller anyway"


def assert_a_short_but_correct_row_is_served(store_obj, frame, window) -> None:
    """A row that is short but correctly sliced is served, and still says where its data ends.

    The other direction of the same rule, and the one a strict reading of "the bars must lie inside the
    window" gets wrong: a row whose every bar *is* inside the window is an answer to it even when it has
    no bar at the end -- a suspension, a scrip that listed mid-window, a window that ends on a weekend.
    Refusing it would make a symbol with a real gap a permanent cache miss, which is the second
    obligation the store's own class docstring states, and the frame that comes back is labelled with
    the end it really reaches rather than the one that was asked for.
    """
    covered = str(frame.index.max())[:10]
    assert covered < window[1], "premise: the planted row is short of the window's own end"

    lookup = read_frame(store_obj, window)
    assert lookup.status == "hit", (
        f"a row whose bars are all inside {window[0]}..{window[1]} was answered as {lookup.status!r} "
        f"because its coverage stops at {covered}: a short frame is served and labelled, never refused"
    )
    assert lookup.value.attrs[frame_store.CACHE_COVERAGE_END_ATTR] == covered, (
        "the served frame does not say where its data ends"
    )
    assert [str(day)[:10] for day in lookup.value.index] == [
        str(day)[:10] for day in frame.index
    ], "the bars handed out are not the bars that were stored"


def assert_an_empty_row_is_not_refused_by_the_window_check(store_obj, window=WINDOW) -> None:
    """An empty index holds no bar that could be outside the window, so this check leaves it alone.

    Empty is inside. The refusal of an empty frame belongs to :func:`_refusal` on the way *in*, where
    it is already implemented and already tested; a second rule about the same shape, here, would be a
    contradiction rather than a guard -- and the read must not crash on the way to it either, because
    ``index.min()`` is ``NaT`` for an empty index and ``NaT`` compares as outside every window.
    """
    lookup = read_frame(store_obj, window)
    assert lookup.status == "hit", (
        f"an empty row was answered as {lookup.status!r}: it holds no bar outside the window, so the "
        f"window check has nothing to refuse -- an empty shape is the write path's refusal to name"
    )
    assert len(lookup.value) == 0, "the served frame is not the empty one that was stored"


# =============================================================== 1 + 2: the round trip itself


class TestTheRoundTrip:
    """Red on: **replacing the pickle pair with the JSON pair** -- ``_payload_blob`` writing
    ``frame.to_json().encode()`` and ``_frame_from_blob`` reading it back with ``pd.read_json``.

    That is the "a cache is a cache, JSON is fine" refactor: it is shorter than the pickle version,
    it passes every assertion about *numbers*, and measured on pandas 3.0.5 it returns ``attrs == {}``.
    A frame from the cache would then be indistinguishable from one whose vendor, basis and source
    attempts nobody recorded -- which is the defect, and it is invisible in the bars. Executed in
    :meth:`TestTheChecksBite.test_the_json_pair_would_erase_the_provenance`.
    """

    def test_the_pickle_pair_round_trips_every_attr_including_the_containers(self):
        frame = price_frame()
        back = frame_store._frame_from_blob(frame_store._payload_blob(frame))

        assert back.attrs is not frame.attrs, "the loaded attrs must be their own dict"
        assert_provenance_survived(back, frame, where="pickle round trip")
        assert isinstance(back.attrs["bfinance_price_source_attempts"], list)
        assert isinstance(back.attrs["bfinance_mirror"], dict)
        assert back.index.equals(frame.index), "the DatetimeIndex did not survive the round trip"
        assert back.index.name == "Date", "the index name did not survive the round trip"
        pd.testing.assert_frame_equal(back, frame)

    def test_a_stored_frame_comes_back_with_its_provenance_and_its_basis(self, tmp_path):
        store_obj = store(tmp_path)
        frame = price_frame()

        assert write_frame(store_obj, frame) == "hit"
        lookup = read_frame(store_obj)

        assert lookup.status == "hit"
        assert isinstance(lookup.value, pd.DataFrame)
        assert_provenance_survived(lookup.value, frame, where="read back from the store")
        assert lookup.value.attrs["bfinance_price_source"] == "nse-bhavcopy"
        assert lookup.value.attrs["price_basis"] == "as_traded"
        assert lookup.value.attrs["bfinance_price_source_attempts"][1]["outcome"] == "served"
        # The real status, not typed twice: the frame's claim and the lookup's answer are the same
        # string from the same call, so a frame labelled "hit" by a constant would fail here.
        assert lookup.value.attrs[frame_store.CACHE_STATUS_ATTR] == lookup.status


# ================================================================= 3: one caller, one object


class TestIsolation:
    """Red on: **``_isolated`` returning the frame it was given, together with a loader that keeps
    the object it loaded** -- the "unpickling is expensive, hold on to the loaded frame" mutation.

    Without the copy, the first caller's ``iloc[0, 0] = -999.0`` *and* its ``attrs`` list append are
    both visible to the second caller, silently: measured on pandas 3.0.5, a shared frame shares its
    data, the list inside ``bfinance_price_source_attempts`` and the dict inside ``bfinance_mirror``.
    Executed in
    :meth:`TestTheChecksBite.test_handing_out_the_loaded_object_would_let_one_caller_corrupt_the_next`.
    """

    def test_mutating_a_served_frame_does_not_reach_the_stored_one(self, tmp_path):
        store_obj = store(tmp_path)
        assert write_frame(store_obj, price_frame()) == "hit"

        assert_reads_are_isolated(store_obj)

        # A third read, after two rounds of mutation, is still the frame that was fetched: the copy
        # has to protect the *stored* data, not merely hand out a different object each time.
        third = read_frame(store_obj).value
        pd.testing.assert_frame_equal(third, price_frame())


# ============================================================ 4: the publish boundary, both sides


class TestThePublishBoundary:
    """Red on: **``_may_serve`` deleted** (or reduced to the one comparison against today), in the
    two places it is called -- :meth:`FrameStore.write` and :meth:`FrameStore.read`.

    Neither layer may cache, or serve, a window that contains today: today's archive file does not
    exist yet, and a partial session served as a final one is a wrong answer that looks like a quiet
    market. The row in the second test is planted by :func:`plant_row`, because the only way such a
    row can exist is if something other than this store's ``write`` put it there -- an older build, a
    copy, or an edit by hand.
    """

    def test_a_window_that_contains_today_is_not_stored(self, tmp_path):
        store_obj = store(tmp_path)

        assert write_frame(store_obj, price_frame(start="2026-10-15"), OPEN_WINDOW) == "miss"
        assert row_count(store_obj.db_path) == 0, (
            "a window containing today was written: today's file does not exist yet, so the row "
            "could only ever be a partial session"
        )
        assert read_frame(store_obj, OPEN_WINDOW).status == "miss"

    def test_a_row_for_such_a_window_is_not_served_even_after_the_day_passes(self, tmp_path):
        store_obj = store(tmp_path)
        plant_row(
            store_obj,
            written_window=WINDOW,
            under_window=OPEN_WINDOW,
            fetched_at="2026-10-17 15:30",
        )
        # ... and a row for the closed window, written through the real path, so that "refused" can
        # be told apart from "this store never serves anything".
        assert write_frame(store_obj, price_frame()) == "hit"
        assert row_count(store_obj.db_path) == 2, "premise: both rows are on disk"

        refused_today = read_frame(store_obj, OPEN_WINDOW)
        assert refused_today.status == "miss", (
            "a row for a window that contains today was served; its session was not final"
        )
        assert refused_today.value is None

        # One day later the window *looks* closed, and the row still must not be served: it was
        # written while its own window was open (the planted fetch time is the window's last day, and
        # is refused for that reason alone), which this store cannot do. Without that second
        # comparison the publish-boundary failure would arrive a day late instead of never, wearing
        # the label of an ordinary cache hit.
        tomorrow = store(tmp_path, moment="2026-10-18")
        assert read_frame(tomorrow, OPEN_WINDOW).status == "miss"

        # The counter-example that keeps this from being a test of "nothing is ever served": the row
        # written for the closed window is still served, on both days.
        assert read_frame(store_obj, WINDOW).status == "hit"
        assert read_frame(tomorrow, WINDOW).status == "hit"


# ====================================================== 5: a failure, a null and a 404


class TestFailuresAreNeverCached:
    """Red on: **``_refusal`` answering ``None`` for every frame** (the failure shapes are stored and
    the next read is a hit), and on **``_frames_to_store`` accepting anything** (a ``None`` frame, an
    HTTP status and an exception object become payloads).

    A 404 cannot be detected by a store, and that is not a gap: it never arrives as a frame. What the
    store can do -- and does -- is refuse the failure *shapes* a caller can hand over by accident, and
    raise for the ones that are not frames at all. Executed in
    :meth:`TestTheChecksBite.test_neutering_the_failure_refusal_would_cache_the_failure`.
    """

    @pytest.mark.parametrize("shape", ["no rows", "all NaN", "no columns"])
    def test_a_failure_shaped_frame_is_refused_and_nothing_is_written(self, tmp_path, shape):
        assert_not_cached(store(tmp_path), failure_frame(shape), where=shape)

    @pytest.mark.parametrize(
        "payload",
        [None, 404, b"<html>Not Found</html>", RuntimeError("Read timed out")],
        ids=["null", "http-404", "response-body", "exception"],
    )
    def test_a_null_or_an_http_status_is_refused_rather_than_stored(self, tmp_path, payload):
        store_obj = store(tmp_path)

        with pytest.raises(TypeError, match="must be a DataFrame"):
            write_frame(store_obj, payload)

        assert row_count(store_obj.db_path) == 0
        assert read_frame(store_obj).status == "miss"


# ====================================================== 6: its own file, its own table, a BLOB


class TestItsOwnStorage:
    """Red on: **pointing this store at ``SQLiteCache``'s table**, or routing its payload through
    ``SQLiteCache.set``. Both assertions fail -- there would be no ``bfinance_price_frames`` table at
    all, and the shared one would hold a row -- and the measured silent no-op would be back.

    The second test is the one that makes the first more than a naming convention: it hands the same
    frame to the screener cache and watches it vanish with a log line and no row, which is the bug
    this module exists to remove, pinned so the "just store frames in SQLiteCache" idea cannot return
    as a fix.
    """

    def test_the_store_uses_its_own_file_and_its_own_table(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BFINANCE_HOME", str(tmp_path / "home"))
        assert frame_store.default_frame_store_path() == tmp_path / "home" / "frames.db"

        cache = SQLiteCache(db_path=str(tmp_path / "cache.db"), default_ttl_hours=1.0)
        store_obj = store(tmp_path, name="frames.db")

        assert store_obj.db_path != cache.db_path, (
            "the frame store is sharing the screener cache file"
        )
        assert write_frame(store_obj, price_frame()) == "hit"

        assert table_names(store_obj.db_path) == {"bfinance_price_frames"}
        assert "bfinance_cache" not in table_names(store_obj.db_path), (
            "the frame store wrote into the screener cache's table"
        )
        assert table_names(cache.db_path) == {"bfinance_cache"}
        assert row_count(cache.db_path, "bfinance_cache") == 0

    def test_the_payload_column_is_a_blob_because_text_would_refuse_it(self, tmp_path, caplog):
        store_obj = store(tmp_path, name="frames.db")
        frame = price_frame()
        assert write_frame(store_obj, frame) == "hit"

        with sqlite3.connect(str(store_obj.db_path), timeout=5.0) as conn:
            kind, count = conn.execute(
                "SELECT typeof(payload), COUNT(*) FROM bfinance_price_frames"
            ).fetchone()
        assert (kind, count) == ("blob", 1), f"the payload was stored as {kind!r}"

        # The counter-factual, measured rather than described: the same frame through the screener
        # cache -- whose column is `TEXT NOT NULL` and whose writer JSON-encodes -- is dropped.
        cache = SQLiteCache(db_path=str(tmp_path / "cache.db"), default_ttl_hours=1.0)
        with caplog.at_level(logging.WARNING, logger="bfinance"):
            assert cache.set("ohlcv_TCS_1y", frame, category="ohlcv", ttl_hours=1.0) is None

        assert "Skipping cache set" in caplog.text, (
            "the screener cache no longer logs the refusal it is documented to make"
        )
        assert row_count(cache.db_path, "bfinance_cache") == 0, (
            "a frame was stored in the screener cache: if that ever becomes possible, it is a "
            "different task with a different decision to make"
        )
        assert cache.get_result("ohlcv_TCS_1y").status == "miss"


# ================================================================ 7: concat keeps the provenance


class TestConcatKeepsAttrs:
    """Red on: **deleting the ``combined.attrs = dict(base)`` re-assertion** in
    ``_concat_keeping_attrs``, leaving a bare ``pd.concat``.

    A range is fetched one trading day at a time, so the store concatenates. The premise is
    re-measured *in the test* rather than quoted from the plan, because it is the reason the
    re-assertion exists: measured on pandas 3.0.5, ``pd.concat`` of two frames that disagree about any
    attr returns ``attrs == {}`` -- no exception, no warning, no other symptom. Executed in
    :meth:`TestTheChecksBite.test_deleting_the_re_assertion_would_empty_the_range_attrs`.
    """

    def test_a_range_keeps_the_first_frames_provenance_instead_of_losing_it(self, tmp_path, caplog):
        first = price_frame(periods=1, start="2026-10-12")
        second = price_frame(
            periods=1,
            start="2026-10-13",
            source="marketlens-close",
            transport="network",
            attempts=[{"source": "marketlens-close", "outcome": "served", "called": True}],
        )
        assert first.attrs != second.attrs, "premise: the two days disagree about provenance"
        assert pd.concat([first, second]).attrs == {}, (
            "pandas no longer empties attrs on a concat of disagreeing frames; the re-assertion in "
            "_concat_keeping_attrs is still correct, but this premise needs re-measuring"
        )

        store_obj = store(tmp_path)
        with caplog.at_level(logging.WARNING, logger="bfinance"):
            assert write_frame(store_obj, [first, second]) == "hit"
        assert "disagree about attrs" in caplog.text, (
            "the disagreement was kept out of the row, and nothing said so"
        )

        served = read_frame(store_obj).value
        assert len(served) == 2, "the two days are not both in the served range"
        assert_provenance_survived(served, first, where="concatenated range")
        assert served.attrs["price_basis"] == "as_traded"
        assert served.attrs["bfinance_price_source"] == "nse-bhavcopy"
        expected_attempts = first.attrs["bfinance_price_source_attempts"]
        assert served.attrs["bfinance_price_source_attempts"] == expected_attempts, (
            "the second day's provenance was blended into the first's"
        )


# ================================================ 8: the negative control (the store switched off)


class TestTheStoreSwitchedOff:
    """Red on: **either ``if not self.enabled`` early return deleted**, or ``_init_db`` left
    unguarded in ``__init__``. Then the write becomes a hit, the read serves a frame, and a switched
    off store leaves a file behind.

    The negative control for the whole file: with no store, the same calls produce no hit anywhere,
    and the two stores in this test differ in *one* argument only -- so the difference in outcome can
    only be the flag. A switched-off store reports ``"disabled"`` rather than ``"miss"``
    (``ownership.py:1288``: "no request was made" has to be visible on the result); a caller that
    only tests ``.found`` cannot tell the two apart from the safe side, and a caller that publishes a
    frame status turns this into ``"miss"`` there, which is A1's convention and not this store's.
    """

    def test_a_disabled_store_writes_nothing_serves_nothing_and_leaves_no_file(self, tmp_path):
        path = tmp_path / "frames.db"
        off = FrameStore(db_path=path, enabled=False, clock=clock_at(TODAY))

        assert write_frame(off, price_frame()) == "disabled"
        assert not path.exists(), "a store that is switched off must not leave an artefact behind"

        lookup = read_frame(off)
        assert lookup.status == "disabled"
        assert lookup.value is None
        assert not lookup.found, "'disabled' must not read as an answer to a caller testing `found`"

        # The same file, the same calls, one argument different.
        on = FrameStore(db_path=path, clock=clock_at(TODAY))
        assert write_frame(on, price_frame()) == "hit"
        assert read_frame(on).status == "hit"


# ================================================== what a served frame says about its own limits


class TestWhatAServedFrameSaysAboutItself:
    """Red on: **publishing the requested end twice**, dropping ``CACHE_FETCHED_ATTR``, or dropping
    ``CACHE_COVERAGE_END_ATTR`` -- i.e. returning a short frame with nothing on it that says it is
    short.

    "The data ends here" and "I asked for it to end here" are different facts, and a caller that
    cannot tell them apart treats a truncated window as a complete one. The frame here covers
    ``2026-10-01`` to ``2026-10-16`` and its last bar is ``2026-10-14``, which is the gap C1 has to be
    able to see.
    """

    def test_the_observed_end_is_published_separately_from_the_requested_end(self, tmp_path):
        store_obj = store(tmp_path)
        assert write_frame(store_obj, price_frame(), WINDOW) == "hit"  # last bar 2026-10-14

        served = read_frame(store_obj, WINDOW).value
        assert served is not None

        observed_end = served.attrs[frame_store.CACHE_COVERAGE_END_ATTR]
        requested_end = served.attrs[frame_store.CACHE_REQUESTED_END_ATTR]
        assert observed_end == COVERAGE_END
        assert requested_end == WINDOW[1]
        assert observed_end != requested_end, (
            "the observed end and the requested end are the same value, so the gap is invisible"
        )
        assert served.index.max().date().isoformat() == observed_end, (
            "the published coverage is not what the frame's own index reaches"
        )

    def test_the_fetch_layer_can_state_the_coverage_it_knows_better(self, tmp_path):
        """A caller that already computed the archive's watermark overrides the index-derived value,
        because the frames a fetch layer holds are not always the whole story of what it fetched."""
        store_obj = store(tmp_path)

        status = store_obj.write(
            price_frame(),
            symbol="TCS",
            exchange="NSE",
            series="EQ",
            start=WINDOW[0],
            end=WINDOW[1],
            observed_coverage_end="2026-10-16",
        )
        assert status == "hit"
        served = read_frame(store_obj).value
        assert served.attrs[frame_store.CACHE_COVERAGE_END_ATTR] == "2026-10-16"
        assert served.attrs[frame_store.CACHE_REQUESTED_END_ATTR] == WINDOW[1]

    def test_the_frame_says_when_the_data_was_fetched(self, tmp_path):
        store_obj = store(tmp_path)
        before = time.time()
        assert write_frame(store_obj, price_frame()) == "hit"
        after = time.time()

        served = read_frame(store_obj).value
        stamp = pd.Timestamp(served.attrs[frame_store.CACHE_FETCHED_ATTR])
        assert stamp.utcoffset() == pd.Timedelta(hours=5, minutes=30), (
            "the fetch time is not the IST instant it claims to be"
        )
        assert before <= stamp.timestamp() <= after, (
            "the published fetch time is not the moment the frame entered the store"
        )


# ============================================================ the status vocabulary, and corruption


class TestTheVocabularyAndCorruptRows:
    """Red on: **inventing a second vocabulary** for the same facts (``"cached"``, ``"stale"``,
    ``"gone"``), and on **returning whatever ``pickle.loads`` produced** for a row that cannot be
    trusted.

    The store reports :class:`CacheLookup` names verbatim, so a consumer that learned the key in
    ``ownership`` or in the screener client can read this store's answers. And a row that will not
    load -- the realistic way: written by another pandas major, or edited -- is ``"corrupt"`` and is
    never returned, because the alternative is a frame that fails one attribute at a time inside the
    caller, far from the row that caused it.
    """

    def test_every_status_it_reports_is_a_cachelookup_name(self, tmp_path):
        seen = set()

        empty = store(tmp_path, name="empty.db")
        seen.add(read_frame(empty).status)  # miss: no row
        assert write_frame(empty, price_frame()) == "hit"
        seen.add(read_frame(empty).status)  # hit
        off = store(tmp_path, name="off.db", enabled=False)
        seen.add(write_frame(off, price_frame()))  # disabled
        corrupt = store(tmp_path, name="corrupt.db")
        write_frame(corrupt, price_frame())
        with sqlite3.connect(str(corrupt.db_path), timeout=5.0) as conn:
            conn.execute("UPDATE bfinance_price_frames SET payload = ?", (b"not a pickle",))
            conn.commit()
        seen.add(read_frame(corrupt).status)  # corrupt

        assert seen == {"miss", "hit", "disabled", "corrupt"}, f"unexpected statuses: {seen}"
        for status in seen:
            assert status in STATUSES, f"{status!r} is not a name from the existing vocabulary"
            assert CacheLookup(status).status == status, "it must be a name the type itself accepts"

    def test_a_row_that_cannot_be_trusted_is_corrupt_and_never_served(self, tmp_path):
        store_obj = store(tmp_path)
        write_frame(store_obj, price_frame())

        with sqlite3.connect(str(store_obj.db_path), timeout=5.0) as conn:
            conn.execute(
                "UPDATE bfinance_price_frames SET payload = ? WHERE frame_key = ?",
                (pickle.dumps({"not": "a frame"}), frame_key("TCS", "NSE", "EQ", *WINDOW)),
            )
            conn.commit()

        lookup = read_frame(store_obj)
        assert lookup.status == "corrupt", (
            "a payload that is not a frame is not a cache hit, whatever pickle.loads returns"
        )
        assert lookup.value is None


# =================================================== a row must answer the key it is filed under


class TestTheRowMustAnswerItsKey:
    """Red on: **the window check in :meth:`FrameStore.read` deleted** -- a store that trusts a row's
    existence and its window's *end* while never inspecting the frame's index, which is what the check
    is for.

    The module docstring's threat model already contemplates a foreign row -- a copied, hand-edited or
    older-build ``frames.db`` -- and that is why ``_may_serve``'s fetch-date clause exists; the guard was
    simply incomplete, so it compared the date and not the index. Measured before this check existed: a
    frame whose bars are all in 2027-01, filed under a key ending 2026-10-16, writes as ``"hit"``, reads
    as ``"hit"``, serves ``2027-01-04..2027-01-06`` and publishes ``coverage_end=2027-01-06`` against
    ``requested_end=2026-10-16`` -- the row's own metadata confirming the lie. Executed in
    :meth:`TestTheChecksBite.test_deleting_the_window_check_would_serve_bars_from_another_window`.

    The refusal is the whole row or nothing: the part of a mis-filed frame that happens to fit the window
    is never trimmed out and served, because a clipped frame presented as the window's own series is the
    same lie with fewer rows -- the doctrine ``ohlcv`` states when its own prefix check lands the other
    way ("never serve a shortened version of this row"). And a row that is short *but correctly sliced*
    is not this check's business: see :func:`assert_a_short_but_correct_row_is_served`.
    """

    def test_a_row_whose_bars_are_all_outside_the_window_is_corrupt_and_not_served(self, tmp_path):
        store_obj = store(tmp_path)
        next_year = price_frame(periods=3, start="2027-01-04")  # 01-04, 01-05, 01-06

        # Written through the real writer, because `write` cannot tell: it stores the frame it was
        # handed for the window it was handed and never looks at the bars. That is the whole reason the
        # read has to, and it is also why this row is not "foreign" in any exotic sense.
        assert write_frame(store_obj, next_year, WINDOW) == "hit"
        assert row_count(store_obj.db_path) == 1, "premise: the row is on disk"

        assert_the_row_does_not_answer_its_key(store_obj, next_year, WINDOW)

    @pytest.mark.parametrize(
        ("start", "periods"),
        [("2026-10-12", 7), ("2026-09-25", 8)],
        ids=["bars-past-the-end", "bars-before-the-start"],
    )
    def test_a_row_that_only_partly_overlaps_the_window_is_refused_not_trimmed(
        self, tmp_path, start, periods
    ):
        """Both directions of one overlap, because the frame's last bar is not the only bar that can
        fall outside the window the key names: a row starting before its own window start is mis-filed
        in the same way and is caught by the same comparison. The bars that *do* fit are what make this
        the interesting case -- the tempting fix is to serve those, and that is the one thing the
        package refuses to do.
        """
        store_obj = store(tmp_path)
        straddling = price_frame(periods=periods, start=start)
        days = [str(day)[:10] for day in straddling.index]
        inside = [day for day in days if WINDOW[0] <= day <= WINDOW[1]]
        assert inside and len(inside) < len(days), (
            f"premise: the {days[0]}..{days[-1]} frame straddles the window {WINDOW}"
        )
        assert write_frame(store_obj, straddling, WINDOW) == "hit"

        assert_the_row_does_not_answer_its_key(store_obj, straddling, WINDOW)

    def test_a_short_but_correctly_sliced_row_still_reads_as_a_hit(self, tmp_path):
        """The case that must not start answering ``"corrupt"``: a window that ends on a Sunday, whose
        last bar is a Friday. Every bar is inside the window, and the window simply has days after the
        data -- a gap to be published, not a reason to refuse. This is also the shape every window that
        ends on a weekend or an exchange holiday has, which is most of them.
        """
        window = ("2026-09-28", "2026-10-11")  # 2026-10-11 is a Sunday
        store_obj = store(tmp_path)
        fortnight = price_frame(periods=10, start="2026-09-28")  # Mon 09-28 .. Fri 10-09

        assert write_frame(store_obj, fortnight, window) == "hit"

        assert_a_short_but_correct_row_is_served(store_obj, fortnight, window)

    def test_an_empty_index_does_not_trip_the_window_check(self, tmp_path):
        """An empty index is inside the window by definition -- it has no bar that could be outside --
        and reading such a row must neither crash nor raise a refusal this check invented.

        Red on: **the empty-index short-circuit deleted**, which makes ``index.min()`` ``NaT``; ``NaT``
        compares as outside every window, so a row the store's own refusal rules already name would be
        reported as corrupt by this one instead.
        """
        store_obj = store(tmp_path)
        plant_empty_row(store_obj)

        assert_an_empty_row_is_not_refused_by_the_window_check(store_obj)


# ============================================================================ the key itself


class TestTheKey:
    """Red on: **moving key construction into a caller** (two spellings of one window become two
    rows) and on **guessing at the components** -- folding ``.NS`` off a symbol, or spelling an
    unresolved date instead of refusing it.
    """

    def test_the_key_names_all_five_components_and_folds_case_and_whitespace(self):
        assert frame_key("tcs", "NSE", None, "2025-10-17", pd.Timestamp("2026-10-16")) == (
            "TCS|NSE|ALL|2025-10-17|2026-10-16"
        )
        assert frame_key(" TCS ", "nse", "eq", "2025-10-17", "2026-10-16") == (
            "TCS|NSE|EQ|2025-10-17|2026-10-16"
        )

        # A yfinance-style suffix is a *different* key, deliberately: a miss costs a refetch, a wrong
        # hit costs correctness, and folding one spelling into the other could produce the second.
        assert frame_key("TCS.NS", "NSE", "EQ", WINDOW[0], WINDOW[1]) != frame_key(
            "TCS", "NSE", "EQ", WINDOW[0], WINDOW[1]
        )
        # ... and the exchange really is part of the identity, not a decoration.
        assert frame_key("TCS", "NSE", "EQ", WINDOW[0], WINDOW[1]) != frame_key(
            "TCS", "BSE", "EQ", WINDOW[0], WINDOW[1]
        )

    def test_an_unresolved_or_missing_component_is_refused_rather_than_spelled(self):
        with pytest.raises(ValueError, match="NaT"):
            frame_key("TCS", "NSE", "EQ", None, WINDOW[1])
        with pytest.raises(ValueError, match="symbol is empty"):
            frame_key("", "NSE", "EQ", WINDOW[0], WINDOW[1])
        with pytest.raises(ValueError, match="exchange is empty"):
            frame_key("TCS", "  ", "EQ", WINDOW[0], WINDOW[1])
        with pytest.raises(ValueError):
            frame_key("TCS", "NSE", "EQ", "yesterday", WINDOW[1])

    def test_a_multiindex_cross_section_is_refused_by_name(self, tmp_path):
        """The bhavcopy parse returns a whole-market ``(Date, Symbol)`` frame, and storing one under a
        single symbol's key would be a wrong hit waiting to happen -- so the refusal names the
        reduction that is needed instead of accepting a frame whose coverage cannot be described.

        The second half passes ``observed_coverage_end``, because that is the shape the bhavcopy range
        cache will call in: a fetch layer that already knows the watermark must not thereby be allowed
        to file a whole-market frame under one symbol.
        """
        cross_section = pd.DataFrame(
            {"Close": [1.0]},
            index=pd.MultiIndex.from_tuples(
                [(pd.Timestamp("2026-10-14"), "TCS")], names=["Date", "Symbol"]
            ),
        )
        store_obj = store(tmp_path)
        with pytest.raises(TypeError, match="xs"):
            write_frame(store_obj, cross_section)
        with pytest.raises(TypeError, match="xs"):
            store_obj.write(
                cross_section,
                symbol="TCS",
                exchange="NSE",
                series="EQ",
                start=WINDOW[0],
                end=WINDOW[1],
                observed_coverage_end="2026-10-16",
            )
        assert row_count(store_obj.db_path) == 0


# ============================================================ the controls: the checks really bite


class TestTheChecksBite:
    """The named mutations, executed.

    Each control patches the seam the mutation would change, drives the *same helper* the test above
    drives, and requires that helper to fail (or, for the publish guard, to flip) -- so "this test
    would catch it" is a measurement rather than a claim. The seams are module-level functions on
    purpose: a mutation that cannot be pointed at is a mutation nobody has tried.
    """

    def test_the_json_pair_would_erase_the_provenance(self, tmp_path, monkeypatch):
        """Mutation: ``_payload_blob`` / ``_frame_from_blob`` replaced by the JSON pair."""
        monkeypatch.setattr(
            frame_store, "_payload_blob", lambda frame: frame.to_json().encode("utf-8")
        )
        monkeypatch.setattr(
            frame_store,
            "_frame_from_blob",
            lambda blob: pd.read_json(io.StringIO(blob.decode("utf-8"))),
        )

        store_obj = store(tmp_path)
        frame = price_frame()
        assert write_frame(store_obj, frame) == "hit", "premise: JSON stores happily"

        served = read_frame(store_obj).value
        assert len(served) == len(frame) and list(served.columns) == list(frame.columns), (
            "premise: the numbers and the columns do come back"
        )
        with pytest.raises(AssertionError, match="price_basis"):
            assert_provenance_survived(served, frame, where="JSON storage")

    def test_handing_out_the_loaded_object_would_let_one_caller_corrupt_the_next(
        self, tmp_path, monkeypatch
    ):
        """Mutation: ``_isolated`` returning its argument, with a loader that memoises."""
        load = frame_store._frame_from_blob  # the real loader, captured before the patch
        loaded = {}

        def memoised(blob):
            if "frame" not in loaded:
                loaded["frame"] = load(blob)
            return loaded["frame"]

        monkeypatch.setattr(frame_store, "_frame_from_blob", memoised)
        monkeypatch.setattr(frame_store, "_isolated", lambda frame: frame)

        store_obj = store(tmp_path)
        assert write_frame(store_obj, price_frame()) == "hit"
        with pytest.raises(AssertionError, match="same object"):
            assert_reads_are_isolated(store_obj)

    def test_neutering_the_failure_refusal_would_cache_the_failure(self, tmp_path, monkeypatch):
        """Mutation: ``_refusal`` answering ``None`` for every frame.

        Run against the all-NaN shape rather than the rowless one: an empty index has no ``max``, so a
        rowless frame trips :func:`_coverage_end` on the way to the insert -- a second refusal that
        would make this control measure the wrong guard. The all-NaN shape is the one where the
        refusal is the *only* thing standing between a failed download and a permanent row.
        """
        monkeypatch.setattr(frame_store, "_refusal", lambda frame: None)

        with pytest.raises(AssertionError, match="row"):
            assert_not_cached(store(tmp_path), failure_frame("all NaN"), where="refusal removed")

    def test_deleting_the_re_assertion_would_empty_the_range_attrs(self, tmp_path, monkeypatch):
        """Mutation: ``_concat_keeping_attrs`` reduced to a bare ``pd.concat``."""
        monkeypatch.setattr(
            frame_store, "_concat_keeping_attrs", lambda frames: pd.concat(list(frames))
        )

        first = price_frame(periods=1, start="2026-10-12")
        second = price_frame(periods=1, start="2026-10-13", source="marketlens-close")
        store_obj = store(tmp_path)
        assert write_frame(store_obj, [first, second]) == "hit", "premise: the bare concat stores"

        served = read_frame(store_obj).value
        with pytest.raises(AssertionError, match="price_basis"):
            assert_provenance_survived(served, first, where="bare concat")

    def test_the_publish_guard_is_the_only_thing_keeping_the_open_window_row_out(
        self, tmp_path, monkeypatch
    ):
        """Mutation: ``_may_serve`` answering ``True`` -- the deleted guard.

        The row is the same row in both halves and the clock is the same in both halves; the only
        thing that changes is whether the guard looks at them.
        """
        store_obj = store(tmp_path)
        plant_row(
            store_obj,
            written_window=WINDOW,
            under_window=OPEN_WINDOW,
            fetched_at="2026-10-17 15:30",
        )
        assert read_frame(store_obj, OPEN_WINDOW).status == "miss", "premise: the guard refuses it"

        monkeypatch.setattr(
            frame_store.FrameStore, "_may_serve", lambda self, window_end, fetched_at: True
        )
        assert read_frame(store_obj, OPEN_WINDOW).status == "hit", (
            "with no guard the planted row is served, so the guard is what kept it out"
        )

    def test_deleting_the_window_check_would_serve_bars_from_another_window(
        self, tmp_path, monkeypatch
    ):
        """Mutation: ``_bars_lie_inside_the_window`` answering ``True`` -- the deleted window check.

        Both shapes are driven, and both must flip: the row whose bars are entirely outside its window
        (the measured 2027-under-2026 row) and the row that only straddles it, because one comparison
        is what keeps both out and a control that covered one of them would not notice the other.
        """
        store_obj = store(tmp_path)
        next_year = price_frame(periods=3, start="2027-01-04")
        assert write_frame(store_obj, next_year, WINDOW) == "hit"
        assert_the_row_does_not_answer_its_key(store_obj, next_year, WINDOW)

        straddling_window = ("2026-10-05", "2026-10-16")
        straddling = price_frame(periods=8, start="2026-09-25")
        assert write_frame(store_obj, straddling, straddling_window) == "hit"
        assert_the_row_does_not_answer_its_key(store_obj, straddling, straddling_window)

        monkeypatch.setattr(frame_store, "_bars_lie_inside_the_window", lambda *args, **kwargs: True)

        served = read_frame(store_obj).value
        assert [str(day)[:10] for day in served.index] == [
            "2027-01-04",
            "2027-01-05",
            "2027-01-06",
        ], "premise: with no check the 2027 row is served for a 2026 window"
        with pytest.raises(AssertionError, match="only ever answers a different question"):
            assert_the_row_does_not_answer_its_key(store_obj, next_year, WINDOW)
        with pytest.raises(AssertionError, match="only ever answers a different question"):
            assert_the_row_does_not_answer_its_key(store_obj, straddling, straddling_window)

    def test_requiring_the_row_to_reach_the_requested_end_would_refuse_a_short_one(
        self, tmp_path, monkeypatch
    ):
        """Mutation: the window check asking whether the row *reaches* the requested end instead of
        whether its bars are inside the window -- the over-refusal this package refuses in the other
        direction.

        It is the plausible over-correction: it is shorter than the real comparison, it looks like
        "the data must answer the whole window", and it turns every symbol with a gap, every window
        ending on a weekend and every mid-window listing into a permanent cache miss.
        """
        window = ("2026-09-28", "2026-10-11")
        store_obj = store(tmp_path)
        fortnight = price_frame(periods=10, start="2026-09-28")
        assert write_frame(store_obj, fortnight, window) == "hit"

        def must_reach_the_requested_end(frame, *, start_iso, end_iso):
            return len(frame) == 0 or str(frame.index.max())[:10] >= end_iso

        monkeypatch.setattr(frame_store, "_bars_lie_inside_the_window", must_reach_the_requested_end)

        with pytest.raises(AssertionError, match="served and labelled, never refused"):
            assert_a_short_but_correct_row_is_served(store_obj, fortnight, window)

    def test_dropping_the_empty_short_circuit_would_refuse_an_empty_row(
        self, tmp_path, monkeypatch
    ):
        """Mutation: the window check without its empty-index short-circuit -- the guard applied to a
        shape that has no bars to guard.

        The row is the same empty row in both halves; the only thing that changes is whether the check
        reads ``index.min()`` of a frame that has no index entries, where ``NaT`` compares as outside
        every window.
        """
        store_obj = store(tmp_path)
        plant_empty_row(store_obj)
        assert_an_empty_row_is_not_refused_by_the_window_check(store_obj)

        def without_the_empty_short_circuit(frame, *, start_iso, end_iso):
            return start_iso <= str(frame.index.min())[:10] and str(frame.index.max())[:10] <= end_iso

        monkeypatch.setattr(
            frame_store, "_bars_lie_inside_the_window", without_the_empty_short_circuit
        )
        with pytest.raises(AssertionError, match="nothing to refuse"):
            assert_an_empty_row_is_not_refused_by_the_window_check(store_obj)
