"""
Drift guard for the ``auto_adjust`` default across every public batch entry point.

Why this test exists
--------------------
On 2026-09-27 ``fetch_history`` stopped fabricating OHLC and flipped its ``auto_adjust``
default from ``True`` to ``False`` (True now raises ``AdjustmentNotAvailableError`` because the
NSE bhavcopy carries no corporate-actions series, so no authoritative factor exists).

The inner history path changed. The PUBLIC batch wrappers did not. ``download()``,
``Tickers.history`` and ``_download_batch_async`` kept defaulting to ``True``, so the raise
happened inside ``Ticker.history_async``, was swallowed by ``_download_single``'s broad
``except Exception``, and surfaced as a **silently empty DataFrame**::

    >>> bfinance.download(['RELIANCE'], period='5d')   # -> empty frame, no error

That is the worst possible failure shape: a silent empty result that only reveals itself if
the caller thinks to pass ``raise_errors=True``, which nobody does. A default that disagrees
between an inner function and its public wrapper is a structural hazard, not a one-off typo,
so it is pinned by reflection here rather than by an incidental behavioural test that the
offline suite (which stubs the Ticker) would not catch anyway.
"""

import inspect

import pytest

from bfinance import Ticker, Tickers, download
from bfinance.market.ohlcv import OHLCVEngine

# Every layer that declares an ``auto_adjust`` default. The batch entry points and the inner
# engine must agree; if one is added, add it here too rather than letting the gap grow.
_ENTRY_POINTS = [
    ("bfinance.download", download),
    ("bfinance.Tickers.history", Tickers.history),
    ("bfinance.Ticker.history", Ticker.history),
    ("bfinance.Ticker.history_async", Ticker.history_async),
    ("OHLCVEngine.fetch_history", OHLCVEngine.fetch_history),
]

EXPECTED_DEFAULT = False


@pytest.mark.parametrize("label,func", _ENTRY_POINTS, ids=[name for name, _ in _ENTRY_POINTS])
def test_auto_adjust_default_is_false(label, func):
    """Every layer must default ``auto_adjust`` to False, or True raises AdjustmentNotAvailableError."""
    param = inspect.signature(func).parameters.get("auto_adjust")
    assert param is not None, f"{label} no longer accepts auto_adjust -- update this test"
    assert param.default is EXPECTED_DEFAULT, (
        f"{label} auto_adjust default is {param.default!r}, expected {EXPECTED_DEFAULT!r}. "
        "A batch entry point that still defaults to True raises AdjustmentNotAvailableError, "
        "which _download_single()'s broad `except Exception` swallows into an empty DataFrame: "
        "a silent empty result instead of data."
    )


def test_public_batch_defaults_match_inner_history_path():
    """The public wrappers and the inner engine must not drift apart from each other."""
    defaults = {
        label: inspect.signature(func).parameters["auto_adjust"].default
        for label, func in _ENTRY_POINTS
    }
    assert len(set(defaults.values())) == 1, f"auto_adjust defaults disagree: {defaults}"


def test_download_single_requires_explicit_auto_adjust():
    """``_download_single`` is keyword-fed by _download_batch_async, so it must carry no default.

    A default here would be a fourth place to keep in sync and would silently mask a wrapper
    that forgets to pass the value through.
    """
    from bfinance.download import _download_single

    assert inspect.signature(_download_single).parameters["auto_adjust"].default is inspect.Parameter.empty
