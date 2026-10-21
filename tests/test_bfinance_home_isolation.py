"""
The suite's per-test ``$BFINANCE_HOME``: the default price cache is shared state, and only a fixture resets it.

**Why this file exists.** ``OHLCVEngine`` reads through a ``PriceCache`` by default
(``market/ohlcv.py:869``), so *any* test that calls ``fetch_history`` without injecting a store of its
own writes to ``$BFINANCE_HOME/frames.db`` -- one file, shared by every test in the process and by
every later run. A frame one test caches is therefore handed to the next test that asks the same
question, and that test measures the earlier test's fixture instead of its own archive stub.
Measured, before ``conftest.py`` grew the autouse fixture this file pins: a row for
``RELIANCE|NSE|ALL|2024-08-16|2024-08-21`` written by one test answered a later test whose stub
returned nothing at all, so ``test_ohlcv_auto_adjust_true.py``'s "an empty frame is refused" came
back as a hit on somebody else's bars, and it did so within a single run as well as across runs.

**What is pinned here.** The isolation itself, in both directions: a default store lands under this
test's own home (and its rows do not outlive the test), and a test that points the home somewhere
itself keeps it -- which is what the settled modules that assert on *their* home depend on
(``test_mirror_integration.py``, ``test_frame_store.py``, ``test_market_price_cache.py``,
``test_ohlcv_cached_window.py``).

Nothing here touches the network: every fetcher is a frame built in-process, and the two bars are
read back through the store's own API and through a raw file path.
"""

from __future__ import annotations

import os
from pathlib import Path

import pandas as pd
import pytest

from bfinance.cache.frame_store import IST, default_frame_store_path
from bfinance.market.price_cache import CACHE_STATUS_ATTR, PriceCache

#: The key every test in this file reads and writes. ``series=None`` is what a plain ``history()``
#: call passes, spelled ``"ALL"`` inside the key -- see ``frame_store.frame_key``.
KEY = {"symbol": "TCS", "exchange": "NSE", "series": None}


def _closed_window() -> tuple:
    """``(start, end)`` for a window that ends on the newest Mon-Fri *before* today.

    Derived from the real clock rather than typed in, for the reason the engine's own boundary is:
    the store may only keep a window whose archive files are final, so a hard-coded window would
    silently stop being storable on a machine whose date is before it -- and the two leak tests below
    would then pass without ever having written a row to leak.

    The end is a weekday *and* strictly before today, so the window is closed by both of the store's
    clauses (``frame_store._may_serve``) on every day of the week.
    """
    end = pd.Timestamp.now(tz=IST).normalize() - pd.Timedelta(days=1)
    while end.weekday() > 4:
        end -= pd.Timedelta(days=1)
    return (end - pd.Timedelta(days=6)).date().isoformat(), end.date().isoformat()


def _frame(start: str, end: str, *, close: float) -> pd.DataFrame:
    """Three bars inside the window, on the index shape the price path produces.

    ``Close`` is a parameter because the leak pin below needs the two tests' data to be *different*:
    a replayed row is then visible in the values and not only in the published status.
    """
    days = pd.date_range(start, end, freq="D")[:3]
    frame = pd.DataFrame(
        {
            "Open": close - 1.0,
            "High": close + 1.0,
            "Low": close - 2.0,
            "Close": close,
            "Volume": 1_000,
        },
        index=days,
    )
    frame.index.name = "Date"
    return frame


class _Fetcher:
    """The seam ``PriceCache.get_or_fetch`` takes: a zero-argument callable, counted."""

    def __init__(self, frame: pd.DataFrame) -> None:
        self.frame = frame
        self.calls = 0

    def __call__(self) -> pd.DataFrame:
        self.calls += 1
        return self.frame.copy()


# --------------------------------------------------------------- 1. the home is this test's own


def test_a_default_store_is_built_under_this_test_s_own_home(tmp_path):
    """``PriceCache()``'s store -- the default the engine builds -- is inside this test's ``tmp_path``.

    **Mutation this is red on:** drop the autouse fixture from ``conftest.py`` and the path becomes
    whatever the runner's ``$BFINANCE_HOME`` was, or ``~/.bfinance/frames.db`` -- i.e. the user's own
    price cache, which a test run then reads and writes.
    """
    store = PriceCache().store

    assert Path(store.db_path).is_relative_to(tmp_path)
    assert Path(store.db_path) != Path.home() / ".bfinance" / "frames.db"
    assert store.db_path == str(default_frame_store_path())


