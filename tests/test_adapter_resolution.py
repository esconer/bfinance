"""
An adapter must hand the cascade a VALUE, never a coroutine.

The defect this pins, confirmed by execution rather than by reading. A registry row names
its entry by string. Most entries are module-level functions and are synchronous, so an
adapter can call them directly. But an entry that resolves to a CLIENT METHOD may be a
coroutine function, and then the adapter returns a coroutine OBJECT where a price series
belongs:

    >>> type(bindings.screener_chart_history(ScreenerClient, {...}))
    <class 'coroutine'>

and the cascade's next step raised ``TypeError: object of type 'coroutine' has no len()``.

Why this class of bug is so expensive: a coroutine is a perfectly legal Python object, so
nothing complains at the boundary where it is created. The row looked wired, every
surface test passed, the capability was listed in the registry as served, and it could
never once answer. The real error either surfaces far from its cause or -- if the caller
only inspects ``attrs`` -- never surfaces at all.

The fix resolves the value in one place, so the defect cannot recur in an adapter that
does not exist yet, rather than patching the single instance found today.

Deliberately not a rename. Renaming every client method to ``*_async`` and adding a sync
twin under the plain name is the convention the rest of the package follows, but it is a
public API break and would silently un-await every duck-typed stub: ``ohlcv.py`` resolves
its profile loader with ``getattr(self.screener, "get_company_profile", None)`` and awaits
whatever it is handed, so renaming the real method would leave test doubles returning
``None``. These tests therefore pin the resolution behaviour, and deliberately do NOT
require a rename.
"""
from __future__ import annotations

import asyncio

import pandas as pd
import pytest

from bfinance.sources import bindings


def test_a_coroutine_is_driven_to_completion_not_returned():
    """The core contract, on the helper the whole layer depends on."""

    async def produce():
        return pd.DataFrame({"Close": [1.0, 2.0]})

    got = bindings._resolved(produce())
    assert isinstance(got, pd.DataFrame)
    assert not asyncio.iscoroutine(got)
    assert list(got["Close"]) == [1.0, 2.0]


def test_a_plain_value_passes_straight_through_unchanged():
    """
    The other direction, and the one that would break if _resolved did something eager.
    A frame handed in is the same object, not a copy and not a round-trip through a loop.
    """
    frame = pd.DataFrame({"Close": [1.0]})
    got = bindings._resolved(frame)
    assert got is frame


@pytest.mark.parametrize("value", [None, 0, "", [], {}, False])
def test_falsy_non_coroutine_values_are_not_mistaken_for_awaitables(value):
    """
    A resolver written as `if value:` would drop these on the floor. The check is on the
    type, never on truthiness, so an empty frame and a zero survive intact.
    """
    assert bindings._resolved(value) == value


def test_a_raising_coroutine_still_raises_rather_than_being_swallowed():
    """
    An adapter that converts a failure into an empty result turns a recorded failure into
    a silent "this source has no data". The module docstring makes that the load-bearing
    rule, so it is pinned here: the exception propagates.
    """

    async def boom():
        raise RuntimeError("upstream said no")

    with pytest.raises(RuntimeError, match="upstream said no"):
        bindings._resolved(boom())


class TestScreenerChartAdapter:
    """
    The specific row that was wired and could never answer. Driven with a client whose
    price method is a coroutine function, which is what the real one is.
    """

    @staticmethod
    def _client_class():
        from bfinance.screener.client import ScreenerClient

        assert asyncio.iscoroutinefunction(ScreenerClient.get_chart_timeseries), (
            "this test's premise is that the real entry point is async; if that changes "
            "the test is exercising nothing"
        )
        return ScreenerClient

    def test_the_adapter_returns_a_frame_not_a_coroutine(self, monkeypatch):
        frame = pd.DataFrame(
            {"Price": [10.0, 11.0], "Volume": [100, 200]},
            index=pd.DatetimeIndex(["2026-01-05", "2026-01-06"], name="Date"),
        )

        class _Fake:
            async def get_chart_timeseries(self, ticker, metric="price", days=1825,
                                           force_refresh=False):
                return frame

        got = bindings.screener_chart_history(_Fake, {"ticker": "ITC", "days": 30})
        assert not asyncio.iscoroutine(got), "the defect: a coroutine reached the cascade"
        assert isinstance(got, pd.DataFrame)
        pd.testing.assert_frame_equal(got, frame)

    def test_the_real_client_class_resolves_through_the_adapter(self):
        """
        The end-to-end shape, without touching the network: point the adapter at the real
        class and confirm it gets far enough to construct a client and call the method.
        With a bad ticker the call fails; the point is that the failure is the CLIENT's
        refusal, not a TypeError about a coroutine having no len().
        """
        client_cls = self._client_class()
        try:
            got = bindings.screener_chart_history(
                client_cls, {"ticker": "ZZZZNOTREAL", "days": 5}
            )
        except Exception as exc:  # network, resolution, or a typed refusal
            assert not isinstance(exc, TypeError) or "coroutine" not in str(exc), (
                f"the coroutine defect is back: {type(exc).__name__}: {exc}"
            )
        else:
            assert not asyncio.iscoroutine(got)
            assert isinstance(got, pd.DataFrame)


def test_every_registered_adapter_is_covered_by_the_resolver():
    """
    Structural, so a future adapter cannot opt out by forgetting.

    Walks the source text of every function registered as an adapter and requires that any
    ``return`` of a call result is routed through ``_resolved``. A plain ``return`` of a
    local or constant is fine; returning the result of calling a client or entry point is
    exactly the shape that produced the bug.
    """
    import inspect
    import pathlib

    module_file = pathlib.Path(inspect.getsourcefile(bindings))
    text = module_file.read_text(encoding="utf-8")

    unguarded = []
    for name in bindings.PATH.values():
        fn_name = str(name).rpartition(".")[2]
        fn = getattr(bindings, fn_name, None)
        if fn is None or not callable(fn):
            continue
        body = inspect.getsource(fn)
        for line in body.splitlines():
            stripped = line.strip()
            if not stripped.startswith("return "):
                continue
            # Returning a call result without the resolver is the defect shape.
            if "(" in stripped and "_resolved(" not in stripped:
                unguarded.append(f"{fn_name}: {stripped}")

    assert not unguarded, "adapters returning an unresolved call result:\n  " + "\n  ".join(
        unguarded
    )
    assert text  # the module file was read; guards against a vacuous path
