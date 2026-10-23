"""
Four ``download()`` parameters that were in the signature and were read by nothing.

The defect
----------
``ignore_tz``, ``progress``, ``session`` and ``timeout`` were accepted, documented as part of the
yfinance-parity surface, and never consulted:

* ``timeout=3`` still let every symbol's history call run on ``history_async``'s own default of 10;
* ``session=requests.Session()`` was dropped on the floor although ``Ticker.__init__`` refuses the
  same argument;
* ``ignore_tz=False`` asked for a timezone-preserving frame and got the tz-naive one anyway;
* ``progress=False`` and ``progress=True`` were the same call, because there was no progress output
  to suppress.

An argument that is accepted and ignored is worse than one that is rejected: the caller has been
told their choice was applied, so the divergence surfaces later as a wrong window or a wrong index
with no call site to blame. Every one of the four is therefore either honoured in a way a test can
observe, or refused with a :class:`NotImplementedError` that names it.

What "honoured" means here, per parameter
----------------------------------------
* ``timeout`` -- reaches the per-symbol ``history_async(timeout=...)`` call and the one HTTP client
  the batch builds. The spies below record the value the request actually carried.
* ``session`` -- refused when not ``None``, naming ``httpx``, before a single ``Ticker`` exists.
* ``ignore_tz`` -- ``True`` is honoured (the index is tz-naive, which is the only index this
  package produces); ``False`` is refused, because the timezone-preserving frame it asks for cannot
  be produced and handing back a frame that silently dropped the tz is the defect itself.
* ``progress`` -- ``True`` emits one ``INFO`` line per finished symbol on the ``bfinance.download``
  logger; ``False`` emits none. yfinance draws a tqdm bar on stderr; a library must not write to a
  caller's terminal, so the log line is the form this package can honestly honour.

One cache and one client per batch
----------------------------------
``Ticker`` grew ``cache=`` / ``screener_client=`` precisely so a batch could share them. Before
that every ``Ticker.__init__`` built its own ``SQLiteCache`` (a schema check against
``~/.bfinance/cache.db``) and its own ``ScreenerClient`` (a connection pool, pacing slot and retry
state), so a three-symbol batch built three of each.
``test_a_batch_builds_one_cache_and_one_client`` pins the count at one, and also pins that the
objects handed to each Ticker are the same objects.

Offline: every Ticker, cache and client here is a stand-in. Nothing reaches the wire.
"""

import importlib
import logging

import pandas as pd
import pytest

from bfinance.utils.exceptions import UpstreamServiceError

# NB: `from bfinance import download` yields the *function*, not the module; importlib reaches the
# module whose `Ticker` / `SQLiteCache` / `ScreenerClient` names are the seams under test.
download_mod = importlib.import_module("bfinance.download")
download = download_mod.download

IST = "Asia/Kolkata"
_COLUMNS = ["Open", "High", "Low", "Close", "Volume"]
_DATES = pd.to_datetime(["2024-03-28", "2024-03-29"])


class _StubTicker:
    """Answers every symbol except ``failing``. Records the timeout each request carried."""

    #: One entry per constructed Ticker, in construction order.
    instances: list = []
    #: ``(SYMBOL, timeout)`` per history call, in call order.
    received: list = []
    #: Symbols that raise instead of answering.
    failing: tuple = ()

    def __init__(self, symbol, proxy=None, cache=None, screener_client=None):
        self.symbol = symbol
        self.proxy = proxy
        self.cache = cache
        self.screener_client = screener_client
        type(self).instances.append(self)

    async def history_async(self, **kwargs):
        sym = self.symbol.upper()
        type(self).received.append((sym, kwargs.get("timeout")))
        if sym in type(self).failing:
            raise UpstreamServiceError(f"{sym}: boom")
        # tz-aware on purpose: this is what Ticker.history_async returns, and it is what the
        # `ignore_tz` test needs in order to observe the batch stripping it.
        index = pd.DatetimeIndex(_DATES, name="Date", tz=IST)
        return pd.DataFrame(
            [[100.0, 102.0, 99.0, 101.0, 10], [101.0, 103.0, 100.0, 101.5, 11]],
            index=index,
            columns=list(_COLUMNS),
        )


class _SpyCache:
    """Counts constructions; the batch must build exactly one of these."""

    built = 0

    def __init__(self, *args, **kwargs):
        type(self).built += 1


class _SpyClient:
    """Counts constructions and records the arguments, so `timeout` can be seen arriving."""

    built = 0
    kwargs: dict = {}

    def __init__(self, *args, **kwargs):
        type(self).built += 1
        type(self).kwargs = kwargs