def test_the_home_named_for_a_test_is_not_created_for_it(tmp_path):
    """The fixture names the directory; it does not create it.

    **Mutation this is red on:** ``mkdir`` in the fixture (or in this test) would make every "nothing
    was written" assertion in the suite unfalsifiable -- a test cannot show that a read left no file
    behind if the fixture already put a directory there -- so the directory has to appear only when
    the code under test makes it.
    """
    assert not (tmp_path / "bfinance_home").exists()


# -------------------------------------------------------- 2. the leak the fixture exists to stop


def test_a_row_written_through_the_default_wrapper_is_really_stored():
    """The premise of the test below: the default wrapper *does* keep a closed window.

    Without this, "the next test does not see it" would be satisfied by a store that stores nothing
    -- which is why the premise is asserted rather than assumed. The write is made through
    ``PriceCache()`` with no injected store, i.e. exactly the object an engine builds for itself.
    """
    start, end = _closed_window()
    cache = PriceCache()
    fetcher = _Fetcher(_frame(start, end, close=100.0))

    frame = cache.get_or_fetch(fetcher, start=start, end=end, **KEY)

    assert frame.attrs[CACHE_STATUS_ATTR] == "miss", "the data came over the seam, so that is what it says"
    assert fetcher.calls == 1
    assert cache.store.read(start=start, end=end, **KEY).status == "hit", (
        "a closed window must be kept, or the test below is pinning nothing"
    )


def test_a_row_the_previous_test_stored_is_not_visible_here():
    """**The leak pin.** The row the test above wrote must not answer this test's identical read.

    Order matters and is deliberate: this is the second of a pair, because the defect is *between*
    tests rather than inside one, and a single test cannot exhibit it.

    **Mutation this is red on:** remove the autouse fixture from ``conftest.py``. The store is then
    one file for the whole session, this read is served from the previous test's row, and both
    assertions fire -- ``status`` is ``"hit"`` instead of ``"miss"``, and ``Close`` is 100.0 instead
    of this test's own 7.0. Measured while this file was written.
    """
    start, end = _closed_window()
    cache = PriceCache()
    fetcher = _Fetcher(_frame(start, end, close=7.0))

    frame = cache.get_or_fetch(fetcher, start=start, end=end, **KEY)

    assert frame.attrs[CACHE_STATUS_ATTR] == "miss", (
        "this test's store is empty, so the window is fetched: a 'hit' is the previous test's row, "
        "i.e. two tests sharing one file"
    )
    assert fetcher.calls == 1, "and the fetch really happened rather than being skipped"
    assert frame["Close"].tolist() == [7.0, 7.0, 7.0], "this test's own bars, not a replay of the other's"
    assert cache.store.read(start=start, end=end, **KEY).status == "hit"


# ------------------------------------------------- 3. a test that names its own home keeps it


@pytest.fixture
def own_home(monkeypatch, tmp_path):
    """Point the home at a directory of this test's own, the way the settled modules do."""
    home = tmp_path / "home_of_this_test"
    monkeypatch.setenv("BFINANCE_HOME", str(home))
    return home


def test_a_test_that_names_its_own_home_is_left_alone(own_home):
    """The autouse fixture must stand down for a home another fixture chose -- in either order.

    ``test_mirror_integration.py`` asserts that *its* home stays empty, and ``test_frame_store.py``
    that the default path equals the home it set; a fixture that always overwrote the variable would
    move what those files measure without failing them, which is the one way an isolation fixture can
    hide a defect instead of exposing one.

    **Mutation this is red on:** make the autouse fixture unconditional (delete its stand-down) --
    ``own_home`` then loses to it whenever it runs first, and this test's two assertions disagree.
    """
    assert os.environ["BFINANCE_HOME"] == str(own_home)
    assert default_frame_store_path() == own_home / "frames.db"
