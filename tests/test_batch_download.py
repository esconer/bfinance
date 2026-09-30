"""
Tests for concurrent multi-ticker batch download matching yfinance.download().

The two original tests here were UNMARKED network tests -- they hit screener.in on every CI
run while carrying no ``@pytest.mark.live``, so they could not be excluded from a network-free
run. They are now marked.

Added alongside them: the ``raise_errors`` contract, which was previously a silent no-op.
"""

import importlib
import logging

import pandas as pd
import pytest

# NB: `from bfinance import download` yields the *function*, not the module, because
# `bfinance/__init__.py` re-exports it. importlib is the reliable way to reach the module.
download_mod = importlib.import_module("bfinance.download")
download = download_mod.download


# --------------------------------------------------------------- live (network)


@pytest.mark.live
def test_batch_download_single_ticker():
    """Verify single ticker download returns standard OHLCV DataFrame."""
    df = download("RELIANCE", period="1mo")
    assert isinstance(df, pd.DataFrame)
    assert not df.empty
    assert "Close" in df.columns
    assert "Volume" in df.columns


@pytest.mark.live
def test_batch_download_multi_tickers():
    """Verify multi-ticker download returns MultiIndex DataFrame."""
    df = download(["RELIANCE", "TCS"], period="1mo")
    assert isinstance(df, pd.DataFrame)
    assert not df.empty
    # Columns are MultiIndex (Metric, Ticker) e.g. ('Close', 'RELIANCE')
    assert isinstance(df.columns, pd.MultiIndex)
    assert "Close" in df.columns.levels[0]


# --------------------------------------------------------------- offline: raise_errors contract


def test_raise_errors_is_an_explicit_parameter():
    """
    ``raise_errors`` must be a real parameter, not swallowed by ``**kwargs``.

    It used to be reachable only through ``**kwargs``, where it was accepted and discarded.
    ``download("X", raise_errors=True)`` therefore looked strict and behaved leniently, which is
    the worst possible failure mode for a debugging flag: the caller believes they have strict
    behaviour while debugging the exact opposite.

    Asserted against the signature rather than by behaviour, because a behavioural test would
    pass just as happily if ``**kwargs`` happened to forward it.
    """
    import inspect

    sig = inspect.signature(download)
    assert "raise_errors" in sig.parameters, (
        "raise_errors must be explicit; if it is only in **kwargs it will be silently ignored"
    )
    # Lenient by default, matching yfinance.
    assert sig.parameters["raise_errors"].default is False


def test_unknown_kwargs_are_rejected_not_just_reported():
    """A misspelled argument must be refused, which is how raise_errors went missing for so long.

    It used to be a WARNING plus a normal frame: the argument vanished, the caller got a different
    window than the one they asked for, and the evidence was a line on a logger nobody reads.
    ``TypeError`` is what Python raises for this and what yfinance 1.7.0 raises (its
    ``download()`` signature is closed), and it puts the mistake at the call site.

    Un-marked ``live``: the refusal happens before any ticker is constructed, so this is now
    network-free. ``tests/test_silent_fallbacks.py::test_download_rejects_before_doing_any_work``
    pins the "before any work" half that makes that true.
    """
    with pytest.raises(TypeError, match="raise_errorz"):
        download("RELIANCE", period="1mo", raise_errorz=True)


class _StubTicker:
    """Stands in for Ticker so no network and no cache is involved."""

    behaviour = "ok"
    calls: list = []

    def __init__(self, symbol, proxy=None):
        self.symbol = symbol

    async def history_async(self, **kwargs):
        type(self).calls.append((self.symbol, kwargs.get("period")))
        if type(self).behaviour == "upstream_error":
            from bfinance.utils.exceptions import UpstreamServiceError

            raise UpstreamServiceError("Screener.in returned status code 403")
        if type(self).behaviour == "empty":
            return pd.DataFrame()
        idx = pd.to_datetime(["2024-03-28", "2024-03-29"])
        return pd.DataFrame(
            {"Open": [1.0, 2.0], "High": [1.5, 2.5], "Low": [0.5, 1.5],
             "Close": [1.2, 2.2], "Volume": [10, 20]},
            index=idx,
        )