@pytest.fixture
def stub(monkeypatch):
    _StubTicker.instances = []
    _StubTicker.received = []
    _StubTicker.failing = ()
    _SpyCache.built = 0
    _SpyClient.built = 0
    _SpyClient.kwargs = {}
    monkeypatch.setattr(download_mod, "Ticker", _StubTicker)
    monkeypatch.setattr(download_mod, "SQLiteCache", _SpyCache)
    monkeypatch.setattr(download_mod, "ScreenerClient", _SpyClient)
    return _StubTicker


def _tickers_in(frame: pd.DataFrame) -> list:
    return list(dict.fromkeys(frame.columns.get_level_values(1)))


def _block(frame: pd.DataFrame, ticker: str) -> pd.DataFrame:
    """One ticker's sub-frame, found by level name so the helper works for either group_by."""
    return frame.xs(ticker, axis=1, level=frame.columns.names.index("Ticker"))


def _progress_lines(caplog) -> list:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == "bfinance.download" and record.levelno == logging.INFO
    ]


# ----------------------------------------------------------------------------- timeout: honoured


def test_timeout_reaches_the_history_call(stub):
    """The request carries the caller's number, not ``history_async``'s own default."""
    download(["AAA"], period="5d", timeout=3)

    assert stub.received == [("AAA", 3)], (
        "download(timeout=3) must reach Ticker.history_async(timeout=3); the request carried "
        f"{stub.received!r}"
    )


def test_the_default_timeout_is_forwarded_too(stub):
    """``timeout``'s default of 10 is this function's, and it is passed on rather than left to
    ``history_async`` to re-decide -- otherwise the two defaults could drift apart in silence."""
    download(["AAA"], period="5d")

    assert stub.received == [("AAA", 10)], f"got {stub.received!r}"


def test_timeout_also_configures_the_batch_client(stub):
    """The shared client is the object that makes the HTTP requests, so it is built with it."""
    download(["AAA"], period="5d", timeout=3)

    assert _SpyClient.kwargs.get("timeout") == 3, (
        f"the batch client must be built with timeout=3; got {_SpyClient.kwargs!r}"
    )


# ----------------------------------------------------------------------------- session: refused


def test_session_is_refused_naming_httpx(stub):
    """A caller-supplied session would never be used, so it must not be accepted in silence."""
    with pytest.raises(NotImplementedError) as exc:
        download(["AAA"], period="5d", session=object())

    message = str(exc.value)
    assert "session" in message, f"the refusal must name the parameter; got {message!r}"
    assert "httpx" in message, (
        f"the refusal must name the transport it cannot accept; got {message!r}"
    )
    assert "cannot be honoured" in message, f"got {message!r}"
    assert stub.instances == [], (
        f"session must be refused before any Ticker is constructed; got {stub.instances!r}"
    )


# --------------------------------------------------------------------------- ignore_tz: split


def test_ignore_tz_false_is_refused(stub):
    """``False`` asks for a tz-aware index, which the batch frame cannot carry."""
    with pytest.raises(NotImplementedError, match="ignore_tz"):
        download(["AAA"], period="5d", ignore_tz=False)

    assert stub.instances == [], (
        f"ignore_tz=False must be refused before any Ticker is constructed; got {stub.instances!r}"
    )


def test_ignore_tz_true_is_honoured(stub):
    """The stub answers tz-aware; ``ignore_tz=True`` promises the returned index is not."""
    frame = download(["AAA"], period="5d", ignore_tz=True)

    assert isinstance(frame.index, pd.DatetimeIndex), f"got {type(frame.index).__name__}"
    assert frame.index.tz is None, (
        f"ignore_tz=True must return a tz-naive index; got {frame.index.tz}"
    )


# ----------------------------------------------------------------------------- progress: honoured


def test_progress_true_reports_each_finished_symbol(stub, caplog):
    """``True`` must produce something a caller can observe, per symbol."""
    with caplog.at_level(logging.INFO, logger="bfinance.download"):
        download(["AAA", "BBB", "CCC"], period="5d", progress=True)

    lines = _progress_lines(caplog)
    assert len(lines) == 3, f"one line per finished symbol; got {lines!r}"
    for symbol in ("AAA", "BBB", "CCC"):
        assert any(symbol in line for line in lines), f"{symbol} missing from {lines!r}"
    assert any("3/3" in line for line in lines), f"the count must reach the total; got {lines!r}"


def test_progress_false_is_silent(stub, caplog):
    """``False`` asks for no progress output, and that is an answer, not a no-op."""
    with caplog.at_level(logging.INFO, logger="bfinance.download"):
        download(["AAA", "BBB", "CCC"], period="5d", progress=False)

    assert _progress_lines(caplog) == [], (
        f"progress=False must emit nothing; got {_progress_lines(caplog)!r}"
    )


