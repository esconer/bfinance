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

Which way it points now, and why
--------------------------------
``EXPECTED_DEFAULT`` was flipped to ``True`` on 2026-09-30. Commit 0910257 made
``auto_adjust=True`` WORK on every path where ``Adj Close`` came from a real ex-dated
corporate-actions feed, and yfinance 1.7.0 -- installed in this venv and measured -- returns
``['Open','High','Low','Close','Volume','Dividends','Stock Splits']`` from a bare
``history()``, because its own default is ``True`` and it drops ``Adj Close``. bfinance
returned eight columns where yfinance returns seven, and that column divergence was the root
cause of most of the 32-of-48 yfinance parity failures. Interchangeability won the argument
that ``False`` had won by being the strictly richer column set.

The guard is unchanged in kind: the five layers must still AGREE, because the failure mode it
exists to catch is a disagreement, not the value. Two distinct failures are now guarded --
the values drifting apart again, and a layer silently dropping the keyword.

What ``True`` now costs, and the one case it does not answer
-----------------------------------------------------------
``True`` is refused where ``Adj Close`` is not an adjusted series AND there is nothing real to
return instead -- ``ohlc="synthetic"``, an empty frame, and no ex-dated corporate-actions feed
at all. That set used to include the Screener.in fiscal-year-end approximation, and the cost
was measured: NSE's feed has no index for an ETF, so MAFANG, HNGSNGBEES and NIFTYBEES all
degrade to the approximation, and the default this file pins made all three raise while the
same call returned 21 real bars for NTPC, RELIANCE, WIPRO, CIPLA and INDGN. That is the
swallowing failure above, at the top of a mixed portfolio, and it is why a refusal is now a
statement about the ANSWER and not about the keyword. Those three symbols are answered
as-traded instead, with yfinance's column set and ``attrs["bfinance_adjustment_applied"] is
False``; see ``tests/test_ohlcv_approximation_unadjusted.py``. The guard does not move either
way -- the value is still ``True``, and the layers must still agree on it.
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

#: yfinance 1.7.0's own default, measured in this venv. bfinance matches it so that a caller
#: who says nothing gets yfinance's column set rather than a superset of it.
EXPECTED_DEFAULT = True


@pytest.mark.parametrize("label,func", _ENTRY_POINTS, ids=[name for name, _ in _ENTRY_POINTS])
def test_auto_adjust_default_is_true(label, func):
    """Every layer must default ``auto_adjust`` to True, so omitting the kwarg is yfinance's call."""
    param = inspect.signature(func).parameters.get("auto_adjust")
    assert param is not None, f"{label} no longer accepts auto_adjust -- update this test"
    assert param.default is EXPECTED_DEFAULT, (
        f"{label} auto_adjust default is {param.default!r}, expected {EXPECTED_DEFAULT!r}. "
        "yfinance 1.7.0 defaults it to True and drops 'Adj Close' on a bare history() call, so "
        "a layer that still defaults to False returns eight columns where yfinance returns "
        "seven. The reverse disagreement is just as fatal: a batch entry point defaulting to "
        "True while the inner path refuses raises AdjustmentNotAvailableError, which "
        "_download_single()'s broad `except Exception` swallows into an empty DataFrame: "
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