@pytest.fixture
def stub_ticker(monkeypatch):
    _StubTicker.behaviour = "ok"
    _StubTicker.calls = []
    monkeypatch.setattr(download_mod, "Ticker", _StubTicker)
    return _StubTicker


def test_download_default_stays_lenient(stub_ticker):
    """Default behaviour must not change: an upstream failure yields an empty frame, not a raise."""
    stub_ticker.behaviour = "upstream_error"
    df = download(["RELIANCE", "TCS"], period="5d")
    assert df.empty


def test_download_raise_errors_true_propagates(stub_ticker):
    """The whole point of the flag: strict mode must actually raise."""
    stub_ticker.behaviour = "upstream_error"
    with pytest.raises(Exception) as exc:
        download(["RELIANCE", "TCS"], period="5d", raise_errors=True)
    assert "403" in str(exc.value)


def test_download_raise_errors_false_still_swallows(stub_ticker):
    stub_ticker.behaviour = "upstream_error"
    assert download(["RELIANCE"], period="5d", raise_errors=False).empty


def test_download_logs_which_symbols_returned_nothing(stub_ticker, caplog):
    """
    A fully-empty run must say which symbols produced nothing.

    Previously a run where every symbol was blocked and a run of bad symbols both produced a
    bare empty DataFrame with no record of either. That is the failure mode behind "why is my
    portfolio empty this morning" being unanswerable.
    """
    stub_ticker.behaviour = "empty"
    with caplog.at_level(logging.WARNING, logger="bfinance.download"):
        df = download(["AAA", "BBB", "CCC"], period="5d")

    assert df.empty
    msg = " ".join(r.getMessage() for r in caplog.records)
    assert "AAA" in msg and "BBB" in msg, f"expected the empty symbols to be named, got: {msg!r}"


def test_download_raise_errors_true_still_collects_good_symbols_first(stub_ticker):
    """
    One bad symbol must not destroy a good batch when strict mode is off.

    The failure is recorded and logged, but the surviving symbols are still returned -- the
    batch is a partial result, not an all-or-nothing operation.

    THE OLD ASSERTION HERE WAS WRONG. It read ``assert not df.empty``, which is satisfied by any
    frame with one live column -- so it never checked that ``BAD`` appeared at all, and the symbol
    was in fact being dropped entirely: ``download.py`` did ``continue`` on an upstream exception
    and on an empty frame, where yfinance 1.7.0 unions ``utils.empty_df()`` and the ticker shows up
    as an all-NaN column. The test passed over a real divergence from yfinance.

    So the assertion is now that BOTH tickers are columns, with BAD's values all NaN and TCS's
    real. ``group_by`` is left at its default here on purpose: ``multi_level_index=False`` only
    takes effect for a single ticker, and a two-ticker request is always a MultiIndex.
    ``tests/test_download_failed_symbol_column.py`` carries the full shape contract, including
    ``group_by="ticker"``, the derived column set, and the mutation control.
    """
    calls = {"n": 0}
    orig = _StubTicker.history_async

    async def flaky(self, **kwargs):
        calls["n"] += 1
        if self.symbol == "BAD":
            from bfinance.utils.exceptions import UpstreamServiceError

            raise UpstreamServiceError("boom")
        return await orig(self, **kwargs)

    stub_ticker.history_async = flaky
    try:
        df = download(["GOOD", "BAD"], period="5d")
        tickers = list(dict.fromkeys(df.columns.get_level_values(1)))
        assert tickers == ["BAD", "GOOD"], (
            f"both requested tickers must be columns; got {tickers!r}. A dropped symbol is "
            "invisible to a caller iterating the columns, and df[('Close','BAD')] raises KeyError "
            "where yfinance returns NaNs."
        )
        bad = df.xs("BAD", axis=1, level=1)
        assert bad.isna().all().all(), (
            f"BAD must be all NaN -- not 0.0, which would read as a flat-traded price; got\n{bad}"
        )
        good = df.xs("GOOD", axis=1, level=1)
        assert not good.isna().any().any(), f"GOOD answered and must stay answered; got\n{good}"
    finally:
        stub_ticker.history_async = orig


def test_no_input_returns_empty_without_touching_network(stub_ticker):
    assert download("", period="5d").empty
    assert download([], period="5d").empty
    assert stub_ticker.calls == []