# --------------------------------------------------------------------- one cache, one client


def test_a_batch_builds_one_cache_and_one_client(stub):
    """Three symbols, one of each. This is what the `cache=`/`screener_client=` seam is for."""
    frame = download(["AAA", "BBB", "CCC"], period="5d")

    assert not frame.empty, "the shared transport must not have cost the batch its data"
    assert _SpyCache.built == 1, f"one cache per batch, not one per symbol; built {_SpyCache.built}"
    assert _SpyClient.built == 1, (
        f"one client per batch, not one per symbol; built {_SpyClient.built}"
    )
    assert len(stub.instances) == 3, (
        f"one Ticker per symbol is still expected; got {stub.instances!r}"
    )

    shared_cache = stub.instances[0].cache
    shared_client = stub.instances[0].screener_client
    assert shared_cache is not None and shared_client is not None, (
        "the seam was not passed: every Ticker built its own again"
    )
    assert {id(t.cache) for t in stub.instances} == {id(shared_cache)}, (
        "every symbol must receive the same cache object"
    )
    assert {id(t.screener_client) for t in stub.instances} == {id(shared_client)}, (
        "every symbol must receive the same client object"
    )
    assert _SpyClient.kwargs.get("cache") is shared_cache, (
        "the client must be built over the batch's own cache"
    )


# ----------------------------------------------------------------- failure isolation unchanged


def test_one_failing_symbol_still_yields_its_nan_column(stub):
    """A failed symbol appears, as all-NaN, and does not abort the batch or touch its neighbours."""
    stub.failing = ("BAD",)
    frame = download(["AAA", "BAD", "CCC"], period="5d")

    assert _tickers_in(frame) == ["AAA", "BAD", "CCC"], f"got {_tickers_in(frame)!r}"
    assert len(frame) == 2, f"the two answers must survive the failure; got {len(frame)} rows"
    assert _block(frame, "BAD").isna().all().all(), (
        f"BAD must be all NaN; got\n{_block(frame, 'BAD')}"
    )

    assert not _block(frame, "AAA").isna().any().any(), "AAA answered and must stay answered"
    assert not _block(frame, "CCC").isna().any().any(), "CCC answered and must stay answered"
    assert frame[("Close", "AAA")].tolist() == [101.0, 101.5], (
        "the neighbours' values must be untouched"
    )


def test_a_failing_symbol_does_not_stop_the_progress_count(stub, caplog):
    """Progress counts what finished, failure included, so the last line still reads N/N."""
    stub.failing = ("BAD",)
    with caplog.at_level(logging.INFO, logger="bfinance.download"):
        download(["AAA", "BAD", "CCC"], period="5d")

    lines = _progress_lines(caplog)
    assert len(lines) == 3, f"every symbol finished, one way or the other; got {lines!r}"
    assert any("3/3" in line for line in lines), f"got {lines!r}"


# ------------------------------------------------------------------------- the negative control


def test_each_probe_distinguishes_honoured_from_ignored(stub, caplog):
    """The control for all four at once: two values, two different observations.

    A parameter that is accepted and ignored cannot make two calls differ, so each pair below is
    exactly what the old implementation fails. ``timeout=3`` and ``timeout=71`` would both arrive
    at ``history_async`` as its own default of 10; ``progress=True`` and ``progress=False`` would
    produce the same number of lines (zero); and the two refusals would return a frame instead of
    raising. Asserting a single value ("got 3") would also be satisfied by a default that happened
    to be 3, which is why the pair is compared and the callee's own default is named.
    """
    download(["AAA"], period="5d", timeout=3)
    download(["AAA"], period="5d", timeout=71)
    assert [carried for _, carried in stub.received] == [3, 71], (
        "an ignored timeout sends history_async's own default (10) on both calls; carried "
        f"{[carried for _, carried in stub.received]!r}"
    )

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="bfinance.download"):
        download(["AAA"], period="5d", progress=True)
    on = len(_progress_lines(caplog))
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="bfinance.download"):
        download(["AAA"], period="5d", progress=False)
    off = len(_progress_lines(caplog))
    assert (on, off) == (1, 0), (
        f"progress=True/{on} and progress=False/{off} lines are indistinguishable if the flag is "
        "ignored; the pair must differ"
    )

    # These two have no second value to compare -- for them the honest answer is a refusal, and a
    # refusal that does not happen is the same silence the parameter used to be.
    for kwargs in ({"session": object()}, {"ignore_tz": False}):
        with pytest.raises(NotImplementedError):
            download(["AAA"], period="5d", **kwargs)
